"""물류 PoC 도메인 엔진.

설계초안 3장(상태 전이), 4장(데이터 모델), 5장(예외 정책)을 SQLite 위에 구현합니다.
상태 변경과 재고 변경은 모두 트랜잭션 안에서 처리하고 audit_log에 남깁니다.
"""
import csv
import io
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED_DIR = os.path.join(ROOT, 'seed')
TS = '%Y-%m-%d %H:%M:%S'
START_CLOCK = '2026-10-26 10:59:00'

STEP_FROM = {'PICKING': 'ALLOCATED', 'PACKED': 'PICKING', 'SHIPPED': 'PACKED'}  # 도착 상태: 출발 상태
STEP_REASON = {'PICKING': '피킹 시작', 'PACKED': '검수·포장 완료, 송장 발행', 'SHIPPED': '출고 확정'}
PRE_WORK = ('RECEIVED', 'CONFIRMED', 'ALLOCATED')
IN_WORK = ('PICKING', 'PACKED')
CARRIER_PREFIX = {'CJ대한통운': 'CJ', '롯데택배': 'LT', '한진택배': 'HJ'}
SEVERITY_RANK = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
SCHEMA_VERSION = '5'  # 스키마가 바뀌면 올림 → 기존 DB는 시작할 때 자동 초기화
MIN_SELLABLE_DAYS = 1  # 잔여일이 이보다 작은 Lot(유통기한 당일·경과)은 만료: E06 승인으로도 출고 불가

SCHEMA = """
CREATE TABLE clients (
    client_id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT, storage_type TEXT,
    cutoff_time TEXT NOT NULL, partial_ship_allowed INTEGER NOT NULL, min_remaining_days INTEGER,
    default_carrier TEXT
);
CREATE TABLE products (
    sku TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients, name TEXT NOT NULL,
    storage_type TEXT, lot_managed INTEGER NOT NULL, barcode TEXT,
    UNIQUE (sku, client_id)                        -- 매핑의 (SKU, 화주) 복합 FK 대상
);
CREATE TABLE channel_product_map (
    channel TEXT NOT NULL, channel_product_code TEXT NOT NULL, client_id TEXT NOT NULL REFERENCES clients,
    sku TEXT NOT NULL REFERENCES products,
    FOREIGN KEY (sku, client_id) REFERENCES products (sku, client_id),  -- 매핑 화주 = SKU 소유 화주 (DB에서 강제)
    -- 화주별 매핑: 다른 화주가 같은 채널에서 같은 상품코드를 써도 충돌하지 않음
    -- (실서비스에서 화주가 채널 계정을 여러 개 쓰면 channel_account_id까지 키에 넣어야 함)
    PRIMARY KEY (client_id, channel, channel_product_code)
);
CREATE TABLE locations (location_code TEXT PRIMARY KEY, zone TEXT, storage_type TEXT);
CREATE TABLE inventory (
    inventory_id INTEGER PRIMARY KEY, sku TEXT NOT NULL REFERENCES products,
    location_code TEXT NOT NULL REFERENCES locations, lot_no TEXT, expiry_date TEXT,
    on_hand_qty INTEGER NOT NULL, allocated_qty INTEGER NOT NULL DEFAULT 0, hold_qty INTEGER NOT NULL DEFAULT 0,
    CHECK (on_hand_qty >= 0), CHECK (allocated_qty >= 0), CHECK (hold_qty >= 0),
    CHECK (allocated_qty + hold_qty <= on_hand_qty)
);
CREATE TABLE inbound_schedule (
    inbound_id TEXT PRIMARY KEY, sku TEXT NOT NULL REFERENCES products, expected_date TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0), received_at TEXT
);
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients, channel TEXT NOT NULL,
    ordered_at TEXT NOT NULL, recipient TEXT, zipcode TEXT, address TEXT, promised_ship_date TEXT NOT NULL,
    status TEXT NOT NULL, hold_from_status TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0, imported_at TEXT,
    cancel_result TEXT,                           -- 취소요청 결과: PENDING / ACCEPTED / REJECTED
    waiting_inbound INTEGER NOT NULL DEFAULT 0,   -- E01 '입고 후 출고' 승인 후 입고 대기 중
    parent_order_id TEXT REFERENCES orders        -- 부분출고 잔량 백오더의 원주문
);
CREATE TABLE order_items (
    order_id TEXT NOT NULL REFERENCES orders, line_no INTEGER NOT NULL, channel_product_code TEXT NOT NULL,
    product_name TEXT, sku TEXT REFERENCES products, qty INTEGER NOT NULL CHECK (qty > 0),
    ship_qty INTEGER NOT NULL CHECK (ship_qty >= 0),
    short_qty INTEGER NOT NULL DEFAULT 0,          -- 부분출고로 출고하지 않는 수량
    short_action TEXT,                             -- CANCEL(잔량 취소) / BACKORDER(백오더 주문 생성)
    near_expiry_ok INTEGER,                        -- E06 결정: NULL 미결정 / 1 임박 Lot 출고 승인 / 0 거절(재고부족 처리)
    PRIMARY KEY (order_id, line_no),
    CHECK (ship_qty + short_qty = qty)
);
CREATE TABLE allocations (
    allocation_id INTEGER PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders, line_no INTEGER NOT NULL,
    inventory_id INTEGER NOT NULL REFERENCES inventory, qty INTEGER NOT NULL CHECK (qty > 0),
    status TEXT NOT NULL  -- ACTIVE / SHIPPED / RELEASED / RETURNING(피킹 후 취소, 원위치 확인 대기)
);
CREATE TABLE shipments (
    shipment_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES orders, picker_id TEXT,
    picked_at TEXT, packed_at TEXT, carrier TEXT, tracking_no TEXT, shipped_at TEXT,
    cancelled_at TEXT, cancel_reason TEXT          -- 출고 중단 시 송장 취소
);
CREATE TABLE exceptions (
    exception_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders, rule_id TEXT NOT NULL,
    severity TEXT, blocking INTEGER NOT NULL, detected_at TEXT NOT NULL, due_at TEXT, status TEXT NOT NULL,
    ref TEXT, detail TEXT, assignee TEXT, resolution_code TEXT, resolution_note TEXT, resolved_at TEXT
);
CREATE TABLE audit_log (
    log_id INTEGER PRIMARY KEY, entity TEXT NOT NULL, entity_id TEXT NOT NULL, field TEXT,
    before_value TEXT, after_value TEXT, actor TEXT, reason TEXT, created_at TEXT NOT NULL
);
CREATE TABLE sim_state (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX ix_items_order ON order_items(order_id);
CREATE INDEX ix_alloc_order ON allocations(order_id);
CREATE INDEX ix_exc_order ON exceptions(order_id);
CREATE INDEX ix_audit_entity ON audit_log(entity, entity_id);
CREATE INDEX ix_inv_sku ON inventory(sku);
"""
TABLES = ['audit_log', 'exceptions', 'shipments', 'allocations', 'order_items', 'orders', 'inbound_schedule',
          'inventory', 'locations', 'channel_product_map', 'products', 'clients', 'sim_state']


class Conflict(Exception):
    """허용되지 않는 상태 변경 (HTTP 409). E07로 기록됩니다."""


class BadRequest(Exception):
    """잘못된 요청 (HTTP 400)."""


class NotFound(Exception):
    """대상 없음 (HTTP 404)."""


