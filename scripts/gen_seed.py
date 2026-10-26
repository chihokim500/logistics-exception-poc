"""물류 PoC 시드 데이터 생성기 (설계초안 6장 규칙).

사용: python gen_seed.py [출력폴더]   (기본: ../seed)
같은 난수 시드를 쓰므로 실행할 때마다 같은 데이터가 나옵니다.
모든 데이터는 가상 데이터입니다.
"""
import csv
import json
import os
import random
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, '..', 'seed')
rng = random.Random(20261026)
BASE = date(2026, 10, 26)  # 데모 기준일 (출고일)


def at(h, m, s=0, d=BASE):
    return datetime(d.year, d.month, d.day, h, m, s)


def fmt(x):
    return x.strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------- 화주
CLIENTS = [
    {'client_id': 'C01', 'name': '생활잡화A', 'category': '생활잡화', 'storage_type': '상온',
     'cutoff_time': '14:00', 'partial_ship_allowed': 'Y', 'min_remaining_days': '',
     'default_carrier': 'CJ대한통운', 'channels': ['스마트스토어', '쿠팡'], 'order_count': 120},
    {'client_id': 'C02', 'name': '신선식품B', 'category': '신선식품', 'storage_type': '냉장',
     'cutoff_time': '12:00', 'partial_ship_allowed': 'N', 'min_remaining_days': '7',
     'default_carrier': '롯데택배', 'channels': ['자사몰', '쿠팡'], 'order_count': 90},
    {'client_id': 'C03', 'name': '패션C', 'category': '패션', 'storage_type': '상온',
     'cutoff_time': '15:00', 'partial_ship_allowed': 'Y', 'min_remaining_days': '',
     'default_carrier': '한진택배', 'channels': ['스마트스토어', '자사몰'], 'order_count': 90},
]
CL = {c['client_id']: c for c in CLIENTS}
CH_PREFIX = {'스마트스토어': 'SS', '쿠팡': 'CP', '자사몰': 'OM'}

# ---------------------------------------------------------------- 상품
NAMES = {
    'C01': [f'{n} {v}' for n in ['주방세제', '수세미', '키친타올', '물티슈', '지퍼백', '고무장갑',
                                  '세탁세제', '섬유유연제', '욕실세정제', '두루마리휴지'] for v in ['1입', '3입']],
    'C02': ['우유 1L', '저지방우유 1L', '플레인요거트', '그릭요거트', '두부 300g', '순두부 350g', '계란 10구',
            '계란 30구', '슬라이스치즈', '무염버터', '생크림 500ml', '샐러드믹스', '닭가슴살 1kg',
            '훈제오리 400g', '어묵 500g'],
    'C03': [f'{n} {s}' for n in ['기본티', '맨투맨', '후드티', '슬랙스', '청바지'] for s in ['S', 'M', 'L']],
}
products = []
for c in CLIENTS:
    cid = c['client_id']
    for i, n in enumerate(NAMES[cid], 1):
        products.append({'sku': f'{cid}-{i:03d}', 'client_id': cid, 'name': n,
                         'storage_type': c['storage_type'], 'lot_managed': 'Y' if cid == 'C02' else 'N',
                         'barcode': f'880{rng.randrange(10**9, 10**10)}'})
PROD = {p['sku']: p for p in products}

# E01 전용 SKU: 주문수량, 재고 (정상 주문에는 쓰지 않음)
SHORT = {'C01': [('C01-019', 10, 6), ('C01-020', 5, 2)],
         'C02': [('C02-012', 10, 6), ('C02-013', 6, 1), ('C02-014', 9, 5), ('C02-015', 4, 1)],
         'C03': [('C03-014', 8, 3), ('C03-015', 12, 4)]}
SHORT_SKUS = {s for v in SHORT.values() for s, _, _ in v}
# E06 전용 SKU: 남은 Lot이 모두 유통기한 7일 미만
NEAR = ['C02-008', 'C02-009', 'C02-010', 'C02-011']
NORMAL = {c['client_id']: [p['sku'] for p in products if p['client_id'] == c['client_id']
                           and p['sku'] not in SHORT_SKUS and p['sku'] not in NEAR] for c in CLIENTS}

# ---------------------------------------------------------------- 채널 상품코드 매핑
codes, used = {}, set()
for p in products:
    for ch in CL[p['client_id']]['channels']:
        while True:
            code = f'{CH_PREFIX[ch]}{rng.randrange(100000, 1000000)}'
            if code not in used:
                break
        used.add(code)
        codes[(ch, p['sku'])] = code
