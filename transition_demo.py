#!/usr/bin/env python3
"""ffmpeg 슬라이드 전환효과 데모 (전체 길이 유지 버전).

입력: 슬라이드별 영상 N개 (이미지 + TTS 나레이션으로 이미 만들어 둔 mp4)
출력: 슬라이드 경계마다 전환효과가 들어간 영상 1개. 전체 길이는 입력 길이의 합과 같다.

원리
  슬라이드는 정지 화면이므로, 앞 영상의 마지막 프레임을 post초 더 늘리고 뒤 영상의
  첫 프레임을 pre초 앞에 더 붙인 뒤 그 pre+post 초를 xfade로 겹친다. 늘린 만큼 겹쳐서
  상쇄되므로 전체 길이는 그대로이고, 오디오는 손대지 않고 그대로 이어 붙인다.

      영상 A  [==========A==========]
      영상 B              [B첫 복제 T초][==========B==========]
                          └── 전환 T초 ──┘   (위치 before: B 시작 시각에 전환이 끝난다)

  전환은 "슬라이드로 들어올 때" 의 속성이다. 슬라이드 k 의 전환은 k-1 에서 k 로 넘어오는
  순간에 들어가고, 전환을 정하지 않은 슬라이드는 그냥 컷(none)으로 넘어간다.
  --images 를 주면 프레임 복제 대신 슬라이드 이미지 2장으로 전환 구간을 만들어 끼운다.
  (영상에 자막이 구워져 있어 첫/끝 프레임이 슬라이드 이미지와 다를 때 쓰는 옵션)

사용 예
  python3 transition_demo.py                                   # 샘플 영상 2개를 만들고 데모 생성
  python3 transition_demo.py --clips A.mp4 B.mp4               # 내 영상 2개로
  python3 transition_demo.py --clips A.mp4 B.mp4 --transition slideleft --duration 0.6
  python3 transition_demo.py --clips A.mp4 B.mp4 --align center    # before(기본) | center | after
  python3 transition_demo.py --clips A.mp4 B.mp4 --images a.png b.png
  python3 transition_demo.py --clips A.mp4 B.mp4 C.mp4 --transition fade,none   # 2번 fade, 3번 컷
  python3 transition_demo.py --clips A.mp4 B.mp4 --showcase    # 자주 쓰는 전환 8가지를 한 영상에

전환 이름 전체 목록: ffmpeg -h filter=xfade
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

AUDIO_RATE = 48000
MAX_TRANSITION = 1.0  # 전환 길이 상한(초)

# 자주 쓰는 전환효과 (showcase 에서 이 순서로 보여줌)
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

ALIGN_KO = {"before": "경계 앞", "center": "경계 중심", "after": "경계 뒤"}

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


def font_opt() -> str:
    """drawtext 에 넘길 fontfile 옵션. 없으면 빈 문자열(ffmpeg 가 fontconfig 기본 글꼴 사용)."""
    for cand in FONT_CANDIDATES:
        if Path(cand).exists():
            return "fontfile=" + cand.replace(":", r"\:") + ":"
    return ""


@dataclass
class ClipInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: Fraction
    has_audio: bool
    ss: float = 0.0          # 입력에서 건너뛸 시작 시각 (미리보기처럼 일부만 쓸 때)
    t: float | None = None   # 입력에서 읽을 길이 (None 이면 끝까지)


def probe(path: Path) -> ClipInfo:
    res = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "stream=codec_type,width,height,r_frame_rate,duration:format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True)
    data = json.loads(res.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError(f"영상 스트림이 없습니다: {path}")
    duration = float(video.get("duration") or data["format"]["duration"])
    num, den = video["r_frame_rate"].split("/")
    return ClipInfo(path, duration, int(video["width"]), int(video["height"]),
                    Fraction(int(num), int(den)),
                    any(s["codec_type"] == "audio" for s in data["streams"]))


def snap(sec: float, fps: Fraction) -> float:
    """프레임 단위로 반올림. 프레임 중간에서 자르면 1프레임씩 오차가 생기므로."""
    return float(round(sec * fps) / fps)


# ---------------------------------------------------------------- 타임라인
@dataclass
class Boundary:
    name: str    # xfade 전환 이름, "none" 이면 컷
    dur: float   # 전환 길이 (= pre + post), 컷이면 0
    pre: float   # 경계 앞쪽(앞 영상 끝부분)에 쓰는 시간
    post: float  # 경계 뒤쪽(뒤 영상 앞부분)에 쓰는 시간
    at: float    # 원래 타임라인에서 경계 시각 (= 뒤 슬라이드 시작 시각)


def plan(durations: list[float], names: list[str], durs: float | list[float], align: str,
         fps: Fraction) -> list[Boundary]:
    """경계마다 전환 이름과 길이를 받아 전환 구간을 계산한다. names[i] 는 durations[i] 와 [i+1] 사이."""
    if not isinstance(durs, list):
        durs = [float(durs)] * len(names)
    if align not in ALIGN_KO:
        raise ValueError("align 은 before / center / after 중 하나")
    bounds = []
    at = 0.0
    for i, (name, dur) in enumerate(zip(names, durs)):
        at += durations[i]
        if dur > MAX_TRANSITION + 1e-9:
            raise ValueError(f"전환 길이는 {MAX_TRANSITION}초 이하여야 합니다")
        whole = snap(dur, fps) if name != "none" else 0.0
        if whole <= 0:
            bounds.append(Boundary("none", 0.0, 0.0, 0.0, at))
            continue
        if align == "center":
            pre = snap(dur / 2, fps)
            post = whole - pre
        elif align == "before":
            pre, post = whole, 0.0
        else:
            pre, post = 0.0, whole
        bounds.append(Boundary(name, pre + post, pre, post, at))
    return bounds


def validate(infos: list[ClipInfo], bounds: list[Boundary]) -> list[str]:
    """문제가 있으면 ValueError, 규격이 달라 맞추는 클립은 경고 문자열로 돌려준다."""
    n = len(infos)
    warnings = []
    for i, info in enumerate(infos):
        used = (bounds[i - 1].post if i > 0 else 0.0) + (bounds[i].pre if i < n - 1 else 0.0)
        if info.duration <= used:
            raise ValueError(f"클립이 전환 구간보다 짧습니다: {info.path.name} ({info.duration:.2f}s)")
        if (info.width, info.height, info.fps) != (infos[0].width, infos[0].height, infos[0].fps):
            warnings.append(f"규격이 첫 클립과 달라서 맞춥니다: {info.path.name} "
                            f"{info.width}x{info.height}@{info.fps} -> "
                            f"{infos[0].width}x{infos[0].height}@{infos[0].fps}")
    for w in warnings:
        print("!! " + w)
    return warnings


# ---------------------------------------------------------------- 필터 그래프
def norm_chain(w: int, h: int, fps: Fraction) -> str:
    """모든 입력을 같은 규격으로. xfade/concat 은 규격이 다르면 받지 않는다."""
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p")


def video_filters_clone(infos: list[ClipInfo], bounds: list[Boundary], norm: str,
                        fps: Fraction) -> tuple[list[str], str]:
    """방식 1: 앞 영상 끝 프레임 / 뒤 영상 첫 프레임을 복제해서 늘린 뒤 그만큼 xfade 로 겹친다.
    컷(none) 경계는 concat 으로 그냥 이어 붙인다."""
    n = len(infos)
    filters, padded = [], []
    for i in range(n):
        start_pad = bounds[i - 1].pre if i > 0 else 0.0
        end_pad = bounds[i].post if i < n - 1 else 0.0
        chain = [norm]
        if start_pad > 0:
            chain.append(f"tpad=start_mode=clone:start_duration={start_pad:.4f}")
        if end_pad > 0:
            chain.append(f"tpad=stop_mode=clone:stop_duration={end_pad:.4f}")
        filters.append(f"[{i}:v]{','.join(chain)}[p{i}]")
        padded.append(infos[i].duration + start_pad + end_pad)

    tb = Fraction(1) / fps  # concat 은 timebase 를 바꾸므로 뒤이어 xfade 를 쓰려면 되돌려야 한다
    cur = "[p0]"
    elapsed = padded[0]  # 지금까지 합쳐진 (늘어난) 영상 길이
    for k, b in enumerate(bounds):
        if b.dur <= 0:
            filters.append(f"{cur}[p{k + 1}]concat=n=2:v=1:a=0,settb={tb}[x{k}]")
            elapsed += padded[k + 1]
        else:
            offset = elapsed - b.dur  # == b.at - b.pre
            filters.append(f"{cur}[p{k + 1}]xfade=transition={b.name}:duration={b.dur:.4f}"
                           f":offset={offset:.4f}[x{k}]")
            elapsed += padded[k + 1] - b.dur
        cur = f"[x{k}]"
    return filters, cur


def video_filters_images(infos: list[ClipInfo], bounds: list[Boundary], norm: str,
                         images: list[Path], fps: Fraction) -> tuple[list[str], list[str], str]:
    """방식 2: 영상을 경계 앞뒤로 잘라내고, 그 자리에 이미지 2장으로 만든 전환 구간을 끼운다.
    전환이 있는 경계마다 이미지 2장을 추가 입력(-loop 1 -t 전환길이)으로 연다.
    반환: (추가 입력 인자, 필터 목록, 출력 라벨)"""
    n = len(infos)
    extra, filters, segs = [], [], []
    next_idx = n
    for i in range(n):
        start = bounds[i - 1].post if i > 0 else 0.0
        end = infos[i].duration - (bounds[i].pre if i < n - 1 else 0.0)
        filters.append(f"[{i}:v]{norm},trim=start={start:.4f}:end={end:.4f},setpts=PTS-STARTPTS[s{i}]")
        segs.append(f"[s{i}]")
        if i < n - 1 and bounds[i].dur > 0:
            b = bounds[i]
            ia, ib = next_idx, next_idx + 1
            next_idx += 2
            for img in (images[i], images[i + 1]):
                extra += ["-loop", "1", "-framerate", str(fps), "-t", f"{b.dur:.4f}", "-i", str(img)]
            filters.append(f"[{ia}:v]{norm}[ia{i}]")
            filters.append(f"[{ib}:v]{norm}[ib{i}]")
            filters.append(f"[ia{i}][ib{i}]xfade=transition={b.name}:duration={b.dur:.4f}:offset=0[t{i}]")
            segs.append(f"[t{i}]")
    filters.append("".join(segs) + f"concat=n={len(segs)}:v=1:a=0[vcat]")
    return extra, filters, "[vcat]"


def audio_filters(infos: list[ClipInfo]) -> tuple[list[str], str]:
    """오디오는 전환과 무관하게 그대로 이어 붙인다. 각 클립의 오디오 길이를 영상 길이에 정확히 맞춰
    (짧으면 무음 채움, 길면 자름) 뒤로 갈수록 영상과 어긋나는 일을 막는다."""
    n = len(infos)
    filters = []
    for i, info in enumerate(infos):
        filters.append(f"[{i}:a]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                       f"apad,atrim=end={info.duration:.4f},asetpts=PTS-STARTPTS[a{i}]")
    filters.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[acat]")
    return filters, "[acat]"


def label_filter(bounds: list[Boundary], src: str) -> str:
    """데모용: 전환이 일어나는 동안 화면 아래에 전환 이름 표시."""
    draws = []
    for b in bounds:
        if b.dur <= 0:
            continue
        draws.append(f"drawtext={font_opt()}text='{b.name}':fontsize=64:fontcolor=yellow:"
                     f"box=1:boxcolor=black@0.6:boxborderw=24:x=(w-text_w)/2:y=h-220:"
                     f"enable='between(t,{b.at - b.pre - 0.5:.3f},{b.at + b.post + 0.5:.3f})'")
    if not draws:
        return f"{src}null[vlab]"
    return f"{src}{','.join(draws)}[vlab]"


# ---------------------------------------------------------------- 합치기
def build_join_command(infos: list[ClipInfo], bounds: list[Boundary], out: Path,
                       images: list[Path] | None = None, label: bool = False) -> list[str]:
    """ffmpeg 에 넘길 인자 목록 (ffmpeg 실행 파일과 -y 등 공통 옵션 제외)."""
    first = infos[0]
    norm = norm_chain(first.width, first.height, first.fps)

    cmd: list[str] = []
    for info in infos:
        if info.ss > 0:
            cmd += ["-ss", f"{info.ss:.4f}"]
        if info.t is not None:
            cmd += ["-t", f"{info.t:.4f}"]
        cmd += ["-i", str(info.path)]
    if images:
        extra, vfilters, vlabel = video_filters_images(infos, bounds, norm, images, first.fps)
        cmd += extra
    else:
        vfilters, vlabel = video_filters_clone(infos, bounds, norm, first.fps)
    afilters, alabel = audio_filters(infos)

    filters = vfilters + afilters
    if label:
        filters.append(label_filter(bounds, vlabel))
        vlabel = "[vlab]"

    cmd += ["-filter_complex", ";".join(filters),
            "-map", vlabel, "-map", alabel,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out)]
    return cmd


def join(infos: list[ClipInfo], bounds: list[Boundary], out: Path,
         images: list[Path] | None = None, label: bool = False) -> list[str]:
    cmd = build_join_command(infos, bounds, out, images, label)
    ffmpeg(*cmd)
    return cmd


def report(infos: list[ClipInfo], bounds: list[Boundary], out: Path, align: str) -> dict:
    """출력 길이가 입력 합계와 같은지 확인하고 타임라인을 출력. 웹 UI 에서도 쓰도록 dict 로 돌려준다."""
    expected = sum(i.duration for i in infos)
    actual = probe(out).duration
    ok = abs(expected - actual) <= float(1 / infos[0].fps) + 1e-6
    lines = [f"입력 합계 {expected:.3f}s / 출력 {actual:.3f}s  " + ("OK 길이 유지" if ok else "!! 길이 불일치")]
    for b in bounds:
        if b.dur <= 0:
            lines.append(f"{b.at:7.3f}s  컷 (전환 없음)")
        else:
            lines.append(f"{b.at - b.pre:7.3f}s ~ {b.at + b.post:7.3f}s  {b.name:<11} "
                         f"(경계 {b.at:.3f}s, {ALIGN_KO[align]})")
    print(f"-> {out}")
    for line in lines:
        print("   " + line)
    return {"ok": ok, "expected": expected, "actual": actual, "lines": lines,
            "bounds": [{"name": b.name, "start": b.at - b.pre, "end": b.at + b.post, "at": b.at}
                       for b in bounds if b.dur > 0]}


def xfade_transitions() -> list[str]:
    """설치된 ffmpeg 가 지원하는 xfade 전환 이름 목록."""
    res = subprocess.run(["ffmpeg", "-hide_banner", "-h", "filter=xfade"],
                         capture_output=True, text=True)
    names = re.findall(r"^\s+([a-z]+)\s+-?\d+\s", res.stdout, flags=re.M)
    return [n for n in names if n != "custom"] or list(POPULAR_TRANSITIONS)


# ---------------------------------------------------------------- 샘플 소스 (TTS 가 없어서 비프음)
SAMPLE_COLORS = ["0x1e3a8a", "0xb45309", "0x065f46", "0x5b21b6"]
SAMPLE_NARRATION = [3.0, 3.5, 2.5, 3.0]
SAMPLE_FREQ = [440, 660, 550, 770]


def make_slide_image(path: Path, title: str, subtitle: str, color: str) -> None:
    w, h = 1920, 1080
    vf = (f"drawbox=x=0:y=0:w=48:h=ih:color=white@0.35:t=fill,"
          f"drawtext={font_opt()}text='{title}':fontsize=170:fontcolor=white:"
          f"x=(w-text_w)/2:y=(h-text_h)/2-70,"
          f"drawtext={font_opt()}text='{subtitle}':fontsize=54:fontcolor=white@0.75:"
          f"x=(w-text_w)/2:y=(h-text_h)/2+110")
    ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d=1", "-vf", vf, "-frames:v", "1", str(path))


def make_narration(path: Path, seconds: float, freq: int) -> None:
    """TTS 대신 0.25초 간격 비프음. 전환 중 나레이션이 잘리면 바로 귀에 들리게 하는 용도."""
    expr = f"0.4*sin({freq}*2*PI*t)*gt(sin(2*PI*2*t),0)"
    ffmpeg("-f", "lavfi", "-i", f"aevalsrc='{expr}':s={AUDIO_RATE}:d={seconds}",
           "-c:a", "pcm_s16le", str(path))


def render_clip(image: Path, narration: Path, seconds: float, out: Path) -> None:
    """슬라이드 이미지 + 나레이션 -> 나레이션 길이만큼의 클립. 패딩 없음 (실제 파이프라인과 같은 조건)."""
    ffmpeg("-loop", "1", "-framerate", "30", "-i", str(image), "-i", str(narration),
           "-filter_complex",
           f"[0:v]fps=30,format=yuv420p[v];[1:a]aresample={AUDIO_RATE},aformat=channel_layouts=stereo[a]",
           "-map", "[v]", "-map", "[a]", "-t", f"{seconds:.3f}",
           "-c:v", "libx264", "-preset", "medium", "-tune", "stillimage", "-crf", "16",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out))


def make_samples(n: int = 2) -> tuple[list[Path], list[Path]]:
    """샘플 슬라이드 n장: 이미지, 비프 나레이션, 패딩 없는 클립."""
    WORK.mkdir(exist_ok=True)
    print(f"== 샘플 생성: 슬라이드 {n}장 (이미지, 비프 나레이션, 패딩 없는 클립)")
    clips, images = [], []
    for k in range(1, n + 1):
        j = (k - 1) % len(SAMPLE_COLORS)
        image, narr, clip = WORK / f"slide{k}.png", WORK / f"narr{k}.wav", WORK / f"clip{k}.mp4"
        make_slide_image(image, f"Slide {k}", "ffmpeg xfade transition demo", SAMPLE_COLORS[j])
        make_narration(narr, SAMPLE_NARRATION[j], SAMPLE_FREQ[j])
        render_clip(image, narr, SAMPLE_NARRATION[j], clip)
        clips.append(clip)
        images.append(image)
    return clips, images


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clips", nargs="+", type=Path, metavar="MP4",
                    help="슬라이드 영상 2개 이상. 생략하면 샘플을 만들어 씀")
    ap.add_argument("--images", nargs="+", type=Path, metavar="PNG",
                    help="클립과 같은 개수의 슬라이드 이미지. 주면 프레임 복제 대신 이미지로 전환 구간 생성")
    ap.add_argument("--transition", default="fade",
                    help="xfade 전환 이름. 쉼표로 경계별 지정 가능, none 은 컷 (예: fade,none). 기본 fade")
    ap.add_argument("--duration", type=float, default=1.0,
                    help=f"전환 길이(초). 기본 1.0, 최대 {MAX_TRANSITION}")
    ap.add_argument("--align", choices=list(ALIGN_KO), default="before",
                    help="전환 위치. before(기본): 앞 영상 끝부분 / center: 경계 앞뒤 반씩 / after: 뒤 영상 앞부분")
    ap.add_argument("--out", type=Path, help="출력 파일. 기본 out/ 아래 자동 이름")
    ap.add_argument("--showcase", action="store_true",
                    help="자주 쓰는 전환 8가지를 한 영상에 차례로 (샘플 모드에서는 항상 생성)")
    args = ap.parse_args()

    if not 0 < args.duration <= MAX_TRANSITION:
        sys.exit(f"--duration 은 0 초과 {MAX_TRANSITION} 이하여야 합니다")
    OUT.mkdir(exist_ok=True)

    sample = args.clips is None
    if sample:
        clips, sample_images = make_samples()
    else:
        clips, sample_images = args.clips, None
    if len(clips) < 2:
        sys.exit("클립은 2개 이상 필요합니다")
    images = args.images
    if images and len(images) != len(clips):
        sys.exit("--images 개수는 --clips 개수와 같아야 합니다")

    infos = [probe(c) for c in clips]
    for info in infos:
        if not info.has_audio:
            sys.exit(f"오디오 스트림이 없습니다: {info.path}")

    names = args.transition.split(",")
    if len(names) == 1:
        names = names * (len(clips) - 1)
    if len(names) != len(clips) - 1:
        sys.exit("전환 이름 개수는 클립 개수 - 1 이어야 합니다")

    bounds = plan([i.duration for i in infos], names, args.duration, args.align, infos[0].fps)
    validate(infos, bounds)

    tag = (names[0] if len(set(names)) == 1 else "multi") + "_" + args.align
    prefix = "demo" if sample else "result"
    out = args.out or OUT / f"{prefix}_{tag}.mp4"

    print(f"== 합치기: {args.transition}, {args.duration}s, {ALIGN_KO[args.align]}"
          + (", 이미지로 전환 구간 생성" if images else ", 프레임 복제"))
    join(infos, bounds, out, images=images)
    ok = report(infos, bounds, out, args.align)["ok"]

    if sample and not images:
        out2 = OUT / f"{prefix}_{tag}_images.mp4"
        print("== 같은 조건을 이미지 방식으로 (--images)")
        join(infos, bounds, out2, images=sample_images)
        ok &= report(infos, bounds, out2, args.align)["ok"]

    if sample or args.showcase:
        k = len(POPULAR_TRANSITIONS)
        sc_infos = [infos[i % len(infos)] for i in range(k + 1)]       # 클립을 번갈아 재사용
        sc_images = [images[i % len(images)] for i in range(k + 1)] if images else None
        sc_bounds = plan([i.duration for i in sc_infos], POPULAR_TRANSITIONS, args.duration,
                         args.align, infos[0].fps)
        validate(sc_infos, sc_bounds)
        out3 = OUT / f"{prefix}_showcase.mp4"
        print("== showcase: 자주 쓰는 전환 8가지")
        join(sc_infos, sc_bounds, out3, images=sc_images, label=True)
        ok &= report(sc_infos, sc_bounds, out3, args.align)["ok"]

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        sys.exit(str(e))
    except subprocess.CalledProcessError as e:
        sys.exit(f"ffmpeg 실패 (exit {e.returncode})")
