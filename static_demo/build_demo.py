"""정적 데모 생성: 기존 app/·seed/를 읽어 static_demo/에 복사본을 만듭니다. (기존 파일은 수정하지 않음)

사용: python static_demo/build_demo.py      (24_물류PoC 폴더에서)
- py/engine.py, scenarios.py, server.py  ← app/ 그대로 복사 (엔진 로직 동일)
- seed/                                  ← seed/ 그대로 복사
- index.html, app.js, style.css          ← app/static/ 복사 후, API 호출만 브라우저 안의 엔진(worker.js)으로 교체
직접 작성한 파일(worker.js, py/bridge.py, README.md)은 건드리지 않습니다.
app/을 고친 뒤에는 이 스크립트를 다시 실행하면 데모에 반영됩니다.
"""
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'app')
SEED_FILES = ['clients.csv', 'products.csv', 'channel_product_map.csv', 'locations.csv', 'inventory.csv',
              'inbound_schedule.csv', 'orders_import.csv', 'events.csv', 'expected_labels.csv', 'policies.json']

API_OLD = """async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
  if (opts.text !== undefined) { init.headers['Content-Type'] = 'text/csv'; init.body = opts.text; }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || res.statusText);
    err.status = res.status;
    throw err;
  }
  return data;
}"""

API_NEW = """/* 정적 데모: 서버 대신 브라우저 안의 Python 엔진(worker.js · Pyodide)에 같은 API 요청을 보냅니다. */
const ENGINE = new Worker('worker.js');
const pending = new Map();
let seq = 0;
ENGINE.onmessage = (e) => {
  const m = e.data;
  if (m.type === 'status') return setBoot(m.text);
  if (m.type === 'ready') return setBoot(null);
  if (m.type === 'fatal') return setBoot('엔진을 불러오지 못했습니다. 인터넷 연결을 확인하고 새로고침하세요. (' + m.text + ')', true);
  const resolve = pending.get(m.id);
  if (resolve) { pending.delete(m.id); resolve(m); }
};

function setBoot(text, isErr = false) {
  const el = document.getElementById('boot');
  if (!el) return;
  el.hidden = !text;
  el.classList.toggle('err', isErr);
  if (text) el.querySelector('.boot-text').textContent = text;
}

async function api(path, opts = {}) {
  const method = opts.method || 'GET';
  let body = '';
  let ctype = '';
  if (opts.json !== undefined) { ctype = 'application/json'; body = JSON.stringify(opts.json); }
  if (opts.text !== undefined) { ctype = 'text/csv'; body = opts.text; }
  const res = await new Promise((resolve) => {
    const id = ++seq;
    pending.set(id, resolve);
    ENGINE.postMessage({ id, method, path, body, ctype });
  });
  const data = JSON.parse(res.text || '{}');
  if (res.status >= 400) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}"""

HTML_PAIRS = [
    ('<div class="brand-sub">주문·재고·출고 · 가상 데이터</div>',
     '<div class="brand-sub">주문·재고·출고 · 가상 데이터 · <b>정적 데모</b> (브라우저에서 실행, 새로고침하면 초기화)</div>'),
    ('<script src="app.js"></script>',
     '<div class="boot" id="boot"><div class="boot-card"><div class="spinner"></div>'
     '<div><b>PoC 엔진 준비 중</b><div class="boot-text">Python 엔진 내려받는 중…</div>'
     '<div class="boot-note">서버 없이 브라우저 안에서 Python 엔진과 SQLite를 실행합니다. 첫 실행은 10초 정도 걸릴 수 있습니다.</div>'
     '</div></div></div>\n<script src="app.js"></script>'),
]

BOOT_CSS = """
/* 정적 데모: 엔진 준비 화면 */
.boot { position: fixed; inset: 0; z-index: 30; display: grid; place-items: center; background: rgba(245, 246, 248, .92); }
.boot[hidden] { display: none; }
.boot-card { display: flex; gap: 16px; align-items: flex-start; max-width: 520px; margin: 16px; padding: 20px 22px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: var(--shadow); }
.boot-text { margin-top: 4px; }
.boot-note { margin-top: 8px; color: var(--muted); font-size: 12.5px; }
.boot.err .boot-text { color: var(--bad); }
.boot.err .spinner { display: none; }
.spinner { width: 28px; height: 28px; flex: none; border-radius: 50%; border: 3px solid var(--accent-soft);
  border-top-color: var(--accent); animation: spin 0.9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) .boot { background: rgba(18, 21, 26, .92); } }
"""


def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)


def main():
    for name in ('engine.py', 'scenarios.py', 'server.py'):
        copy(os.path.join(APP, name), os.path.join(HERE, 'py', name))
    for name in SEED_FILES:
        copy(os.path.join(ROOT, 'seed', name), os.path.join(HERE, 'seed', name))

    js = open(os.path.join(APP, 'static', 'app.js'), encoding='utf-8').read()
    assert js.count(API_OLD) == 1, 'app.js의 api() 함수가 바뀌었습니다. build_demo.py의 API_OLD를 맞춰 주세요.'
    js = js.replace(API_OLD, API_NEW).replace("toast('서버 연결 실패: ' + e.message, true)",
                                              "toast('엔진 오류: ' + e.message, true)")
    open(os.path.join(HERE, 'app.js'), 'w', encoding='utf-8', newline='').write(js)

    html = open(os.path.join(APP, 'static', 'index.html'), encoding='utf-8').read()
    for a, b in HTML_PAIRS:
        assert html.count(a) == 1, a
        html = html.replace(a, b)
    open(os.path.join(HERE, 'index.html'), 'w', encoding='utf-8', newline='').write(html)

    css = open(os.path.join(APP, 'static', 'style.css'), encoding='utf-8').read()
    open(os.path.join(HERE, 'style.css'), 'w', encoding='utf-8', newline='').write(css + BOOT_CSS)

    open(os.path.join(HERE, '.nojekyll'), 'w').close()  # GitHub Pages가 파일을 가공하지 않도록
    print('정적 데모 생성 완료:', HERE)


if __name__ == '__main__':
    main()
