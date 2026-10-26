'use strict';

const STATUS = {
  RECEIVED: ['수집', ''], CONFIRMED: ['확정', 'b-info'], ALLOCATED: ['할당', 'b-accent'], PICKING: ['피킹중', 'b-accent'],
  PACKED: ['포장완료', 'b-accent'], SHIPPED: ['출고완료', 'b-ok'], ON_HOLD: ['보류', 'b-bad'], CANCELLED: ['취소', ''],
};
const FLOW = ['RECEIVED', 'CONFIRMED', 'ALLOCATED', 'PICKING', 'PACKED', 'SHIPPED'];
const FLOW_COLOR = {
  RECEIVED: '#a3acb9', CONFIRMED: '#8b7fe0', ALLOCATED: '#6f8ff0', PICKING: '#3f6fe0', PACKED: '#2856d8',
  SHIPPED: '#2f9e66', ON_HOLD: '#d9534f', CANCELLED: '#c3c8d0',
};
const EXC_STATUS = { OPEN: ['미처리', 'b-bad'], IN_PROGRESS: ['처리중', 'b-warn'], RESOLVED: ['완료', 'b-ok'], LOGGED: ['기록', ''] };
const SEVERITY = { CRITICAL: ['긴급', 'b-bad'], HIGH: ['높음', 'b-warn'], MEDIUM: ['중간', 'b-info'], LOW: ['낮음', ''] };

const S = { tab: 'dashboard', summary: null, excRule: '', excStatus: 'open', orderFilter: { status: '', client: '', q: '' },
  invFilter: { client: '', q: '' }, openInv: new Set(), drawer: null };

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const badge = (map, key) => { const [t, c] = map[key] || [key, '']; return `<span class="badge ${c}">${esc(t)}</span>`; };
const lotBadge = (r) => (r.expired ? '<span class="badge b-bad" title="유통기한 경과·당일: 어떤 승인으로도 출고 불가">만료</span>'
  : r.usable ? '<span class="badge b-ok">가능</span>' : '<span class="badge b-warn" title="화주 최소 잔여일 미달: E06 승인 시에만 출고">임박</span>');
const hm = (ts) => (ts ? esc(ts.slice(11, 16)) : '-');
const md = (ts) => (ts ? esc(ts.slice(5, 16)) : '-');

async function api(path, opts = {}) {
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
}

function toast(msg, isErr = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isErr ? ' err' : '');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), isErr ? 6000 : 3000);
}

async function act(fn, okMsg) {
  try {
    const r = await fn();
    await refresh();
    if (okMsg) toast(typeof okMsg === 'function' ? okMsg(r) : okMsg);
    return r;
  } catch (e) {
    toast((e.status === 409 ? '차단됨 (E07 기록) · ' : '') + e.message, true);
    await refresh();
    return null;
  }
}

/* ---------------------------------------------------------------- 상단 */
function renderTop() {
  const s = S.summary;
  $('#clock').textContent = s.clock;
  $('#progress').textContent = `이벤트 ${s.last_seq}/${s.total_events}` +
    (s.next_event ? ` · 다음 ${s.next_event.at.slice(11, 16)}` : ' · 재생 완료') +
    (s.manual_ops ? ` · 수동처리 ${s.manual_ops}` : '');
}

$('#btn-replay').addEventListener('click', () => {
  const until = $('#checkpoint').value;
  if (until && until <= S.summary.clock && S.summary.last_seq > 0) {
    toast('이미 지난 시점입니다. 초기화한 뒤 재생하세요.', true);
    return;
  }
  act(() => api('/api/sim/replay', { method: 'POST', json: { until } }),
    (r) => `이벤트 ${r.executed}건 실행 → ${r.clock}` + (r.conflicts.length ? ` · 충돌 ${r.conflicts.length}건` : ''));
});
$('#btn-detect').addEventListener('click', () => act(() => api('/api/sim/run-detection', { method: 'POST', json: {} }),
  (r) => `마감위험 감지: 신규 ${r.new_exceptions.length}건`));
$('#btn-inbound').addEventListener('click', () => act(() => api('/api/sim/receive-inbound', { method: 'POST', json: {} }),
  (r) => `입고 ${r.received.length}건 반영 · 재할당 ${r.released.length}건` + (r.still_waiting.length ? ` · 계속 대기 ${r.still_waiting.length}건` : '') +
    (r.received.length ? '' : ` (기준일 ${S.summary.clock.slice(5, 10)} 이전 입고예정 없음)`)));
$('#btn-reset').addEventListener('click', () => {
  if (!confirm('DB를 시드 상태로 초기화할까요? 처리 이력이 모두 지워집니다.')) return;
  closeDrawer();
  act(() => api('/api/sim/reset', { method: 'POST', json: {} }), '초기화 완료 · 재생 버튼으로 시나리오를 시작하세요');
});

/* ---------------------------------------------------------------- 탭 */
document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => { location.hash = b.dataset.tab; }));
window.addEventListener('hashchange', () => { S.tab = location.hash.slice(1) || 'dashboard'; closeDrawer(); refresh(); });

async function refresh() {
  S.summary = await api('/api/summary');
  renderTop();
  document.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', b.dataset.tab === S.tab));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + S.tab));
  const render = { dashboard: renderDashboard, orders: renderOrders, inventory: renderInventory, verify: renderVerify }[S.tab];
  if (render) await render();
  if (S.drawer) await S.drawer();
}

