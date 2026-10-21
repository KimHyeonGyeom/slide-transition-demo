#!/usr/bin/env python3
"""슬라이드 영상에 "들어올 때 전환"을 붙이는 도구. 원본 영상과 규격, 길이, 음성이 동일한 영상을 만든다.

입력: 슬라이드 k 영상(음성 포함) + 앞 슬라이드(k-1) 이미지 + 전환 설정(종류, 길이, 움직임)
출력: 처음 T초만 "앞 슬라이드 이미지 -> 이 영상" 전환으로 바뀐 영상.
      해상도, fps, 프레임 수, 길이는 원본과 같고, 음성은 손대지 않고 그대로 복사한다.
첫 슬라이드: 앞 슬라이드가 없으므로 검정(또는 흰색) 화면에서 들어온다. 파워포인트와 같다.

    원본 k   [======================= 영상 k =======================]  + 음성
    출력 k   [앞 이미지 -> 영상 k 전환 T초][====== 영상 k 나머지 ======]  + 음성 그대로    길이 동일

인코더: LGPL ffmpeg 에는 libx264 가 없다. libopenh264 가 있으면 그것을 쓰고 (옵션은 실행 시점에
        그 인코더의 도움말로 확인해 지원되는 것만 넣는다), 없으면 libx264, 그다음 하드웨어 인코더 순으로 고른다.
        --encoder 로 직접 지정할 수 있다.

사용 예
  python3 slide_transition.py apply --video 2.mp4 --prev 1.png --transition push_right --out 2_t.mp4
  python3 slide_transition.py apply --video 1.mp4 --prev black --transition fade --out 1_t.mp4    # 첫 슬라이드
  python3 slide_transition.py verify --original 2.mp4 --output 2_t.mp4                           # 동일성 검사
  python3 slide_transition.py concat 1_t.mp4 2_t.mp4 3.mp4 --out all.mp4                        # 확인용 (재인코딩)
  python3 slide_transition.py sample                                                              # 샘플 4장으로 전 과정
  python3 slide_transition.py list                                                                # 프리셋, 인코더 목록
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from slide_render import (PRESETS, EASINGS, MAX_TRANSITION, DEFAULT_TRANSITION_DURATION, EASE_OVERSAMPLE,  # noqa: E402
                          SAMPLE_COLORS, SAMPLE_DURATIONS, WORK, OUT, resolve_transition, ease_setpts,
                          xfade_transitions, run, ffmpeg, command_text, font_opt, make_slide_image)

ENCODER_PREFERENCE = ["libopenh264", "libx264", "h264_videotoolbox", "h264_nvenc", "h264_qsv",
                      "h264_amf", "h264_mf", "h264_vaapi"]
START_COLORS = {"black": "검정 화면에서", "white": "흰색 화면에서"}   # 첫 슬라이드가 들어오는 시작 화면
SAMPLE_AUDIO_RATE = 48000


def fps_str(fps: Fraction) -> str:
    """30/1 은 30 으로, 30000/1001 은 그대로."""
    return str(fps.numerator) if fps.denominator == 1 else f"{fps.numerator}/{fps.denominator}"


def snap(sec: float, fps: Fraction) -> float:
    """프레임 단위로 반올림."""
    return float(round(sec * fps) / fps)


# ---------------------------------------------------------------- 영상 정보
@dataclass
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: Fraction
    nb_frames: int
    duration: float      # 프레임 수 / fps
    pix_fmt: str
    codec: str
    profile: str
    bit_rate: int        # bps, 모르면 0
    audio: str | None    # "aac 48000Hz 2ch" 같은 요약, 없으면 None

    @property
    def has_audio(self) -> bool:
        return self.audio is not None


def probe(path: Path) -> VideoInfo:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,profile,width,height,r_frame_rate,nb_frames,duration,pix_fmt,bit_rate,"
         "sample_rate,channels:format=duration,bit_rate", "-of", "json", str(path)],
        check=True, capture_output=True, text=True)
    data = json.loads(res.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        raise ValueError(f"영상 스트림이 없습니다: {path}")
    num, den = v["r_frame_rate"].split("/")
    fps = Fraction(int(num), int(den))
    nb = int(v.get("nb_frames") or 0)
    if nb <= 0:   # 컨테이너에 프레임 수가 없으면 세어 본다
        cnt = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                              "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(path)],
                             check=True, capture_output=True, text=True).stdout.strip()
        nb = int(cnt)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    audio = f"{a['codec_name']} {a.get('sample_rate', '?')}Hz {a.get('channels', '?')}ch" if a else None
    bit_rate = int(v.get("bit_rate") or data.get("format", {}).get("bit_rate") or 0)
    return VideoInfo(Path(path), int(v["width"]), int(v["height"]), fps, nb, float(Fraction(nb) / fps),
                     v.get("pix_fmt", "yuv420p"), v.get("codec_name", "?"), str(v.get("profile", "")), bit_rate, audio)


def audio_md5(path: Path) -> str | None:
    """오디오 스트림을 복사해서 낸 md5. 두 파일이 같으면 오디오 데이터가 비트 단위로 같다."""
    res = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "md5", "-"],
                         capture_output=True, text=True)
    m = re.search(r"MD5=([0-9a-f]+)", res.stdout)
    return m.group(1) if m else None


# ---------------------------------------------------------------- 인코더
_HELP_CACHE: dict[str, str] = {}


def available_encoders() -> set[str]:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return set(re.findall(r"^\s*V[\w.]{5}\s+(\S+)", out, flags=re.M))


def encoder_help(name: str) -> str:
    if name not in _HELP_CACHE:
        _HELP_CACHE[name] = subprocess.run(["ffmpeg", "-hide_banner", "-h", f"encoder={name}"],
                                           capture_output=True, text=True).stdout
    return _HELP_CACHE[name]


def pick_encoder(preferred: str | None = None) -> str:
    avail = available_encoders()
    if preferred:
        if preferred not in avail:
            raise ValueError(f"이 ffmpeg 에 {preferred} 인코더가 없습니다. 있는 것: "
                             + ", ".join(e for e in ENCODER_PREFERENCE if e in avail))
        return preferred
    for e in ENCODER_PREFERENCE:
        if e in avail:
            return e
    raise ValueError("H.264 인코더가 없습니다 (ffmpeg -encoders 로 확인)")


def auto_bitrate(info: VideoInfo) -> int:
    """정지 슬라이드 원본은 비트레이트가 아주 낮으므로 그대로 쓰면 전환 구간이 뭉개진다.
    원본의 4배와 해상도별 바닥값(1080p 6Mbps) 중 큰 값, 상한 16Mbps."""
    scale = (info.width * info.height) / (1920 * 1080)
    floor, cap = 6_000_000 * scale, 16_000_000 * scale
    return int(min(max(4 * (info.bit_rate or 0), floor), cap))


def openh264_profile(info: VideoInfo) -> str:
    p = info.profile.lower()
    if "baseline" in p:
        return "constrained_baseline"
    if "main" in p:
        return "main"
    return "high"


def encode_args(encoder: str, info: VideoInfo, bitrate: int | None = None) -> list[str]:
    br = bitrate or auto_bitrate(info)
    if encoder == "libopenh264":
        h = encoder_help(encoder)
        args = ["-c:v", "libopenh264", "-b:v", str(br), "-maxrate", str(br), "-bufsize", str(2 * br)]
        if "rc_mode" in h:
            args += ["-rc_mode", "quality"]
        if "profile" in h:
            args += ["-profile:v", openh264_profile(info)]
        args += ["-qmin", "8", "-qmax", "30", "-g", str(max(1, round(float(info.fps) * 2)))]
    elif encoder == "libx264":
        args = ["-c:v", "libx264", "-preset", "medium", "-crf", "14"]
    elif encoder == "h264_nvenc":
        args = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    elif encoder == "h264_qsv":
        args = ["-c:v", "h264_qsv", "-global_quality", "20"]
    elif encoder == "h264_amf":
        args = ["-c:v", "h264_amf", "-quality", "quality", "-b:v", str(br)]
    else:   # h264_videotoolbox, h264_mf, h264_vaapi 등
        args = ["-c:v", encoder, "-b:v", str(br)]
    return args + ["-pix_fmt", "yuv420p"]


# ---------------------------------------------------------------- 전환 붙이기
def build_apply_command(info: VideoInfo, prev: str, transition: str, out: Path,
                        tdur: float | None = None, easing: str | None = None,
                        encoder: str = "libx264", bitrate: int | None = None) -> list[str]:
    """영상 k 의 처음 T초를 prev(이미지 경로 또는 black/white) -> 영상 k 전환으로 바꾼 ffmpeg 인자.
    출력 프레임 수 = 원본 프레임 수, 음성은 복사."""
    f, fs = info.fps, fps_str(info.fps)
    w, h = info.width, info.height
    xf, def_dur, def_ease, _ = resolve_transition(transition)
    requested = tdur if tdur is not None else def_dur
    if not 0 < requested <= MAX_TRANSITION + 1e-9:
        raise ValueError(f"전환 길이는 0 초과 {MAX_TRANSITION}초 이하여야 합니다")
    t = snap(requested, f)          # 프레임 단위 반올림 (29.97fps 면 1.0초 -> 30프레임 = 1.001초)
    if t <= 0:
        raise ValueError("전환 길이가 한 프레임보다 짧습니다")
    easing = easing or def_ease
    if easing not in EASINGS:
        raise ValueError("움직임(easing)은 inout / soft / out / none 중 하나")
    if t >= info.duration:
        raise ValueError(f"전환 길이가 영상 길이({info.duration:.2f}s)보다 길거나 같습니다")
    if prev not in START_COLORS and not Path(prev).exists():
        raise ValueError(f"앞 슬라이드 이미지가 없습니다: {prev}")
    pixfmt = "yuv420p"

    def still(rate: Fraction, secs: float) -> list[str]:
        if prev in START_COLORS:
            return ["-f", "lavfi", "-i", f"color=c={prev}:s={w}x{h}:r={fps_str(rate)}:d={secs:.4f}"]
        return ["-loop", "1", "-framerate", fps_str(rate), "-t", f"{secs:.4f}", "-i", str(prev)]

    def norm(rate: Fraction) -> str:
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1,fps={fps_str(rate)},format={pixfmt}")

    enc = encode_args(encoder, info, bitrate)
    tail = ["-movflags", "+faststart", str(out)]
    setpts = ease_setpts(easing, t)
    if setpts is None:
        # 입력 0: 앞 그림 t초, 입력 1: 영상 k 전체. xfade offset=0 -> 길이 = t + D - t = D
        return [*still(f, t), "-i", str(info.path),
                "-filter_complex",
                f"[0:v]{norm(f)}[a];[1:v]fps={fs},format={pixfmt}[b];"
                f"[a][b]xfade=transition={xf}:duration={t:.4f}:offset=0[v]",
                "-map", "[v]", "-map", "1:a?", "-c:a", "copy", *enc, *tail]

    hi = f * EASE_OVERSAMPLE
    extra = 2 / f
    # 입력 0: 앞 그림(4배 fps, t초), 입력 1: 영상 k 앞부분(t초 + 2프레임), 입력 2: 영상 k 전체(나머지와 음성)
    return [*still(hi, t), "-t", f"{float(t + extra):.4f}", "-i", str(info.path), "-i", str(info.path),
            "-filter_complex",
            f"[0:v]{norm(hi)}[a];[1:v]fps={fps_str(hi)},format={pixfmt}[b];"
            f"[a][b]xfade=transition={xf}:duration={t:.4f}:offset=0,"
            f"setpts='{setpts}',fps={fs},trim=duration={t:.4f},setpts=PTS-STARTPTS[tr];"
            f"[2:v]trim=start={t:.4f},setpts=PTS-STARTPTS,fps={fs},format={pixfmt}[rest];"
            f"[tr][rest]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-map", "2:a?", "-c:a", "copy", *enc, *tail]


def apply(video: Path, prev: str, transition: str, out: Path, tdur: float | None = None,
          easing: str | None = None, encoder: str = "libx264", bitrate: int | None = None) -> tuple[list[str], VideoInfo]:
    info = probe(video)
    cmd = build_apply_command(info, prev, transition, out, tdur, easing, encoder, bitrate)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(*cmd)
    return cmd, info


def verify(original: Path, output: Path) -> dict:
    """출력이 원본과 같은 규격인지: 해상도, fps, 프레임 수, 길이, 오디오(비트 단위)."""
    a, b = probe(original), probe(output)
    ma, mb = audio_md5(original), audio_md5(output)
    checks = [
        ("해상도", f"{a.width}x{a.height}", f"{b.width}x{b.height}"),
        ("fps", fps_str(a.fps), fps_str(b.fps)),
        ("프레임 수", str(a.nb_frames), str(b.nb_frames)),
        ("영상 길이", f"{a.duration:.3f}s", f"{b.duration:.3f}s"),
        ("픽셀 포맷", a.pix_fmt, b.pix_fmt),
        ("오디오 스트림", a.audio or "없음", b.audio or "없음"),
        ("오디오 데이터 md5", ma or "없음", mb or "없음"),
    ]
    items = [{"name": n, "original": x, "output": y, "ok": x == y} for n, x, y in checks]
    ok = all(i["ok"] for i in items)
    lines = [("OK 원본과 동일" if ok else "!! 원본과 다름")]
    for i in items:
        mark = "O" if i["ok"] else "X"
        lines.append(f"  [{mark}] {i['name']}: 원본 {i['original']} / 출력 {i['output']}")
    for line in lines:
        print("   " + line)
    return {"ok": ok, "items": items, "lines": lines}


def report(info: VideoInfo, out: Path, transition: str, tdur: float | None, easing: str | None, prev: str) -> dict:
    xf, def_dur, def_ease, label = resolve_transition(transition)
    t = snap(tdur if tdur is not None else def_dur, info.fps)
    ease = easing or def_ease
    src = START_COLORS.get(prev, f"앞 슬라이드 이미지({Path(prev).name})")
    lines = [f"0.000s ~ {t:.3f}s  {label} [xfade {xf}], 움직임: {EASINGS[ease]}, {src} -> 이 영상"]
    print(f"-> {out}")
    for line in lines:
        print("   " + line)
    return {"lines": lines, "transition": t}


# ---------------------------------------------------------------- 확인용 이어 붙이기 (재인코딩)
def build_concat_command(videos: list[Path], out: Path, encoder: str) -> list[str]:
    infos = [probe(v) for v in videos]
    first = infos[0]
    with_audio = all(i.has_audio for i in infos)
    fs = fps_str(first.fps)
    cmd: list[str] = []
    filters, vs, as_ = [], [], []
    for i, v in enumerate(videos):
        cmd += ["-i", str(v)]
        filters.append(f"[{i}:v]scale={first.width}:{first.height}:force_original_aspect_ratio=decrease,"
                       f"pad={first.width}:{first.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fs},format=yuv420p[v{i}]")
        vs.append(f"[v{i}]")
        if with_audio:
            filters.append(f"[{i}:a]aresample={SAMPLE_AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
            as_.append(f"[a{i}]")
    if with_audio:
        filters.append("".join(f"{v}{a}" for v, a in zip(vs, as_)) + f"concat=n={len(videos)}:v=1:a=1[v][a]")
        maps = ["-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    else:
        filters.append("".join(vs) + f"concat=n={len(videos)}:v=1:a=0[v]")
        maps = ["-map", "[v]"]
    return cmd + ["-filter_complex", ";".join(filters), *maps, *encode_args(encoder, first),
                  "-movflags", "+faststart", str(out)]


def concat(videos: list[Path], out: Path, encoder: str) -> list[str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_concat_command(videos, out, encoder)
    ffmpeg(*cmd)
    expected = sum(probe(v).duration for v in videos)
    actual = probe(out).duration
    print(f"-> {out}\n   슬라이드 합계 {expected:.3f}s / 출력 {actual:.3f}s")
    return cmd


# ---------------------------------------------------------------- 샘플 (파이프라인이 만든 원본 영상 흉내)
def sample_encode_args(encoder: str) -> list[str]:
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    return ["-c:v", encoder, "-b:v", "3000000", "-pix_fmt", "yuv420p"]


def make_sample_originals(n: int = 4, encoder: str = "libx264") -> tuple[list[Path], list[Path]]:
    """슬라이드 이미지 n장과, 이미지 + 비프 나레이션 + 자막을 넣은 '원본' 영상 n개 (30fps, AAC)."""
    WORK.mkdir(exist_ok=True)
    print(f"== 샘플 생성: 슬라이드 이미지 {n}장 + 원본 영상 {n}개 (나레이션은 비프음, 자막 포함)")
    images, videos = [], []
    for k in range(1, n + 1):
        j = (k - 1) % len(SAMPLE_COLORS)
        img, wav, mp4 = WORK / f"slide{k}.png", WORK / f"narr{k}.wav", WORK / f"orig{k}.mp4"
        make_slide_image(img, f"Slide {k}", "video transition demo", SAMPLE_COLORS[j])
        d = SAMPLE_DURATIONS[j]
        ffmpeg("-f", "lavfi", "-i",
               f"aevalsrc='0.4*sin({440 + 110 * j}*2*PI*t)*gt(sin(2*PI*2*t),0)':s={SAMPLE_AUDIO_RATE}:d={d}",
               "-c:a", "pcm_s16le", str(wav))
        ffmpeg("-loop", "1", "-framerate", "30", "-i", str(img), "-i", str(wav),
               "-filter_complex",
               f"[0:v]format=yuv420p,drawtext={font_opt()}text='Narration subtitle for slide {k}':fontsize=44:"
               f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=16:x=(w-text_w)/2:y=h-150[v]",
               "-map", "[v]", "-map", "1:a", "-t", f"{d:.3f}", *sample_encode_args(encoder),
               "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(mp4))
        images.append(img)
        videos.append(mp4)
    return videos, images


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply", help="영상 하나에 들어올 때 전환 붙이기")
    a.add_argument("--video", type=Path, required=True, help="이 슬라이드 영상 (음성 포함)")
    a.add_argument("--prev", required=True, help="앞 슬라이드 이미지 경로, 또는 첫 슬라이드면 black / white")
    a.add_argument("--transition", required=True, help="프리셋 키(push_right 등) 또는 xfade 원본 이름. 목록: list")
    a.add_argument("--tdur", type=float, help=f"전환 길이(초), 최대 {MAX_TRANSITION}. 생략하면 프리셋 기본값")
    a.add_argument("--easing", choices=list(EASINGS), help="움직임. 생략하면 프리셋 기본값")
    a.add_argument("--encoder", help="H.264 인코더 (기본: libopenh264 > libx264 > 하드웨어 순으로 자동)")
    a.add_argument("--bitrate", type=int, help="비트레이트(bps). libx264 는 crf 를 쓰므로 무시")
    a.add_argument("--out", type=Path, required=True)

    v = sub.add_parser("verify", help="출력이 원본과 같은 규격인지 검사")
    v.add_argument("--original", type=Path, required=True)
    v.add_argument("--output", type=Path, required=True)

    c = sub.add_parser("concat", help="확인용으로 이어 붙이기 (재인코딩)")
    c.add_argument("videos", nargs="+", type=Path)
    c.add_argument("--encoder")
    c.add_argument("--out", type=Path, required=True)

    s = sub.add_parser("sample", help="샘플 4장: 원본 영상을 만들고 1번 검정에서 페이드, 2번 밀어내기, 4번 닦아내기")
    s.add_argument("--encoder")

    sub.add_parser("list", help="프리셋과 인코더 목록")
    args = ap.parse_args()

    if args.cmd == "list":
        print("프리셋 (키: 이름, xfade, 기본 길이, 움직임)")
        for key, (label, xf, dur, ease) in PRESETS.items():
            print(f"  {key:<14} {label:<22} {xf:<12} {dur}s  {ease}")
        avail = available_encoders()
        print("H.264 인코더:", ", ".join(e for e in ENCODER_PREFERENCE if e in avail) or "(없음)",
              "| 기본 선택:", next((e for e in ENCODER_PREFERENCE if e in avail), "(없음)"))
        print("xfade 원본:", " ".join(xfade_transitions()))
        return

    if args.cmd == "verify":
        sys.exit(0 if verify(args.original, args.output)["ok"] else 1)

    encoder = pick_encoder(getattr(args, "encoder", None))
    print(f"== 인코더: {encoder}")
    if args.cmd == "apply":
        cmd, info = apply(args.video, args.prev, args.transition, args.out, args.tdur, args.easing, encoder, args.bitrate)
        report(info, args.out, args.transition, args.tdur, args.easing, args.prev)
        sys.exit(0 if verify(args.video, args.out)["ok"] else 1)
    if args.cmd == "concat":
        concat(args.videos, args.out, encoder)
        return

    # sample
    OUT.mkdir(exist_ok=True)
    videos, images = make_sample_originals(4, encoder)
    plan = {1: ("black", "fade"), 2: (str(images[0]), "push_right"), 4: (str(images[2]), "wipe_right")}
    outputs, ok = [], True
    for k, video in enumerate(videos, start=1):
        if k in plan:
            prev, name = plan[k]
            out = OUT / f"slide{k}_t.mp4"
            print(f"== 슬라이드 {k}: {name} ({START_COLORS.get(prev, '앞 슬라이드 이미지')} -> 영상)")
            apply(video, prev, name, out, encoder=encoder)
            report(probe(video), out, name, None, None, prev)
            ok &= verify(video, out)["ok"]
            outputs.append(out)
        else:
            print(f"== 슬라이드 {k}: 전환 없음, 원본 그대로")
            outputs.append(video)
    print("== 확인용 이어 붙이기 (재인코딩)")
    concat(outputs, OUT / "all.mp4", encoder)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        sys.exit(str(e))
    except subprocess.CalledProcessError as e:
        sys.exit(f"ffmpeg 실패 (exit {e.returncode})")