UNMAPPED = []  # E02용: 매핑 테이블에서 의도적으로 뺀 (채널, SKU)
for cid, n in [('C01', 2), ('C02', 1), ('C03', 2)]:
    for sku in rng.sample(NORMAL[cid], n):
        UNMAPPED.append((rng.choice(CL[cid]['channels']), sku))
map_rows = [{'channel': ch, 'channel_product_code': code, 'client_id': PROD[sku]['client_id'], 'sku': sku}
            for (ch, sku), code in codes.items() if (ch, sku) not in UNMAPPED]

# ---------------------------------------------------------------- 주문
SPECIAL = {
    'C01': ['E01'] * 2 + ['E02'] * 2 + ['E03'] * 2 + ['E04'] + ['E05'] * 2 + ['CANCEL'],
    'C02': ['E01'] * 4 + ['E02'] + ['E03'] * 2 + ['E04'] * 2 + ['E05'] + ['E06'] * 4 + ['CANCEL'],
    'C03': ['E01'] * 2 + ['E02'] * 2 + ['E03'] + ['E04'] + ['E05'] + ['CANCEL'],
}
ADDR = ['서울특별시 강남구 테헤란로', '서울특별시 마포구 월드컵북로', '경기도 성남시 분당구 판교역로',
        '부산광역시 해운대구 센텀중앙로', '인천광역시 연수구 컨벤시아대로', '대전광역시 유성구 대학로',
        '경기도 고양시 일산동구 중앙로', '광주광역시 서구 상무중앙로']
SURNAME = '김이박최정강조윤장임한오서신권황안송류홍'
GIVEN = '민서준지윤현우수연하은도영진호경아'
BAD_ADDR = [('1234', None), ('', None), ('O6236', None), (None, '')]  # E05: 4자리 / 빈값 / 영문 O / 주소없음


def addr():
    return f'{rng.choice(ADDR)} {rng.randint(1, 300)} {rng.randint(1, 15)}{rng.randint(1, 5):02d}호'


def zipcode():
    return f'{rng.randint(1000, 63999):05d}'


def normal_lines(cid, ch, exclude=()):
    pool = [s for s in NORMAL[cid] if (ch, s) not in UNMAPPED and s not in exclude]
    n = rng.choices([1, 2, 3], weights=[6, 3, 1])[0]
    hi = 2 if cid == 'C03' else 3
    return [(codes[(ch, s)], s, rng.randint(1, hi)) for s in rng.sample(pool, n)]


specs = []
e05_i = 0
for c in CLIENTS:
    cid = c['client_id']
    kinds = SPECIAL[cid] + ['NORMAL'] * (c['order_count'] - len(SPECIAL[cid]))
    seen = {}
    for kind in kinds:
        v = seen.get(kind, 0)
        seen[kind] = v + 1
        ch = rng.choice(c['channels'])
        o = {'client_id': cid, 'kind': kind, 'variant': v, 'channel': ch, 'promised': BASE,
             'zipcode': zipcode(), 'address': addr(), 'note': ''}
        if kind == 'E01':
            sku, q, s = SHORT[cid][v]
            o['lines'] = [(codes[(ch, sku)], sku, q)]
            if v % 2 == 0:
                o['lines'] += normal_lines(cid, ch)[:1]
            if cid == 'C02' and v < 2:
                o['promised'] = BASE + timedelta(days=2)  # 지정일 배송
            o['short'] = (sku, q, s)
        elif kind == 'E02':
            ch, sku = [u for u in UNMAPPED if PROD[u[1]]['client_id'] == cid][v]
            o['channel'] = ch
            o['lines'] = [(codes[(ch, sku)], sku, 1)]
            if v == 0:
                o['lines'] += normal_lines(cid, ch, exclude=[sku])[:1]
            o['note'] = f'{ch} 상품코드 {codes[(ch, sku)]} 매핑 없음'
        elif kind == 'E06':
            sku = NEAR[v]
            o['lines'] = [(codes[(ch, sku)], sku, 2)]
            o['note'] = f'{sku} 남은 Lot 유통기한 D+2·D+4 (기준 7일)'
        else:
            o['lines'] = normal_lines(cid, ch)
        if kind == 'E05':
            z, a = BAD_ADDR[e05_i]
            e05_i += 1
            if z is not None:
                o['zipcode'] = z
            if a is not None:
                o['address'] = a
            o['note'] = f"우편번호 '{o['zipcode']}' / 주소 '{o['address']}'"
        specs.append(o)