/* ---------------------------------------------------------------- 대시보드 */
async function renderDashboard() {
  const s = S.summary;
  const st = s.status;
  const c = (k) => st[k] || 0;
  const openTotal = Object.values(s.open_exceptions).reduce((a, b) => a + b, 0);
  const total = s.orders_total;
  const kpi = (label, value, hint, alert) =>
    `<div class="card kpi${alert ? ' alert' : ''}"><div class="label">${label}</div><div class="value">${value}</div><div class="hint">${hint}</div></div>`;
  const order = [...FLOW, 'ON_HOLD', 'CANCELLED'];
  const bar = total ? order.map((k) => c(k) ? `<span style="width:${(c(k) / total) * 100}%;background:${FLOW_COLOR[k]}" title="${STATUS[k][0]} ${c(k)}"></span>` : '').join('') : '';
  const legend = order.map((k) => `<span><i style="background:${FLOW_COLOR[k]}"></i>${STATUS[k][0]} ${c(k)}</span>`).join('');

  const exc = await api(`/api/exceptions?status=${S.excStatus}` + (S.excRule ? `&rule=${S.excRule}` : ''));
  const allOpen = s.open_exceptions;
  const chips = ['', 'E01', 'E02', 'E03', 'E04', 'E05', 'E06'].map((r) => {
    const n = r ? (allOpen[r] || 0) : openTotal;
    const label = r ? `${r} ${s.policies[r].name}` : '전체';
    return `<button class="chip" data-rule="${r}" aria-pressed="${S.excRule === r}">${esc(label)}<span class="count">${n}</span></button>`;
  }).join('');

  const rows = exc.filter((e) => e.rule_id !== 'E07' || S.excRule === '').map((e) => {
    const left = e.minutes_left;
    const leftTxt = e.status === 'RESOLVED' ? '-' : left === null ? '-' :
      left < 0 ? `<span class="badge b-bad">${-left}분 초과</span>` : `${left}분`;
    return `<tr class="clickable" data-exc="${esc(e.exception_id)}">
      <td>${badge(SEVERITY, e.severity)}</td>
      <td class="mono">${esc(e.exception_id)}</td>
      <td><b>${esc(e.rule_id)}</b> ${esc(e.name)}${e.blocking ? '' : ' <span class="badge">경고</span>'}</td>
      <td class="mono">${esc(e.order_id)}</td>
      <td>${esc(e.client_id)}</td>
      <td>${badge(STATUS, e.order_status)}</td>
      <td>${hm(e.detected_at)}</td>
      <td>${leftTxt}</td>
      <td>${badge(EXC_STATUS, e.status)}</td></tr>`;
  }).join('');

  const clients = s.clients.map((cl) => `<div class="card client"><h3>${esc(cl.client_id)} · ${esc(cl.name)}</h3>
    <dl class="kv"><dt>보관</dt><dd>${esc(cl.storage_type)}</dd><dt>출고 마감</dt><dd>${esc(cl.cutoff_time)}</dd>
    <dt>부분출고</dt><dd>${cl.partial_ship_allowed ? '허용' : '불허'}</dd>
    <dt>최소 잔여일</dt><dd>${cl.min_remaining_days ? cl.min_remaining_days + '일' : '-'}</dd>
    <dt>택배사</dt><dd>${esc(cl.default_carrier)}</dd></dl></div>`).join('');

  $('#tab-dashboard').innerHTML = `
    ${total ? '' : `<div class="callout warn" style="margin-bottom:16px">아직 주문이 없습니다. 상단에서 재생 시점을 고르고 <b>▶ 재생</b>을 누르세요. (11:00 주문수집 → 11:01 재고할당 → 화주별 마감 30분 전 감지)</div>`}
    <div class="grid kpis">
      ${kpi('전체 주문', total, '수집된 주문')}
      ${kpi('작업 대기', c('RECEIVED') + c('CONFIRMED') + c('ALLOCATED'), '수집·확정·할당')}
      ${kpi('작업 중', c('PICKING') + c('PACKED'), '피킹·포장')}
      ${kpi('출고 완료', c('SHIPPED'), '출고 확정')}
      ${kpi('보류', c('ON_HOLD'), '차단형 예외', c('ON_HOLD') > 0)}
      ${kpi('열린 예외', openTotal, '미처리·처리중', openTotal > 0)}
      ${s.restock_pending ? kpi('원위치 대기', s.restock_pending, '피킹 후 취소 · 확인 전 재고 묶임', true) : ''}
    </div>
    <div class="card"><div class="flowbar">${bar}</div><div class="legend">${legend}</div></div>
    <div class="section-title"><h2>예외 큐</h2>
      <div class="toolbar">
        <select id="exc-status"><option value="open">열린 예외</option><option value="all">전체 (완료·E07 포함)</option></select>
      </div></div>
    <div class="chips" id="exc-chips" style="margin-bottom:10px">${chips}</div>
    <div class="table-wrap"><table>
      <thead><tr><th>심각도</th><th>예외 ID</th><th>유형</th><th>주문</th><th>화주</th><th>주문 상태</th><th>감지</th><th>처리기한</th><th>상태</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="9" class="empty">표시할 예외가 없습니다</td></tr>`}</tbody></table></div>
    <p class="muted" style="font-size:12.5px;margin:8px 0 0">E03 마감위험 감지 방식: 운영에서는 스케줄러가 10분마다 실행한다고 가정합니다. PoC에는 상시 스케줄러가 없으므로, 시나리오 재생 중의 감지 이벤트(11:10~15:00, 10분 간격) 또는 상단 <b>감지 실행</b> 버튼으로만 감지합니다. 기준시각만 바꾸면 감지되지 않습니다.</p>
    <div class="section-title"><h2>화주 정책</h2></div>
    <div class="grid clients">${clients}</div>`;
  $('#exc-status').value = S.excStatus;
  $('#exc-status').addEventListener('change', (e) => { S.excStatus = e.target.value; refresh(); });
  $('#exc-chips').addEventListener('click', (e) => {
    const b = e.target.closest('.chip');
    if (b) { S.excRule = b.dataset.rule; refresh(); }
  });
  $('#tab-dashboard tbody').addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-exc]');
    if (tr) openException(tr.dataset.exc);
  });
}

/* ---------------------------------------------------------------- 주문 */
const orderBadges = (o) => `${o.priority ? ' <span class="badge b-warn">우선</span>' : ''}${o.waiting_inbound ? ' <span class="badge b-info">입고 대기</span>' : ''}${o.restock_pending ? ` <span class="badge b-warn">원위치 대기 ${o.restock_pending}</span>` : ''}${o.parent_order_id ? ' <span class="badge">백오더</span>' : ''}`;

async function renderOrders() {
  const f = S.orderFilter;
  const queue = S.orderView === 'queue';
  const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v)).toString();
  const list = queue ? await api('/api/work-queue') : await api('/api/orders?' + qs);
  const rows = queue
    ? list.map((o) => `<tr class="clickable" data-order="${esc(o.order_id)}">
      <td class="num"><b>${o.rank}</b></td><td class="mono">${esc(o.order_id)}${orderBadges(o)}</td><td>${esc(o.client_id)}</td>
      <td>${o.due_today ? `${esc(o.cutoff_time)} ${o.minutes_to_cutoff < 0 ? `<span class="badge b-bad">${-o.minutes_to_cutoff}분 지남</span>` : `<span class="muted">${o.minutes_to_cutoff}분 남음</span>`}` : `<span class="muted">${esc(o.promised_ship_date.slice(5))} 출고</span>`}</td>
      <td>${md(o.ordered_at)}</td><td class="num">${o.line_count} / ${o.total_qty}</td><td>${badge(STATUS, o.status)}</td></tr>`).join('')
    : list.map((o) => `<tr class="clickable" data-order="${esc(o.order_id)}">
      <td class="mono">${esc(o.order_id)}${orderBadges(o)}</td>
      <td>${esc(o.client_id)}</td><td>${esc(o.channel)}</td><td>${md(o.ordered_at)}</td><td>${esc(o.promised_ship_date.slice(5))}</td>
      <td class="num">${o.line_count} / ${o.total_qty}</td><td>${badge(STATUS, o.status)}</td>
      <td>${(o.open_rules || '').split(',').filter(Boolean).map((r) => `<span class="badge b-bad">${esc(r)}</span>`).join(' ')}</td></tr>`).join('');
  const opts = (vals, cur) => vals.map(([v, t]) => `<option value="${v}"${v === cur ? ' selected' : ''}>${t}</option>`).join('');
  const imp = S.lastImport;
  const importNote = imp ? `<div class="callout ${imp.rejected.length ? 'bad' : 'ok'}" style="margin-bottom:10px">
      <b>최근 업로드</b> · 신규 ${imp.created}건 (보류 ${imp.held}건)${imp.skipped.length ? ` · 이미 있는 주문 ${imp.skipped.length}건 제외` : ''} · 거부 ${imp.rejected.length}건
      ${imp.rejected.map((x) => `<div style="margin-top:6px"><b class="mono">${esc(x.order_id)}</b> <span class="muted">(${x.rows.join(', ')}행)</span>: ${x.errors.map(esc).join(' · ')}</div>`).join('')}
      <div class="muted" style="margin-top:6px">같은 주문번호의 행은 화주·채널·주문시각·수령인·배송지·약속출고일이 모두 같아야 하며, 라인번호는 중복될 수 없습니다. 정합성이 깨진 주문만 거부하고 나머지는 등록합니다.</div></div>` : '';
  $('#tab-orders').innerHTML = `
    <div class="section-title"><h2>${queue ? `피킹 대기열 ${list.length}건` : `주문 ${list.length}건`}</h2>
      <div class="toolbar">
        <select id="of-view">${opts([['all', '보기: 전체 주문'], ['queue', '보기: 피킹 대기열 (작업 순서)']], queue ? 'queue' : 'all')}</select>
        ${queue ? '' : `<select id="of-status">${opts([['', '전체 상태'], ...Object.entries(STATUS).map(([k, v]) => [k, v[0]])], f.status)}</select>
        <select id="of-client">${opts([['', '전체 화주'], ...S.summary.clients.map((c) => [c.client_id, c.client_id + ' ' + c.name])], f.client)}</select>
        <input type="search" id="of-q" placeholder="주문번호·수령인" value="${esc(f.q)}">`}
        <label class="btn" title="주문수집 CSV (orders_import.csv와 같은 형식)">주문 CSV 업로드<input type="file" id="of-file" accept=".csv" hidden></label>
      </div></div>
    ${importNote}
    ${queue ? '<div class="callout" style="margin-bottom:10px">할당 완료·피킹 중 주문의 작업 순서입니다. ① 우선 작업(E03 \'우선 작업 지정\') ② 오늘 출고분 중 화주 마감이 빠른 순 ③ 약속출고일 ④ 주문시각. 재고 할당(전체 할당)도 우선 작업 주문부터 진행합니다.</div>' : ''}
    <div class="table-wrap"><table>
      <thead><tr>${queue ? '<th class="num">순서</th><th>주문번호</th><th>화주</th><th>마감</th><th>주문시각</th><th class="num">라인/수량</th><th>상태</th>'
    : '<th>주문번호</th><th>화주</th><th>채널</th><th>주문시각</th><th>약속출고</th><th class="num">라인/수량</th><th>상태</th><th>열린 예외</th>'}</tr></thead>
      <tbody>${rows || `<tr><td colspan="8" class="empty">${queue ? '작업 대기 중인 주문이 없습니다.' : '주문이 없습니다. 상단에서 재생하거나 CSV를 업로드하세요.'}</td></tr>`}</tbody></table></div>`;
  $('#of-view').addEventListener('change', (e) => { S.orderView = e.target.value; refresh(); });
  if (!queue) {
    $('#of-status').addEventListener('change', (e) => { f.status = e.target.value; refresh(); });
    $('#of-client').addEventListener('change', (e) => { f.client = e.target.value; refresh(); });
    $('#of-q').addEventListener('change', (e) => { f.q = e.target.value.trim(); refresh(); });
  }
  $('#of-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const text = await file.text();
    act(async () => { S.lastImport = await api('/api/orders/import', { method: 'POST', text }); return S.lastImport; },
      (r) => `신규 ${r.created}건 (보류 ${r.held}건)` + (r.skipped.length ? ` · 중복 제외 ${r.skipped.length}건` : '') +
        (r.rejected.length ? ` · 거부 ${r.rejected.length}건 (사유는 주문 탭 위쪽)` : ''));
  });
  $('#tab-orders tbody').addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-order]');
    if (tr) openOrder(tr.dataset.order);
  });
}

/* ---------------------------------------------------------------- 재고 */
async function renderInventory() {
  const f = S.invFilter;
  const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v)).toString();
  const list = await api('/api/inventory?' + qs);
  const rows = list.map((p) => {
    const open = S.openInv.has(p.sku);
    const pending = p.inbound.filter((i) => !i.received_at);
    let html = `<tr class="clickable" data-sku="${esc(p.sku)}">
      <td>${open ? '▾' : '▸'} <span class="mono">${esc(p.sku)}</span></td><td>${esc(p.name)}</td><td>${esc(p.client_id)}</td><td>${esc(p.storage_type)}</td>
      <td class="num">${p.on_hand_qty}</td><td class="num">${p.allocated_qty}</td><td class="num">${p.hold_qty}</td>
      <td class="num"><b>${p.available}</b></td>
      <td class="num">${p.near_expiry_qty ? `<span class="badge b-warn">${p.near_expiry_qty}</span>` : '-'}${p.expired_qty ? ` <span class="badge b-bad" title="유통기한 경과: 출고 불가">만료 ${p.expired_qty}</span>` : ''}</td>
      <td>${pending.map((i) => `${esc(i.expected_date.slice(5))} +${i.qty}`).join(', ') || '-'}</td></tr>`;
    if (open) {
      html += `<tr><td colspan="10" style="background:var(--surface-2)"><table>
        <thead><tr><th>로케이션</th><th>Lot</th><th>유통기한</th><th class="num">잔여일</th><th class="num">실재고</th><th class="num">할당</th><th class="num">보류</th><th class="num">가용</th><th>출고</th></tr></thead><tbody>
        ${p.lots.map((r) => `<tr><td class="mono">${esc(r.location_code)}</td><td class="mono">${esc(r.lot_no || '-')}</td><td>${esc(r.expiry_date || '-')}</td>
          <td class="num">${r.remaining_days ?? '-'}</td><td class="num">${r.on_hand_qty}</td><td class="num">${r.allocated_qty}</td><td class="num">${r.hold_qty}</td><td class="num">${r.available}</td>
          <td>${lotBadge(r)}</td></tr>`).join('')}
        </tbody></table></td></tr>`;
    }
    return html;
  }).join('');
  const opts = [['', '전체 화주'], ...S.summary.clients.map((c) => [c.client_id, c.client_id + ' ' + c.name])]
    .map(([v, t]) => `<option value="${v}"${v === f.client ? ' selected' : ''}>${t}</option>`).join('');
  $('#tab-inventory').innerHTML = `
    <div class="section-title"><h2>재고 (SKU ${list.length}개)</h2>
      <div class="toolbar"><select id="if-client">${opts}</select><input type="search" id="if-q" placeholder="SKU·상품명" value="${esc(f.q)}"></div></div>
    <div class="callout" style="margin-bottom:10px">가용 = 실재고 − 할당 − 보류. 화주의 최소 잔여일보다 유통기한이 짧은 Lot은 <b>임박</b>(E06 승인 시에만 출고), 유통기한 당일·경과 Lot은 <b>만료</b>(어떤 경우에도 출고 불가)로 표시하며, 할당은 유통기한이 빠른 순서(FEFO)입니다.</div>
    <div class="table-wrap"><table>
      <thead><tr><th>SKU</th><th>상품</th><th>화주</th><th>보관</th><th class="num">실재고</th><th class="num">할당</th><th class="num">보류</th><th class="num">가용</th><th class="num">임박</th><th>입고예정</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  $('#if-client').addEventListener('change', (e) => { f.client = e.target.value; refresh(); });
  $('#if-q').addEventListener('change', (e) => { f.q = e.target.value.trim(); refresh(); });
  $('#tab-inventory tbody').addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-sku]');
    if (!tr) return;
    S.openInv.has(tr.dataset.sku) ? S.openInv.delete(tr.dataset.sku) : S.openInv.add(tr.dataset.sku);
    renderInventory();
  });
}

