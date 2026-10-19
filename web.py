#!/usr/bin/env python3
"""transition_demo 를 브라우저에서 써 볼 수 있는 작은 웹 UI. 파이썬 표준 라이브러리만 사용.

  python3 web.py              # http://127.0.0.1:8000 에서 열림
  python3 web.py --port 9000

흐름
  1. 슬라이드 영상(2개 이상)을 올리거나 "샘플 4장으로 시작"                 POST /api/upload
  2. 왼쪽 목록에서 슬라이드를 고르고, 그 슬라이드로 들어올 때의 전환 종류와 길이를 정한다
     - 위치는 "앞"으로 고정: 앞 슬라이드의 마지막 T초 동안 전환하고, 이 슬라이드 시작 시각에 끝난다
     - 전환을 정하지 않은 슬라이드는 컷으로 넘어간다
     - "이 구간만 미리보기": 경계 앞뒤 몇 초만 잘라 바로 확인                 POST /api/preview
  3. 전체 렌더링                                                             POST /api/render
업로드, 썸네일, 미리보기, 결과는 jobs/<프로젝트 id>/ 아래에 남는다.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from email.parser import BytesParser
from email.policy import default as EMAIL_POLICY
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transition_demo as td  # noqa: E402

BASE = Path(__file__).resolve().parent
WEB_DIR = BASE / "web"
JOBS = BASE / "jobs"

ALIGN = "before"        # 전환 위치 고정: 앞 슬라이드의 마지막 구간
DEFAULT_DURATION = 0.5  # 전환 길이 기본값(초)
PREVIEW_TAIL = 1.0      # 미리보기: 앞 슬라이드는 (전환 길이 + 1초), 뒤 슬라이드는 1.5초만 사용
PREVIEW_HEAD = 1.5
SAMPLE_SLIDES = 4
PROJECT_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
CONTENT_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".png": "image/png", ".json": "application/json"}


# ---------------------------------------------------------------- 업로드
def parse_multipart(content_type: str, body: bytes):
    """multipart/form-data -> (fields, files). fields[name] = [str], files[name] = [(파일명, bytes)]"""
    if not content_type.startswith("multipart/form-data"):
        raise ValueError("multipart/form-data 요청이 아닙니다")
    msg = BytesParser(policy=EMAIL_POLICY).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    fields: dict[str, list[str]] = {}
    files: dict[str, list[tuple[str, bytes]]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            if payload:
                files.setdefault(name, []).append((Path(filename).name, payload))
        else:
            fields.setdefault(name, []).append(payload.decode("utf-8", "replace"))
    return fields, files


def field(fields: dict[str, list[str]], name: str, default: str) -> str:
    return (fields.get(name) or [default])[0].strip() or default


def save_uploads(dirpath: Path, items: list[tuple[str, bytes]]) -> list[Path]:
    dirpath.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (name, data) in enumerate(items):
        p = dirpath / f"{i:02d}_{name}"
        p.write_bytes(data)
        paths.append(p)
    return paths


def sample_inputs() -> tuple[list[Path], list[Path]]:
    clips = [td.WORK / f"clip{k}.mp4" for k in range(1, SAMPLE_SLIDES + 1)]
    images = [td.WORK / f"slide{k}.png" for k in range(1, SAMPLE_SLIDES + 1)]
    if not all(p.exists() for p in clips + images):
        clips, images = td.make_samples(SAMPLE_SLIDES)
    return clips, images


# ---------------------------------------------------------------- 프로젝트
def new_project_dir() -> Path:
    job = JOBS / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    job.mkdir(parents=True)
    return job


def load_project(pid: str | None) -> dict:
    if not pid or not PROJECT_ID.match(pid):
        raise ValueError("잘못된 프로젝트 id")
    f = JOBS / pid / "project.json"
    if not f.exists():
        raise ValueError("프로젝트를 찾을 수 없습니다. 영상을 다시 올려주세요")
    return json.loads(f.read_text(encoding="utf-8"))


def public_project(project: dict) -> dict:
    return {"id": project["id"], "align": ALIGN, "max_duration": td.MAX_TRANSITION,
            "default_duration": DEFAULT_DURATION,
            "slides": [{"index": s["index"], "name": s["name"], "duration": s["duration"],
                        "thumb": s["thumb"], "has_image": bool(s["image"])} for s in project["slides"]]}


def make_thumb(clip: Path, out: Path) -> None:
    td.ffmpeg("-i", str(clip), "-frames:v", "1", "-vf", "scale=320:-2", str(out))


def create_project(fields: dict[str, list[str]], files: dict[str, list[tuple[str, bytes]]]) -> dict:
    job = new_project_dir()
    if field(fields, "use_sample", "0") == "1":
        clips, images = sample_inputs()
    else:
        clip_files = files.get("clips", [])
        if len(clip_files) < 2:
            raise ValueError("슬라이드 영상을 2개 이상 올려주세요")
        clips = save_uploads(job / "in", clip_files)
        image_files = files.get("images", [])
        images = save_uploads(job / "img", image_files) if image_files else None
        if images and len(images) != len(clips):
            raise ValueError("이미지 개수는 영상 개수와 같아야 합니다")

    (job / "thumbs").mkdir()
    slides = []
    for i, clip in enumerate(clips, start=1):
        info = td.probe(clip)
        if not info.has_audio:
            raise ValueError(f"오디오 스트림이 없습니다: {clip.name}")
        make_thumb(clip, job / "thumbs" / f"{i}.jpg")
        slides.append({"index": i, "name": clip.name, "duration": info.duration,
                       "clip": str(clip), "image": str(images[i - 1]) if images else None,
                       "thumb": f"/jobs/{job.name}/thumbs/{i}.jpg"})
    project = {"id": job.name, "slides": slides}
    (job / "project.json").write_text(json.dumps(project, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "project": public_project(project)}


def command_text(cmd: list[str]) -> str:
    return "ffmpeg -y " + " ".join(shlex.quote(c) for c in cmd)


# ---------------------------------------------------------------- 렌더링
def preview_job(body: dict) -> dict:
    """슬라이드 k 로 들어오는 전환만 짧게 렌더: 앞 슬라이드 끝 (전환 + 1초) + 뒤 슬라이드 앞 1.5초."""
    proj = load_project(body.get("project"))
    slides = proj["slides"]
    k = int(body.get("slide") or 0)
    name = str(body.get("transition") or "none")
    dur = float(body.get("duration") or DEFAULT_DURATION)
    if not 2 <= k <= len(slides):
        raise ValueError("전환은 2번 슬라이드부터 넣을 수 있습니다")
    if name == "none":
        raise ValueError("전환 종류를 골라주세요")

    prev, cur = slides[k - 2], slides[k - 1]
    a, b = td.probe(Path(prev["clip"])), td.probe(Path(cur["clip"]))
    tail = td.snap(min(a.duration, dur + PREVIEW_TAIL), a.fps)
    head = td.snap(min(b.duration, PREVIEW_HEAD), a.fps)
    a_part = replace(a, ss=td.snap(a.duration - tail, a.fps), t=tail, duration=tail)
    b_part = replace(b, ss=0.0, t=head, duration=head)

    bounds = td.plan([tail, head], [name], dur, ALIGN, a.fps)
    td.validate([a_part, b_part], bounds)
    images = [Path(prev["image"]), Path(cur["image"])] if prev["image"] and cur["image"] else None
    out = JOBS / proj["id"] / f"preview_{k}_{uuid.uuid4().hex[:6]}.mp4"
    cmd = td.join([a_part, b_part], bounds, out, images=images)
    rep = td.report([a_part, b_part], bounds, out, ALIGN)
    return {"ok": True, "video": f"/jobs/{proj['id']}/{out.name}", "report": rep,
            "tail": tail, "head": head, "command": command_text(cmd)}


def render_job(body: dict) -> dict:
    """프로젝트 전체 렌더. transitions = {"2": {"type": "fade", "duration": 0.5}, ...} (없는 슬라이드는 컷)."""
    proj = load_project(body.get("project"))
    slides = proj["slides"]
    tmap = body.get("transitions") or {}
    if not isinstance(tmap, dict):
        raise ValueError("transitions 형식이 잘못됐습니다")

    infos = [td.probe(Path(s["clip"])) for s in slides]
    names, durs = [], []
    for k in range(2, len(slides) + 1):
        t = tmap.get(str(k)) or {}
        typ = str(t.get("type") or "none")
        names.append(typ)
        durs.append(0.0 if typ == "none" else float(t.get("duration") or DEFAULT_DURATION))

    bounds = td.plan([i.duration for i in infos], names, durs, ALIGN, infos[0].fps)
    warnings = td.validate(infos, bounds)
    images = [Path(s["image"]) for s in slides] if all(s["image"] for s in slides) else None
    out = JOBS / proj["id"] / f"result_{uuid.uuid4().hex[:6]}.mp4"
    cmd = td.join(infos, bounds, out, images=images)
    rep = td.report(infos, bounds, out, ALIGN)
    return {"ok": True, "video": f"/jobs/{proj['id']}/{out.name}", "report": rep, "warnings": warnings,
            "clips": [{"name": s["name"], "duration": i.duration} for s, i in zip(slides, infos)],
            "command": command_text(cmd)}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "transition-demo/2.0"

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            return self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/transitions":
            return self._send_json({"popular": td.POPULAR_TRANSITIONS, "all": td.xfade_transitions()})
        if path.startswith("/jobs/"):
            target = (JOBS / path[len("/jobs/"):]).resolve()
            if target.is_file() and JOBS.resolve() in target.parents:
                ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
                return self._send_file(target, ctype, ranges=True)
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        ctype = self.headers.get("Content-Type", "")
        try:
            if path == "/api/upload":
                fields, files = parse_multipart(ctype, body)
                result = create_project(fields, files)
            elif path in ("/api/preview", "/api/render"):
                data = json.loads(body.decode("utf-8") or "{}")
                result = preview_job(data) if path == "/api/preview" else render_job(data)
            else:
                return self.send_error(404)
            self._send_json(result)
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, status=400)
        except subprocess.CalledProcessError as e:
            self._send_json({"ok": False, "error": f"ffmpeg 실패 (exit {e.returncode}). 서버 콘솔을 확인하세요"},
                            status=500)

    def _send_json(self, obj: dict, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, ctype: str, ranges: bool = False) -> None:
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range") if ranges else None
        if rng and rng.startswith("bytes="):          # <video> 탐색(seek)용 Range 지원
            s, _, e = rng[6:].partition("-")
            start = int(s) if s else max(0, size - int(e))
            end = min(int(e), size - 1) if (s and e) else size - 1
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    JOBS.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"브라우저에서 열기: http://{args.host}:{args.port}   (종료: Ctrl+C)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
