# slide-transition-demo

이미 만들어 둔 **슬라이드 영상(음성 포함)에 "들어올 때 전환"을 붙이는** ffmpeg 도구.

- 슬라이드 k 영상의 처음 T초가 "k−1번 슬라이드 이미지 → k번 영상" 전환으로 바뀐다. 그 뒤는 원본 그대로.
- **원본과 동일**: 해상도, fps, 프레임 수, 길이가 같고 음성은 손대지 않고 복사한다. 만든 뒤 자동으로 검사한다.
- 첫 슬라이드는 앞 슬라이드가 없으므로 **검정(또는 흰색) 화면에서** 들어온다. 파워포인트와 같다.
- 전환은 파워포인트 이름의 프리셋으로 고르고(아래 표), 움직임 계열에는 천천히 시작해 천천히 끝나는 이징이 들어간다.
- 전환 길이는 1초 이내. 전환을 정하지 않은 슬라이드는 원본 파일을 그대로 쓴다.
- **LGPL ffmpeg 지원**: libx264 가 없으면 libopenh264 를 쓴다 (아래 참고).

```
원본 k   [======================= 영상 k =======================]  + 음성
출력 k   [앞 이미지 → 영상 k 전환 T초][====== 영상 k 나머지 ======]  + 음성 그대로    길이 동일
```

## 개발자 전달용 페이지 (handoff/index.html)

서버 없이 더블클릭으로 열리는 파일 하나. 22개 효과의 미리보기 영상이 안에 들어 있다.

1. "파일 선택"으로 슬라이드 영상을 고르면 MP4 컨테이너에서 해상도, fps, 길이를 읽어 보여준다 (브라우저 디코더 불필요).
2. 앞에 둘 그림을 고른다: 앞 슬라이드 이미지(파일 선택) 또는 첫 슬라이드용 검정/흰색 화면.
3. 효과 카드를 고르면 ffmpeg 명령어 한 줄이 나오고 "명령어 복사"로 복사한다. 전환 1초 고정, 움직임은 효과별 자동.

브라우저는 파일의 전체 경로를 알려주지 않으므로 명령어에는 파일 이름만 들어간다. 파일이 있는 폴더에서 실행하거나
"파일이 있는 폴더" 칸에 경로를 넣으면 앞에 붙는다. 인코더는 libopenh264 기본, libx264 선택 가능. 접힌 영역에 코드용 JSON 인자 배열이 있다.

- 다시 만들기: `python3 build_handoff.py` (미리보기 캐시 재사용, `--rebuild` 로 전부 다시)
- 페이지가 만드는 인자 목록은 `slide_transition.build_apply_command` 와 동일하다 (테스트로 확인).

## 준비

- ffmpeg, ffprobe (xfade 필터가 있는 4.3 이상), Python 3.8 이상. 추가 패키지 없음.
- H.264 인코더 하나. 확인: `ffmpeg -encoders | grep 264`

### LGPL ffmpeg 과 인코더

LGPL 빌드에는 libx264 가 없다. 도구는 `libopenh264 → libx264 → h264_videotoolbox → h264_nvenc → h264_qsv → h264_amf → h264_mf`
순서로 있는 것을 자동으로 고르고, `--encoder` 로 지정할 수도 있다. libopenh264 는 CRF 가 없어서 비트레이트로 품질을 정한다.
정지 슬라이드 원본은 비트레이트가 아주 낮으므로 그대로 쓰면 전환 구간이 뭉개진다. 그래서 원본의 4배와 1080p 기준 6Mbps 중
큰 값(상한 16Mbps)을 쓰고, `--bitrate` 로 바꿀 수 있다. 옵션(`rc_mode`, `profile`)은 실행 시점에 `ffmpeg -h encoder=libopenh264`
결과를 보고 지원되는 것만 넣는다. 전환은 xfade, tpad, concat, setpts, fps, scale 로만 만들므로 모두 LGPL 범위다.

## 1. 웹으로 테스트

```bash
python3 web.py              # 브라우저에서 http://127.0.0.1:8000 열기 (포트 변경: --port 9000)
```

1. 슬라이드 영상들과 슬라이드 이미지들을 올리고 **프로젝트 만들기**. 업로드 없이 보려면 **샘플 4장으로 시작**.
   이미지는 영상과 같은 순서로, k번 이미지가 k+1번 영상의 전환에 쓰인다 (마지막 이미지는 없어도 된다).
2. 왼쪽 목록에서 슬라이드를 클릭 → 들어올 때 **전환 종류**, **전환 길이**, **움직임**을 정한다. 1번은 **시작 화면**(검정/흰색)도 고른다.
3. **전환본 만들기**: 그 슬라이드의 전환본 영상을 만들고 재생한다. 원본과의 동일성 검사(해상도, fps, 프레임 수, 길이, 음성 md5)가 같이 표시된다.
4. **전환이 설정된 슬라이드 모두 만들기** 후 **이어 붙여 보기**: 전환본(없으면 원본)을 순서대로 이어 붙여 확인한다. 확인용이라 재인코딩한다.

## 2. 명령줄로 테스트