/* ---------------------------------------------------------------- 검증 */
async function renderVerify() {
  if (new URLSearchParams(location.search).get('scen')) {  // 캡처용: 처리 시나리오 결과만 표시
    S.scen = S.scen || await api('/api/verify/scenarios');
    $('#tab-verify').innerHTML = `<div class="section-title"><h2>처리 시나리오 검증 (예외 처리 후 주문·재고·출고 변화)</h2></div>
      <div id="scen">${renderScenarios(S.scen)}</div>`;
    return;
  }
  const v = await api('/api/verify');
  const rules = Object.entries(v.rules).map(([r, d]) => `<tr><td><b>${esc(r)}</b> ${esc(S.summary.policies[r]?.name || '')}</td>
    <td class="num">${d.expected}</td><td class="num">${d.detected}</td><td class="num">${d.false_positive}</td>
    <td>${d.expected === d.detected && !d.false_positive ? '<span class="badge b-ok">일치</span>' : '<span class="badge b-bad">불일치</span>'}</td></tr>`).join('');
  const checks = v.checks.map((c) => `<tr><td><b>${esc(c.name)}</b></td><td class="muted">${esc(c.method)}</td><td>${esc(c.result)}</td>
    <td>${c.ok ? '<span class="badge b-ok">통과</span>' : '<span class="badge b-bad">실패</span>'}</td></tr>`).join('');
  $('#tab-verify').innerHTML = `
    <div class="card verdict ${v.pass ? 'pass' : 'fail'}">
      <div class="big">${v.pass ? 'PASS' : 'FAIL'}</div>
      <div>정답 라벨(<span class="mono">seed/expected_labels.csv</span>) 300건과 비교한 결과입니다.<br>
      <span class="muted">재생 완료 ${v.replay_complete ? '예' : '아니오'} · 수동 처리 ${v.manual_ops}건 · 기준시각 ${esc(v.clock)}</span></div>
    </div>
    ${v.note ? `<div class="callout warn" style="margin-top:10px">${esc(v.note)}</div>` : ''}
    <div class="section-title"><h2>검증 항목</h2><button class="btn" id="btn-verify">다시 검증</button></div>
    <div class="table-wrap"><table><thead><tr><th>항목</th><th>방법</th><th>결과</th><th>판정</th></tr></thead><tbody>${checks}</tbody></table></div>
    <div class="section-title"><h2>예외 유형별 감지</h2></div>
    <div class="table-wrap"><table><thead><tr><th>규칙</th><th class="num">정답</th><th class="num">감지(일치)</th><th class="num">오탐</th><th>판정</th></tr></thead>
      <tbody>${rules || '<tr><td colspan="5" class="empty">아직 감지된 예외가 없습니다</td></tr>'}</tbody></table></div>
    ${v.mismatch_count ? `<div class="section-title"><h2>불일치 ${v.mismatch_count}건</h2></div>
      <div class="card mono" style="max-height:320px;overflow:auto">${v.mismatches.map(esc).join('<br>')}</div>` : ''}
    <div class="section-title"><h2>처리 시나리오 검증 (예외 처리 후 주문·재고·출고 변화)</h2>
      <button class="btn primary" id="btn-scen">${S.scen ? '다시 실행' : '처리 시나리오 실행'}</button></div>
    <div class="callout" style="margin-bottom:10px">시나리오마다 <b>임시 DB</b>를 새로 만들어 11:30까지 재생한 뒤 예외 하나를 처리하고, 처리 전후 데이터를 기대값과 비교합니다. 지금 보고 있는 DB에는 영향이 없습니다.</div>
    <div id="scen">${renderScenarios(S.scen)}</div>`;
  $('#btn-verify').addEventListener('click', () => refresh());
  $('#btn-scen').addEventListener('click', async (ev) => {
    ev.target.disabled = true;
    ev.target.textContent = '실행 중… (약 5초)';
    try { S.scen = await api('/api/verify/scenarios'); } catch (e) { toast(e.message, true); }
    await renderVerify();
    $('#scen').scrollIntoView();
  });
}

