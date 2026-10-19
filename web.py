#!/usr/bin/env python3
"""slide_render 를 브라우저에서 써 볼 수 있는 작은 웹 UI. 파이썬 표준 라이브러리만 사용.

  python3 web.py              # http://127.0.0.1:8000 에서 열림
  python3 web.py --port 9000

흐름
  1. 슬라이드 이미지(2장 이상)를 올리거나 "샘플 4장으로 시작"                      POST /api/upload
  2. 왼쪽 목록에서 슬라이드를 고르고 길이(초)와 들어올 때 전환(종류/길이)을 정한 뒤
     "이 슬라이드 영상 만들기". k 번 영상은 k 번 이미지(전환이 있으면 k-1 번 이미지도)만으로 만든다
                                                                                   POST /api/render_slide
  3. 만든 슬라이드 영상들을 재인코딩 없이 이어 붙여 전체를 확인                      POST /api/concat
업로드, 썸네일, 슬라이드 영상, 이어 붙인 영상은 jobs/<프로젝트 id>/ 아래에 남는다.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from email.parser import BytesParser
from email.policy import default as EMAIL_POLICY
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slide_render as sr  # noqa: E402

BASE = Path(__file__).resolve().parent
WEB_DIR = BASE / "web"
JOBS = BASE / "jobs"

DEFAULT_SLIDE_DURATION = 3.0
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


def sample_images() -> list[Path]:
    images = [sr.WORK / f"slide{k}.png" for k in range(1, SAMPLE_SLIDES + 1)]
    if not all(p.exists() for p in images):
        images = sr.make_sample_images(SAMPLE_SLIDES)
    return images


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
        raise ValueError("프로젝트를 찾을 수 없습니다. 이미지를 다시 올려주세요")
    return json.loads(f.read_text(encoding="utf-8"))


def save_project(project: dict) -> None:
    (JOBS / project["id"] / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=1), encoding="utf-8")


def public_slide(s: dict) -> dict:
    return {"index": s["index"], "name": s["name"], "duration": s["duration"], "thumb": s["thumb"],
            "transition": s.get("transition"), "video": s.get("video_url")}


def public_project(project: dict) -> dict:
    return {"id": project["id"], "max_transition": sr.MAX_TRANSITION,
            "default_transition_duration": sr.DEFAULT_TRANSITION_DURATION,
            "slides": [public_slide(s) for s in project["slides"]]}


def make_thumb(image: Path, out: Path) -> None:
    sr.ffmpeg("-i", str(image), "-vf", "scale=320:-2", "-frames:v", "1", str(out))


def create_project(fields: dict[str, list[str]], files: dict[str, list[tuple[str, bytes]]]) -> dict:
    job = new_project_dir()
    default_dur = float(field(fields, "duration", str(DEFAULT_SLIDE_DURATION)))
    if default_dur <= 0:
        raise ValueError("슬라이드 길이는 0보다 커야 합니다")
    if field(fields, "use_sample", "0") == "1":
        images = sample_images()
        durations = list(sr.SAMPLE_DURATIONS)
    else:
        image_files = files.get("images", [])
        if len(image_files) < 2:
            raise ValueError("슬라이드 이미지를 2장 이상 올려주세요")
        images = save_uploads(job / "img", image_files)
        durations = [default_dur] * len(images)

    (job / "thumbs").mkdir()
    slides = []
    for i, image in enumerate(images, start=1):
        make_thumb(image, job / "thumbs" / f"{i}.jpg")
        slides.append({"index": i, "name": image.name, "image": str(image),
                       "thumb": f"/jobs/{job.name}/thumbs/{i}.jpg",
                       "duration": sr.snap(durations[i - 1]), "transition": None,
                       "video": None, "video_url": None})
    project = {"id": job.name, "slides": slides}
    save_project(project)
    return {"ok": True, "project": public_project(project)}


# ---------------------------------------------------------------- 렌더링
def render_slide_job(body: dict) -> dict:
    """슬라이드 k 영상 하나만 만든다. 전환이 있으면 k-1 번 이미지를 가져와 처음 T초에 넣는다."""
    proj = load_project(body.get("project"))
    slides = proj["slides"]
    k = int(body.get("slide") or 0)
    if not 1 <= k <= len(slides):
        raise ValueError("슬라이드 번호가 잘못됐습니다")
    s = slides[k - 1]
    duration = float(body.get("duration") or s["duration"])

    t = body.get("transition") or None
    ttype = str(t.get("type") or "none") if isinstance(t, dict) else "none"
    tdur = float(t.get("duration") or sr.DEFAULT_TRANSITION_DURATION) if isinstance(t, dict) else 0.0
    if k == 1:
        ttype, tdur = "none", 0.0        # 첫 슬라이드는 앞 슬라이드가 없다
    prev = Path(slides[k - 2]["image"]) if ttype != "none" else None

    out = JOBS / proj["id"] / f"slide{k}_{uuid.uuid4().hex[:6]}.mp4"
    cmd = sr.render_slide(Path(s["image"]), duration, out, prev, ttype, tdur)
    rep = sr.report_slide(out, duration, ttype, tdur)

    s["duration"] = sr.snap(duration)
    s["transition"] = {"type": ttype, "duration": sr.snap(tdur)} if ttype != "none" else None
    s["video"] = str(out)
    s["video_url"] = f"/jobs/{proj['id']}/{out.name}"
    save_project(proj)
    return {"ok": True, "video": s["video_url"], "report": rep, "command": sr.command_text(cmd),
            "slide": public_slide(s)}


def concat_job(body: dict) -> dict:
    """만들어 둔 슬라이드 영상들을 순서대로, 재인코딩 없이 이어 붙인다."""
    proj = load_project(body.get("project"))
    slides = proj["slides"]
    missing = [str(s["index"]) for s in slides if not s.get("video")]
    if missing:
        raise ValueError("아직 영상을 만들지 않은 슬라이드가 있습니다: " + ", ".join(missing) + "번")
    videos = [Path(s["video"]) for s in slides]
    out = JOBS / proj["id"] / f"all_{uuid.uuid4().hex[:6]}.mp4"
    cmd = sr.concat_videos(videos, out)
    rep = sr.report_concat(videos, out)
    return {"ok": True, "video": f"/jobs/{proj['id']}/{out.name}", "report": rep, "command": sr.command_text(cmd)}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "slide-render-demo/1.0"

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            return self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/transitions":
            return self._send_json({"popular": sr.POPULAR_TRANSITIONS, "all": sr.xfade_transitions()})
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
            elif path in ("/api/render_slide", "/api/concat"):
                data = json.loads(body.decode("utf-8") or "{}")
                result = render_slide_job(data) if path == "/api/render_slide" else concat_job(data)
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
