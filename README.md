# slide-transition-demo

슬라이드별 영상을 만들 때 **들어올 때 전환**을 넣는 ffmpeg 데모.

- 슬라이드 k 영상 = (k−1번 이미지 → k번 이미지 전환 T초) + k번 이미지. **영상 길이는 슬라이드 길이 그대로**, 전환이 처음 T초를 차지한다.
- 슬라이드마다 독립 렌더링. k번 영상은 k번 이미지(전환이 있으면 k−1번 이미지도)만으로 만들고, 1번은 1번 이미지만으로 만든다.
- 전환을 정하지 않은 슬라이드는 이미지만으로 만든다. 전환 길이는 1초 이내.
- 전환은 파워포인트 이름의 프리셋(페이드, 밀어내기, 닦아내기, 덮기, 벗기기, 나누기, 도형, 시계, 확대 ...)으로 고르고,
  움직임 계열에는 천천히 시작해 천천히 끝나는 이징이 들어간다. xfade 원본 이름도 그대로 쓸 수 있다.
- 규격이 같으므로 만든 영상들은 재인코딩 없이 이어 붙일 수 있다.
- TTS 오디오는 아직 다루지 않는다 (영상만 출력).

```
슬라이드 1 영상 = [ 이미지1 ................................ ]            길이 D1
슬라이드 2 영상 = [이미지1→이미지2 전환 T][ 이미지2 .......... ]            길이 D2
슬라이드 3 영상 = [ 이미지3 ................................ ]            길이 D3  (전환 없음)
```

## 준비

- ffmpeg, ffprobe (xfade 필터가 있는 4.3 이상), Python 3.8 이상. 추가 패키지 없음.
- 확인: `ffmpeg -version`, 전환 목록: `ffmpeg -h filter=xfade`

## 1. 웹으로 테스트

```bash
python3 web.py              # 브라우저에서 http://127.0.0.1:8000 열기 (포트 변경: --port 9000)
```

1. 슬라이드 이미지 2장 이상 선택(파일 이름 순서) 후 **프로젝트 만들기**. 업로드 없이 보려면 **샘플 4장으로 시작**.
2. 왼쪽 목록에서 슬라이드를 클릭 → 길이(초)와, 2번부터는 들어올 때 **전환 종류**, **전환 길이**, **움직임**을 정한다.
   종류를 고르면 그 전환의 파워포인트 기본 길이와 움직임이 자동으로 채워진다.
3. **이 슬라이드 영상 만들기**: 그 슬라이드 영상 하나만 만들고 바로 재생. 길이 검증과 실행한 ffmpeg 명령도 표시.
4. **모든 슬라이드 만들기** 후 **만든 영상 이어 붙여 보기**: 재인코딩 없이 이어 붙인 전체를 확인.

업로드, 썸네일, 슬라이드 영상, 이어 붙인 영상은 `jobs/<프로젝트 id>/` 아래에 남는다.

## 2. 명령줄로 테스트

```bash
python3 slide_render.py sample                                               # 샘플 4장 -> out/slide1..4.mp4, out/all.mp4
python3 slide_render.py render --image 1.png --duration 3.0 --out slide1.mp4  # 전환 없음
python3 slide_render.py render --image 2.png --duration 3.5 --prev 1.png --transition push_right --out slide2.mp4
python3 slide_render.py render --image 2.png --duration 3.5 --prev 1.png --transition wipeleft --tdur 0.6 --easing none --out slide2.mp4
python3 slide_render.py concat slide1.mp4 slide2.mp4 --out all.mp4           # 재인코딩 없이 이어 붙이기
python3 slide_render.py list                                                 # 프리셋과 xfade 원본 목록
```

실행하면 ffmpeg 명령이 그대로 출력되고, 출력 길이가 슬라이드 길이와 같은지 확인해 준다.

## 전환 프리셋

