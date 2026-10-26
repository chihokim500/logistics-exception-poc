# 정적 데모 (서버 없이 브라우저에서 실행)

서버 버전(`../app`)과 **같은 엔진 코드**를 브라우저 안에서 실행하는 데모입니다. [Pyodide](https://pyodide.org)(브라우저용 Python)로 `engine.py`와 SQLite를 돌리므로, GitHub Pages 같은 정적 호스팅에 올려 **링크만으로** 모든 기능(재생, 예외 처리, 검증, 처리 시나리오)을 쓸 수 있습니다.

- 데이터는 보는 사람의 브라우저 메모리에만 있습니다. 서로 영향을 주지 않고, **새로고침하면 처음 상태**로 돌아갑니다.
- 처음 열 때 Pyodide(약 10MB)를 CDN에서 받으므로 인터넷 연결이 필요하고, 10초 정도 걸릴 수 있습니다. 두 번째부터는 브라우저 캐시를 씁니다.
- 기존 서버 버전(`python app/server.py`)은 그대로 쓸 수 있습니다. 이 폴더는 `app/`과 `seed/`를 **복사해서** 만든 것입니다.

## 구성

| 파일 | 내용 |
|---|---|
| `index.html`, `app.js`, `style.css` | `app/static/` 복사본. `app.js`의 API 호출 함수만 브라우저 엔진으로 바꿈 (자동 생성) |
| `worker.js` | Pyodide를 불러오고 코드·시드를 넣은 뒤, 화면의 API 요청을 처리하는 Web Worker |
| `py/engine.py`, `py/scenarios.py`, `py/server.py` | `app/`의 파일 그대로 (자동 생성) |
| `py/bridge.py` | `server.py`의 API 라우트를 HTTP 없이 호출하는 연결부. DB는 메모리(`:memory:`) |
| `seed/` | `seed/`의 시드 데이터 그대로 (자동 생성) |
| `build_demo.py` | 위 "자동 생성" 파일을 `app/`·`seed/`에서 다시 만드는 스크립트 |

`app/`을 고친 뒤에는 다음 명령으로 데모에 반영합니다.

```bash
python static_demo/build_demo.py
```

## 내 PC에서 확인

`index.html`을 더블클릭하면 브라우저 보안 정책 때문에 파일을 읽지 못합니다. 간단한 웹 서버로 엽니다.

```bash
python -m http.server 8080 --directory static_demo
```

브라우저에서 http://localhost:8080 을 엽니다. 이 서버는 파일만 전달하고, 처리는 모두 브라우저 안에서 합니다.

## GitHub Pages로 공개

1. GitHub에 저장소를 만들고 `24_물류PoC` 폴더 내용을 올립니다. `data/`(실행 중 생기는 DB)와 `__pycache__/`는 올리지 않습니다.
2. 저장소 **Settings → Pages**에서 Source를 **GitHub Actions**로 고르고, 아래 워크플로를 `.github/workflows/pages.yml`로 추가합니다. `static_demo` 폴더만 사이트로 배포합니다.

```yaml
name: Deploy static demo
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
  pages: write
  id-token: write
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/upload-pages-artifact@v3
        with:
          path: static_demo
      - id: deployment
        uses: actions/deploy-pages@v4
```

3. 배포가 끝나면 `https://<GitHub 아이디>.github.io/<저장소 이름>/` 이 데모 주소입니다. 포트폴리오와 README에 이 링크를 넣습니다.

## 확인한 것 (2026-10-24, 로컬 브라우저)

- 엔진 준비: 약 10초 (첫 실행)
- 11:30까지 재생(이벤트 238건) 0.4초, 끝까지 재생 후 검증 15항목 **PASS**
- 처리 시나리오 12/12 통과(검사 54개), 약 3.5초
- E01 입고 대기 처리, 출고 후 취소 409 차단 등 예외 처리가 서버 버전과 같은 결과
