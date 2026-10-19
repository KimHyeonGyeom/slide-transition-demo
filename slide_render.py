#!/usr/bin/env python3
"""슬라이드별 영상 렌더링 데모. 전환은 영상 맨 앞에 들어가고 길이는 슬라이드 길이 그대로.

슬라이드 k 의 영상은 k 번 이미지와 길이(초)만으로 만든다.
k 번에 전환이 설정돼 있으면 k-1 번 이미지를 추가로 가져와 "이미지 k-1 -> 이미지 k" 전환을
영상의 처음 T초에 넣는다. 전환이 처음 T초를 차지하므로 영상 길이는 슬라이드 길이 그대로다.
TTS 오디오는 아직 다루지 않는다 (영상만 출력).

    슬라이드 k 영상 = [이미지 k-1 -> 이미지 k 전환 T초][ 이미지 k ................ ]   길이 = D_k
    전환 없음       = [ 이미지 k ........................................... ]   길이 = D_k

슬라이드마다 독립 렌더링이라 k 번 전환을 바꾸면 k 번 영상만 다시 만들면 되고,
규격이 같으니 나중에 재인코딩 없이(concat demuxer, -c copy) 이어 붙일 수 있다.

사용 예
  python3 slide_render.py render --image 2.png --duration 3.5 --out slide2.mp4
  python3 slide_render.py render --image 2.png --duration 3.5 --prev 1.png --transition fade --tdur 0.5 --out slide2.mp4
  python3 slide_render.py concat slide1.mp4 slide2.mp4 slide3.mp4 --out all.mp4   # 재인코딩 없이 이어 붙이기
  python3 slide_render.py sample      # 샘플 4장: 2번 fade, 4번 slideleft 로 만들고 all.mp4 까지

전환 이름 전체 목록: ffmpeg -h filter=xfade
"""
from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

WIDTH, HEIGHT, FPS = 1920, 1080, 30   # 모든 슬라이드 영상의 공통 규격 (이어 붙이려면 같아야 한다)
MAX_TRANSITION = 1.0                  # 전환 길이 상한(초)
DEFAULT_TRANSITION_DURATION = 0.5

# 자주 쓰는 전환효과
POPULAR_TRANSITIONS = [
    "fade",        # 크로스페이드
    "fadeblack",   # 검정으로 어두워졌다가 다음 슬라이드
    "dissolve",    # 노이즈 디졸브
    "wipeleft",    # 왼쪽으로 닦아내기
    "slideleft",   # 다음 슬라이드가 밀고 들어옴
    "smoothleft",  # 경계가 부드러운 와이프
    "circleopen",  # 원이 열리며 다음 슬라이드
    "zoomin",      # 줌인
]

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",          # Linux
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",  # Linux
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",             # macOS
    "/Library/Fonts/Arial Bold.ttf",                                 # macOS
    "C:/Windows/Fonts/arialbd.ttf",                                  # Windows
]

BASE = Path(__file__).resolve().parent
WORK = BASE / "work"
OUT = BASE / "out"


# ---------------------------------------------------------------- 공통
def run(cmd: list[str]) -> None:
    print("$ " + " ".join(shlex.quote(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True)


def ffmpeg(*args: str) -> None:
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args])


def command_text(cmd: list[str]) -> str:
    return "ffmpeg -y " + " ".join(shlex.quote(c) for c in cmd)


def font_opt() -> str:
    for cand in FONT_CANDIDATES:
        if Path(cand).exists():
            return "fontfile=" + cand.replace(":", r"\:") + ":"
    return ""


def snap(sec: float, fps: int = FPS) -> float:
    """프레임 단위로 반올림."""
    return round(sec * fps) / fps


def probe_duration(path: Path) -> float:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True)
    return float(res.stdout.strip())


def xfade_transitions() -> list[str]:
    """설치된 ffmpeg 가 지원하는 xfade 전환 이름 목록."""
    res = subprocess.run(["ffmpeg", "-hide_banner", "-h", "filter=xfade"],
                         capture_output=True, text=True)
    names = re.findall(r"^\s+([a-z]+)\s+-?\d+\s", res.stdout, flags=re.M)
    return [n for n in names if n != "custom"] or list(POPULAR_TRANSITIONS)


def norm_chain(w: int = WIDTH, h: int = HEIGHT, fps: int = FPS) -> str:
    """이미지를 공통 규격으로. 비율이 다르면 여백(검정)을 채운다."""
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p")


