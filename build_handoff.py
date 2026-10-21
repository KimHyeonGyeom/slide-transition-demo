#!/usr/bin/env python3
"""개발자 전달용 단일 HTML(handoff/index.html) 생성기.

프리셋마다 미리보기(샘플 슬라이드 1 -> 2, 전환 1초, 480x270, 2초)를 렌더해 base64 로 HTML 에 내장한다.
명령어는 handoff_template.html 의 자바스크립트가 전역 설정에 맞춰 조립한다 (파이썬 build_apply_command 와 같은 구조).

  python3 build_handoff.py            # -> handoff/index.html (미리보기는 work/handoff/ 에 캐시)
  python3 build_handoff.py --rebuild  # 미리보기까지 다시 렌더
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slide_render as sr      # noqa: E402
import slide_transition as st  # noqa: E402

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "handoff"
TMP = sr.WORK / "handoff"
PW, PH = 480, 270          # 미리보기 크기
PREVIEW_SEC = 2.0          # 전환 1초 + 정지 1초
EASING_SHORT = {"inout": "부드럽게", "soft": "살짝 부드럽게", "out": "감속", "none": "일정 속도"}


def ensure_samples() -> None:
    if not all((sr.WORK / f).exists() for f in ("orig2.mp4", "slide1.png")):
        st.make_sample_originals(4, st.pick_encoder())


def make_preview_source() -> Path:
    """샘플 2번 원본 영상을 480x270, 2초, 무음으로 줄인 미리보기용 원본."""
    src = TMP / "src.mp4"
    sr.ffmpeg("-i", str(sr.WORK / "orig2.mp4"), "-t", f"{PREVIEW_SEC}", "-vf", f"scale={PW}:{PH}", "-an",
              "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(src))
    return src


def render_preview(key: str, src: Path) -> tuple[bytes, bytes]:
    mp4, webm = TMP / f"{key}.mp4", TMP / f"{key}.webm"
    if mp4.exists() and webm.exists() and "--rebuild" not in sys.argv:      # 이미 만든 미리보기는 재사용
        return mp4.read_bytes(), webm.read_bytes()
    raw = TMP / f"{key}_raw.mp4"
    st.apply(src, str(sr.WORK / "slide1.png"), key, raw, tdur=1.0, easing=None, encoder="libx264")
    sr.ffmpeg("-i", str(raw), "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "26", "-pix_fmt", "yuv420p",
              "-movflags", "+faststart", str(mp4))
    sr.ffmpeg("-i", str(raw), "-an", "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "36", "-row-mt", "1", str(webm))
    return mp4.read_bytes(), webm.read_bytes()


def main() -> None:
    ensure_samples()
    TMP.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)
    src = make_preview_source()
    presets = []
    total = 0
    for key, (label, xf, _dur, ease) in sr.PRESETS.items():
        mp4, webm = render_preview(key, src)
        total += len(mp4) + len(webm)
        presets.append({
            "key": key, "label": label, "xfade": xf, "easing": ease,
            "easingLabel": sr.EASINGS[ease], "easingShort": EASING_SHORT[ease],
            "preview": "data:video/mp4;base64," + base64.b64encode(mp4).decode(),
            "previewWebm": "data:video/webm;base64," + base64.b64encode(webm).decode(),
        })
        print(f"  {key:<14} {label:<22} mp4 {len(mp4) // 1024:3d}KB  webm {len(webm) // 1024:3d}KB", flush=True)
    data = {"presets": presets, "easings": sr.EASINGS}
    template = (BASE / "handoff_template.html").read_text(encoding="utf-8")
    html = template.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    out = OUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"-> {out}  ({out.stat().st_size / 1024 / 1024:.2f} MB, 미리보기 {total / 1024 / 1024:.2f} MB)")


if __name__ == "__main__":
    main()
