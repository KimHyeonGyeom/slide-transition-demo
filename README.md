# slide-transition-demo

슬라이드별 영상(이미지 + TTS 나레이션) 사이에 전환효과를 넣되, **전체 길이는 그대로 유지**하는 ffmpeg 데모.
전환은 "슬라이드로 들어올 때"의 속성이고, 위치는 **앞**으로 고정이다. 즉 슬라이드 k 의 전환은
k−1 슬라이드의 마지막 T초 동안 일어나서 k 슬라이드의 시작 시각에 끝난다. 오디오는 손대지 않으므로
나레이션이 잘리지 않고, 전환 길이는 1초 이내로 제한한다.

## 준비

- ffmpeg, ffprobe (xfade 필터가 있는 4.3 이상), Python 3.8 이상. 추가 패키지 없음.
- 확인: `ffmpeg -version`, 전환 목록: `ffmpeg -h filter=xfade`

## 1. 웹으로 테스트

```bash
python3 web.py              # 브라우저에서 http://127.0.0.1:8000 열기 (포트 변경: --port 9000)
```

1. 슬라이드 영상 2개 이상 선택(파일 이름 순서로 이어짐) 후 **프로젝트 만들기**. 업로드 없이 보려면 **샘플 4장으로 시작**.
2. 왼쪽 목록에서 슬라이드를 클릭 → 오른쪽에서 그 슬라이드로 들어올 때의 **전환 종류**와 **길이**를 정한다.
   정하지 않은 슬라이드는 컷으로 넘어간다. 첫 슬라이드는 들어오는 전환이 없다.
3. **이 구간만 미리보기**: 경계 앞뒤 몇 초만 잘라 1~2초 안에 결과를 보여준다. 종류를 바꿔 가며 바로 확인.
4. **전체 렌더링**: 영상 재생, 입력 합계와 출력 길이 비교, 실행한 ffmpeg 명령, 다운로드.
5. 영상에 자막이 구워져 있으면 슬라이드 이미지도 같이 올린다. 그러면 프레임 복제 대신 이미지 2장으로 전환 구간을 만든다.

업로드, 썸네일, 미리보기, 결과는 `jobs/<프로젝트 id>/` 아래에 남는다.

## 2. 명령줄로 테스트

```bash
python3 transition_demo.py                                   # 샘플 생성 후 out/demo_*.mp4
python3 transition_demo.py --clips A.mp4 B.mp4               # 내 영상 2개 -> out/result_fade_before.mp4
python3 transition_demo.py --clips A.mp4 B.mp4 --transition slideleft --duration 0.6
python3 transition_demo.py --clips A.mp4 B.mp4 C.mp4 --transition fade,none   # 2번 fade, 3번 컷
python3 transition_demo.py --clips A.mp4 B.mp4 --images a.png b.png
python3 transition_demo.py --clips A.mp4 B.mp4 --align center    # before(기본) | center | after
python3 transition_demo.py --clips A.mp4 B.mp4 --showcase        # 자주 쓰는 전환 8가지를 한 영상에
```

실행하면 각 단계의 ffmpeg 명령이 그대로 출력되고, 마지막에 입력 합계와 출력 길이가 같은지 확인해 준다.

## 3. ffmpeg 명령만 직접 써 보기

A.mp4 가 3.0초, B 로 들어올 때 fade 0.5초(앞 고정)일 때. `offset` 은 A 길이 − 전환 길이.

```bash
ffmpeg -i A.mp4 -i B.mp4 -filter_complex "
[1:v]tpad=start_mode=clone:start_duration=0.5[vb];
[0:v][vb]xfade=transition=fade:duration=0.5:offset=2.5[v];
[0:a][1:a]concat=n=2:v=0:a=1[a]" -map "[v]" -map "[a]" out.mp4
```

## 원리

```
영상 A  [==========A==========]
영상 B              [B첫 복제 T초][==========B==========]
                    └── 전환 T초 ──┘   B 시작 시각에 전환이 끝난다. 오디오는 원래 그대로
```

- 슬라이드는 정지 화면이므로 뒤 영상의 첫 프레임을 T초 복제해 앞에 붙인 뒤, 그 T초를 xfade 로 겹친다.
  늘린 만큼 겹쳐서 상쇄되므로 전체 길이는 입력 길이의 합과 같다.
- 컷(none) 경계는 concat 으로 그냥 이어 붙인다.
- `--images`: 영상에 자막이 구워져 있어 첫/끝 프레임이 슬라이드 이미지와 다르면, 프레임 복제 대신 이미지 2장으로 전환 구간을 만들어 끼운다.
- 규격(해상도, fps, 픽셀 포맷, 오디오)은 첫 클립 기준으로 맞춘다. xfade 와 concat 은 규격이 다르면 받지 않는다.
- 전환 길이 상한은 `MAX_TRANSITION` (1.0초).

## 확인할 것

- "입력 합계 / 출력" 길이가 같은지 (화면과 콘솔에 표시)
- 전환 구간에서도 나레이션이 원래 그대로 들리는지