function renderScenarios(r) {
  if (!r) return '<div class="card muted">아직 실행하지 않았습니다.</div>';
  const head = `<div class="card verdict ${r.pass ? 'pass' : 'fail'}" style="margin-bottom:12px"><div class="big">${r.pass ? 'PASS' : 'FAIL'}</div>
    <div>처리 시나리오 ${r.passed} / ${r.total} 통과 · 검사 항목 ${r.checks}개 · ${r.seconds}초<br><span class="muted">기준 시점 ${esc(r.checkpoint)}에서 각 시나리오를 독립 실행</span></div></div>`;
  const body = r.scenarios.map((s) => `<div class="card" style="margin-bottom:10px">
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px">
      ${s.pass ? '<span class="badge b-ok">통과</span>' : '<span class="badge b-bad">실패</span>'}
      <b class="mono">${esc(s.id)}</b> <b>${esc(s.rule)}</b> ${esc(s.title)}
      <span class="muted">· ${esc(s.action)} · 주문 <span class="mono">${esc(s.order_id || '-')}</span> · <span class="mono">${esc(s.exception_id || '')}</span></span></div>
    ${s.error ? `<div class="callout bad">${esc(s.error)}</div>` : ''}
    <div class="table-wrap"><table><thead><tr><th>확인 항목</th><th>처리 전</th><th>처리 후</th><th>기대값</th><th>판정</th></tr></thead><tbody>
      ${s.checks.map((c) => `<tr><td>${esc(c.item)}</td><td>${esc(c.before)}</td><td><b>${esc(c.after)}</b></td><td>${esc(c.expected)}</td>
        <td>${c.ok ? '<span class="badge b-ok">일치</span>' : '<span class="badge b-bad">불일치</span>'}</td></tr>`).join('')}
    </tbody></table></div></div>`).join('');
  return head + body;
}