# ---------------------------------------------------------------- 슬라이드 1장 렌더
def build_render_command(image: Path, duration: float, out: Path,
                         prev_image: Path | None = None, transition: str | None = None,
                         tdur: float = DEFAULT_TRANSITION_DURATION,
                         w: int = WIDTH, h: int = HEIGHT, fps: int = FPS) -> list[str]:
    """슬라이드 1장 영상의 ffmpeg 인자. 전환이 있으면 처음 tdur 초가 prev_image -> image 전환."""
    d = snap(duration, fps)
    if d <= 0:
        raise ValueError("슬라이드 길이는 0보다 커야 합니다")
    norm = norm_chain(w, h, fps)
    use_transition = bool(prev_image) and bool(transition) and transition != "none"
    if use_transition:
        t = snap(tdur, fps)
        if not 0 < t <= MAX_TRANSITION + 1e-9:
            raise ValueError(f"전환 길이는 0 초과 {MAX_TRANSITION}초 이하여야 합니다")
        if t >= d:
            raise ValueError("전환 길이는 슬라이드 길이보다 짧아야 합니다")
        # 입력 0: 앞 슬라이드 이미지를 t초, 입력 1: 이 슬라이드 이미지를 d초.
        # xfade offset=0, duration=t -> 출력 길이 = t + d - t = d. 처음 t초만 섞이고 나머지는 이 슬라이드.
        cmd = ["-loop", "1", "-framerate", str(fps), "-t", f"{t:.4f}", "-i", str(prev_image),
               "-loop", "1", "-framerate", str(fps), "-t", f"{d:.4f}", "-i", str(image),
               "-filter_complex",
               f"[0:v]{norm}[a];[1:v]{norm}[b];[a][b]xfade=transition={transition}:duration={t:.4f}:offset=0[v]",
               "-map", "[v]"]
    else:
        cmd = ["-loop", "1", "-framerate", str(fps), "-t", f"{d:.4f}", "-i", str(image), "-vf", norm]
    cmd += ["-t", f"{d:.4f}",
            "-c:v", "libx264", "-preset", "medium", "-tune", "stillimage", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    return cmd


def render_slide(image: Path, duration: float, out: Path, prev_image: Path | None = None,
                 transition: str | None = None, tdur: float = DEFAULT_TRANSITION_DURATION) -> list[str]:
    cmd = build_render_command(image, duration, out, prev_image, transition, tdur)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(*cmd)
    return cmd


def report_slide(out: Path, duration: float, transition: str | None, tdur: float) -> dict:
    expected = snap(duration)
    actual = probe_duration(out)
    ok = abs(expected - actual) <= 1 / FPS + 1e-6
    lines = [f"슬라이드 길이 {expected:.3f}s / 출력 {actual:.3f}s  " + ("OK 길이 유지" if ok else "!! 길이 불일치")]
    if transition and transition != "none":
        lines.append(f"  0.000s ~ {snap(tdur):.3f}s  {transition} (앞 슬라이드 이미지 -> 이 슬라이드)")
    else:
        lines.append("  전환 없음")
    print(f"-> {out}")
    for line in lines:
        print("   " + line)
    return {"ok": ok, "expected": expected, "actual": actual, "lines": lines}


# ---------------------------------------------------------------- 이어 붙이기 (재인코딩 없음)
def build_concat_command(videos: list[Path], listfile: Path, out: Path) -> list[str]:
    listfile.write_text("".join(f"file '{Path(v).resolve().as_posix()}'\n" for v in videos), encoding="utf-8")
    return ["-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(out)]


def concat_videos(videos: list[Path], out: Path) -> list[str]:
    if len(videos) < 1:
        raise ValueError("이어 붙일 영상이 없습니다")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_concat_command(videos, out.with_suffix(".txt"), out)
    ffmpeg(*cmd)
    return cmd


def report_concat(videos: list[Path], out: Path) -> dict:
    expected = sum(probe_duration(v) for v in videos)
    actual = probe_duration(out)
    ok = abs(expected - actual) <= len(videos) / FPS + 1e-6
    lines = [f"슬라이드 합계 {expected:.3f}s / 출력 {actual:.3f}s  " + ("OK" if ok else "!! 길이 불일치")]
    t = 0.0
    for i, v in enumerate(videos, start=1):
        d = probe_duration(v)
        lines.append(f"  {t:7.3f}s  {i}번 {Path(v).name} ({d:.3f}s)")
        t += d
    print(f"-> {out}")
    for line in lines:
        print("   " + line)
    return {"ok": ok, "expected": expected, "actual": actual, "lines": lines}


# ---------------------------------------------------------------- 샘플 이미지
SAMPLE_COLORS = ["0x1e3a8a", "0xb45309", "0x065f46", "0x5b21b6"]
SAMPLE_DURATIONS = [3.0, 3.5, 2.5, 3.0]


def make_slide_image(path: Path, title: str, subtitle: str, color: str) -> None:
    vf = (f"drawbox=x=0:y=0:w=48:h=ih:color=white@0.35:t=fill,"
          f"drawtext={font_opt()}text='{title}':fontsize=170:fontcolor=white:"
          f"x=(w-text_w)/2:y=(h-text_h)/2-70,"
          f"drawtext={font_opt()}text='{subtitle}':fontsize=54:fontcolor=white@0.75:"
          f"x=(w-text_w)/2:y=(h-text_h)/2+110")
    ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s={WIDTH}x{HEIGHT}:d=1", "-vf", vf, "-frames:v", "1", str(path))


def make_sample_images(n: int = 4) -> list[Path]:
    WORK.mkdir(exist_ok=True)
    print(f"== 샘플 슬라이드 이미지 {n}장")
    images = []
    for k in range(1, n + 1):
        p = WORK / f"slide{k}.png"
        make_slide_image(p, f"Slide {k}", "per-slide render demo", SAMPLE_COLORS[(k - 1) % len(SAMPLE_COLORS)])
        images.append(p)
    return images


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="슬라이드 1장 영상 만들기")
    r.add_argument("--image", type=Path, required=True, help="이 슬라이드 이미지")
    r.add_argument("--duration", type=float, required=True, help="슬라이드 길이(초)")
    r.add_argument("--prev", type=Path, help="앞 슬라이드 이미지 (전환을 넣을 때)")
    r.add_argument("--transition", default="none", help="xfade 전환 이름 (기본 none)")
    r.add_argument("--tdur", type=float, default=DEFAULT_TRANSITION_DURATION,
                   help=f"전환 길이(초), 최대 {MAX_TRANSITION}")
    r.add_argument("--out", type=Path, required=True)

    c = sub.add_parser("concat", help="슬라이드 영상들을 재인코딩 없이 이어 붙이기")
    c.add_argument("videos", nargs="+", type=Path)
    c.add_argument("--out", type=Path, required=True)

    sub.add_parser("sample", help="샘플 4장을 만들고 2번 fade, 4번 slideleft 로 렌더한 뒤 이어 붙이기")
    args = ap.parse_args()

    if args.cmd == "render":
        if args.transition != "none" and not args.prev:
            sys.exit("전환을 넣으려면 --prev 로 앞 슬라이드 이미지를 주세요")
        render_slide(args.image, args.duration, args.out, args.prev, args.transition, args.tdur)
        ok = report_slide(args.out, args.duration, args.transition, args.tdur)["ok"]
    elif args.cmd == "concat":
        concat_videos(args.videos, args.out)
        ok = report_concat(args.videos, args.out)["ok"]
    else:
        OUT.mkdir(exist_ok=True)
        images = make_sample_images(4)
        settings = {2: ("fade", 0.5), 4: ("slideleft", 0.8)}   # k: (전환, 길이)
        videos, ok = [], True
        for k, image in enumerate(images, start=1):
            out = OUT / f"slide{k}.mp4"
            name, tdur = settings.get(k, ("none", 0.0))
            prev = images[k - 2] if k > 1 and name != "none" else None
            print(f"== 슬라이드 {k}: {'전환 ' + name + f' {tdur}s' if prev else '전환 없음'}")
            render_slide(image, SAMPLE_DURATIONS[k - 1], out, prev, name, tdur)
            ok &= report_slide(out, SAMPLE_DURATIONS[k - 1], name, tdur)["ok"]
            videos.append(out)
        print("== 이어 붙이기 (재인코딩 없음)")
        concat_videos(videos, OUT / "all.mp4")
        ok &= report_concat(videos, OUT / "all.mp4")["ok"]
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        sys.exit(str(e))
    except subprocess.CalledProcessError as e:
        sys.exit(f"ffmpeg 실패 (exit {e.returncode})")