```bash
python3 slide_transition.py sample                                                   # 샘플 4장 전 과정 -> out/
python3 slide_transition.py apply --video 2.mp4 --prev 1.png --transition push_right --out 2_t.mp4
python3 slide_transition.py apply --video 1.mp4 --prev black --transition fade --out 1_t.mp4      # 첫 슬라이드
python3 slide_transition.py apply --video 2.mp4 --prev 1.png --transition wipe_right --tdur 0.6 --easing none --out 2_t.mp4
python3 slide_transition.py verify --original 2.mp4 --output 2_t.mp4                                # 동일성 검사만
python3 slide_transition.py concat 1_t.mp4 2_t.mp4 3.mp4 --out all.mp4                           # 확인용 (재인코딩)
python3 slide_transition.py list                                                                   # 프리셋, 인코더
```

`apply` 는 끝나면 원본과의 동일성 검사를 출력하고, 다르면 종료 코드 1을 돌려준다.

## 3. ffmpeg 명령만 직접 써 보기

2번 영상(1920x1080, 30fps)에 1번 이미지에서 fade 0.7초로 들어올 때. 앞 이미지를 전환 길이만큼만 넣고 `xfade` 를 `offset=0` 으로
걸면 출력 길이는 0.7 + D − 0.7 = D 로 원본과 같다. 음성은 복사.

```bash
ffmpeg -loop 1 -framerate 30 -t 0.7 -i 1.png -i 2.mp4 -filter_complex "
[0:v]scale=1920:1080,format=yuv420p,fps=30[a];
[1:v]fps=30,format=yuv420p[b];
[a][b]xfade=transition=fade:duration=0.7:offset=0[v]" -map "[v]" -map 1:a -c:a copy -c:v libopenh264 -b:v 6M -pix_fmt yuv420p 2_t.mp4
```

첫 슬라이드는 이미지 대신 `-f lavfi -i "color=c=black:s=1920x1080:r=30:d=0.7"` 를 넣는다.

## 전환 프리셋

| 키 | 이름 | xfade | 기본 길이 | 움직임 |
|---|---|---|---|---|
| fade / fade_black | 페이드 / 페이드 (검정 거쳐서) | fade / fadeblack | 0.7s | 일정 |
| push_right / push_left / push_bottom / push_top | 밀어내기 (새 슬라이드가 들어오는 방향) | slideleft / slideright / slideup / slidedown | 1.0s | 부드럽게 |
| wipe_right / wipe_left / wipe_bottom / wipe_top | 닦아내기 | wipeleft / wiperight / wipeup / wipedown | 1.0s | 살짝 부드럽게 |
| cover_right / cover_left | 덮기 | coverleft / coverright | 1.0s | 부드럽게 |
| uncover_left / uncover_right | 벗기기 (앞 슬라이드가 나가는 방향) | revealleft / revealright | 1.0s | 부드럽게 |
| split_h / split_v | 나누기 (바깥쪽으로) | horzopen / vertopen | 1.0s | 부드럽게 |
| shape_circle | 도형 (원, 바깥쪽으로) | circleopen | 1.0s | 부드럽게 |
| clock | 시계 | radial | 1.0s | 부드럽게 |
| zoom | 확대 | zoomin | 1.0s | 부드럽게 |
| blinds | 블라인드 | hlslice | 1.0s | 부드럽게 |
| dissolve | 디졸브 | dissolve | 1.0s | 일정 |
| flash | 밝기 변화 (흰색 거쳐서) | fadewhite | 0.7s | 일정 |

움직임(easing)은 `inout`(부드럽게), `soft`(살짝 부드럽게), `out`(감속), `none`(일정한 속도) 중 고른다.
닦아내기에 xfade 의 smooth 계열을 쓰지 않는 이유: 그라데이션 띠가 넓어서 앞선이 0.6초 만에 화면을 가로질러 빠르게 느껴진다.

### 이징은 어떻게 넣나

xfade 는 진행 속도가 일정하다. 그래서 전환 구간만 `fps × 4` 로 선형 렌더한 뒤 `setpts` 로 각 프레임의 시각을 속도 곡선의
역함수 위치로 옮기고(`inout` 은 smoothstep 의 역함수 `0.5 − sin(asin(1 − 2p) / 3)`), `fps` 로 다시 샘플한다.
그 뒤 원본 영상의 나머지와 concat 하므로 프레임 수는 원본과 같다.

## 화질에 대해

- 출력 fps 는 원본을 따른다. 원본이 30fps 면 1초 밀어내기는 프레임당 64~98px 씩 움직여서 글자가 번져 보일 수 있다. 파이프라인에서
  원본을 60fps 로 만들면 전환도 60fps 가 된다.
- 전환 구간은 다시 인코딩된다 (xfade 가 프레임을 섞으려면 디코딩해야 한다). 음성은 복사라 손실이 없다.
- 페이드는 두 그림이 겹치는 효과라 전환 중에는 겹쳐 보인다. 파워포인트도 같다.
- 이미지가 영상보다 작으면 확대되어 흐려진다. 슬라이드 이미지는 영상 해상도 이상으로 내보낸다.

## 이전 도구

- `slide_render.py`: 이미지와 길이만으로 슬라이드 영상을 만들던 도구 (음성 없음). 프리셋과 이징 코드를 여기서 가져다 쓴다.
- `legacy/transition_demo.py`: 슬라이드 영상들을 하나로 합치며 경계에 전환을 넣던 첫 방식.