| 키 | 이름 | xfade | 기본 길이 | 움직임 |
|---|---|---|---|---|
| fade / fade_black | 페이드 / 페이드 (검정 거쳐서) | fade / fadeblack | 0.7s | 일정 |
| push_right / push_left / push_bottom / push_top | 밀어내기 (새 슬라이드가 들어오는 방향) | slideleft / slideright / slideup / slidedown | 1.0s | 부드럽게 |
| wipe_right / wipe_left / wipe_bottom / wipe_top | 닦아내기 (경계가 부드러운 와이프) | smoothleft / smoothright / smoothup / smoothdown | 1.0s | 부드럽게 |
| cover_right / cover_left | 덮기 | coverleft / coverright | 1.0s | 부드럽게 |
| uncover_left / uncover_right | 벗기기 (앞 슬라이드가 나가는 방향) | revealleft / revealright | 1.0s | 부드럽게 |
| split_h / split_v | 나누기 (바깥쪽으로) | horzopen / vertopen | 1.0s | 부드럽게 |
| shape_circle | 도형 (원, 바깥쪽으로) | circleopen | 1.0s | 부드럽게 |
| clock | 시계 | radial | 1.0s | 부드럽게 |
| zoom | 확대 | zoomin | 1.0s | 부드럽게 |
| blinds | 블라인드 | hlslice | 1.0s | 부드럽게 |
| dissolve | 디졸브 | dissolve | 1.0s | 일정 |
| flash | 밝기 변화 (흰색 거쳐서) | fadewhite | 0.7s | 일정 |

움직임(easing)은 `inout`(부드럽게: 천천히 시작해 천천히 끝), `out`(감속), `none`(일정한 속도) 중 고른다.

### 이징은 어떻게 넣나

ffmpeg 의 xfade 는 진행 속도가 일정하다. 그래서 전환 구간만 `FPS × 4` 프레임레이트로 선형 렌더한 뒤,
`setpts` 로 각 프레임의 시각을 원하는 속도 곡선의 역함수 위치로 옮기고(`inout` 은 smoothstep 의 역함수
`0.5 − sin(asin(1 − 2p) / 3)`), `fps` 필터로 일정한 간격으로 다시 샘플한다. 그 뒤 나머지 정지 구간과
concat 하므로 영상 길이는 그대로다. 필터 몇 개로만 되고 ffmpeg 패치나 외부 라이브러리는 필요 없다.

## 3. ffmpeg 명령만 직접 써 보기

2번 슬라이드 3.5초, 1번 이미지에서 fade 0.5초로 들어올 때. 앞 이미지는 전환 길이만큼만, 이 슬라이드 이미지는 슬라이드 길이만큼 넣고
`xfade` 를 `offset=0` 으로 걸면 출력 길이는 0.5 + 3.5 − 0.5 = 3.5초가 된다.

```bash
ffmpeg -loop 1 -framerate 30 -t 0.5 -i 1.png -loop 1 -framerate 30 -t 3.5 -i 2.png -filter_complex "
[0:v]scale=1920:1080,format=yuv420p[a];
[1:v]scale=1920:1080,format=yuv420p[b];
[a][b]xfade=transition=fade:duration=0.5:offset=0[v]" -map "[v]" -t 3.5 -c:v libx264 -pix_fmt yuv420p slide2.mp4
```

전환이 없으면 이미지 하나로 `ffmpeg -loop 1 -framerate 30 -t 3.0 -i 1.png -vf "scale=1920:1080,format=yuv420p" -c:v libx264 -pix_fmt yuv420p slide1.mp4`.

이어 붙이기는 목록 파일을 만들어 `ffmpeg -f concat -safe 0 -i list.txt -c copy all.mp4`.

## 알아둘 것

- 모든 슬라이드 영상은 같은 규격(1920x1080, 30fps, yuv420p)으로 만든다. `slide_render.py` 의 `WIDTH, HEIGHT, FPS` 로 바꿀 수 있다. 비율이 다른 이미지는 검정 여백으로 채운다.
- 전환 길이는 슬라이드 길이보다 짧아야 한다. 상한은 `MAX_TRANSITION` (1.0초).
- `legacy/transition_demo.py` 는 이전 방식(이미 만든 슬라이드 영상들을 하나로 합치며 경계에 전환 넣기)이다. 지금 흐름에는 쓰지 않는다.