start, end = at(15, 1, d=BASE - timedelta(days=1)), at(10, 59)
span = int((end - start).total_seconds())
for o in specs:
    o['ordered_at'] = start + timedelta(seconds=rng.randrange(span))
specs.sort(key=lambda o: o['ordered_at'])
for i, o in enumerate(specs, 1):
    o['order_id'] = f'ORD-{i:04d}'
    o['recipient'] = rng.choice(SURNAME) + '*' + rng.choice(GIVEN)

# ---------------------------------------------------------------- 로케이션·재고
locations = [{'location_code': f'{z}-{r:02d}-{l:02d}', 'zone': z, 'storage_type': '냉장' if z == 'C' else '상온'}
             for z in 'ABC' for r in range(1, 5) for l in range(1, 6)]
ambient = [l['location_code'] for l in locations if l['zone'] != 'C']
cold = [l['location_code'] for l in locations if l['zone'] == 'C']

demand = {}
for o in specs:
    for _, sku, q in o['lines']:
        if sku not in SHORT_SKUS and sku not in NEAR:
            demand[sku] = demand.get(sku, 0) + q

split = set(rng.sample(NORMAL['C01'] + NORMAL['C03'], 5))
near_noise = set(rng.sample(NORMAL['C02'], 3))
inv = []


def lot(sku, exp, n):
    return f'LOT-{sku[-3:]}-{exp:%m%d}-{n}'


for p in products:
    sku, cid = p['sku'], p['client_id']
    short = {s: st for s, _, st in SHORT[cid]}
    if cid != 'C02':
        if sku in short:
            inv.append([sku, ambient.pop(0), '', '', short[sku]])
            continue
        total = demand.get(sku, 0) + rng.randint(3, 15)
        if sku in split:
            a = total // 2
            inv.append([sku, ambient.pop(0), '', '', a])
            inv.append([sku, ambient.pop(0), '', '', total - a])
        else:
            inv.append([sku, ambient.pop(0), '', '', total])
        continue
    loc = cold.pop(0)  # 같은 SKU의 Lot은 같은 로케이션
    if sku in short:
        e = BASE + timedelta(days=rng.randint(15, 30))
        inv.append([sku, loc, lot(sku, e, 1), e, short[sku]])
    elif sku in NEAR:
        for n, d in enumerate([2, 4], 1):
            e = BASE + timedelta(days=d)
            inv.append([sku, loc, lot(sku, e, n), e, 5])
    else:
        total = demand.get(sku, 0) + rng.randint(5, 15)
        e1, e2 = BASE + timedelta(days=rng.randint(10, 20)), BASE + timedelta(days=rng.randint(25, 40))
        a = int(total * 0.4)
        inv.append([sku, loc, lot(sku, e1, 1), e1, a])
        inv.append([sku, loc, lot(sku, e2, 2), e2, total - a])
        if sku in near_noise:  # 출고 불가(임박) Lot 잡음: 할당에서 제외되어야 함
            e = BASE + timedelta(days=rng.randint(1, 5))
            inv.append([sku, loc, lot(sku, e, 0), e, rng.randint(1, 4)])

inv_rows = []
hold_idx = set(rng.sample([i for i, r in enumerate(inv) if r[0] in demand], 6))
for i, (sku, loc, lotno, exp, qty) in enumerate(inv):
    hold = rng.randint(1, 2) if i in hold_idx else 0
    inv_rows.append({'inventory_id': i + 1, 'sku': sku, 'location_code': loc, 'lot_no': lotno,
                     'expiry_date': exp.isoformat() if exp else '', 'on_hand_qty': qty + hold,
                     'allocated_qty': 0, 'hold_qty': hold})

# ---------------------------------------------------------------- 입고예정
inbound = [  # E01 처리옵션 ② 판정용
    ('C01-019', BASE, 10),                          # 약속출고일 당일 입고 → ② 가능
    ('C03-014', BASE + timedelta(days=3), 10),      # 약속출고일 이후 → ② 불가
    ('C02-012', BASE + timedelta(days=1), 12),      # 약속출고일 D+2 이전 → ② 가능
    ('C02-013', BASE + timedelta(days=1), 10),
    ('C02-014', BASE + timedelta(days=4), 10),      # 약속출고일 당일 이후 → ② 불가
]
for sku in rng.sample(NORMAL['C01'] + NORMAL['C02'] + NORMAL['C03'], 10):
    inbound.append((sku, BASE + timedelta(days=rng.randint(1, 11)), rng.randrange(20, 101, 10)))