/* ---------------------------------------------------------------- 드로어 */
function openDrawer(loader) {
  S.drawer = loader;
  $('#drawer').classList.add('open');
  $('#scrim').classList.add('open');
  $('#drawer').setAttribute('aria-hidden', 'false');
  return loader();
}
function closeDrawer() {
  S.drawer = null;
  $('#drawer').classList.remove('open');
  $('#scrim').classList.remove('open');
  $('#drawer').setAttribute('aria-hidden', 'true');
}
$('#drawer-close').addEventListener('click', closeDrawer);
$('#scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
$('#drawer-body').addEventListener('click', (e) => {
  const o = e.target.closest('[data-open-order]');
  if (o) { e.preventDefault(); openOrder(o.dataset.openOrder); }
  const x = e.target.closest('[data-open-exc]');
  if (x) { e.preventDefault(); openException(x.dataset.openExc); }
});

function auditList(rows) {
  if (!rows.length) return '<div class="muted">이력 없음</div>';
  return `<ul class="timeline">${rows.map((a) => `<li><span class="t">${md(a.created_at)}</span> ·
    <b>${esc(a.entity === 'order' ? a.field : a.entity + ' ' + a.entity_id)}</b>
    ${a.before_value !== null ? esc(a.before_value) + ' → ' : ''}${esc(a.after_value)}
    <span class="muted">· ${esc(a.actor)} · ${esc(a.reason)}</span></li>`).join('')}</ul>`;
}

/* 주문 상세 */
function openOrder(oid) {
  return openDrawer(async () => {
    let d;
    try { d = await api('/api/orders/' + encodeURIComponent(oid)); } catch (e) { closeDrawer(); toast(e.message, true); return; }
    const o = d.order;
    const curIdx = FLOW.indexOf(o.status === 'ON_HOLD' ? o.hold_from_status : o.status);
    const stepCls = (i) => (i < curIdx ? 'done' : i !== curIdx ? '' : o.status === 'ON_HOLD' ? 'hold' : o.status === 'CANCELLED' ? '' : 'cur');
    const stepper = FLOW.map((k, i) => `<div class="s ${stepCls(i)}">${STATUS[k][0]}${stepCls(i) === 'hold' ? ' · 보류' : ''}</div>`).join('');
    const holdNote = o.status === 'ON_HOLD' ? (o.waiting_inbound
      ? '<div class="callout warn">입고 대기 중 · 입고예정일이 되어 <b>입고 처리</b>를 실행하면 자동으로 재할당됩니다. 아직 들어오지 않은 재고로는 할당하지 않습니다.</div>'
      : `<div class="callout bad">보류 중 · ${esc(STATUS[o.hold_from_status]?.[0] || o.hold_from_status)} 단계에서 멈춤. 예외를 처리하면 이어서 진행됩니다.</div>`) :
      o.status === 'CANCELLED' ? (d.items.some((l) => l.allocations.some((a) => a.status === 'RETURNING'))
        ? '<div class="callout warn">취소된 주문 · 피킹한 상품이 아직 로케이션에 돌아오지 않았습니다. 상품을 원래 위치에 놓은 뒤 <b>원위치 확인</b>을 누르면 할당재고가 풀립니다. 그 전까지는 다른 주문에 할당되지 않습니다.</div>'
        : '<div class="callout">취소된 주문입니다.</div>') : '';
    const related = [
      o.parent_order_id ? `원주문 <a data-open-order="${esc(o.parent_order_id)}" class="mono">${esc(o.parent_order_id)}</a>의 부분출고 잔량 백오더` : '',
      ...d.backorders.map((b) => `백오더 <a data-open-order="${esc(b.order_id)}" class="mono">${esc(b.order_id)}</a> ${badge(STATUS, b.status)}`),
    ].filter(Boolean).join(' · ');
    const actions = d.actions.map((a) => `<button class="btn ${a.action === 'cancel-request' ? 'danger' : 'primary'}" data-act="${a.action}" data-to="${a.to || ''}">${esc(a.label)}</button>`).join('') +
      (['SHIPPED', 'CANCELLED'].includes(o.status) ? '<button class="btn ghost" data-act="cancel-request" title="차단되는지 확인용">취소요청 (차단 테스트)</button>' : '') +
      (o.status === 'SHIPPED' ? '<button class="btn ghost" data-act="transition" data-to="SHIPPED" title="중복 출고 차단 확인용">출고 재요청 (차단 테스트)</button>' : '');
    const items = d.items.map((l) => `<tr><td>${l.line_no}</td><td class="mono">${esc(l.channel_product_code)}</td>
      <td>${l.sku ? `<span class="mono">${esc(l.sku)}</span> ${esc(l.sku_name)}` : '<span class="badge b-bad">매핑 없음</span>'}${l.near_expiry_ok === 1 ? ' <span class="badge b-info">임박 Lot 승인</span>' : l.near_expiry_ok === 0 ? ' <span class="badge">임박 거절</span>' : ''}</td>
      <td class="num">${l.qty}${l.ship_qty !== l.qty ? ` → <b>${l.ship_qty}</b><br><span class="badge b-warn">미출고 ${l.short_qty} ${l.short_action === 'BACKORDER' ? '백오더' : '잔량 취소'}</span>` : ''}</td>
      <td>${l.allocations.map((a) => `<span class="mono">${esc(a.location_code)}</span> ${esc(a.lot_no || '')} ×${a.qty} ${a.status === 'ACTIVE' ? '' : a.status === 'RETURNING' ? '<span class="badge b-warn">원위치 대기</span>' : `<span class="badge">${esc(a.status)}</span>`}`).join('<br>') || '-'}</td></tr>`).join('');
    const sh = d.shipment;
    const exc = d.exceptions.map((e) => `<tr class="clickable" data-open-exc="${esc(e.exception_id)}"><td class="mono">${esc(e.exception_id)}</td>
      <td><b>${esc(e.rule_id)}</b></td><td>${md(e.detected_at)}</td><td>${badge(EXC_STATUS, e.status)}</td><td>${esc(e.resolution_note || '')}</td></tr>`).join('');
    $('#drawer-body').innerHTML = `
      <h2 class="mono">${esc(o.order_id)}</h2>
      <div class="meta">${badge(STATUS, o.status)} <span>${esc(d.client.client_id)} ${esc(d.client.name)}</span> · <span>${esc(o.channel)}</span>
        · <span>주문 ${md(o.ordered_at)}</span> · <span>약속출고 ${esc(o.promised_ship_date)}</span>${o.priority ? ' <span class="badge b-warn">우선 작업</span>' : ''}${{ PENDING: ' <span class="badge b-bad">취소요청 · 판단 대기</span>', ACCEPTED: ' <span class="badge">취소요청 수락</span>', REJECTED: ' <span class="badge b-info">취소요청 거절 · 출고 진행</span>' }[o.cancel_result] || ''}${o.waiting_inbound ? ' <span class="badge b-info">입고 대기</span>' : ''}</div>
      ${related ? `<div class="meta" style="margin-top:6px">${related}</div>` : ''}
      <div class="stepper">${stepper}</div>
      ${holdNote}
      <div class="actions">${actions}</div>
      <h3>배송지</h3>
      <dl class="kv"><dt>수령인</dt><dd>${esc(o.recipient)}</dd><dt>우편번호</dt><dd class="mono">${esc(o.zipcode) || '<span class="badge b-bad">없음</span>'}</dd><dt>주소</dt><dd>${esc(o.address) || '<span class="badge b-bad">없음</span>'}</dd></dl>
      <h3>주문 라인 · 할당</h3>
      <div class="table-wrap"><table><thead><tr><th>#</th><th>채널 상품코드</th><th>SKU</th><th class="num">수량</th><th>할당 (로케이션 · Lot)</th></tr></thead><tbody>${items}</tbody></table></div>
      <h3>출고</h3>
      ${sh ? `<dl class="kv"><dt>피킹</dt><dd>${md(sh.picked_at)} · ${esc(sh.picker_id)}</dd><dt>포장</dt><dd>${md(sh.packed_at)}</dd>
        <dt>송장</dt><dd>${esc(sh.carrier || '-')} <span class="mono">${esc(sh.tracking_no || '')}</span>${sh.cancelled_at ? ` <span class="badge b-bad">송장 취소 ${md(sh.cancelled_at)}</span>` : ''}</dd><dt>출고</dt><dd>${md(sh.shipped_at)}</dd></dl>` : '<div class="muted">아직 작업 전</div>'}
      <h3>예외 ${d.exceptions.length}건</h3>
      ${exc ? `<div class="table-wrap"><table><thead><tr><th>ID</th><th>유형</th><th>감지</th><th>상태</th><th>처리 내용</th></tr></thead><tbody>${exc}</tbody></table></div>` : '<div class="muted">없음</div>'}
      <h3>변경 이력 (audit_log)</h3>${auditList(d.audit)}`;
    $('#drawer-body .actions').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-act]');
      if (!b) return;
      const path = `/api/orders/${encodeURIComponent(oid)}/${b.dataset.act}`;
      const json = b.dataset.act === 'transition' ? { to: b.dataset.to, actor: 'OPERATOR' } : { actor: b.dataset.act === 'cancel-request' ? 'CHANNEL' : 'OPERATOR' };
      act(() => api(path, { method: 'POST', json }), (r) => (b.dataset.act === 'confirm-restock'
        ? `${oid} 원위치 확인 → 할당재고 해제` : `${oid} → ${STATUS[r.status]?.[0] || r.status}`));
    });
  });
}

