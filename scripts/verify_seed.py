"""시드 데이터 검증기: 설계초안 3장(상태 전이)·5장(예외 규칙)을 그대로 구현해
events.csv를 재생하고, 결과를 expected_labels.csv와 비교합니다.
구현할 시스템의 기준 동작(reference) 역할도 합니다.

사용: python verify_seed.py [시드폴더]   (기본: ../seed)
"""
import csv
import os
import re
import sys
from datetime import date, datetime, time, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, '..', 'seed')


def load(name):
    with open(os.path.join(SEED, name), encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


clients = {r['client_id']: r for r in load('clients.csv')}
cmap = {(r['channel'], r['channel_product_code']): r['sku'] for r in load('channel_product_map.csv')}
inv = load('inventory.csv')
for r in inv:
    r['inventory_id'] = int(r['inventory_id'])
    for k in ('on_hand_qty', 'allocated_qty', 'hold_qty'):
        r[k] = int(r[k])
    r['expiry'] = date.fromisoformat(r['expiry_date']) if r['expiry_date'] else None
initial_on_hand = sum(r['on_hand_qty'] for r in inv)
inbound = [(r['sku'], date.fromisoformat(r['expected_date']), int(r['qty'])) for r in load('inbound_schedule.csv')]

orders = {}
for r in load('orders_import.csv'):
    o = orders.setdefault(r['order_id'], {
        'order_id': r['order_id'], 'client_id': r['client_id'], 'channel': r['channel'],
        'ordered_at': datetime.fromisoformat(r['ordered_at']), 'zipcode': r['zipcode'], 'address': r['address'],
        'promised': date.fromisoformat(r['promised_ship_date']), 'lines': [], 'status': None,
        'hold_from': None, 'exc': [], 'alloc': [], 'shipped_qty': 0})
    o['lines'].append({'code': r['channel_product_code'], 'qty': int(r['qty']), 'sku': None})
events = sorted(load('events.csv'), key=lambda e: int(e['seq']))
labels = {r['order_id']: r for r in load('expected_labels.csv')}
e07 = []
integrity_errors = []


def raise_exc(o, rule, now, blocking=True):
    if any(e['rule'] == rule for e in o['exc']):  # 주문·규칙당 1건 (중복 감지 방지)
        return
    o['exc'].append({'rule': rule, 'at': now})
    if blocking and o['status'] != 'ON_HOLD':
        o['hold_from'] = o['status']
        o['status'] = 'ON_HOLD'


def available(r):
    return r['on_hand_qty'] - r['allocated_qty'] - r['hold_qty']


def usable_rows(sku, client, today):
    rows = [r for r in inv if r['sku'] == sku and available(r) > 0]
    md = client['min_remaining_days']
    if md:
        rows = [r for r in rows if r['expiry'] is None or (r['expiry'] - today).days >= int(md)]
    return sorted(rows, key=lambda r: (r['expiry'] or date.max, r['inventory_id']))  # FEFO


def check_integrity(when):
    for r in inv:
        if r['on_hand_qty'] < 0 or r['allocated_qty'] < 0 or r['allocated_qty'] + r['hold_qty'] > r['on_hand_qty']:
            integrity_errors.append(f'{when}: inventory_id {r["inventory_id"]} {r}')


def do_import(now):
    for o in orders.values():
        o['status'] = 'RECEIVED'
        bad_map = False
        for l in o['lines']:
            l['sku'] = cmap.get((o['channel'], l['code']))
            bad_map |= l['sku'] is None
        if bad_map:
            raise_exc(o, 'E02', now)
        if not re.fullmatch(r'\d{5}', o['zipcode']) or not o['address'].strip():
            raise_exc(o, 'E05', now)
        if o['status'] == 'RECEIVED':
            o['status'] = 'CONFIRMED'


def do_allocate(now):
    for o in sorted(orders.values(), key=lambda o: o['ordered_at']):
        if o['status'] != 'CONFIRMED':
            continue
        c = clients[o['client_id']]
        fails = set()
        for l in o['lines']:
            usable = sum(available(r) for r in usable_rows(l['sku'], c, now.date()))
            total = sum(available(r) for r in inv if r['sku'] == l['sku'])
            if usable < l['qty']:
                fails.add('E06' if total >= l['qty'] else 'E01')
        if fails:  # 전량 할당 원칙: 한 라인이라도 실패하면 할당하지 않음
            for rule in sorted(fails):
                raise_exc(o, rule, now)
            continue
        for l in o['lines']:
            need = l['qty']
            for r in usable_rows(l['sku'], c, now.date()):
                q = min(need, available(r))
                r['allocated_qty'] += q
                o['alloc'].append((r, q))
                need -= q
                if need == 0:
                    break
        o['status'] = 'ALLOCATED'
    check_integrity('ALLOCATE_ALL')


def release(o):
    for r, q in o['alloc']:
        r['allocated_qty'] -= q
    o['alloc'] = []


STEP = {'START_PICKING': ('ALLOCATED', 'PICKING'), 'PACK': ('PICKING', 'PACKED'), 'SHIP': ('PACKED', 'SHIPPED')}


def do_transition(o, typ, now):
    frm, to = STEP[typ]
    if o['status'] != frm:
        e07.append((now, o['order_id'], typ, o['status']))
        return
    if typ == 'SHIP':
        for r, q in o['alloc']:
            r['on_hand_qty'] -= q
            r['allocated_qty'] -= q
            o['shipped_qty'] += q
        o['alloc'] = []
    o['status'] = to


def do_cancel(o, now):
    if o['status'] in ('RECEIVED', 'CONFIRMED', 'ALLOCATED'):
        release(o)
        o['status'] = 'CANCELLED'
    elif o['status'] in ('PICKING', 'PACKED'):
        raise_exc(o, 'E04', now)
    else:
        e07.append((now, o['order_id'], 'CANCEL_REQUEST', o['status']))


def do_detection(now):
    for o in orders.values():
        if o['status'] not in ('CONFIRMED', 'ALLOCATED', 'PICKING') or o['promised'] != now.date():
            continue
        cutoff = datetime.combine(now.date(), time.fromisoformat(clients[o['client_id']]['cutoff_time']))
        if now >= cutoff - timedelta(minutes=30):
            raise_exc(o, 'E03', now, blocking=False)


for e in events:
    now, typ, o = datetime.fromisoformat(e['event_at']), e['event_type'], orders.get(e['order_id'])
    if typ == 'IMPORT_ORDERS':
        do_import(now)
    elif typ == 'ALLOCATE_ALL':
        do_allocate(now)
    elif typ in STEP:
        do_transition(o, typ, now)
    elif typ == 'CANCEL_REQUEST':
        do_cancel(o, now)
    elif typ == 'RUN_DETECTION':
        do_detection(now)
    else:
        raise SystemExit(f'알 수 없는 이벤트: {typ}')
check_integrity('END')


def e01_options(o):
    c = clients[o['client_id']]
    l = next(l for l in o['lines']
             if sum(available(r) for r in inv if r['sku'] == l['sku']) < l['qty'])
    avail = sum(available(r) for r in inv if r['sku'] == l['sku'])
    opts = []
    if c['partial_ship_allowed'] == 'Y' and avail > 0:
        opts.append('1')
    if sum(q for s, d, q in inbound if s == l['sku'] and d <= o['promised']) >= l['qty'] - avail:
        opts.append('2')
    return ','.join(opts + ['3'])


# ---------------------------------------------------------------- 비교
mismatch = []
by_rule = {}
for oid, o in orders.items():
    lab = labels[oid]
    actual = sorted({e['rule'] for e in o['exc']})
    expected = [] if lab['expected_exception'] == 'NONE' else [lab['expected_exception']]
    for rule in actual:
        by_rule.setdefault(rule, [0, 0])
        by_rule[rule][0 if rule in expected else 1] += 1
    if actual != expected:
        mismatch.append(f'{oid}: 예외 기대 {expected} / 실제 {actual}')
    if o['status'] != lab['expected_final_status']:
        mismatch.append(f'{oid}: 최종상태 기대 {lab["expected_final_status"]} / 실제 {o["status"]}')
    if 'E01' in actual and e01_options(o) != lab['expected_options']:
        mismatch.append(f'{oid}: E01 옵션 기대 {lab["expected_options"]} / 실제 {e01_options(o)}')

expected_cnt = {}
for l in labels.values():
    if l['expected_exception'] != 'NONE':
        expected_cnt[l['expected_exception']] = expected_cnt.get(l['expected_exception'], 0) + 1
shipped = sum(o['shipped_qty'] for o in orders.values())
final_on_hand = sum(r['on_hand_qty'] for r in inv)
statuses = {}
for o in orders.values():
    statuses[o['status']] = statuses.get(o['status'], 0) + 1

print(f'시드: {SEED}')
print(f'주문 {len(orders)}, 이벤트 {len(events)}')
print('예외 규칙별  정답 / 감지(일치) / 오탐')
for rule in sorted(set(expected_cnt) | set(by_rule)):
    hit, fp = by_rule.get(rule, [0, 0])
    print(f'  {rule}: {expected_cnt.get(rule, 0):>3} / {hit:>3} / {fp}')
print('최종 상태:', dict(sorted(statuses.items())))
print(f'재고: 초기 실재고 {initial_on_hand} - 출고수량 {shipped} = {initial_on_hand - shipped}, '
      f'최종 실재고 {final_on_hand} → {"일치" if initial_on_hand - shipped == final_on_hand else "불일치"}')
print(f'재고 정합성 위반: {len(integrity_errors)}건, 비허용 전이(E07): {len(e07)}건')
for x in integrity_errors[:10] + [str(x) for x in e07[:10]]:
    print('  ', x)
print(f'라벨 불일치: {len(mismatch)}건')
for m in mismatch[:30]:
    print('  ', m)
ok = not mismatch and not integrity_errors and not e07 and initial_on_hand - shipped == final_on_hand
print('결과:', 'PASS' if ok else 'FAIL')
sys.exit(0 if ok else 1)