inbound_rows = [{'inbound_id': f'IB-{i:03d}', 'sku': s, 'expected_date': d.isoformat(), 'qty': q}
                for i, (s, d, q) in enumerate(inbound, 1)]


def e01_options(o):
    sku, q, s = o['short']
    opts = []
    if CL[o['client_id']]['partial_ship_allowed'] == 'Y' and s > 0:
        opts.append('1')
    if sum(qq for ss, d, qq in inbound if ss == sku and d <= o['promised']) >= q - s:
        opts.append('2')
    return ','.join(opts + ['3'])


# ---------------------------------------------------------------- 이벤트 시나리오
events = []
WORKERS = ['W01', 'W02', 'W03', 'W04', 'W05']


def ev(t, typ, oid='', actor='SYSTEM', carrier='', note=''):
    events.append((t, len(events), typ, oid, actor, carrier, note))


def pick(t, o):
    ev(t, 'START_PICKING', o['order_id'], rng.choice(WORKERS))


def pack(t, o):
    ev(t, 'PACK', o['order_id'], rng.choice(WORKERS), CL[o['client_id']]['default_carrier'])


def ship(t, o):
    ev(t, 'SHIP', o['order_id'], rng.choice(WORKERS))


def cancel(t, o, note):
    ev(t, 'CANCEL_REQUEST', o['order_id'], 'CHANNEL', note=note)


ev(at(11, 0), 'IMPORT_ORDERS', note='orders_import.csv 전체 업로드 → 매핑·주소 검증')
ev(at(11, 1), 'ALLOCATE_ALL', note='확정 주문을 주문시각 순으로 FEFO 할당')
t = at(11, 10)
while t <= at(15, 0):
    ev(t, 'RUN_DETECTION', note='10분 주기 마감위험 감지')
    t += timedelta(minutes=10)

WINDOW = {'C02': (at(11, 2), at(11, 28)), 'C01': (at(11, 31), at(13, 28)), 'C03': (at(13, 31), at(14, 28))}
for o in specs:
    cid, k, v = o['client_id'], o['kind'], o['variant']
    if k == 'NORMAL':
        ws, we = WINDOW[cid]
        t0 = ws + timedelta(seconds=rng.randrange(int((we - ws).total_seconds()) - 180))
        pick(t0, o)
        pack(t0 + timedelta(seconds=rng.randint(60, 120)), o)
        ship(t0 + timedelta(seconds=rng.randint(150, 180)), o)
    elif k == 'E03':
        # v0: 할당 상태로 마감 30분 전을 넘김, v1: 피킹 중 상태로 넘김 → 경고 후 늦게 출고
        plan = {'C02': [at(11, 20), at(12, 5), at(12, 8), at(12, 10)],
                'C01': [at(13, 0), at(13, 40), at(13, 50), at(14, 5)],
                'C03': [None, at(14, 40), at(14, 50), at(15, 10)]}[cid]
        if v == 1:
            pick(plan[0], o)
        else:
            pick(plan[1], o)
        pack(plan[2], o)
        ship(plan[3], o)
        o['note'] = '마감 30분 전 시점에 ' + ('피킹중' if v == 1 else '할당') + ' 상태 → 경고 후 지연 출고'
    elif k == 'E04':
        if (cid, v) == ('C01', 0):
            pick(at(12, 0), o); cancel(at(12, 5), o, '피킹 중 취소요청')
        elif (cid, v) == ('C02', 0):
            pick(at(11, 10), o); cancel(at(11, 15), o, '피킹 중 취소요청')
        elif (cid, v) == ('C02', 1):
            pick(at(11, 5), o); pack(at(11, 8), o); cancel(at(11, 12), o, '포장 완료 후 취소요청')
        else:
            pick(at(13, 40), o); pack(at(13, 45), o); cancel(at(13, 50), o, '포장 완료 후 취소요청')
        o['note'] = '피킹/포장 이후 취소요청'
    elif k == 'CANCEL':
        t, stage = {'C01': (at(11, 0, 30), '확정'), 'C02': (at(11, 3), '할당'), 'C03': (at(12, 30), '할당')}[cid]
        cancel(t, o, f'{stage} 단계 취소요청')
        o['note'] = f'{stage} 단계 취소 → 예외 없이 즉시 취소, 할당 해제'
    elif k == 'E01':
        sku, q, s = o['short']
        o['note'] = (f'{sku} 주문 {q} > 가용 {s}, 부분출고 {CL[cid]["partial_ship_allowed"]}, '
                     f'약속출고일 {o["promised"]:%m-%d}')