/* 예외 상세·처리 */
function causeSection(d) {
  const e = d.exception;
  const rule = e.rule_id;
  const c = d.client;
  const policy = `<dl class="kv"><dt>화주</dt><dd>${esc(c.client_id)} ${esc(c.name)}</dd><dt>출고 마감</dt><dd>${esc(c.cutoff_time)}</dd>
    <dt>부분출고</dt><dd>${c.partial_ship_allowed ? '허용' : '<b>불허</b>'}</dd><dt>최소 잔여일</dt><dd>${c.min_remaining_days ? c.min_remaining_days + '일' : '-'}</dd>
    <dt>약속출고일</dt><dd>${esc(d.order.promised_ship_date)}</dd></dl>`;
  let body = '';
  if (rule === 'E01' || rule === 'E06') {
    const open = ['OPEN', 'IN_PROGRESS'].includes(e.status);
    if (!open) body = '<div class="muted" style="margin-bottom:6px">처리 후 현재 재고입니다.</div>';
    body += d.lines.map((l) => {
      const short = open && l.short_kind;
      return `<div class="card" style="margin-bottom:8px">
        <div><b>라인 ${l.line_no}</b> · <span class="mono">${esc(l.sku)}</span> ${esc(l.sku_name)}
          ${open ? (l.short_kind === 'E06' ? '<span class="badge b-warn">E06 임박 Lot 결정 대기</span>' : short ? '<span class="badge b-bad">E01 재고 부족</span>' : '<span class="badge b-ok">충분</span>') : ''}
          ${l.near_expiry_ok === 1 ? '<span class="badge b-info">임박 Lot 출고 승인됨</span>' : l.near_expiry_ok === 0 ? '<span class="badge">임박 Lot 거절 → 재고부족 처리</span>' : ''}</div>
        <div class="muted">주문 ${l.qty}${l.ship_qty !== l.qty ? ` (출고 ${l.ship_qty})` : ''} · 출고 가능 가용 <b>${l.usable_available}</b> · 전체 가용 ${l.total_available}</div>
        ${l.lots.length ? `<table style="margin-top:6px"><thead><tr><th>로케이션</th><th>Lot</th><th>유통기한</th><th class="num">잔여일</th><th class="num">가용</th><th>출고</th></tr></thead><tbody>
          ${l.lots.map((r) => `<tr><td class="mono">${esc(r.location_code)}</td><td class="mono">${esc(r.lot_no || '-')}</td><td>${esc(r.expiry_date || '-')}</td><td class="num">${r.remaining_days ?? '-'}</td><td class="num">${r.available}</td><td>${lotBadge(r)}</td></tr>`).join('')}</tbody></table>` : ''}
        ${short ? `<div style="margin-top:6px" class="muted">입고예정: ${l.inbound.map((i) => `${esc(i.expected_date)} +${i.qty}${i.received_at ? ' (입고완료)' : ''}`).join(', ') || '없음'}</div>` : ''}
      </div>`;
    }).join('');
  } else if (rule === 'E02') {
    body = `<div class="callout">채널 <b>${esc(e.detail.channel)}</b>의 상품코드 <span class="mono">${(e.detail.unmapped_codes || []).map(esc).join(', ')}</span>에 연결된 SKU가 없습니다.
      쇼핑몰에 새로 등록한 상품을 센터 상품과 연결하지 않은 경우입니다.</div>${(e.detail.owner_mismatch || []).length ? `
      <div class="callout bad" style="margin-top:8px">다른 화주의 SKU로 잘못 연결된 매핑을 발견해 출고를 막았습니다: <span class="mono">${e.detail.owner_mismatch.map(esc).join(', ')}</span>. 이 화주의 SKU로 다시 매핑하세요.</div>` : ''}`;
  } else if (rule === 'E05') {
    body = `<div class="callout">우편번호 <span class="mono">'${esc(d.order.zipcode)}'</span> · 주소 '${esc(d.order.address)}' — 우편번호는 숫자 5자리, 주소는 필수입니다.</div>`;
  } else if (rule === 'E03') {
    body = `<div class="callout warn">${esc(c.client_id)} 마감 ${esc(c.cutoff_time)} 30분 전인데 주문이 아직 <b>${esc(STATUS[e.detail.status]?.[0] || e.detail.status)}</b> 상태였습니다. 경고형 예외라 작업은 멈추지 않습니다.</div>`;
  } else if (rule === 'E04') {
    body = `<div class="callout">${esc(STATUS[e.detail.status_at_request]?.[0] || e.detail.status_at_request)} 단계에서 취소요청이 들어왔습니다. 이미 상품을 꺼냈으므로 출고를 멈출지 운영자가 결정합니다.
      <br><b>출고 중단</b>: 주문 취소·송장 취소 → 상품이 로케이션에 돌아온 것을 <b>원위치 확인</b>해야 재고가 풀립니다. <b>출고 진행</b>: 취소요청을 거절로 기록하고 이어서 출고합니다.</div>`;
  } else if (rule === 'E07') {
    body = `<div class="callout">차단된 요청: ${esc(e.detail.attempt)} (당시 상태 ${esc(e.detail.status)})</div>`;
  }
  return body + `<h3>화주 정책</h3>${policy}`;
}

