#!/usr/bin/env python3
"""slide_transition 을 브라우저에서 써 볼 수 있는 작은 웹 UI. 파이썬 표준 라이브러리만 사용.

  python3 web.py              # http://127.0.0.1:8000 에서 열림
  python3 web.py --port 9000

흐름
  1. 슬라이드 영상(음성 포함)들을 올리거나 "샘플 4장으로 체험"                                POST /api/upload
  2. 왼쪽 목록에서 슬라이드를 고르고, 들어올 때 앞에 둘 그림(검정/흰색/이미지)과 전환(종류/길이/움직임)을
     정한 뒤 "전환본 만들기". 앞에 둘 이미지는 슬라이드마다 패널에서 올린다                     POST /api/slide_image
     k 번 영상의 처음 T초가 "앞 그림 -> k 번 영상" 전환으로 바뀌고, 결과가 원본과
     해상도/fps/프레임 수/길이/음성이 같은지 자동으로 검사한다                                 POST /api/apply
  3. 전환본(없으면 원본)을 순서대로 이어 붙여 전체를 확인 (확인용, 재인코딩)                    POST /api/concat
업로드, 썸네일, 전환본, 이어 붙인 영상은 jobs/<프로젝트 id>/ 아래에 남는다.
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
import slide_render as sr      # noqa: E402  프리셋, 이징, xfade 목록
import slide_transition as st  # noqa: E402  영상에 전환 붙이기, 검증

BASE = Path(__file__).resolve().parent
WEB_DIR = BASE / "web"
JOBS = BASE / "jobs"
SAMPLE_SLIDES = 4
PROJECT_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
CONTENT_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".png": "image/png", ".json": "application/json"}

try:
    ENCODER = st.pick_encoder()
except ValueError as e:
    sys.exit(str(e))
XFADE_ALL = sr.xfade_transitions()


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
    videos = [sr.WORK / f"orig{k}.mp4" for k in range(1, SAMPLE_SLIDES + 1)]
    images = [sr.WORK / f"slide{k}.png" for k in range(1, SAMPLE_SLIDES + 1)]
    if not all(p.exists() for p in videos + images):
        videos, images = st.make_sample_originals(SAMPLE_SLIDES, ENCODER)
    return videos, images


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


def save_project(project: dict) -> None:
    (JOBS / project["id"] / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=1), encoding="utf-8")


def public_slide(s: dict) -> dict:
    return {k: s.get(k) for k in ("index", "name", "duration", "fps", "width", "height", "has_audio", "thumb",
                                  "source", "prev_image_name", "prev_thumb", "transition", "verify")} | {"output": s.get("output_url")}


def public_project(project: dict) -> dict:
    return {"id": project["id"], "encoder": ENCODER, "max_transition": sr.MAX_TRANSITION,
            "default_transition_duration": sr.DEFAULT_TRANSITION_DURATION,
            "slides": [public_slide(s) for s in project["slides"]]}


def make_thumb(video: Path, out: Path) -> None:
    sr.ffmpeg("-i", str(video), "-frames:v", "1", "-vf", "scale=320:-2", str(out))


def attach_prev_image(job: Path, s: dict, image: Path, display_name: str | None = None) -> None:
    """슬라이드 s 에 '들어올 때 앞에 둘 이미지' 를 붙인다 (썸네일 포함). 화면에는 원래 파일 이름을 보여준다."""
    thumb = job / "thumbs" / f"prev{s['index']}_{uuid.uuid4().hex[:4]}.jpg"
    make_thumb(image, thumb)
    s["prev_image"] = str(image)
    s["prev_image_name"] = display_name or image.name
    s["prev_thumb"] = f"/jobs/{job.name}/thumbs/{thumb.name}"
    s["source"] = "image"


def create_project(fields: dict[str, list[str]], files: dict[str, list[tuple[str, bytes]]]) -> dict:
    job = new_project_dir()
    sample = field(fields, "use_sample", "0") == "1"
    if sample:
        videos, sample_images = sample_inputs()
    else:
        video_files = files.get("videos", [])
        if len(video_files) < 1:
            raise ValueError("슬라이드 영상을 1개 이상 올려주세요")
        videos = save_uploads(job / "video", video_files)
        sample_images = []

    (job / "thumbs").mkdir()
    slides = []
    for i, video in enumerate(videos, start=1):
        info = st.probe(video)
        make_thumb(video, job / "thumbs" / f"{i}.jpg")
        s = {"index": i, "name": video.name, "video": str(video),
             "duration": info.duration, "fps": st.fps_str(info.fps), "width": info.width,
             "height": info.height, "has_audio": info.has_audio,
             "thumb": f"/jobs/{job.name}/thumbs/{i}.jpg",
             "source": "black" if i == 1 else "image",      # 1번은 검정 화면에서, 나머지는 앞 이미지에서
             "prev_image": None, "prev_image_name": None, "prev_thumb": None,
             "transition": None, "output": None, "output_url": None, "verify": None}
        if sample and i >= 2:                                 # 샘플은 앞 슬라이드 이미지를 미리 붙여 둔다
            attach_prev_image(job, s, sample_images[i - 2])
        slides.append(s)
    project = {"id": job.name, "slides": slides}
    save_project(project)
    return {"ok": True, "project": public_project(project)}


def slide_image_job(fields: dict[str, list[str]], files: dict[str, list[tuple[str, bytes]]]) -> dict:
    """슬라이드 k 에 들어올 때 앞에 둘 이미지를 올린다."""
    proj = load_project(field(fields, "project", ""))
    k = int(field(fields, "slide", "0"))
    if not 1 <= k <= len(proj["slides"]):
        raise ValueError("슬라이드 번호가 잘못됐습니다")
    items = files.get("image", [])
    if not items:
        raise ValueError("이미지 파일이 없습니다")
    name, data = items[0]
    job = JOBS / proj["id"]
    (job / "img").mkdir(exist_ok=True)
    image = job / "img" / f"slide{k}_prev_{uuid.uuid4().hex[:4]}_{name}"
    image.write_bytes(data)
    s = proj["slides"][k - 1]
    attach_prev_image(job, s, image, display_name=name)
    save_project(proj)
    return {"ok": True, "slide": public_slide(s)}


# ---------------------------------------------------------------- 전환 붙이기
def apply_job(body: dict) -> dict:
    proj = load_project(body.get("project"))
    slides = proj["slides"]
    k = int(body.get("slide") or 0)
    if not 1 <= k <= len(slides):
        raise ValueError("슬라이드 번호가 잘못됐습니다")
    s = slides[k - 1]

    t = body.get("transition") if isinstance(body.get("transition"), dict) else {}
    ttype = str(t.get("type") or "none")
    if ttype == "none":                                  # 전환 해제: 원본 그대로
        s["transition"], s["output"], s["output_url"], s["verify"] = None, None, None, None
        save_project(proj)
        return {"ok": True, "video": None, "report": ["전환 없음. 원본 영상을 그대로 씁니다."], "verify": None,
                "command": "", "slide": public_slide(s)}
    if ttype not in sr.PRESETS and ttype not in XFADE_ALL:
        raise ValueError(f"알 수 없는 전환: {ttype}")
    _, def_dur, def_ease, label = sr.resolve_transition(ttype)
    tdur = float(t.get("duration") or def_dur)
    easing = str(t.get("easing") or def_ease)

    source = str(body.get("source") or s.get("source") or ("black" if k == 1 else "image"))
    if source in st.START_COLORS:
        prev = source
    elif source == "image":
        prev = s.get("prev_image")
        if not prev:
            raise ValueError("앞에 둘 이미지를 먼저 올려주세요 (패널의 '이미지' 에서 선택)")
    else:
        raise ValueError("앞에 둘 그림은 black / white / image 중 하나")
    s["source"] = source

    out = JOBS / proj["id"] / f"slide{k}_t_{uuid.uuid4().hex[:6]}.mp4"
    cmd, info = st.apply(Path(s["video"]), prev, ttype, out, tdur, easing, ENCODER)
    rep = st.report(info, out, ttype, tdur, easing, prev)
    ver = st.verify(Path(s["video"]), out)

    s["transition"] = {"type": ttype, "duration": st.snap(tdur, info.fps), "easing": easing, "label": label}
    s["output"] = str(out)
    s["output_url"] = f"/jobs/{proj['id']}/{out.name}"
    s["verify"] = ver
    save_project(proj)
    return {"ok": True, "video": s["output_url"], "report": rep["lines"], "verify": ver,
            "command": sr.command_text(cmd), "slide": public_slide(s)}


def concat_job(body: dict) -> dict:
    """전환본이 있으면 전환본, 없으면 원본을 순서대로 이어 붙인다 (확인용, 재인코딩)."""
    proj = load_project(body.get("project"))
    videos = [Path(s["output"] or s["video"]) for s in proj["slides"]]
    out = JOBS / proj["id"] / f"all_{uuid.uuid4().hex[:6]}.mp4"
    cmd = st.concat(videos, out, ENCODER)
    expected = sum(st.probe(v).duration for v in videos)
    actual = st.probe(out).duration
    lines = [f"슬라이드 합계 {expected:.3f}s / 출력 {actual:.3f}s (확인용 재인코딩, 오디오는 AAC 로 다시 인코딩)"]
    t = 0.0
    for s, v in zip(proj["slides"], videos):
        d = st.probe(v).duration
        lines.append(f"  {t:7.3f}s  {s['index']}번 {v.name} ({d:.3f}s){' 전환본' if s['output'] else ' 원본'}")
        t += d
    return {"ok": True, "video": f"/jobs/{proj['id']}/{out.name}", "report": lines, "command": sr.command_text(cmd)}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "slide-transition-demo/3.1"

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            return self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/transitions":
            return self._send_json({
                "encoder": ENCODER,
                "presets": [{"key": k, "label": v[0], "xfade": v[1], "duration": v[2], "easing": v[3]}
                            for k, v in sr.PRESETS.items()],
                "easings": sr.EASINGS, "linear_raw": sorted(sr.LINEAR_RAW),
                "default_duration": sr.DEFAULT_TRANSITION_DURATION, "all": XFADE_ALL,
                "start_colors": st.START_COLORS})
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
            if path in ("/api/upload", "/api/slide_image"):
                fields, files = parse_multipart(ctype, body)
                result = create_project(fields, files) if path == "/api/upload" else slide_image_job(fields, files)
            elif path in ("/api/apply", "/api/concat"):
                data = json.loads(body.decode("utf-8") or "{}")
                result = apply_job(data) if path == "/api/apply" else concat_job(data)
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
    print(f"브라우저에서 열기: http://{args.host}:{args.port}   (종료: Ctrl+C)   인코더: {ENCODER}", flush=True)
    print("이 주소로 열어야 /api/... 요청이 이 서버로 옵니다. 다른 서버(Live Server, http.server 등)로 열면 API 가 동작하지 않습니다.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