events.sort(key=lambda e: (e[0], e[1]))

# ---------------------------------------------------------------- 정답 라벨
FINAL = {'NORMAL': 'SHIPPED', 'E03': 'SHIPPED', 'CANCEL': 'CANCELLED'}
labels = []
for o in specs:
    k = o['kind']
    exc = k if k.startswith('E') else 'NONE'
    labels.append({'order_id': o['order_id'], 'client_id': o['client_id'], 'expected_exception': exc,
                   'expected_final_status': FINAL.get(k, 'ON_HOLD'),
                   'expected_options': e01_options(o) if k == 'E01' else ('' if exc == 'NONE' else '1,2'),
                   'scenario': o['note'] or ('정상 출고' if k == 'NORMAL' else '')})

# ---------------------------------------------------------------- 정책
POLICIES = [
    {'rule_id': 'E01', 'name': '재고부족', 'detect_at': 'ALLOCATE', 'blocking': True, 'severity': 'HIGH',
     'sla_minutes': 60, 'approver_role': 'OPERATOR',
     'condition': '라인 주문수량 > 전체 가용재고(실재고-할당-보류) 합계',
     'options': [{'code': '1', 'label': '부분출고', 'enable_if': '화주 partial_ship_allowed=Y AND 가용재고>0'},
                 {'code': '2', 'label': '입고 후 출고', 'enable_if': '약속출고일 이전 입고예정 수량 합계 >= 부족수량'},
                 {'code': '3', 'label': '화주 확인 후 취소', 'enable_if': '항상'}]},
    {'rule_id': 'E02', 'name': '상품매핑 누락', 'detect_at': 'IMPORT', 'blocking': True, 'severity': 'MEDIUM',
     'sla_minutes': 120, 'approver_role': 'OPERATOR',
     'condition': '(채널, 채널상품코드)에 대응하는 SKU 매핑 없음',
     'options': [{'code': '1', 'label': 'SKU 매핑 등록 후 재검증', 'enable_if': '항상'},
                 {'code': '2', 'label': '화주 문의', 'enable_if': '항상'}]},
    {'rule_id': 'E03', 'name': '출고마감 위험', 'detect_at': 'SCHEDULED_CHECK', 'blocking': False, 'severity': 'HIGH',
     'sla_minutes': None, 'sla_note': '화주 마감시각까지', 'approver_role': 'OPERATOR',
     'schedule_note': ('운영 시 스케줄러가 10분마다 실행한다고 가정. PoC에는 상시 스케줄러가 없고, 시나리오 이벤트'
                       '(RUN_DETECTION, 11:10~15:00 10분 간격) 또는 화면의 감지 실행 버튼으로만 실행'),
     'condition': '약속출고일=오늘 AND 기준시각 >= 마감-30분 AND 상태 IN (CONFIRMED, ALLOCATED, PICKING)',
     'options': [{'code': '1', 'label': '우선 작업 지정', 'enable_if': '마감 전'},
                 {'code': '2', 'label': '익일 출고 전환(화주 알림)', 'enable_if': '항상'}]},
    {'rule_id': 'E04', 'name': '피킹 후 취소요청', 'detect_at': 'CANCEL_REQUEST', 'blocking': True,
     'severity': 'CRITICAL', 'sla_minutes': 15, 'approver_role': 'OPERATOR',
     'condition': '취소요청 시 상태 IN (PICKING, PACKED)',
     'options': [{'code': '1', 'label': '출고 중단(송장 취소, 원위치 확인 후 재고 해제)', 'enable_if': '항상'},
                 {'code': '2', 'label': '출고 진행(고객 반품 안내)', 'enable_if': '항상'}]},
    {'rule_id': 'E05', 'name': '주소 오류', 'detect_at': 'IMPORT', 'blocking': True, 'severity': 'MEDIUM',
     'sla_minutes': 120, 'approver_role': 'OPERATOR',
     'condition': '우편번호가 숫자 5자리가 아님 OR 주소 공백',
     'options': [{'code': '1', 'label': '주소 수정 후 재검증', 'enable_if': '항상'},
                 {'code': '2', 'label': '화주 문의', 'enable_if': '항상'}]},
    {'rule_id': 'E06', 'name': '유통기한 임박', 'detect_at': 'ALLOCATE', 'blocking': True, 'severity': 'MEDIUM',
     'sla_minutes': 60, 'approver_role': 'OPERATOR',
     'condition': '잔여일 >= 화주 min_remaining_days 인 Lot 가용 합계 < 주문수량 <= 전체 Lot 가용 합계',
     'options': [{'code': '1', 'label': '임박 Lot 출고(화주 승인)', 'enable_if': '항상'},
                 {'code': '2', 'label': '재고부족으로 처리(E01 옵션 적용)', 'enable_if': '항상'}]},
    {'rule_id': 'E07', 'name': '비허용 전이·중복 출고', 'detect_at': 'API', 'blocking': False, 'severity': 'LOW',
     'sla_minutes': None, 'approver_role': None,
     'condition': '전이 규칙표에 없는 상태 변경 요청 (SHIPPED 후 취소/재출고 등)', 'options': []},
]