function openException(eid) {
  return openDrawer(async () => {
    let d;
    try { d = await api('/api/exceptions/' + encodeURIComponent(eid)); } catch (err) { closeDrawer(); toast(err.message, true); return; }
    const e = d.exception;
    const open = ['OPEN', 'IN_PROGRESS'].includes(e.status);
    const opts = d.options.map((o) => `<label class="option ${o.enabled ? '' : 'disabled'}">
      <input type="radio" name="opt" value="${esc(o.code)}" ${o.enabled ? '' : 'disabled'}>
      <div><div><b>${esc(o.code)}.</b> ${esc(o.label)}</div><div class="cond">조건: ${esc(o.enable_if)}</div>
      ${o.enabled ? '' : `<div class="why">선택 불가 · ${esc(o.reason)}</div>`}</div></label>`).join('');
    const unmapped = e.rule_id === 'E02' ? (e.detail.unmapped_codes || []) : [];
    const params = `
      <div id="p-mappings" hidden>${unmapped.map((code) => {
        const name = d.lines.find((l) => l.channel_product_code === code)?.product_name || '';
        return `<div class="form-row"><label>${esc(code)} (쇼핑몰 상품명: ${esc(name) || '-'}) → 연결할 SKU <span class="muted">· 상품명이 같은 SKU를 미리 골라 둠, 확인 후 승인</span></label>
        <select data-map="${esc(code)}"><option value="">선택</option>${d.products.map((p) => `<option value="${esc(p.sku)}"${p.name === name ? ' selected' : ''}>${esc(p.sku)} ${esc(p.name)}</option>`).join('')}</select></div>`;
      }).join('')}</div>
      <div id="p-address" hidden>
        <div class="form-row"><label>우편번호 (숫자 5자리)</label><input type="text" id="p-zip" value="${esc(d.order.zipcode)}"></div>
        <div class="form-row"><label>주소</label><input type="text" id="p-addr" value="${esc(d.order.address)}"></div></div>
      <div id="p-remainder" hidden><div class="form-row"><label>출고하지 않는 수량(잔량) 처리</label>
        <div class="options">
          <label class="option"><input type="radio" name="remainder" value="CANCEL" checked><div><b>잔량 취소</b><div class="cond">부족분은 주문에서 빼고, 출고수량 기준으로 정산</div></div></label>
          <label class="option"><input type="radio" name="remainder" value="BACKORDER"><div><b>백오더</b><div class="cond">부족분으로 새 주문(원주문-B1)을 만들어 입고 후 출고</div></div></label>
        </div></div></div>
      <div id="p-wait" hidden class="callout warn" style="margin-top:10px">승인하면 주문은 <b>입고 대기</b> 상태로 보류됩니다. 입고예정일에 <b>입고 처리</b>를 실행하면 입고가 반영되고 자동으로 재할당됩니다.</div>`;
    $('#drawer-body').innerHTML = `
      <h2>${esc(e.rule_id)} ${esc(e.name)}</h2>
      <div class="meta"><span class="mono">${esc(e.exception_id)}</span> ${badge(SEVERITY, e.severity)} ${badge(EXC_STATUS, e.status)}
        ${e.blocking ? '<span class="badge">차단형</span>' : '<span class="badge">경고형</span>'}
        · 주문 <a data-open-order="${esc(e.order_id)}" class="mono">${esc(e.order_id)}</a> ${badge(STATUS, d.order.status)}
        · 감지 ${md(e.detected_at)}${e.due_at ? ` · 처리기한 ${md(e.due_at)}` : ''}</div>
      ${d.order.waiting_inbound ? '<div class="callout warn" style="margin-top:12px">입고 대기 중 · 입고예정일에 <b>입고 처리</b>를 실행하면 자동으로 재할당되고 이 예외가 완료됩니다. 대기 대신 다른 옵션으로 바꿀 수도 있습니다.</div>' : ''}
      <h3>원인 데이터</h3>${causeSection(d)}
      ${open ? `<h3>처리 옵션</h3><div class="options" id="opts">${opts}</div>${params}
        <div class="form-row"><label>처리 사유 (이력에 남음)</label><textarea id="p-reason" placeholder="예: 화주 담당자와 통화, 부분출고 동의"></textarea></div>
        <div class="actions" style="margin-top:12px"><button class="btn primary" id="btn-resolve" disabled>승인</button>
        <span class="muted" style="align-self:center">승인하면 처리자·사유와 함께 상태·재고 변경이 이력에 남습니다.</span></div>` :
        `<div class="callout ok" style="margin-top:12px">${e.status === 'LOGGED' ? '시스템이 차단하고 기록만 남긴 예외입니다.' : `처리 완료 · 옵션 ${esc(e.resolution_code)} · ${esc(e.assignee)} · ${md(e.resolved_at)}${e.resolution_note ? ' · ' + esc(e.resolution_note) : ''}`}</div>`}
      <h3>예외 이력</h3>${auditList(d.audit)}`;
    if (!open) return;
    const sel = () => document.querySelector('input[name=opt]:checked')?.value;
    $('#opts').addEventListener('change', () => {
      const opt = d.options.find((o) => o.code === sel());
      $('#p-mappings').hidden = !opt?.params.includes('mappings');
      $('#p-address').hidden = !opt?.params.includes('zipcode');
      $('#p-remainder').hidden = !opt?.params.includes('remainder');
      $('#p-wait').hidden = !(e.rule_id === 'E01' && opt?.code === '2');
      $('#btn-resolve').disabled = !opt;
    });
    $('#btn-resolve').addEventListener('click', () => {
      const params = {};
      const opt = d.options.find((o) => o.code === sel());
      if (opt.params.includes('mappings')) {
        params.mappings = {};
        document.querySelectorAll('[data-map]').forEach((s) => { params.mappings[s.dataset.map] = s.value; });
      }
      if (opt.params.includes('zipcode')) { params.zipcode = $('#p-zip').value; params.address = $('#p-addr').value; }
      if (opt.params.includes('remainder')) params.remainder = document.querySelector('input[name=remainder]:checked').value;
      act(() => api(`/api/exceptions/${encodeURIComponent(eid)}/resolve`, { method: 'POST',
        json: { option: opt.code, actor: 'OPERATOR', reason: $('#p-reason').value, params } }),
      (r) => `${eid} 처리 → 주문 ${STATUS[r.order.status]?.[0] || r.order.status}`);
    });
  });
}

/* 딥링크: ?exc=EX-0010&opt=2 · ?order=ORD-0002 · ?client=C02&inv=C02-008 · ?scen=1#verify */
const LINK = new URLSearchParams(location.search);
if (LINK.get('client')) { S.invFilter.client = LINK.get('client'); S.orderFilter.client = LINK.get('client'); }
if (LINK.get('inv')) S.openInv.add(LINK.get('inv'));
if (LINK.get('view') === 'queue') S.orderView = 'queue';

S.tab = location.hash.slice(1) || 'dashboard';
refresh().then(async () => {
  if (LINK.get('exc')) {
    await openException(LINK.get('exc'));
    const radio = LINK.get('opt') && document.querySelector(`input[name=opt][value="${LINK.get('opt')}"]`);
    if (radio && !radio.disabled) { radio.checked = true; radio.dispatchEvent(new Event('change', { bubbles: true })); }
  } else if (LINK.get('order')) {
    await openOrder(LINK.get('order'));
  }
}).catch((e) => toast('서버 연결 실패: ' + e.message, true));
