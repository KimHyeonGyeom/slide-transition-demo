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
  python3 slide_render.py render --image 2.png --duration 3.5 --prev 1.png --transition push_right --out slide2.mp4
  python3 slide_render.py render --image 2.png --duration 3.5 --prev 1.png --transition wipeleft --tdur 0.6 --easing none --out slide2.mp4
  python3 slide_render.py concat slide1.mp4 slide2.mp4 slide3.mp4 --out all.mp4   # 재인코딩 없이 이어 붙이기
  python3 slide_render.py sample      # 샘플 4장: 2번 페이드, 4번 밀어내기로 만들고 all.mp4 까지
  python3 slide_render.py list        # 프리셋(파워포인트 이름)과 xfade 원본 목록

전환 프리셋은 파워포인트 이름과 기본 길이를 따르고, 움직임 계열에는 천천히 시작해 천천히 끝나는
이징(inout)을 넣는다. xfade 자체는 속도가 일정하므로, 전환 구간을 4배 프레임레이트로 선형 렌더한 뒤
setpts 로 시간축을 휘고 fps 로 다시 샘플해서 만든다.

전환 이름 전체 목록: ffmpeg -h filter=xfade
"""
from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

WIDTH, HEIGHT, FPS = 1920, 1080, 60   # 모든 슬라이드 영상의 공통 규격 (이어 붙이려면 같아야 한다)
# FPS 60 인 이유: 30 에서는 1초 밀어내기가 한 프레임에 64~98px 씩 점프해 글자가 번져 보인다.
# 정지 구간은 같은 프레임의 반복이라 60 이어도 용량이 거의 늘지 않는다. 필요하면 --fps 30.
MAX_TRANSITION = 1.0                  # 전환 길이 상한(초)
DEFAULT_TRANSITION_DURATION = 0.5
EASE_OVERSAMPLE = 4                   # 이징: 전환 구간을 FPS*4 로 먼저 렌더한 뒤 시간축을 휘어 FPS 로 다시 샘플

# 파워포인트 스타일 프리셋: 키 -> (한글 이름, xfade 전환, 기본 길이(초), 기본 움직임)
# 방향은 "새 슬라이드가 어디에서 들어오는지" 기준 (파워포인트의 '오른쪽에서' 와 같은 표현)
PRESETS = {
    "fade":          ("페이드",                  "fade",        0.7, "none"),
    # 나타내기는 xfade 가 아니라 overlay + fade(알파): 새 슬라이드가 살짝 밀려 들어오며 페이드인 (APPEAR 참고)
    "appear_right":  ("나타내기 (오른쪽에서)",    "appear_right", 1.0, "out"),
    "appear_left":   ("나타내기 (왼쪽에서)",      "appear_left",  1.0, "out"),
    "push_right":    ("밀어내기 (오른쪽에서)",    "slideleft",   1.0, "inout"),
    "push_left":     ("밀어내기 (왼쪽에서)",      "slideright",  1.0, "inout"),
    "push_bottom":   ("밀어내기 (아래에서)",      "slideup",     1.0, "inout"),
    "push_top":      ("밀어내기 (위에서)",        "slidedown",   1.0, "inout"),
    # 닦아내기는 또렷한 경계(wipe*)를 쓴다. smooth* 는 그라데이션 띠가 너무 넓어 앞선이 0.6초 만에
    # 화면을 가로질러 실제보다 빠르게 느껴진다. 움직임도 살짝만 완만하게 해서 1초를 고르게 쓴다.
    "wipe_right":    ("닦아내기 (오른쪽에서)",    "wipeleft",    1.0, "soft"),
    "wipe_left":     ("닦아내기 (왼쪽에서)",      "wiperight",   1.0, "soft"),
    "wipe_bottom":   ("닦아내기 (아래에서)",      "wipeup",      1.0, "soft"),
    "wipe_top":      ("닦아내기 (위에서)",        "wipedown",    1.0, "soft"),
    "cover_right":   ("덮기 (오른쪽에서)",        "coverleft",   1.0, "inout"),
    "cover_left":    ("덮기 (왼쪽에서)",          "coverright",  1.0, "inout"),
    "uncover_left":  ("벗기기 (왼쪽으로)",        "revealleft",  1.0, "inout"),
    "uncover_right": ("벗기기 (오른쪽으로)",      "revealright", 1.0, "inout"),
    "split_h":       ("나누기 (가로, 바깥쪽으로)", "horzopen",    1.0, "inout"),
    "split_v":       ("나누기 (세로, 바깥쪽으로)", "vertopen",    1.0, "inout"),
    "shape_circle":  ("도형 (원, 바깥쪽으로)",    "circleopen",  1.0, "inout"),
    "clock":         ("시계",                    "radial",      1.0, "inout"),
    "zoom":          ("확대",                    "zoomin",      1.0, "inout"),
    "blinds":        ("블라인드",                "hlslice",     1.0, "inout"),
    "dissolve":      ("디졸브",                  "dissolve",    1.0, "none"),
}
EASINGS = {
    "inout": "부드럽게 (천천히 시작해서 천천히 끝)",
    "soft":  "살짝 부드럽게 (조금만 완만하게)",
    "out":   "감속 (빠르게 시작해서 천천히 끝)",
    "none":  "일정한 속도",
}
SOFT_GAMMA = 1.25   # soft 곡선 세기. 1 이면 일정 속도, 클수록 inout 에 가까워진다
# 나타내기: "xfade 이름" 자리에 들어가는 가짜 이름 -> 새 슬라이드가 들어오는 방향(+1 오른쪽에서, -1 왼쪽에서)
APPEAR = {"appear_right": 1, "appear_left": -1}
APPEAR_SHIFT = 0.075   # 이동 거리 = 폭의 7.5% (1920 이면 144px)
# 원본 xfade 이름을 직접 쓸 때, 움직임이 아니라 섞임 위주라 이징이 의미 없는 것들
LINEAR_RAW = {"fade", "fadeblack", "fadewhite", "fadefast", "fadeslow", "fadegrays",
              "dissolve", "pixelize", "hblur", "distance"}

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


def resolve_transition(name: str) -> tuple[str, float, str, str]:
    """프리셋 키 또는 xfade 원본 이름 -> (xfade 이름, 기본 길이, 기본 움직임, 표시 이름)"""
    if name in PRESETS:
        label, xf, dur, ease = PRESETS[name]
        return xf, dur, ease, label
    return name, DEFAULT_TRANSITION_DURATION, ("none" if name in LINEAR_RAW else "inout"), name


def ease_setpts(easing: str, t: float) -> str | None:
    """전환 구간(0~t초)의 시간축을 휘는 setpts 식. 선형으로 렌더한 프레임(진행도 p = T/t)을
    원하는 속도 곡선의 역함수 위치로 옮긴다. 그 뒤 fps 필터가 일정한 간격으로 다시 샘플한다.
      inout: smoothstep p = u^2(3-2u) 의 역함수 u = 0.5 - sin(asin(1-2p)/3)
      soft:  gain 곡선 p = u^g/(u^g+(1-u)^g) (g=SOFT_GAMMA) 의 역함수 = 같은 식에 지수 1/g
      out:   ease-out   p = 1-(1-u)^2 의 역함수 u = 1 - sqrt(1-p)
    T 는 setpts 의 프레임 시각 변수(초)."""
    if easing == "inout":
        inv = f"(0.5-sin(asin(1-2*min(T/{t:.4f},1))/3))"
    elif easing == "soft":
        q = 1 / SOFT_GAMMA
        inv = (f"(pow(min(T/{t:.4f},1),{q:.4f})/(pow(min(T/{t:.4f},1),{q:.4f})"
               f"+pow(1-min(T/{t:.4f},1),{q:.4f})))")
    elif easing == "out":
        inv = f"(1-sqrt(1-min(T/{t:.4f},1)))"
    else:
        return None
    return f"if(lt(T,{t:.4f}),{inv}*{t:.4f}/TB,PTS)"


def appear_x_expr(xf: str, easing: str, t: float, w: int) -> str:
    """나타내기의 overlay x 식. 진행도 u = t/전환길이 (0~1), 오프셋 = 이동거리 * (1 - 속도곡선(u)).
    t 는 overlay 의 프레임 시각 변수(초). 페이드(알파)는 fade 필터가 일정하게, 이동만 이 곡선을 따른다."""
    u = f"min(t/{t:.4f},1)"
    if easing == "inout":
        curve = f"1-{u}*{u}*(3-2*{u})"
    elif easing == "soft":
        g = f"{SOFT_GAMMA:.4f}"
        curve = f"1-pow({u},{g})/(pow({u},{g})+pow(1-{u},{g}))"
    elif easing == "out":
        curve = f"pow(1-{u},2)"
    else:
        curve = f"1-{u}"
    dx = int(w * APPEAR_SHIFT + 0.5)
    return f"{'-' if APPEAR[xf] < 0 else ''}({curve})*{dx}"


def norm_chain(w: int = WIDTH, h: int = HEIGHT, fps: int = FPS) -> str:
    """이미지를 공통 규격으로. 비율이 다르면 여백(검정)을 채운다."""
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p")


# ---------------------------------------------------------------- 슬라이드 1장 렌더
def build_render_command(image: Path, duration: float, out: Path,
                         prev_image: Path | None = None, transition: str | None = None,
                         tdur: float | None = None, easing: str | None = None,
                         w: int = WIDTH, h: int = HEIGHT, fps: int = FPS) -> list[str]:
    """슬라이드 1장 영상의 ffmpeg 인자. 전환이 있으면 처음 tdur 초가 prev_image -> image 전환.

    transition 은 프리셋 키(push_right 등) 또는 xfade 원본 이름. tdur/easing 을 생략하면 프리셋 기본값.
    easing 이 none 이 아니면: 전환 구간만 fps*EASE_OVERSAMPLE 로 선형 렌더 -> setpts 로 시간축을 휨
    -> fps 로 다시 샘플 -> 나머지 구간(이 슬라이드 정지 화면)과 concat. 길이는 그대로 d.
    """
    d = snap(duration, fps)
    if d <= 0:
        raise ValueError("슬라이드 길이는 0보다 커야 합니다")
    norm = norm_chain(w, h, fps)
    use_transition = bool(prev_image) and bool(transition) and transition != "none"
    if not use_transition:
        return ["-loop", "1", "-framerate", str(fps), "-t", f"{d:.4f}", "-i", str(image), "-vf", norm,
                "-t", f"{d:.4f}", *_encode_args(out)]

    xf, def_dur, def_ease, _ = resolve_transition(transition)
    requested = tdur if tdur is not None else def_dur
    if not 0 < requested <= MAX_TRANSITION + 1e-9:
        raise ValueError(f"전환 길이는 0 초과 {MAX_TRANSITION}초 이하여야 합니다")
    t = snap(requested, fps)
    if t <= 0:
        raise ValueError("전환 길이가 한 프레임보다 짧습니다")
    easing = easing or def_ease
    if easing not in EASINGS:
        raise ValueError("움직임(easing)은 inout / soft / out / none 중 하나")
    if t >= d:
        raise ValueError("전환 길이는 슬라이드 길이보다 짧아야 합니다")

    if xf in APPEAR:
        # 나타내기: 앞 이미지 위에 이 이미지를 살짝 밀려 들어오며 페이드인(알파)으로 얹는다. xfade 를 쓰지 않는다.
        extra = 2 / fps
        x = appear_x_expr(xf, easing, t, w)
        return ["-loop", "1", "-framerate", str(fps), "-t", f"{t:.4f}", "-i", str(prev_image),
                "-loop", "1", "-framerate", str(fps), "-t", f"{t + extra:.4f}", "-i", str(image),
                "-loop", "1", "-framerate", str(fps), "-t", f"{d - t:.4f}", "-i", str(image),
                "-filter_complex",
                f"[0:v]{norm}[a];[1:v]{norm},trim=duration={t:.4f},setpts=PTS-STARTPTS,format=yuva420p,"
                f"fade=t=in:st=0:d={t:.4f}:alpha=1[b];"
                f"[a][b]overlay=x='{x}':y=0:eof_action=pass,format=yuv420p,trim=duration={t:.4f},setpts=PTS-STARTPTS[tr];"
                f"[2:v]{norm}[rest];[tr][rest]concat=n=2:v=1:a=0[v]",
                "-map", "[v]", "-t", f"{d:.4f}", *_encode_args(out)]

    setpts = ease_setpts(easing, t)
    if setpts is None:
        # 입력 0: 앞 이미지 t초, 입력 1: 이 이미지 d초. xfade offset=0 -> 출력 길이 = t + d - t = d
        return ["-loop", "1", "-framerate", str(fps), "-t", f"{t:.4f}", "-i", str(prev_image),
                "-loop", "1", "-framerate", str(fps), "-t", f"{d:.4f}", "-i", str(image),
                "-filter_complex",
                f"[0:v]{norm}[a];[1:v]{norm}[b];[a][b]xfade=transition={xf}:duration={t:.4f}:offset=0[v]",
                "-map", "[v]", "-t", f"{d:.4f}", *_encode_args(out)]

    hi = fps * EASE_OVERSAMPLE
    norm_hi = norm_chain(w, h, hi)
    extra = 2 / fps   # 전환 뒤 몇 프레임을 더 뽑아 두고 trim 으로 정확히 t초를 만든다
    return ["-loop", "1", "-framerate", str(hi), "-t", f"{t:.4f}", "-i", str(prev_image),
            "-loop", "1", "-framerate", str(hi), "-t", f"{t + extra:.4f}", "-i", str(image),
            "-loop", "1", "-framerate", str(fps), "-t", f"{d - t:.4f}", "-i", str(image),
            "-filter_complex",
            f"[0:v]{norm_hi}[a];[1:v]{norm_hi}[b];"
            f"[a][b]xfade=transition={xf}:duration={t:.4f}:offset=0,"
            f"setpts='{setpts}',fps={fps},trim=duration={t:.4f},setpts=PTS-STARTPTS[tr];"
            f"[2:v]{norm}[rest];[tr][rest]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-t", f"{d:.4f}", *_encode_args(out)]


def _encode_args(out: Path) -> list[str]:
    # crf 14, tune 없음: 전환처럼 움직이는 프레임도 선명하게 유지된다. 측정(무손실 대비 PSNR)에서
    # stillimage+crf18 은 전환 프레임 49dB/645KB, crf14 는 52dB/666KB 로 용량은 비슷하고 화질은 위.
    # 정지 구간은 어차피 같은 프레임의 반복이라 용량이 거의 늘지 않는다.
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "14",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]


def render_slide(image: Path, duration: float, out: Path, prev_image: Path | None = None,
                 transition: str | None = None, tdur: float | None = None,
                 easing: str | None = None, fps: int = FPS) -> list[str]:
    cmd = build_render_command(image, duration, out, prev_image, transition, tdur, easing, fps=fps)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(*cmd)
    return cmd


def report_slide(out: Path, duration: float, transition: str | None, tdur: float | None = None,
                 easing: str | None = None, fps: int = FPS) -> dict:
    expected = snap(duration, fps)
    actual = probe_duration(out)
    ok = abs(expected - actual) <= 1 / fps + 1e-6
    lines = [f"슬라이드 길이 {expected:.3f}s / 출력 {actual:.3f}s  " + ("OK 길이 유지" if ok else "!! 길이 불일치")]
    if transition and transition != "none":
        xf, def_dur, def_ease, label = resolve_transition(transition)
        t = snap(tdur if tdur is not None else def_dur, fps)
        ease = easing or def_ease
        lines.append(f"  0.000s ~ {t:.3f}s  {label} [xfade {xf}], 움직임: {EASINGS[ease]} (앞 슬라이드 이미지 -> 이 슬라이드)")
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


def make_slide_image(path: Path, title: str, subtitle: str, color: str, strip: bool = True) -> None:
    """단색 배경 + 제목/부제. strip: 왼쪽 가장자리의 밝은 띠(전환 미리보기에서 슬라이드가 움직이는 게 보이도록)."""
    vf = ((f"drawbox=x=0:y=0:w=48:h=ih:color=white@0.35:t=fill," if strip else "") +
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
    r.add_argument("--transition", default="none",
                   help="프리셋 키(push_right, wipe_left, fade ...) 또는 xfade 원본 이름. 기본 none. 목록: --list")
    r.add_argument("--tdur", type=float, help=f"전환 길이(초), 최대 {MAX_TRANSITION}. 생략하면 프리셋 기본값")
    r.add_argument("--easing", choices=list(EASINGS), help="움직임: inout / out / none. 생략하면 프리셋 기본값")
    r.add_argument("--fps", type=int, default=FPS, help=f"프레임레이트 (기본 {FPS}). 60 이면 움직임이 더 매끄럽다")
    r.add_argument("--out", type=Path, required=True)

    c = sub.add_parser("concat", help="슬라이드 영상들을 재인코딩 없이 이어 붙이기")
    c.add_argument("videos", nargs="+", type=Path)
    c.add_argument("--out", type=Path, required=True)

    sp = sub.add_parser("sample", help="샘플 4장을 만들고 2번 페이드, 4번 밀어내기로 렌더한 뒤 이어 붙이기")
    sp.add_argument("--fps", type=int, default=FPS)
    sub.add_parser("list", help="프리셋과 xfade 전환 목록")
    args = ap.parse_args()

    if args.cmd == "list":
        print("프리셋 (키: 이름, xfade, 기본 길이, 움직임)")
        for key, (label, xf, dur, ease) in PRESETS.items():
            print(f"  {key:<14} {label:<22} {xf:<12} {dur}s  {ease}")
        print("xfade 원본:", " ".join(xfade_transitions()))
        return

    if args.cmd == "render":
        if args.transition != "none" and not args.prev:
            sys.exit("전환을 넣으려면 --prev 로 앞 슬라이드 이미지를 주세요")
        render_slide(args.image, args.duration, args.out, args.prev, args.transition, args.tdur, args.easing, args.fps)
        ok = report_slide(args.out, args.duration, args.transition, args.tdur, args.easing, args.fps)["ok"]
    elif args.cmd == "concat":
        concat_videos(args.videos, args.out)
        ok = report_concat(args.videos, args.out)["ok"]
    else:
        OUT.mkdir(exist_ok=True)
        images = make_sample_images(4)
        settings = {2: ("fade", 0.7), 4: ("push_right", 1.0)}   # k: (전환, 길이)
        videos, ok = [], True
        for k, image in enumerate(images, start=1):
            out = OUT / f"slide{k}.mp4"
            name, tdur = settings.get(k, ("none", 0.0))
            prev = images[k - 2] if k > 1 and name != "none" else None
            print(f"== 슬라이드 {k}: {'전환 ' + name + f' {tdur}s' if prev else '전환 없음'}")
            render_slide(image, SAMPLE_DURATIONS[k - 1], out, prev, name, tdur, fps=args.fps)
            ok &= report_slide(out, SAMPLE_DURATIONS[k - 1], name, tdur, fps=args.fps)["ok"]
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