# ---------------------------------------------------------------- 저장
def write(name, rows, cols):
    with open(os.path.join(OUT, name), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


os.makedirs(OUT, exist_ok=True)
write('clients.csv', CLIENTS, ['client_id', 'name', 'category', 'storage_type', 'cutoff_time',
                               'partial_ship_allowed', 'min_remaining_days', 'default_carrier'])
write('products.csv', products, ['sku', 'client_id', 'name', 'storage_type', 'lot_managed', 'barcode'])
write('channel_product_map.csv', map_rows, ['channel', 'channel_product_code', 'client_id', 'sku'])
write('locations.csv', locations, ['location_code', 'zone', 'storage_type'])
write('inventory.csv', inv_rows, ['inventory_id', 'sku', 'location_code', 'lot_no', 'expiry_date',
                                  'on_hand_qty', 'allocated_qty', 'hold_qty'])
write('inbound_schedule.csv', inbound_rows, ['inbound_id', 'sku', 'expected_date', 'qty'])
write('orders_import.csv',
      [{'order_id': o['order_id'], 'client_id': o['client_id'], 'channel': o['channel'],
        'ordered_at': fmt(o['ordered_at']), 'recipient': o['recipient'], 'zipcode': o['zipcode'],
        'address': o['address'], 'promised_ship_date': o['promised'].isoformat(), 'line_no': n,
        'channel_product_code': code, 'product_name': PROD[sku]['name'], 'qty': q}
       for o in specs for n, (code, sku, q) in enumerate(o['lines'], 1)],
      ['order_id', 'client_id', 'channel', 'ordered_at', 'recipient', 'zipcode', 'address',
       'promised_ship_date', 'line_no', 'channel_product_code', 'product_name', 'qty'])
write('events.csv',
      [{'seq': i, 'event_at': fmt(e[0]), 'event_type': e[2], 'order_id': e[3], 'actor': e[4],
        'carrier': e[5], 'note': e[6]} for i, e in enumerate(events, 1)],
      ['seq', 'event_at', 'event_type', 'order_id', 'actor', 'carrier', 'note'])
write('expected_labels.csv', labels, ['order_id', 'client_id', 'expected_exception', 'expected_final_status',
                                      'expected_options', 'scenario'])
with open(os.path.join(OUT, 'policies.json'), 'w', encoding='utf-8') as f:
    json.dump(POLICIES, f, ensure_ascii=False, indent=2)

cnt = {}
for l in labels:
    cnt[l['expected_exception']] = cnt.get(l['expected_exception'], 0) + 1
print(f'출력: {OUT}')
print(f'화주 {len(CLIENTS)}, 상품 {len(products)}, 매핑 {len(map_rows)} (누락 {len(UNMAPPED)}), '
      f'로케이션 {len(locations)}, 재고행 {len(inv_rows)}, 입고예정 {len(inbound_rows)}')
print(f'주문 {len(specs)}, 주문라인 {sum(len(o["lines"]) for o in specs)}, 이벤트 {len(events)}')
print('정답 라벨:', dict(sorted(cnt.items())))
