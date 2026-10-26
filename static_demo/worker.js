/* 정적 데모 엔진 워커: Pyodide(브라우저용 Python)로 PoC 엔진을 실행합니다.
   화면(app.js)은 서버 버전과 같은 API 경로로 요청하고, 이 워커가 py/bridge.py로 처리해 돌려줍니다. */
const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.26.4/full/';
importScripts(PYODIDE + 'pyodide.js');

const CODE = ['engine.py', 'scenarios.py', 'server.py', 'bridge.py'];
const SEED = ['clients.csv', 'products.csv', 'channel_product_map.csv', 'locations.csv', 'inventory.csv',
  'inbound_schedule.csv', 'orders_import.csv', 'events.csv', 'expected_labels.csv', 'policies.json'];
const BASE = '/home/pyodide/poc';

async function bytes(url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`${url} ${res.status}`);
  return new Uint8Array(await res.arrayBuffer());
}

let handle = null;
const ready = (async () => {
  self.postMessage({ type: 'status', text: 'Python 엔진 내려받는 중…' });
  const py = await loadPyodide({ indexURL: PYODIDE });
  await py.loadPackage('sqlite3');
  self.postMessage({ type: 'status', text: 'PoC 코드와 시드 데이터 불러오는 중…' });
  py.FS.mkdirTree(BASE + '/app');
  py.FS.mkdirTree(BASE + '/seed');
  for (const f of CODE) py.FS.writeFile(`${BASE}/app/${f}`, await bytes(`py/${f}`));
  for (const f of SEED) py.FS.writeFile(`${BASE}/seed/${f}`, await bytes(`seed/${f}`));
  self.postMessage({ type: 'status', text: '시드 데이터로 DB 만드는 중…' });
  py.runPython(`import sys; sys.path.insert(0, '${BASE}/app')`);
  handle = py.pyimport('bridge').handle;
  self.postMessage({ type: 'ready' });
})().catch((err) => {
  self.postMessage({ type: 'fatal', text: String(err && err.message || err) });
  throw err;
});

self.onmessage = async (e) => {
  const { id, method, path, body, ctype } = e.data;
  try {
    await ready;
    const out = handle(method, path, body || '', ctype || '');
    const [status, text] = out.toJs();
    out.destroy();
    self.postMessage({ id, status, text });
  } catch (err) {
    self.postMessage({ id, status: 500, text: JSON.stringify({ error: String(err && err.message || err) }) });
  }
};