def _read_csv(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


class Engine:
    def __init__(self, db_path, seed_dir=SEED_DIR):
        self.seed_dir = seed_dir
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.execute('PRAGMA journal_mode = WAL')
        self.db.execute('PRAGMA synchronous = NORMAL')
        with open(os.path.join(seed_dir, 'policies.json'), encoding='utf-8') as f:
            self.policies = {p['rule_id']: p for p in json.load(f)}
        self._depth = 0
        self._replaying = False
        self._events_cache = None
        if (not self._q("SELECT name FROM sqlite_master WHERE type='table' AND name='sim_state'")
                or self._state('schema_version') != SCHEMA_VERSION):
            self.reset()

    # ------------------------------------------------------------ 기본 도구
    @contextmanager
    def tx(self):
        with self.lock:
            outer = self._depth == 0
            if outer:
                self.db.execute('BEGIN IMMEDIATE')
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self.db.execute('ROLLBACK')
                raise
            self._depth -= 1
            if outer:
                self.db.execute('COMMIT')

    def _q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def _x(self, sql, args=()):
        return self.db.execute(sql, args)

    def _state(self, key, default=None):
        r = self._one('SELECT value FROM sim_state WHERE key=?', (key,))
        return r['value'] if r else default

    def _set_state(self, key, value):
        self._x('INSERT INTO sim_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, str(value)))

    def now(self):
        return datetime.strptime(self._state('clock', START_CLOCK), TS)

    def _now_s(self):
        return self._state('clock', START_CLOCK)

    def set_clock(self, value):
        try:
            datetime.strptime(value, TS)
        except (TypeError, ValueError):
            raise BadRequest(f'기준시각 형식 오류: {value!r} (예: 2026-10-26 11:30:00)')
        with self.tx():
            self._set_state('clock', value)
        return value

    def _manual(self):
        if not self._replaying:
            self._set_state('manual_ops', int(self._state('manual_ops', 0)) + 1)

    def _audit(self, entity, entity_id, field, before, after, actor, reason):
        self._x('INSERT INTO audit_log(entity, entity_id, field, before_value, after_value, actor, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (entity, str(entity_id), field, None if before is None else str(before),
                 None if after is None else str(after), actor, reason, self._now_s()))

    def _order(self, oid):
        o = self._one('SELECT * FROM orders WHERE order_id=?', (oid,))
        if not o:
            raise NotFound(f'주문 없음: {oid}')
        return o

    def _client(self, cid):
        return self._one('SELECT * FROM clients WHERE client_id=?', (cid,))

    def _items(self, oid):
        return self._q('SELECT i.*, p.name AS sku_name FROM order_items i LEFT JOIN products p ON p.sku=i.sku '
                       'WHERE order_id=? ORDER BY line_no', (oid,))

    def _set_status(self, oid, to, actor, reason):
        before = self._order(oid)['status']
        self._x('UPDATE orders SET status=? WHERE order_id=?', (to, oid))
        self._audit('order', oid, 'status', before, to, actor, reason)

    def _inv_delta(self, inv_id, field, delta, actor, reason):
        before = self._one(f'SELECT {field} AS v FROM inventory WHERE inventory_id=?', (inv_id,))['v']
        self._x(f'UPDATE inventory SET {field}={field}+? WHERE inventory_id=?', (delta, inv_id))
        self._audit('inventory', inv_id, field, before, before + delta, actor, reason)

    def _cutoff(self, client, day):
        return datetime.combine(day, time.fromisoformat(client['cutoff_time']))

    # ------------------------------------------------------------ 초기화
    def reset(self):
        """DB를 지우고 마스터 데이터(화주·상품·매핑·로케이션·재고·입고예정)를 시드에서 다시 적재합니다.
        주문은 이벤트 재생(IMPORT_ORDERS) 또는 CSV 업로드로 들어옵니다."""
        with self.lock:
            self.db.executescript(''.join(f'DROP TABLE IF EXISTS {t};' for t in TABLES) + SCHEMA)
            self._events_cache = None
            with self.tx():
                self._load_masters()
                self._set_state('clock', START_CLOCK)
                self._set_state('last_seq', 0)
                self._set_state('manual_ops', 0)
                self._set_state('schema_version', SCHEMA_VERSION)
                self._set_state('initial_on_hand', self._one('SELECT SUM(on_hand_qty) s FROM inventory')['s'])

    def _load_masters(self):
        s = lambda name: _read_csv(os.path.join(self.seed_dir, name))
        for r in s('clients.csv'):
            self._x('INSERT INTO clients VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (r['client_id'], r['name'], r['category'], r['storage_type'], r['cutoff_time'],
                     1 if r['partial_ship_allowed'] == 'Y' else 0,
                     int(r['min_remaining_days']) if r['min_remaining_days'] else None, r['default_carrier']))
        for r in s('products.csv'):
            self._x('INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)',
                    (r['sku'], r['client_id'], r['name'], r['storage_type'], 1 if r['lot_managed'] == 'Y' else 0,
                     r['barcode']))
        owner = {r['sku']: r['client_id'] for r in self._q('SELECT sku, client_id FROM products')}
        bad = [f'{r["client_id"]}/{r["channel"]}/{r["channel_product_code"]} → {r["sku"]}'
               f'(소유 {owner.get(r["sku"], "없음")})'
               for r in s('channel_product_map.csv') if owner.get(r['sku']) != r['client_id']]
        if bad:  # 잘못된 마스터는 적재하지 않고 바로 알림 (DB 복합 FK도 같은 규칙을 강제)
            raise ValueError('마스터 데이터 오류: 매핑 화주와 SKU 소유 화주가 다름 — ' + '; '.join(bad[:10]))
        for r in s('channel_product_map.csv'):
            self._x('INSERT INTO channel_product_map VALUES (?, ?, ?, ?)',
                    (r['channel'], r['channel_product_code'], r['client_id'], r['sku']))
        for r in s('locations.csv'):
            self._x('INSERT INTO locations VALUES (?, ?, ?)', (r['location_code'], r['zone'], r['storage_type']))
        for r in s('inventory.csv'):
            self._x('INSERT INTO inventory VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (int(r['inventory_id']), r['sku'], r['location_code'], r['lot_no'] or None,
                     r['expiry_date'] or None, int(r['on_hand_qty']), int(r['allocated_qty']), int(r['hold_qty'])))
        for r in s('inbound_schedule.csv'):
            self._x('INSERT INTO inbound_schedule(inbound_id, sku, expected_date, qty) VALUES (?, ?, ?, ?)',
                    (r['inbound_id'], r['sku'], r['expected_date'], int(r['qty'])))

    # ------------------------------------------------------------ 예외 생성
    def _raise(self, oid, rule, detail, actor='SYSTEM', ref=None):
        p = self.policies[rule]
        if rule != 'E07':  # 주문·규칙당 열린 예외는 1건 (E03은 같은 약속출고일에 1번만)
            if self._q("SELECT 1 FROM exceptions WHERE order_id=? AND rule_id=? AND "
                       "(status IN ('OPEN', 'IN_PROGRESS') OR (ref IS NOT NULL AND ref=?))", (oid, rule, ref)):
                return None
        o = self._order(oid)
        now = self.now()
        if p.get('sla_minutes'):
            due = (now + timedelta(minutes=p['sla_minutes'])).strftime(TS)
        elif rule == 'E03':
            due = self._cutoff(self._client(o['client_id']), now.date()).strftime(TS)
        else:
            due = None
        n = self._one('SELECT COUNT(*) c FROM exceptions')['c'] + 1
        eid = f'EX-{n:04d}'
        status = 'LOGGED' if rule == 'E07' else 'OPEN'
        self._x('INSERT INTO exceptions(exception_id, order_id, rule_id, severity, blocking, detected_at, due_at, '
                'status, ref, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (eid, oid, rule, p['severity'], 1 if p['blocking'] else 0, self._now_s(), due, status, ref,
                 json.dumps(detail, ensure_ascii=False)))
        self._audit('exception', eid, 'status', None, status, actor, f'{rule} {p["name"]} ({oid})')
        if p['blocking'] and o['status'] != 'ON_HOLD':
            self._x('UPDATE orders SET hold_from_status=? WHERE order_id=?', (o['status'], oid))
            self._set_status(oid, 'ON_HOLD', actor, f'{rule} {p["name"]}')
        return eid

    def _reject(self, oid, attempt, actor):
        o = self._order(oid)
        self._raise(oid, 'E07', {'attempt': attempt, 'status': o['status']}, actor)
        return f'허용되지 않는 요청: {oid} {attempt} (현재 상태 {o["status"]})'

    # ------------------------------------------------------------ 검증(T1)·할당(T2)
    def _validate(self, oid, actor):
        o = self._order(oid)
        if o['status'] != 'RECEIVED':
            return
        unmapped, owner_mismatch = [], []
        for l in self._items(oid):
            # 매핑은 화주별: 다른 화주가 같은 채널에서 같은 상품코드를 써도 서로 영향을 주지 않음
            m = self._one('SELECT m.sku, p.client_id AS owner FROM channel_product_map m '
                          'LEFT JOIN products p ON p.sku=m.sku '
                          'WHERE m.client_id=? AND m.channel=? AND m.channel_product_code=?',
                          (o['client_id'], o['channel'], l['channel_product_code']))
            sku = m['sku'] if m and m['owner'] == o['client_id'] else None
            if m and not sku:  # 제약을 우회해 들어온 잘못된 매핑도 다른 화주 상품을 출고하지 않도록 한 번 더 확인
                owner_mismatch.append(f'{l["channel_product_code"]}→{m["sku"]}')
            self._x('UPDATE order_items SET sku=? WHERE order_id=? AND line_no=?', (sku, oid, l['line_no']))
            if not sku:
                unmapped.append(l['channel_product_code'])
        bad_addr = not re.fullmatch(r'\d{5}', o['zipcode'] or '') or not (o['address'] or '').strip()
        if unmapped:
            self._raise(oid, 'E02', {'channel': o['channel'], 'unmapped_codes': unmapped,
                                     'owner_mismatch': owner_mismatch}, actor)
        if bad_addr:
            self._raise(oid, 'E05', {'zipcode': o['zipcode'], 'address': o['address']}, actor)
        if not unmapped and not bad_addr:
            self._set_status(oid, 'CONFIRMED', actor, '상품매핑·주소 검증 통과')

    def _remaining_days(self, r):
        return (date.fromisoformat(r['expiry_date']) - self.now().date()).days if r['expiry_date'] else None

    def _is_expired(self, r):
        d = self._remaining_days(r)
        return d is not None and d < MIN_SELLABLE_DAYS

    def _lot_view(self, r, md):
        """화면 표시용: 잔여일, 만료 여부, 기본 정책상 출고 가능 여부."""
        r['remaining_days'] = self._remaining_days(r)
        r['expired'] = self._is_expired(r)
        r['usable'] = not r['expired'] and (r['remaining_days'] is None or not md or r['remaining_days'] >= md)
        return r

    def _lots(self, sku, client, ignore_expiry=False, taken=None):
        """출고 가능 Lot(FEFO 정렬)과 가용재고 합계(만료 Lot 제외)를 돌려줍니다.

        ignore_expiry는 화주의 '최소 잔여일' 기준만 완화합니다(E06 승인).
        유통기한이 지난 Lot은 어떤 승인으로도 출고하지 않으므로 처음부터 제외합니다.
        """
        taken = taken or {}
        rows = self._q('SELECT *, on_hand_qty-allocated_qty-hold_qty AS available FROM inventory WHERE sku=?', (sku,))
        for r in rows:
            r['available'] -= taken.get(r['inventory_id'], 0)
        rows = [r for r in rows if r['available'] > 0 and not self._is_expired(r)]
        md = client['min_remaining_days']

        def usable(r):
            return ignore_expiry or not md or not r['expiry_date'] or self._remaining_days(r) >= md
        usable_rows = sorted([r for r in rows if usable(r)],
                             key=lambda r: (r['expiry_date'] or '9999-12-31', r['inventory_id']))
        return usable_rows, sum(r['available'] for r in rows)

    def _line_plan(self, oid):
        """주문 전체의 할당 계획. 라인 순서대로 재고를 예약하므로, 같은 SKU가 여러 라인에 있어도
        뒤 라인은 앞 라인이 쓴 재고를 뺀 나머지만 봅니다. 할당(_allocate)과 부분출고 판단(_short_lines)이
        같은 계획을 쓰므로 둘의 계산이 어긋나지 않습니다.

        라인마다 qty(출고수량), usable(이 라인이 쓸 수 있는 출고 가능 재고), total(만료 제외 가용),
        take([(inventory_id, 수량)] — 부족하면 usable만큼만), kind(부족 유형)를 돌려줍니다.
        kind: None(충분) / 'E06'(임박 Lot까지 치면 충분하고 임박 출고 여부 미결정) / 'E01'(실제 부족 또는 임박 거절).
        라인의 near_expiry_ok=1(E06 승인)이면 그 라인만 최소 잔여일 기준을 완화합니다. 승인은 라인에 저장되므로
        다른 예외(E01)를 먼저 처리하거나 다시 할당해도 유지됩니다.
        """
        client = self._client(self._order(oid)['client_id'])
        taken, out = {}, []
        for l in self._items(oid):
            if not l['sku'] or l['ship_qty'] == 0:
                continue
            usable, total = self._lots(l['sku'], client, l['near_expiry_ok'] == 1, taken)
            u = sum(r['available'] for r in usable)
            rest, take = min(l['ship_qty'], u), []
            for r in usable:
                if rest == 0:
                    break
                q = min(rest, r['available'])
                take.append((r['inventory_id'], q))
                taken[r['inventory_id']] = taken.get(r['inventory_id'], 0) + q
                rest -= q
            kind = None
            if u < l['ship_qty']:
                kind = 'E06' if total >= l['ship_qty'] and l['near_expiry_ok'] is None else 'E01'
            out.append({'line_no': l['line_no'], 'sku': l['sku'], 'qty': l['ship_qty'], 'usable': u, 'total': total,
                        'take': take, 'kind': kind, 'near_expiry_ok': l['near_expiry_ok']})
        return out

    def _allocate(self, oid, actor):
        o = self._order(oid)
        if o['status'] != 'CONFIRMED':
            return False
        plan = self._line_plan(oid)
        fails = {}
        for p in plan:
            if p['kind']:
                fails.setdefault(p['kind'], []).append(
                    {k: p[k] for k in ('line_no', 'sku', 'qty', 'usable', 'total')})
        if fails:  # 전량 할당 원칙: 한 라인이라도 부족하면 할당하지 않고 예외로 보냄
            for rule in sorted(fails):
                self._raise(oid, rule, {'lines': fails[rule]}, actor)
            return False
        for line_no, inv_id, q in [(p['line_no'], i, q) for p in plan for i, q in p['take']]:
            self._x("INSERT INTO allocations(order_id, line_no, inventory_id, qty, status) VALUES (?, ?, ?, ?, 'ACTIVE')",
                    (oid, line_no, inv_id, q))
            self._inv_delta(inv_id, 'allocated_qty', q, actor, f'{oid} 할당')
        approved = any(p['near_expiry_ok'] == 1 for p in plan)
        self._set_status(oid, 'ALLOCATED', actor, '재고 할당(FEFO)' + (' · 임박 Lot 승인 라인 포함' if approved else ''))
        return True

    def _release_allocations(self, oid, actor, reason):
        for a in self._q("SELECT * FROM allocations WHERE order_id=? AND status='ACTIVE'", (oid,)):
            self._inv_delta(a['inventory_id'], 'allocated_qty', -a['qty'], actor, reason)
            self._x("UPDATE allocations SET status='RELEASED' WHERE allocation_id=?", (a['allocation_id'],))

    def _cancel(self, oid, actor, reason):
        sh = self._one('SELECT * FROM shipments WHERE order_id=?', (oid,))
        if sh and sh['picked_at'] and not sh['shipped_at']:
            # 이미 상품을 꺼낸 주문: 상품이 실제로 로케이션에 돌아왔는지 확인(confirm_restock)한 뒤에 재고를 풉니다.
            # 그 전까지는 할당재고로 묶어 두어, 손에 들린 상품을 다른 주문에 할당하지 않습니다.
            for a in self._q("SELECT * FROM allocations WHERE order_id=? AND status='ACTIVE'", (oid,)):
                self._x("UPDATE allocations SET status='RETURNING' WHERE allocation_id=?", (a['allocation_id'],))
                self._audit('allocation', a['allocation_id'], 'status', 'ACTIVE', 'RETURNING', actor,
                            f'{oid} 피킹 상품 원위치 대기 ({a["qty"]}개)')
        else:
            self._release_allocations(oid, actor, f'{oid} 할당 해제({reason})')
        if sh and not sh['shipped_at'] and not sh['cancelled_at']:  # 출고 중단: 발행된 송장도 취소
            self._x('UPDATE shipments SET cancelled_at=?, cancel_reason=? WHERE order_id=?',
                    (self._now_s(), reason, oid))
            self._audit('shipment', sh['shipment_id'], 'cancelled_at', None, self._now_s(), actor,
                        f'송장 취소 {sh["tracking_no"] or "(송장 미발행, 피킹 작업 취소)"}')
        self._x('UPDATE orders SET waiting_inbound=0 WHERE order_id=?', (oid,))
        for e in self._q("SELECT * FROM exceptions WHERE order_id=? AND status IN ('OPEN', 'IN_PROGRESS')", (oid,)):
            self._x("UPDATE exceptions SET status='RESOLVED', resolution_code='ORDER_CANCELLED', resolution_note=?, "
                    "resolved_at=?, assignee=COALESCE(assignee, ?) WHERE exception_id=?",
                    (reason, self._now_s(), actor, e['exception_id']))
            self._audit('exception', e['exception_id'], 'status', e['status'], 'RESOLVED', actor, '주문 취소로 종료')
        self._x('UPDATE orders SET hold_from_status=NULL WHERE order_id=?', (oid,))
        self._set_status(oid, 'CANCELLED', actor, reason)

    def _set_cancel_result(self, o, result, actor, reason):
        """취소요청 처리 결과: PENDING(판단 대기) → ACCEPTED(취소) / REJECTED(출고 진행). 결정되면 요청 플래그를 내림."""
        self._x('UPDATE orders SET cancel_requested=?, cancel_result=? WHERE order_id=?',
                (1 if result == 'PENDING' else 0, result, o['order_id']))
        self._audit('order', o['order_id'], 'cancel_result', o['cancel_result'], result, actor, reason)

    def confirm_restock(self, oid, actor='OPERATOR'):
        """취소된 주문의 피킹 상품이 원래 로케이션에 돌아왔음을 확인하고 할당재고를 풉니다."""
        with self.tx():
            self._manual()
            self._order(oid)
            rows = self._q("SELECT a.*, i.location_code, i.lot_no FROM allocations a JOIN inventory i "
                           "ON i.inventory_id=a.inventory_id WHERE a.order_id=? AND a.status='RETURNING'", (oid,))
            if not rows:
                raise BadRequest(f'{oid}: 원위치 확인을 기다리는 상품이 없습니다')
            for a in rows:
                self._inv_delta(a['inventory_id'], 'allocated_qty', -a['qty'], actor,
                                f'{oid} 원위치 확인 {a["location_code"]} {a["lot_no"] or ""} ×{a["qty"]}')
                self._x("UPDATE allocations SET status='RELEASED' WHERE allocation_id=?", (a['allocation_id'],))
                self._audit('allocation', a['allocation_id'], 'status', 'RETURNING', 'RELEASED', actor, '원위치 확인')
            self._audit('order', oid, 'restock', None, sum(a['qty'] for a in rows), actor,
                        '피킹 상품 원위치 확인 → 재고 해제')
        return self._order(oid)

    def _advance(self, oid, actor):
        """보류 해제 뒤 자동 처리 단계(검증 → 할당)를 이어서 실행합니다."""
        if self._order(oid)['status'] == 'RECEIVED':
            self._validate(oid, actor)
        if self._order(oid)['status'] == 'CONFIRMED':
            self._allocate(oid, actor)

    def _try_release(self, oid, actor, reason):
        o = self._order(oid)
        if o['status'] != 'ON_HOLD':
            return False
        if self._q("SELECT 1 FROM exceptions WHERE order_id=? AND blocking=1 AND status IN ('OPEN', 'IN_PROGRESS')",
                   (oid,)):
            return False
        self._x('UPDATE orders SET hold_from_status=NULL WHERE order_id=?', (oid,))
        self._set_status(oid, o['hold_from_status'], actor, reason)
        self._advance(oid, actor)
        return True

    # ------------------------------------------------------------ 공개 동작
    def import_orders_csv(self, text, actor='SYSTEM'):
        """주문수집 CSV(주문 1건 = 여러 행)를 받아 RECEIVED로 등록하고 바로 검증(T1)합니다."""
        rows = list(csv.DictReader(io.StringIO(text.lstrip('﻿'))))
        need = {'order_id', 'client_id', 'channel', 'ordered_at', 'zipcode', 'address', 'promised_ship_date',
                'line_no', 'channel_product_code', 'qty'}
        if not rows or not need <= set(rows[0]):
            raise BadRequest('CSV 헤더 누락: ' + ', '.join(sorted(need - set(rows[0] if rows else []))))
        groups = {}
        for i, r in enumerate(rows, 2):  # 2 = 헤더 다음 행 번호 (엑셀 행 번호와 같음)
            r['_row'] = i
            groups.setdefault((r['order_id'] or '').strip(), []).append(r)
        created, skipped, rejected = [], [], []
        with self.tx():
            self._manual()
            for oid, rs in groups.items():
                errors = self._check_import_order(oid, rs)
                if errors:  # 정합성이 깨진 주문은 등록하지 않고 사유와 함께 돌려줌 (다른 주문은 계속 등록)
                    rejected.append({'order_id': oid or '(빈 주문번호)', 'rows': [r['_row'] for r in rs],
                                     'errors': errors})
                    continue
                if self._one('SELECT 1 FROM orders WHERE order_id=?', (oid,)):
                    skipped.append(oid)
                    continue
                r0 = rs[0]
                self._x('INSERT INTO orders(order_id, client_id, channel, ordered_at, recipient, zipcode, address, '
                        "promised_ship_date, status, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RECEIVED', ?)",
                        (oid, r0['client_id'], r0['channel'], r0['ordered_at'], r0.get('recipient'), r0['zipcode'],
                         r0['address'], r0['promised_ship_date'], self._now_s()))
                self._audit('order', oid, 'status', None, 'RECEIVED', actor, '주문수집')
                for r in rs:
                    qty = int(r['qty'])
                    self._x('INSERT INTO order_items(order_id, line_no, channel_product_code, product_name, qty, '
                            'ship_qty) VALUES (?, ?, ?, ?, ?, ?)',
                            (oid, int(r['line_no']), r['channel_product_code'], r.get('product_name'), qty, qty))
                created.append(oid)
            for oid in created:
                self._validate(oid, actor)
        held = self._one(f"SELECT COUNT(*) c FROM orders WHERE status='ON_HOLD' AND order_id IN "
                         f"({','.join('?' * len(created)) or 'NULL'})", created)['c']
        return {'created': len(created), 'skipped': skipped, 'held': held, 'rejected': rejected}

    # 같은 주문번호의 행들이 반드시 같은 값을 가져야 하는 주문 헤더 항목
    ORDER_HEADER = {'client_id': '화주', 'channel': '채널', 'ordered_at': '주문시각', 'recipient': '수령인',
                    'zipcode': '우편번호', 'address': '주소', 'promised_ship_date': '약속출고일'}

    def _check_import_order(self, oid, rs):
        """주문수집 파일의 주문 단위 정합성 검사. 오류 문구 목록을 돌려줍니다(비어 있으면 통과).

        우편번호·주소 오류는 여기서 거르지 않습니다. 형식은 맞는 데이터의 업무 예외이므로 E05로 처리합니다.
        """
        errors = []
        if not oid:
            return ['주문번호가 비어 있음']
        for key, label in self.ORDER_HEADER.items():
            values = sorted({(r.get(key) or '').strip() for r in rs})
            if len(values) > 1:
                errors.append(f'{label} 값이 행마다 다름: {" / ".join(v or "(빈값)" for v in values)}')
        r0 = rs[0]
        if not self._client(r0['client_id']):
            errors.append(f'등록되지 않은 화주: {r0["client_id"]!r}')
        try:
            datetime.strptime(r0['ordered_at'], TS)
        except (TypeError, ValueError):
            errors.append(f'주문시각 형식 오류: {r0["ordered_at"]!r} (예: 2026-10-26 09:30:00)')
        try:
            date.fromisoformat(r0['promised_ship_date'])
        except (TypeError, ValueError):
            errors.append(f'약속출고일 형식 오류: {r0["promised_ship_date"]!r} (예: 2026-10-26)')
        seen = set()
        for r in rs:
            try:
                ln = int(r['line_no'])
            except (TypeError, ValueError):
                errors.append(f'{r["_row"]}행: 라인번호 오류 {r["line_no"]!r}')
                ln = None
            if ln is not None:
                if ln in seen:
                    errors.append(f'{r["_row"]}행: 라인번호 {ln} 중복')
                seen.add(ln)
            try:
                if int(r['qty']) <= 0:
                    errors.append(f'{r["_row"]}행: 수량은 1 이상이어야 함 ({r["qty"]})')
            except (TypeError, ValueError):
                errors.append(f'{r["_row"]}행: 수량 오류 {r["qty"]!r}')
            if not (r.get('channel_product_code') or '').strip():
                errors.append(f'{r["_row"]}행: 채널 상품코드가 비어 있음')
        return errors

    def allocate_all(self, actor='SYSTEM'):
        with self.tx():
            self._manual()
            ok = 0
            # 우선 작업 주문이 같은 재고를 먼저 가져가도록 우선순위 → 주문시각 순으로 할당
            for o in self._q("SELECT order_id FROM orders WHERE status='CONFIRMED' "
                             'ORDER BY priority DESC, ordered_at, order_id'):
                ok += self._allocate(o['order_id'], actor)
        return {'allocated': ok}

    def allocate(self, oid, actor='OPERATOR'):
        err = None
        with self.tx():
            self._manual()
            o = self._order(oid)
            if o['status'] != 'CONFIRMED':
                err = self._reject(oid, f'{o["status"]} 상태에서 할당', actor)
            else:
                self._allocate(oid, actor)
        if err:
            raise Conflict(err)
        return self._order(oid)

    def transition(self, oid, to, actor='OPERATOR', carrier=None):
        """T3~T5 (피킹 시작 / 포장 완료 / 출고 확정). 그 밖의 전이는 차단하고 E07로 기록합니다."""
        err = None
        with self.tx():
            self._manual()
            o = self._order(oid)
            ship = self._one('SELECT * FROM shipments WHERE order_id=?', (oid,))
            if to not in STEP_FROM or STEP_FROM[to] != o['status']:
                err = self._reject(oid, f'{o["status"]} → {to}', actor)
            elif to == 'SHIPPED' and ship and ship['shipped_at']:
                err = self._reject(oid, '중복 출고', actor)
            elif to == 'SHIPPED' and (bad := self._ship_lot_problems(oid)):
                err = self._reject(oid, '출고 시점 유통기한 재검증 실패: ' + '; '.join(bad), actor)
            else:
                now = self._now_s()
                if to == 'PICKING':
                    self._x('INSERT INTO shipments(shipment_id, order_id, picker_id, picked_at) VALUES (?, ?, ?, ?) '
                            'ON CONFLICT(order_id) DO UPDATE SET picker_id=excluded.picker_id, '
                            'picked_at=excluded.picked_at', (f'SHP-{oid[4:]}', oid, actor, now))
                elif to == 'PACKED':
                    carrier = carrier or self._client(o['client_id'])['default_carrier']
                    tracking = f'{CARRIER_PREFIX.get(carrier, "XX")}{20261000000 + int(re.sub(r"[^0-9]", "", oid) or 0)}'
                    self._x('UPDATE shipments SET packed_at=?, carrier=?, tracking_no=? WHERE order_id=?',
                            (now, carrier, tracking, oid))
                else:
                    for a in self._q("SELECT * FROM allocations WHERE order_id=? AND status='ACTIVE'", (oid,)):
                        # 할당을 먼저 줄여야 '할당+보류 <= 실재고' 제약을 중간에도 지킴
                        self._inv_delta(a['inventory_id'], 'allocated_qty', -a['qty'], actor, f'{oid} 출고')
                        self._inv_delta(a['inventory_id'], 'on_hand_qty', -a['qty'], actor, f'{oid} 출고')
                        self._x("UPDATE allocations SET status='SHIPPED' WHERE allocation_id=?", (a['allocation_id'],))
                    self._x('UPDATE shipments SET shipped_at=? WHERE order_id=?', (now, oid))
                self._set_status(oid, to, actor, STEP_REASON[to])
        if err:
            raise Conflict(err)
        return self._order(oid)

    def _ship_lot_problems(self, oid):
        """출고 확정 직전 재검증: 할당 뒤 시간이 지나 만료됐거나 화주 최소 잔여일에 못 미치게 된 Lot.
        (그 라인에 E06 승인이 있으면 최소 잔여일 미달은 허용하지만, 만료는 어떤 경우에도 허용하지 않음)"""
        md = self._client(self._order(oid)['client_id'])['min_remaining_days']
        out = []
        for a in self._q('SELECT i.lot_no, i.expiry_date, it.near_expiry_ok FROM allocations a '
                         'JOIN inventory i ON i.inventory_id=a.inventory_id '
                         'JOIN order_items it ON it.order_id=a.order_id AND it.line_no=a.line_no '
                         "WHERE a.order_id=? AND a.status='ACTIVE' AND i.expiry_date IS NOT NULL", (oid,)):
            d = self._remaining_days(a)
            if d < MIN_SELLABLE_DAYS:
                out.append(f'{a["lot_no"]} 유통기한 {a["expiry_date"]} 만료(잔여 {d}일)')
            elif md and d < md and a['near_expiry_ok'] != 1:
                out.append(f'{a["lot_no"]} 잔여 {d}일 < 화주 기준 {md}일')
        return out

    def cancel_request(self, oid, actor='CHANNEL'):
        err = None
        with self.tx():
            self._manual()
            o = self._order(oid)
            st, origin = o['status'], o['hold_from_status']
            if st in PRE_WORK or (st == 'ON_HOLD' and origin in PRE_WORK):
                self._set_cancel_result(o, 'ACCEPTED', actor, '출고 작업 전 취소요청 → 즉시 취소')
                self._cancel(oid, actor, f'취소요청 ({origin or st} 단계, 출고 작업 전)')
            elif st in IN_WORK or (st == 'ON_HOLD' and origin in IN_WORK):
                self._x("UPDATE orders SET cancel_requested=1, cancel_result='PENDING' WHERE order_id=?", (oid,))
                self._audit('order', oid, 'cancel_result', o['cancel_result'], 'PENDING', actor, '취소요청 접수 (운영자 판단 대기)')
                self._raise(oid, 'E04', {'status_at_request': origin if st == 'ON_HOLD' else st}, actor)
            else:
                err = self._reject(oid, f'{st} 상태에서 취소요청', actor)
        if err:
            raise Conflict(err)
        return self._order(oid)

    def work_queue(self):
        """피킹 작업 대기열: 할당 완료·피킹 중 주문을 작업 순서대로 정렬합니다.

        순서: ① 우선 작업(E03 '우선 작업 지정') ② 오늘 출고분 중 화주 마감이 빠른 순 ③ 약속출고일 ④ 주문시각.
        """
        now = self.now()
        today = now.date().isoformat()
        rows = self.list_orders(limit=10000)
        rows = [o for o in rows if o['status'] in ('ALLOCATED', 'PICKING')]
        cutoff = {c['client_id']: c['cutoff_time'] for c in self._q('SELECT * FROM clients')}
        for o in rows:
            o['cutoff_time'] = cutoff[o['client_id']]
            o['due_today'] = o['promised_ship_date'] == today
            mins = int((self._cutoff({'cutoff_time': o['cutoff_time']}, now.date()) - now).total_seconds() // 60)
            o['minutes_to_cutoff'] = mins if o['due_today'] else None
        rows.sort(key=lambda o: (-o['priority'], not o['due_today'],
                                 o['cutoff_time'] if o['due_today'] else '99:99',
                                 o['promised_ship_date'], o['ordered_at'], o['order_id']))
        for i, o in enumerate(rows, 1):
            o['rank'] = i
        return rows

    def run_detection(self, actor='SYSTEM'):
        """E03 출고마감 위험 감지를 지금 기준시각으로 한 번 실행합니다.

        PoC에는 상시 스케줄러가 없습니다. 운영에서는 스케줄러가 10분마다 호출한다고 가정하고,
        PoC에서는 ① 시나리오 이벤트(events.csv의 RUN_DETECTION, 11:10~15:00 10분 간격) ②
        화면의 '감지 실행' 버튼 두 경로로만 호출합니다. 기준시각을 바꾸는 것만으로는 감지되지 않습니다.
        """
        new = []
        with self.tx():
            self._manual()
            now = self.now()
            today = now.date().isoformat()
            for o in self._q("SELECT * FROM orders WHERE status IN ('CONFIRMED', 'ALLOCATED', 'PICKING') "
                             'AND promised_ship_date=?', (today,)):
                client = self._client(o['client_id'])
                cutoff = self._cutoff(client, now.date())
                if now >= cutoff - timedelta(minutes=30):
                    eid = self._raise(o['order_id'], 'E03',
                                      {'status': o['status'], 'cutoff': client['cutoff_time']}, actor, ref=today)
                    if eid:
                        new.append(eid)
        return {'new_exceptions': new}

    # ------------------------------------------------------------ 처리 옵션
    def _short_lines(self, oid, kind=None):
        """부족한 라인. kind='E01'이면 실제 재고부족 라인만, 'E06'이면 임박 Lot 결정 대기 라인만.
        E01 처리(부분출고·입고 대기)는 E01 라인만 바꾸므로 E06 라인의 승인·결정에 영향을 주지 않습니다."""
        return [{k: p[k] for k in ('line_no', 'sku', 'qty', 'usable', 'total', 'kind')}
                for p in self._line_plan(oid) if p['kind'] and (kind is None or p['kind'] == kind)]

    def _inbound_cover(self, sku, until):
        r = self._one('SELECT COALESCE(SUM(qty), 0) q FROM inbound_schedule '
                      'WHERE sku=? AND received_at IS NULL AND expected_date<=?', (sku, until))
        return r['q']

    def _options(self, e):
        p = self.policies[e['rule_id']]
        o = self._order(e['order_id'])
        client = self._client(o['client_id'])
        out = []
        for opt in p['options']:
            enabled, why, params = True, '', []
            code = opt['code']
            if e['rule_id'] == 'E01':
                short = self._short_lines(o['order_id'], 'E01')
                if code == '1':
                    params = ['remainder']
                    # 부분출고 = 지금 출고할 수 있는 것만 먼저 보내기. 재고 0인 상품이 있어도 다른 상품을 보낼 수 있으면 허용
                    shippable = sum(min(p['usable'], p['qty']) for p in self._line_plan(o['order_id'])
                                    if p['kind'] != 'E06')
                    if not client['partial_ship_allowed']:
                        enabled, why = False, f'{client["client_id"]} 화주는 부분출고 불허'
                    elif shippable == 0:
                        enabled, why = False, '주문 전체에서 지금 출고할 수 있는 수량이 0이라 부분출고할 수 없음'
                elif code == '2':
                    gap = {}  # 같은 SKU가 여러 라인이면 부족분을 합쳐서 입고예정과 비교
                    for s in short:
                        gap[s['sku']] = gap.get(s['sku'], 0) + s['qty'] - s['usable']
                    lacking = [sku for sku, n in gap.items()
                               if self._inbound_cover(sku, o['promised_ship_date']) < n]
                    if o['waiting_inbound']:
                        enabled, why = False, '이미 입고 대기 중 (입고 처리 시 자동 재할당)'
                    elif lacking:
                        enabled, why = False, (f'약속출고일({o["promised_ship_date"]}) 이전 입고예정으로 부족분을 '
                                               f'채울 수 없음: {", ".join(lacking)}')
            elif e['rule_id'] == 'E02' and code == '1':
                params = ['mappings']
            elif e['rule_id'] == 'E03' and code == '1':
                if self.now() >= self._cutoff(client, self.now().date()):
                    enabled, why = False, '이미 마감시각이 지남'
            elif e['rule_id'] == 'E05' and code == '1':
                params = ['zipcode', 'address']
            out.append({'code': code, 'label': opt['label'], 'enable_if': opt['enable_if'], 'enabled': enabled,
                        'reason': why, 'params': params})
        return out

    def _close(self, e, code, actor, reason, status='RESOLVED'):
        label = next(o['label'] for o in self.policies[e['rule_id']]['options'] if o['code'] == code)
        self._x('UPDATE exceptions SET status=?, assignee=?, resolution_code=?, resolution_note=?, resolved_at=? '
                'WHERE exception_id=?',
                (status, actor, code, reason, self._now_s() if status == 'RESOLVED' else None, e['exception_id']))
        self._audit('exception', e['exception_id'], 'status', e['status'], status, actor,
                    f'옵션 {code} {label}' + (f' · {reason}' if reason else ''))

    def resolve(self, eid, option, actor='OPERATOR', reason='', params=None):
        params = params or {}
        with self.tx():
            self._manual()
            e = self._one('SELECT * FROM exceptions WHERE exception_id=?', (eid,))
            if not e:
                raise NotFound(f'예외 없음: {eid}')
            if e['status'] not in ('OPEN', 'IN_PROGRESS'):
                raise BadRequest(f'이미 종료된 예외입니다 ({e["status"]})')
            opts = {o['code']: o for o in self._options(e)}
            if option not in opts:
                raise BadRequest(f'없는 처리 옵션: {option}')
            if not opts[option]['enabled']:
                raise BadRequest(f'선택할 수 없는 옵션: {opts[option]["reason"]}')
            getattr(self, f'_resolve_{e["rule_id"]}')(e, option, actor, reason, params)
        return self.exception_detail(eid)

    def _resolve_E01(self, e, option, actor, reason, params):
        oid = e['order_id']
        short = self._short_lines(oid, 'E01')  # E06(임박 결정 대기) 라인은 건드리지 않음
        if option == '1':
            remainder = params.get('remainder') or 'CANCEL'
            if remainder not in ('CANCEL', 'BACKORDER'):
                raise BadRequest('잔량 처리는 CANCEL(잔량 취소) 또는 BACKORDER(백오더) 중 하나입니다')
            label = '잔량 취소' if remainder == 'CANCEL' else '잔량 백오더'
            self._x('UPDATE orders SET waiting_inbound=0 WHERE order_id=?', (oid,))  # 입고 대기 중 변경한 경우
            for s in short:
                rest = s['qty'] - s['usable']
                self._x('UPDATE order_items SET ship_qty=?, short_qty=short_qty+?, short_action=? '
                        'WHERE order_id=? AND line_no=?', (s['usable'], rest, remainder, oid, s['line_no']))
                self._audit('order_item', f'{oid}#{s["line_no"]}', 'ship_qty', s['qty'], s['usable'], actor,
                            f'부분출고 승인 · 미출고 {rest} {label}')
            self._close(e, option, actor, reason)
            self._try_release(oid, actor, f'E01 부분출고 승인 ({label})')
            if remainder == 'BACKORDER':
                self._create_backorder(oid, short, actor)
        elif option == '2':
            # 아직 들어오지 않은 재고로 할당하지 않음: 입고 대기로 두고, 입고 처리(process_inbound) 때 재할당
            self._x('UPDATE orders SET waiting_inbound=1 WHERE order_id=?', (oid,))
            self._audit('order', oid, 'waiting_inbound', 0, 1, actor, '입고 대기 (입고 처리 시 자동 재할당)')
            self._close(e, option, actor, reason or '입고 대기', status='IN_PROGRESS')
        else:
            self._close(e, option, actor, reason)
            self._cancel(oid, actor, 'E01 화주 확인 후 취소')

    def _create_backorder(self, oid, short, actor):
        o = self._order(oid)
        n = self._one('SELECT COUNT(*) c FROM orders WHERE parent_order_id=?', (oid,))['c'] + 1
        bid = f'{oid}-B{n}'
        self._x('INSERT INTO orders(order_id, client_id, channel, ordered_at, recipient, zipcode, address, '
                'promised_ship_date, status, imported_at, parent_order_id) '
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RECEIVED', ?, ?)",
                (bid, o['client_id'], o['channel'], o['ordered_at'], o['recipient'], o['zipcode'], o['address'],
                 o['promised_ship_date'], self._now_s(), oid))
        self._audit('order', bid, 'status', None, 'RECEIVED', actor, f'{oid} 부분출고 잔량 백오더')
        items = {l['line_no']: l for l in self._items(oid)}
        for i, s in enumerate(short, 1):
            l = items[s['line_no']]
            rest = s['qty'] - s['usable']
            self._x('INSERT INTO order_items(order_id, line_no, channel_product_code, product_name, qty, ship_qty) '
                    'VALUES (?, ?, ?, ?, ?, ?)', (bid, i, l['channel_product_code'], l['product_name'], rest, rest))
        self._validate(bid, actor)
        self._allocate(bid, actor)  # 재고가 없으면 E01로 보류되어 입고를 기다림
        return bid

    def _receive_row(self, ib, actor):
        prod = self._one('SELECT * FROM products WHERE sku=?', (ib['sku'],))
        loc = self._one('SELECT location_code FROM inventory WHERE sku=? ORDER BY inventory_id LIMIT 1', (ib['sku'],))
        loc = loc['location_code'] if loc else self._one(
            'SELECT location_code FROM locations WHERE storage_type=? LIMIT 1', (prod['storage_type'],))['location_code']
        expiry = (self.now().date() + timedelta(days=30)).isoformat() if prod['lot_managed'] else None
        cur = self._x('INSERT INTO inventory(sku, location_code, lot_no, expiry_date, on_hand_qty) '
                      'VALUES (?, ?, ?, ?, ?)', (ib['sku'], loc, f'IN-{ib["inbound_id"]}', expiry, ib['qty']))
        self._audit('inventory', cur.lastrowid, 'on_hand_qty', 0, ib['qty'], actor,
                    f'입고 처리 {ib["inbound_id"]}' + (' (유통기한은 입고일+30일로 가정)' if expiry else ''))
        self._x('UPDATE inbound_schedule SET received_at=? WHERE inbound_id=?', (self._now_s(), ib['inbound_id']))

    def process_inbound(self, actor='SYSTEM'):
        """입고예정일이 기준일 이하인 입고를 반영하고, 입고 대기 주문을 재할당합니다."""
        with self.tx():
            self._manual()
            today = self.now().date().isoformat()
            received = []
            for ib in self._q('SELECT * FROM inbound_schedule WHERE received_at IS NULL AND expected_date<=? '
                              'ORDER BY expected_date, inbound_id', (today,)):
                self._receive_row(ib, actor)
                received.append(ib['inbound_id'])
            released, still = [], []
            for o in self._q('SELECT order_id FROM orders WHERE waiting_inbound=1 ORDER BY ordered_at, order_id'):
                oid = o['order_id']
                if self._short_lines(oid, 'E01'):
                    still.append(oid)
                    continue
                self._x('UPDATE orders SET waiting_inbound=0 WHERE order_id=?', (oid,))
                self._audit('order', oid, 'waiting_inbound', 1, 0, actor, '입고 완료')
                e = self._one("SELECT * FROM exceptions WHERE order_id=? AND rule_id='E01' AND status='IN_PROGRESS' "
                              'ORDER BY exception_id DESC', (oid,))
                if e:
                    self._close(e, '2', actor, '입고 완료 → 재할당')
                if self._try_release(oid, actor, 'E01 입고 완료 후 재할당'):
                    released.append(oid)
        return {'received': received, 'released': released, 'still_waiting': still}

    def _resolve_E02(self, e, option, actor, reason, params):
        oid = e['order_id']
        if option == '2':
            return self._close(e, option, actor, reason or '화주 문의 중', status='IN_PROGRESS')
        o = self._order(oid)
        mappings = params.get('mappings') or {}
        for l in self._items(oid):
            if l['sku']:
                continue
            sku = mappings.get(l['channel_product_code'])
            p = self._one('SELECT * FROM products WHERE sku=?', (sku,)) if sku else None
            if not p or p['client_id'] != o['client_id']:
                raise BadRequest(f'{l["channel_product_code"]}에 연결할 {o["client_id"]} 화주 SKU를 지정하세요')
            self._x('INSERT OR REPLACE INTO channel_product_map VALUES (?, ?, ?, ?)',
                    (o['channel'], l['channel_product_code'], o['client_id'], sku))
            self._audit('mapping', f'{o["channel"]}:{l["channel_product_code"]}', 'sku', None, sku, actor, '매핑 등록')
        self._close(e, option, actor, reason)
        self._try_release(oid, actor, 'E02 매핑 등록 후 재검증')

    def _resolve_E03(self, e, option, actor, reason, params):
        oid = e['order_id']
        o = self._order(oid)
        if option == '1':
            self._x('UPDATE orders SET priority=1 WHERE order_id=?', (oid,))
            self._audit('order', oid, 'priority', o['priority'], 1, actor, '우선 작업 지정')
        else:
            nxt = (date.fromisoformat(o['promised_ship_date']) + timedelta(days=1)).isoformat()
            self._x('UPDATE orders SET promised_ship_date=? WHERE order_id=?', (nxt, oid))
            self._audit('order', oid, 'promised_ship_date', o['promised_ship_date'], nxt, actor, '익일 출고 전환')
        self._close(e, option, actor, reason)

    def _resolve_E04(self, e, option, actor, reason, params):
        oid = e['order_id']
        self._close(e, option, actor, reason)
        o = self._order(oid)
        if option == '1':
            self._set_cancel_result(o, 'ACCEPTED', actor, '취소요청 수락: 출고 중단')
            self._cancel(oid, actor, 'E04 출고 중단 (송장 취소, 원위치 확인 후 재고 해제)')
        else:
            self._set_cancel_result(o, 'REJECTED', actor, '취소요청 거절: 출고 진행, 고객 반품 안내')
            self._try_release(oid, actor, 'E04 출고 진행 (고객 반품 안내)')

    def _resolve_E05(self, e, option, actor, reason, params):
        oid = e['order_id']
        if option == '2':
            return self._close(e, option, actor, reason or '화주 문의 중', status='IN_PROGRESS')
        z, a = (params.get('zipcode') or '').strip(), (params.get('address') or '').strip()
        if not re.fullmatch(r'\d{5}', z) or not a:
            raise BadRequest('우편번호는 숫자 5자리, 주소는 필수입니다')
        o = self._order(oid)
        self._x('UPDATE orders SET zipcode=?, address=? WHERE order_id=?', (z, a, oid))
        self._audit('order', oid, 'zipcode', o['zipcode'], z, actor, '주소 수정')
        self._audit('order', oid, 'address', o['address'], a, actor, '주소 수정')
        self._close(e, option, actor, reason)
        self._try_release(oid, actor, 'E05 주소 수정 후 재검증')

    def _resolve_E06(self, e, option, actor, reason, params):
        oid = e['order_id']
        flag = 1 if option == '1' else 0
        for s in self._short_lines(oid, 'E06'):  # 결정을 라인에 저장 → 다른 예외를 처리하거나 재할당해도 유지
            self._x('UPDATE order_items SET near_expiry_ok=? WHERE order_id=? AND line_no=?', (flag, oid, s['line_no']))
            self._audit('order_item', f'{oid}#{s["line_no"]}', 'near_expiry_ok', None, flag, actor,
                        '임박 Lot 출고 승인' if flag else '임박 Lot 출고 거절 → 재고부족으로 처리')
        if option == '1':
            self._close(e, option, actor, reason)
            self._try_release(oid, actor, 'E06 임박 Lot 출고 승인')
        else:
            self._raise(oid, 'E01', {'lines': self._short_lines(oid, 'E01'), 'from': e['exception_id']}, actor)
            self._close(e, option, actor, reason)

    # ------------------------------------------------------------ 이벤트 재생
    def events(self):
        if self._events_cache is None:
            self._events_cache = [dict(e, seq=int(e['seq']))
                                  for e in _read_csv(os.path.join(self.seed_dir, 'events.csv'))]
        return self._events_cache

    def replay(self, until=None):
        """events.csv를 마지막 재생 위치부터 until 시각까지 순서대로 실행합니다."""
        if until:
            try:
                datetime.strptime(until, TS)
            except (TypeError, ValueError):
                raise BadRequest(f'재생 시각 형식 오류: {until!r} (예: 2026-10-26 11:30:00)')
        events = self.events()
        done, conflicts = 0, []
        with self.tx():
            last = int(self._state('last_seq', 0))
            self._replaying = True
            try:
                for e in events:
                    if e['seq'] <= last:
                        continue
                    if until and e['event_at'] > until:
                        break
                    self._set_state('clock', max(e['event_at'], self._now_s()))
                    try:
                        self._dispatch(e)
                    except Conflict as x:
                        conflicts.append(f'#{e["seq"]} {x}')
                    except NotFound as x:
                        conflicts.append(f'#{e["seq"]} {x}')
                    self._set_state('last_seq', e['seq'])
                    done += 1
                if until and until > self._now_s():
                    self._set_state('clock', until)
            finally:
                self._replaying = False
        return {'executed': done, 'conflicts': conflicts, 'clock': self._now_s(),
                'last_seq': int(self._state('last_seq', 0)), 'total_events': len(events)}

    def _dispatch(self, e):
        t, oid, actor = e['event_type'], e['order_id'], e['actor'] or 'SYSTEM'
        if t == 'IMPORT_ORDERS':
            with open(os.path.join(self.seed_dir, 'orders_import.csv'), encoding='utf-8-sig') as f:
                self.import_orders_csv(f.read(), actor)
        elif t == 'ALLOCATE_ALL':
            self.allocate_all(actor)
        elif t == 'START_PICKING':
            self.transition(oid, 'PICKING', actor)
        elif t == 'PACK':
            self.transition(oid, 'PACKED', actor, e.get('carrier') or None)
        elif t == 'SHIP':
            self.transition(oid, 'SHIPPED', actor)
        elif t == 'CANCEL_REQUEST':
            self.cancel_request(oid, actor)
        elif t == 'RUN_DETECTION':
            self.run_detection(actor)
        else:
            raise BadRequest(f'알 수 없는 이벤트 유형: {t}')

    # ------------------------------------------------------------ 조회
    def summary(self):
        status = {r['status']: r['c'] for r in self._q('SELECT status, COUNT(*) c FROM orders GROUP BY status')}
        open_exc = {r['rule_id']: r['c'] for r in self._q(
            "SELECT rule_id, COUNT(*) c FROM exceptions WHERE status IN ('OPEN', 'IN_PROGRESS') GROUP BY rule_id")}
        last = int(self._state('last_seq', 0))
        events = self.events()
        nxt = next((e for e in events if e['seq'] > last), None)
        return {
            'clock': self._now_s(), 'last_seq': last, 'total_events': len(events),
            'next_event': {'at': nxt['event_at'], 'type': nxt['event_type']} if nxt else None,
            'manual_ops': int(self._state('manual_ops', 0)),
            'orders_total': sum(status.values()), 'status': status, 'open_exceptions': open_exc,
            'e07_logged': self._one("SELECT COUNT(*) c FROM exceptions WHERE rule_id='E07'")['c'],
            'restock_pending': self._one("SELECT COUNT(DISTINCT order_id) c FROM allocations "
                                         "WHERE status='RETURNING'")['c'],
            'priority_orders': self._one("SELECT COUNT(*) c FROM orders WHERE priority=1 "
                                         "AND status IN ('CONFIRMED', 'ALLOCATED', 'PICKING')")['c'],
            'clients': self._q('SELECT * FROM clients ORDER BY client_id'),
            'policies': {k: {'name': v['name'], 'severity': v['severity'], 'blocking': v['blocking']}
                         for k, v in self.policies.items()},
        }

    def list_orders(self, status=None, client=None, q=None, limit=500):
        sql = ("SELECT o.*, (SELECT GROUP_CONCAT(rule_id) FROM exceptions e WHERE e.order_id=o.order_id "
               "AND e.status IN ('OPEN', 'IN_PROGRESS')) AS open_rules, "
               "(SELECT SUM(qty) FROM order_items i WHERE i.order_id=o.order_id) AS total_qty, "
               "(SELECT COUNT(*) FROM order_items i WHERE i.order_id=o.order_id) AS line_count, "
               "(SELECT COALESCE(SUM(qty), 0) FROM allocations a WHERE a.order_id=o.order_id "
               "AND a.status='RETURNING') AS restock_pending "
               "FROM orders o WHERE 1=1")
        args = []
        if status:
            sql += ' AND o.status=?'
            args.append(status)
        if client:
            sql += ' AND o.client_id=?'
            args.append(client)
        if q:
            sql += ' AND (o.order_id LIKE ? OR o.recipient LIKE ?)'
            args += [f'%{q}%', f'%{q}%']
        sql += ' ORDER BY o.priority DESC, o.ordered_at, o.order_id LIMIT ?'  # 우선 작업 주문을 맨 위에
        args.append(limit)
        return self._q(sql, args)

    def order_detail(self, oid):
        o = self._order(oid)
        items = self._items(oid)
        allocs = self._q('SELECT a.*, i.sku, i.location_code, i.lot_no, i.expiry_date FROM allocations a '
                         'JOIN inventory i ON i.inventory_id=a.inventory_id WHERE a.order_id=? ORDER BY allocation_id',
                         (oid,))
        for l in items:
            l['allocations'] = [a for a in allocs if a['line_no'] == l['line_no']]
        exc = self._q('SELECT * FROM exceptions WHERE order_id=? ORDER BY detected_at, exception_id', (oid,))
        ids = [e['exception_id'] for e in exc]
        ship = self._one('SELECT * FROM shipments WHERE order_id=?', (oid,))
        audit = self._q("SELECT * FROM audit_log WHERE (entity='order' AND entity_id=?) OR "
                        "(entity='order_item' AND entity_id LIKE ?) OR (entity='shipment' AND entity_id=?) OR "
                        f"(entity='exception' AND entity_id IN ({','.join('?' * len(ids)) or 'NULL'})) "
                        "ORDER BY log_id", [oid, f'{oid}#%', ship['shipment_id'] if ship else ''] + ids)
        actions = []
        for to, frm in STEP_FROM.items():
            if o['status'] == frm:
                actions.append({'action': 'transition', 'to': to, 'label': STEP_REASON[to]})
        if o['status'] == 'CONFIRMED':
            actions.append({'action': 'allocate', 'label': '재고 할당'})
        if o['status'] not in ('SHIPPED', 'CANCELLED'):
            actions.append({'action': 'cancel-request', 'label': '취소요청'})
        restock = sum(a['qty'] for a in allocs if a['status'] == 'RETURNING')
        if restock:
            actions.append({'action': 'confirm-restock', 'label': f'원위치 확인 ({restock}개)'})
        return {'order': o, 'client': self._client(o['client_id']), 'items': items, 'shipment': ship,
                'backorders': self._q('SELECT order_id, status FROM orders WHERE parent_order_id=? ORDER BY order_id',
                                      (oid,)),
                'exceptions': exc, 'audit': audit, 'actions': actions}

    def list_exceptions(self, status='open', rule=None):
        sql = ('SELECT e.*, o.client_id, o.status AS order_status, o.promised_ship_date FROM exceptions e '
               'JOIN orders o ON o.order_id=e.order_id WHERE 1=1')
        args = []
        if status == 'open':
            sql += " AND e.status IN ('OPEN', 'IN_PROGRESS')"
        elif status and status != 'all':
            sql += ' AND e.status=?'
            args.append(status)
        if rule:
            sql += ' AND e.rule_id=?'
            args.append(rule)
        rows = self._q(sql, args)
        now = self.now()
        for r in rows:
            r['name'] = self.policies[r['rule_id']]['name']
            r['minutes_left'] = (int((datetime.strptime(r['due_at'], TS) - now).total_seconds() // 60)
                                 if r['due_at'] else None)
        rows.sort(key=lambda r: (r['status'] not in ('OPEN', 'IN_PROGRESS'), SEVERITY_RANK.get(r['severity'], 9),
                                 r['due_at'] or '9999', r['exception_id']))
        return rows

    def exception_detail(self, eid):
        e = self._one('SELECT * FROM exceptions WHERE exception_id=?', (eid,))
        if not e:
            raise NotFound(f'예외 없음: {eid}')
        e['detail'] = json.loads(e['detail'] or '{}')
        e['name'] = self.policies[e['rule_id']]['name']
        o = self._order(e['order_id'])
        client = self._client(o['client_id'])
        lines = []
        plan = {p['line_no']: p for p in self._line_plan(o['order_id'])}  # 같은 SKU 여러 라인도 라인별로 계산
        for l in self._items(o['order_id']):
            lots = []
            if l['sku']:
                lots = [self._lot_view(r, client['min_remaining_days']) for r in self._q(
                    'SELECT *, on_hand_qty-allocated_qty-hold_qty AS available FROM inventory '
                    "WHERE sku=? ORDER BY COALESCE(expiry_date, '9999'), inventory_id", (l['sku'],))]
            l['lots'] = lots
            p = plan.get(l['line_no'])
            l['usable_available'] = p['usable'] if p else 0
            l['short_kind'] = p['kind'] if p else None
            l['total_available'] = p['total'] if p else 0
            l['inbound'] = self._q('SELECT * FROM inbound_schedule WHERE sku=? ORDER BY expected_date',
                                   (l['sku'],)) if l['sku'] else []
            lines.append(l)
        open_ = e['status'] in ('OPEN', 'IN_PROGRESS')
        return {'exception': e, 'policy': self.policies[e['rule_id']], 'order': o, 'client': client,
                'lines': lines, 'options': self._options(e) if open_ else [],
                'products': self._q('SELECT sku, name FROM products WHERE client_id=? ORDER BY sku',
                                    (o['client_id'],)) if e['rule_id'] == 'E02' else [],
                'audit': self._q("SELECT * FROM audit_log WHERE entity='exception' AND entity_id=? ORDER BY log_id",
                                 (eid,))}

    def inventory(self, client=None, q=None):
        sql = 'SELECT p.*, c.name AS client_name FROM products p JOIN clients c ON c.client_id=p.client_id WHERE 1=1'
        args = []
        if client:
            sql += ' AND p.client_id=?'
            args.append(client)
        if q:
            sql += ' AND (p.sku LIKE ? OR p.name LIKE ?)'
            args += [f'%{q}%', f'%{q}%']
        prods = self._q(sql + ' ORDER BY p.sku', args)
        for p in prods:
            md = self._client(p['client_id'])['min_remaining_days']
            lots = [self._lot_view(r, md) for r in self._q(
                'SELECT *, on_hand_qty-allocated_qty-hold_qty AS available FROM inventory WHERE sku=? '
                "ORDER BY COALESCE(expiry_date, '9999'), inventory_id", (p['sku'],))]
            p['lots'] = lots
            for k in ('on_hand_qty', 'allocated_qty', 'hold_qty', 'available'):
                p[k] = sum(r[k] for r in lots)
            p['near_expiry_qty'] = sum(r['available'] for r in lots
                                       if not r['usable'] and not r['expired'] and r['available'] > 0)
            p['expired_qty'] = sum(r['available'] for r in lots if r['expired'] and r['available'] > 0)
            p['inbound'] = self._q('SELECT * FROM inbound_schedule WHERE sku=? ORDER BY expected_date', (p['sku'],))
        return prods

    def products(self, client=None):
        if client:
            return self._q('SELECT * FROM products WHERE client_id=? ORDER BY sku', (client,))
        return self._q('SELECT * FROM products ORDER BY sku')

    # ------------------------------------------------------------ 검증 (설계초안 11장)
    def verify(self):
        labels = _read_csv(os.path.join(self.seed_dir, 'expected_labels.csv'))
        orders = {o['order_id']: o for o in self._q('SELECT * FROM orders')}
        exc = {}
        for e in self._q("SELECT * FROM exceptions WHERE rule_id != 'E07' ORDER BY exception_id"):
            exc.setdefault(e['order_id'], []).append(e)
        rules, mismatches = {}, []
        status_ok = opt_total = opt_ok = 0
        for lab in labels:
            oid = lab['order_id']
            o = orders.get(oid)
            if not o:
                mismatches.append(f'{oid}: 주문 없음 (재생 전)')
                continue
            actual = sorted({e['rule_id'] for e in exc.get(oid, [])})
            expected = [] if lab['expected_exception'] == 'NONE' else [lab['expected_exception']]
            for r in expected:
                rules.setdefault(r, {'expected': 0, 'detected': 0, 'false_positive': 0})['expected'] += 1
            for r in actual:
                d = rules.setdefault(r, {'expected': 0, 'detected': 0, 'false_positive': 0})
                d['detected' if r in expected else 'false_positive'] += 1
            if actual != expected:
                mismatches.append(f'{oid}: 예외 기대 {expected or "없음"} / 실제 {actual or "없음"}')
            if o['status'] == lab['expected_final_status']:
                status_ok += 1
            else:
                mismatches.append(f'{oid}: 최종상태 기대 {lab["expected_final_status"]} / 실제 {o["status"]}')
            if lab['expected_exception'] == 'E01':
                opt_total += 1
                e01 = next((e for e in exc.get(oid, []) if e['rule_id'] == 'E01' and e['status'] == 'OPEN'), None)
                got = ','.join(x['code'] for x in self._options(e01) if x['enabled']) if e01 else None
                if got == lab['expected_options']:
                    opt_ok += 1
                else:
                    mismatches.append(f'{oid}: E01 활성 옵션 기대 {lab["expected_options"]} / 실제 {got}')

        bad_inv = self._one('SELECT COUNT(*) c FROM inventory WHERE on_hand_qty<0 OR allocated_qty<0 OR '
                            'allocated_qty+hold_qty>on_hand_qty')['c']
        alloc_mismatch = self._one(
            "SELECT COUNT(*) c FROM inventory i WHERE i.allocated_qty != (SELECT COALESCE(SUM(qty), 0) FROM "
            "allocations a WHERE a.inventory_id=i.inventory_id AND a.status IN ('ACTIVE', 'RETURNING'))")['c']
        cancelled_active = self._one(  # 취소 주문에 활성 할당이 남음 (원위치 대기 RETURNING은 정상)
            "SELECT COUNT(*) c FROM allocations a JOIN orders o ON o.order_id=a.order_id "
            "WHERE o.status='CANCELLED' AND a.status='ACTIVE'")['c']
        restock_pending = self._one("SELECT COUNT(DISTINCT order_id) c FROM allocations WHERE status='RETURNING'")['c']
        cancel_unsettled = self._one(  # 취소요청이 판단 대기(PENDING)인데 열린 E04가 없음 = 요청이 방치됨
            "SELECT COUNT(*) c FROM orders o WHERE (o.cancel_requested=1 OR o.cancel_result='PENDING') AND NOT EXISTS "
            "(SELECT 1 FROM exceptions e WHERE e.order_id=o.order_id AND e.rule_id='E04' "
            "AND e.status IN ('OPEN', 'IN_PROGRESS'))")['c']
        initial = int(self._state('initial_on_hand', 0))
        received = self._one('SELECT COALESCE(SUM(qty), 0) s FROM inbound_schedule WHERE received_at IS NOT NULL')['s']
        shipped = self._one("SELECT COALESCE(SUM(qty), 0) s FROM allocations WHERE status='SHIPPED'")['s']
        current = self._one('SELECT COALESCE(SUM(on_hand_qty), 0) s FROM inventory')['s']
        shipped_orders = self._one("SELECT COUNT(*) c FROM orders WHERE status='SHIPPED'")['c']
        shipped_records = self._one('SELECT COUNT(*) c FROM shipments WHERE shipped_at IS NOT NULL')['c']
        e07 = self._one("SELECT COUNT(*) c FROM exceptions WHERE rule_id='E07'")['c']
        live_label_on_cancelled = self._one(
            "SELECT COUNT(*) c FROM shipments s JOIN orders o ON o.order_id=s.order_id "
            "WHERE o.status='CANCELLED' AND s.cancelled_at IS NULL")['c']
        untracked_short = self._one('SELECT COUNT(*) c FROM order_items WHERE short_qty>0 AND short_action IS NULL')['c']
        expired_alloc = sum(1 for a in self._q(
            "SELECT i.expiry_date FROM allocations a JOIN inventory i ON i.inventory_id=a.inventory_id "
            "WHERE a.status='ACTIVE' AND i.expiry_date IS NOT NULL") if self._is_expired(a))
        expired_ship = self._one(  # 출고일 기준 유통기한 당일·경과 Lot이 출고된 건
            "SELECT COUNT(*) c FROM allocations a JOIN inventory i ON i.inventory_id=a.inventory_id "
            "JOIN shipments s ON s.order_id=a.order_id WHERE a.status='SHIPPED' AND i.expiry_date IS NOT NULL "
            "AND julianday(i.expiry_date) - julianday(substr(s.shipped_at, 1, 10)) < ?", (MIN_SELLABLE_DAYS,))['c']
        over_alloc = self._one(  # 라인별 할당 합계가 출고수량을 넘는 라인 (같은 SKU 여러 라인 포함)
            "SELECT COUNT(*) c FROM order_items i WHERE (SELECT COALESCE(SUM(qty), 0) FROM allocations a "
            "WHERE a.order_id=i.order_id AND a.line_no=i.line_no AND a.status IN ('ACTIVE', 'SHIPPED')) > i.ship_qty")['c']
        chain_ok = 0
        audit = {}
        for a in self._q("SELECT * FROM audit_log WHERE entity='order' AND field='status' ORDER BY log_id"):
            audit.setdefault(a['entity_id'], []).append(a)
        for oid, o in orders.items():
            rows, prev, ok = audit.get(oid, []), None, True
            for a in rows:
                ok &= a['before_value'] == prev
                prev = a['after_value']
            chain_ok += ok and prev == o['status']

        exp_total = sum(d['expected'] for d in rules.values())
        det_total = sum(d['detected'] for d in rules.values())
        fp_total = sum(d['false_positive'] for d in rules.values())
        n = len(labels)
        checks = [
            ('예외 감지 정확성', '정답 라벨 대비 감지·오탐', f'{det_total} / {exp_total} 감지, 오탐 {fp_total}',
             det_total == exp_total and fp_total == 0),
            ('최종 상태', '라벨의 기대 최종 상태와 비교', f'{status_ok} / {n} 일치', status_ok == n),
            ('처리 옵션 조건', 'E01 활성 옵션을 라벨과 비교', f'{opt_ok} / {opt_total} 일치', opt_ok == opt_total),
            ('재고 정합성', '음수·초과할당 행, 할당합계 불일치 행', f'위반 {bad_inv}행, 불일치 {alloc_mismatch}행',
             bad_inv == 0 and alloc_mismatch == 0),
            ('재고 수불', '초기 실재고 + 입고 − 출고 = 현재 실재고',
             f'{initial} + {received} − {shipped} = {initial + received - shipped} / 현재 {current}',
             initial + received - shipped == current),
            ('중복 출고 차단', '출고 기록 수 = 출고완료 주문 수 (주문당 1건 UNIQUE)',
             f'출고 기록 {shipped_records} / 출고완료 {shipped_orders}', shipped_records == shipped_orders),
            ('비허용 전이 차단', '차단·기록된 E07 건수 (시나리오 재생 중 0건이어야 함)', f'{e07}건', e07 == 0),
            ('이력 기록', '주문별 상태 이력이 끊김 없이 현재 상태까지 이어짐', f'{chain_ok} / {len(orders)} 주문',
             chain_ok == len(orders)),
            ('취소 주문 송장', '취소된 주문에 취소 처리되지 않은 출고 기록(송장)이 남아 있지 않음',
             f'{live_label_on_cancelled}건', live_label_on_cancelled == 0),
            ('부분출고 잔량', '출고하지 않은 수량에 처리 방식(잔량 취소·백오더)이 기록됨 (DB: 출고+잔량=주문수량)',
             f'미기록 {untracked_short}건', untracked_short == 0),
            ('만료 Lot 할당', f'현재 할당 중인 Lot 중 유통기한이 지난(잔여 {MIN_SELLABLE_DAYS}일 미만) Lot이 없음',
             f'{expired_alloc}건', expired_alloc == 0),
            ('만료 Lot 출고', '출고일 기준 유통기한 당일·경과 Lot이 출고된 건이 없음 (출고 확정 직전 재검증)',
             f'{expired_ship}건', expired_ship == 0),
            ('라인별 할당 수량', '라인마다 할당 합계 ≤ 출고수량 (같은 SKU가 여러 라인이어도 초과 할당 없음)',
             f'초과 {over_alloc}라인', over_alloc == 0),
            ('취소 주문 재고', '취소 주문에 활성 할당이 없음 (피킹한 상품은 원위치 확인 전까지 RETURNING으로 묶임)',
             f'활성 할당 {cancelled_active}건 · 원위치 대기 {restock_pending}주문', cancelled_active == 0),
            ('취소요청 처리 상태', '취소요청은 판단 대기(열린 E04) / 수락 / 거절 중 하나로 정리됨',
             f'방치 {cancel_unsettled}건', cancel_unsettled == 0),
        ]
        replay_complete = int(self._state('last_seq', 0)) == len(self.events())
        manual = int(self._state('manual_ops', 0))
        all_ok = all(c[3] for c in checks) and not mismatches
        return {
            'replay_complete': replay_complete, 'manual_ops': manual, 'clock': self._now_s(),
            'pass': all_ok and replay_complete and manual == 0,
            'note': ('' if replay_complete and manual == 0 else
                     '정답 라벨은 "시나리오를 끝까지 재생하고 수동 처리는 하지 않은 상태" 기준입니다. '
                     '초기화 → 끝까지 재생 후 다시 검증하세요.'),
            'rules': dict(sorted(rules.items())),
            'checks': [{'name': c[0], 'method': c[1], 'result': c[2], 'ok': c[3]} for c in checks],
            'mismatches': mismatches[:100], 'mismatch_count': len(mismatches),
        }
