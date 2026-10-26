"""처리 시나리오 검증: 운영자가 예외를 처리한 뒤 주문·재고·출고 상태가 기대대로 바뀌는지 확인합니다.

시나리오마다 임시 DB를 새로 만들어 시드를 11:30까지 재생한 뒤, 예외 하나를 처리하고
처리 전후 데이터를 비교합니다. 화면에서 보고 있는 DB에는 영향을 주지 않습니다.
"""
import os
import shutil
import tempfile
import time as _time
from datetime import date

from engine import Engine

CHECKPOINT = '2026-10-26 11:30:00'
NEXT_DAY = '2026-10-27 09:00:00'


def _inv(e, sku):
    return e._one('SELECT COALESCE(SUM(on_hand_qty), 0) on_hand, COALESCE(SUM(allocated_qty), 0) allocated, '
                  'COALESCE(SUM(on_hand_qty-allocated_qty-hold_qty), 0) available FROM inventory WHERE sku=?', (sku,))


def _exc_status(e, eid):
    return e._one('SELECT status FROM exceptions WHERE exception_id=?', (eid,))['status']


def _find(e, rule, cond=lambda e, x, o: True):
    for x in e.list_exceptions('open', rule):
        o = e._order(x['order_id'])
        if cond(e, x, o):
            return x, o
    raise LookupError(f'조건에 맞는 {rule} 예외가 없습니다')


def _enabled(e, x, code):
    return any(o['code'] == code and o['enabled'] for o in e._options(x))


class Run:
    def __init__(self, sid, rule, title, action):
        self.r = {'id': sid, 'rule': rule, 'title': title, 'action': action, 'order_id': None, 'exception_id': None,
                  'checks': [], 'error': None}

    def target(self, x):
        self.r['order_id'], self.r['exception_id'] = x['order_id'], x['exception_id']

    def check(self, item, before, after, expected, ok=None):
        ok = (after == expected) if ok is None else ok
        self.r['checks'].append({'item': item, 'before': _s(before), 'after': _s(after), 'expected': _s(expected),
                                 'ok': bool(ok)})


def _s(v):
    return '-' if v is None else str(v)


# ---------------------------------------------------------------- 시나리오
def sc_partial_cancel(e, run):
    x, o = _find(e, 'E01', lambda e, x, o: _enabled(e, x, '1'))
    run.target(x)
    s = e._short_lines(o['order_id'])[0]
    b = _inv(e, s['sku'])
    e.resolve(x['exception_id'], '1', 'OPERATOR', '화주 동의: 잔량 취소', {'remainder': 'CANCEL'})
    a = _inv(e, s['sku'])
    line = e._one('SELECT * FROM order_items WHERE order_id=? AND line_no=?', (o['order_id'], s['line_no']))
    run.check('주문 상태', 'ON_HOLD', e._order(o['order_id'])['status'], 'ALLOCATED')
    run.check('E01 예외', 'OPEN', _exc_status(e, x['exception_id']), 'RESOLVED')
    run.check(f'{s["sku"]} 출고수량', s['qty'], line['ship_qty'], s['usable'])
    run.check('미출고 잔량 기록', '-', f'{line["short_qty"]} {line["short_action"]}', f'{s["qty"] - s["usable"]} CANCEL')
    run.check(f'{s["sku"]} 할당재고', b['allocated'], a['allocated'], b['allocated'] + s['usable'])
    run.check(f'{s["sku"]} 실재고 (출고 전이라 불변)', b['on_hand'], a['on_hand'], b['on_hand'])


def sc_partial_backorder(e, run):
    x, o = _find(e, 'E01', lambda e, x, o: _enabled(e, x, '1'))
    run.target(x)
    s = e._short_lines(o['order_id'])[0]
    e.resolve(x['exception_id'], '1', 'OPERATOR', '화주 요청: 잔량은 입고 후 발송', {'remainder': 'BACKORDER'})
    bo = e._one('SELECT * FROM orders WHERE parent_order_id=?', (o['order_id'],))
    run.check('원주문 상태', 'ON_HOLD', e._order(o['order_id'])['status'], 'ALLOCATED')
    run.check('백오더 주문 생성', '-', bo['order_id'] if bo else None, f'{o["order_id"]}-B1')
    bo_qty = e._one('SELECT SUM(qty) q FROM order_items WHERE order_id=?', (bo['order_id'],))['q'] if bo else None
    run.check('백오더 수량 = 부족 수량', '-', bo_qty, s['qty'] - s['usable'])
    bo_rules = sorted(r['rule_id'] for r in e._q("SELECT rule_id FROM exceptions WHERE order_id=? AND status='OPEN'",
                                                 (bo['order_id'],))) if bo else []
    run.check('백오더 상태 (재고 없음 → 보류)', '-', f'{bo["status"] if bo else None} {",".join(bo_rules)}',
              'ON_HOLD E01')


def sc_wait_inbound(e, run):
    x, o = _find(e, 'E01', lambda e, x, o: _enabled(e, x, '2') and o['promised_ship_date'] > '2026-10-26')
    run.target(x)
    oid = o['order_id']
    s = e._short_lines(oid)[0]
    inbound = e._q('SELECT * FROM inbound_schedule WHERE sku=? AND received_at IS NULL ORDER BY expected_date',
                   (s['sku'],))
    due = inbound[0]['expected_date']
    b = _inv(e, s['sku'])
    e.resolve(x['exception_id'], '2', 'OPERATOR', f'{due} 입고분으로 출고')
    m = _inv(e, s['sku'])
    run.check('승인 직후 주문 상태', 'ON_HOLD', e._order(oid)['status'], 'ON_HOLD')
    run.check('승인 직후 입고 대기', 0, e._order(oid)['waiting_inbound'], 1)
    run.check('승인 직후 E01 예외', 'OPEN', _exc_status(e, x['exception_id']), 'IN_PROGRESS')
    run.check(f'승인 직후 {s["sku"]} 실재고 (입고 전이라 불변)', b['on_hand'], m['on_hand'], b['on_hand'])
    e.process_inbound('OPERATOR')  # 아직 입고일 전
    run.check(f'입고일 전({e.now():%m-%d}) 입고 처리 → 할당 안 함', 'ON_HOLD', e._order(oid)['status'], 'ON_HOLD')
    e.set_clock(NEXT_DAY)
    r = e.process_inbound('OPERATOR')
    a = _inv(e, s['sku'])
    got = sum(i['qty'] for i in inbound if i['expected_date'] <= NEXT_DAY[:10])
    run.check(f'입고일({NEXT_DAY[5:10]}) 입고 처리 후 주문 상태', 'ON_HOLD', e._order(oid)['status'], 'ALLOCATED',
              ok=e._order(oid)['status'] == 'ALLOCATED' and oid in r['released'])
    run.check('입고 후 E01 예외', 'IN_PROGRESS', _exc_status(e, x['exception_id']), 'RESOLVED')
    run.check(f'{s["sku"]} 실재고 (+입고 {got})', b['on_hand'], a['on_hand'], b['on_hand'] + got)
    run.check(f'{s["sku"]} 할당재고 (+주문 {s["qty"]})', b['allocated'], a['allocated'], b['allocated'] + s['qty'])


def sc_cancel(e, run):
    x, o = _find(e, 'E01', lambda e, x, o: not _enabled(e, x, '1') and not _enabled(e, x, '2'))
    run.target(x)
    s = e._short_lines(o['order_id'])[0]
    b = _inv(e, s['sku'])
    e.resolve(x['exception_id'], '3', 'OPERATOR', '화주 확인: 주문 취소')
    a = _inv(e, s['sku'])
    run.check('주문 상태', 'ON_HOLD', e._order(o['order_id'])['status'], 'CANCELLED')
    run.check('E01 예외', 'OPEN', _exc_status(e, x['exception_id']), 'RESOLVED')
    run.check(f'{s["sku"]} 할당재고 (불변)', b['allocated'], a['allocated'], b['allocated'])


def sc_mapping_inquiry(e, run):
    x, o = _find(e, 'E02')
    run.target(x)
    oid = o['order_id']
    e.resolve(x['exception_id'], '2', 'OPERATOR', '화주에 신규 상품 등록 여부 문의')
    run.check('문의 후 E02 예외', 'OPEN', _exc_status(e, x['exception_id']), 'IN_PROGRESS')
    run.check('문의 후 주문 상태', 'ON_HOLD', e._order(oid)['status'], 'ON_HOLD')
    mappings = {}
    for l in e._items(oid):
        if not l['sku']:
            p = e._one('SELECT sku FROM products WHERE client_id=? AND name=?', (o['client_id'], l['product_name']))
            mappings[l['channel_product_code']] = p['sku']
    e.resolve(x['exception_id'], '1', 'OPERATOR', '화주 회신: 기존 상품과 동일', {'mappings': mappings})
    run.check('매핑 등록 후 주문 상태', 'ON_HOLD', e._order(oid)['status'], 'ALLOCATED')
    run.check('E02 예외', 'IN_PROGRESS', _exc_status(e, x['exception_id']), 'RESOLVED')
    unmapped = e._one('SELECT COUNT(*) c FROM order_items WHERE order_id=? AND sku IS NULL', (oid,))['c']
    run.check('매핑 안 된 라인', len(mappings), unmapped, 0)


def sc_address(e, run):
    x, o = _find(e, 'E05')
    run.target(x)
    oid = o['order_id']
    e.resolve(x['exception_id'], '2', 'OPERATOR', '화주에 배송지 확인 요청')
    run.check('문의 후 E05 예외', 'OPEN', _exc_status(e, x['exception_id']), 'IN_PROGRESS')
    e.resolve(x['exception_id'], '1', 'OPERATOR', '화주 회신 주소로 수정',
              {'zipcode': '04524', 'address': '서울특별시 중구 세종대로 110'})
    after = e._order(oid)
    run.check('우편번호', o['zipcode'] or '(빈값)', after['zipcode'], '04524')
    run.check('주문 상태', 'ON_HOLD', after['status'], 'ALLOCATED')
    run.check('E05 예외', 'IN_PROGRESS', _exc_status(e, x['exception_id']), 'RESOLVED')


def sc_near_expiry_approve(e, run):
    x, o = _find(e, 'E06')
    run.target(x)
    oid = o['order_id']
    e.resolve(x['exception_id'], '1', 'OPERATOR', '화주 승인: 임박 Lot 출고')
    lot = e._one('SELECT MIN(i.expiry_date) d FROM allocations a JOIN inventory i ON i.inventory_id=a.inventory_id '
                 "WHERE a.order_id=? AND a.status='ACTIVE'", (oid,))['d']
    days = (date.fromisoformat(lot) - e.now().date()).days if lot else None
    md = e._client(o['client_id'])['min_remaining_days']
    run.check('주문 상태', 'ON_HOLD', e._order(oid)['status'], 'ALLOCATED')
    run.check(f'할당 Lot 잔여일 (기준 {md}일 미만 = 승인 출고)', '-', f'{days}일', f'{md}일 미만',
              ok=days is not None and days < md)
    run.check('E06 예외', 'OPEN', _exc_status(e, x['exception_id']), 'RESOLVED')


def sc_near_expiry_to_shortage(e, run):
    x, o = _find(e, 'E06')
    run.target(x)
    oid = o['order_id']
    e.resolve(x['exception_id'], '2', 'OPERATOR', '화주 거절: 임박 Lot 출고 불가')
    rules = sorted(r['rule_id'] for r in e._q("SELECT rule_id FROM exceptions WHERE order_id=? AND status='OPEN'",
                                              (oid,)))
    run.check('주문 상태', 'ON_HOLD', e._order(oid)['status'], 'ON_HOLD')
    run.check('E06 예외', 'OPEN', _exc_status(e, x['exception_id']), 'RESOLVED')
    run.check('새로 열린 예외', 'E06', ','.join(rules), 'E01')


def sc_stop_after_pack(e, run):
    x, o = _find(e, 'E04', lambda e, x, o: o['hold_from_status'] == 'PACKED')
    run.target(x)
    oid = o['order_id']
    skus = [l['sku'] for l in e._items(oid)]
    b = {s: _inv(e, s) for s in skus}
    sh_b = e._one('SELECT * FROM shipments WHERE order_id=?', (oid,))
    e.resolve(x['exception_id'], '1', 'OPERATOR', '포장 해체 지시')
    m = {s: _inv(e, s) for s in skus}
    sh_a = e._one('SELECT * FROM shipments WHERE order_id=?', (oid,))
    after = e._order(oid)
    run.check('주문 상태', 'ON_HOLD', after['status'], 'CANCELLED')
    run.check('취소요청 결과', 'PENDING', after['cancel_result'], 'ACCEPTED')
    run.check(f'송장 {sh_b["tracking_no"]}', '발행', '취소' if sh_a['cancelled_at'] else '유효', '취소')
    qty = {l['sku']: l['ship_qty'] for l in e._items(oid)}
    for s in skus:
        run.check(f'원위치 전 {s} 할당재고 (상품이 손에 있으므로 유지)', b[s]['allocated'], m[s]['allocated'],
                  b[s]['allocated'])
    e.confirm_restock(oid, 'W01')
    a = {s: _inv(e, s) for s in skus}
    for s in skus:
        run.check(f'원위치 확인 후 {s} 할당재고 (−{qty[s]})', m[s]['allocated'], a[s]['allocated'],
                  b[s]['allocated'] - qty[s])
        run.check(f'{s} 실재고 (출고 전이라 불변)', b[s]['on_hand'], a[s]['on_hand'], b[s]['on_hand'])


def sc_proceed_after_pick(e, run):
    x, o = _find(e, 'E04', lambda e, x, o: o['hold_from_status'] == 'PICKING')
    run.target(x)
    oid = o['order_id']
    skus = [l['sku'] for l in e._items(oid)]
    b = {s: _inv(e, s) for s in skus}
    e.resolve(x['exception_id'], '2', 'OPERATOR', '고객 반품 안내 후 출고 진행')
    o2 = e._order(oid)
    run.check('처리 직후 주문 상태 (보류 전 단계로 복귀)', 'ON_HOLD', o2['status'], 'PICKING')
    run.check('취소요청 결과 / 요청 플래그', 'PENDING / 1', f'{o2["cancel_result"]} / {o2["cancel_requested"]}',
              'REJECTED / 0')
    e.transition(oid, 'PACKED', 'W01')
    e.transition(oid, 'SHIPPED', 'W01')
    a = {s: _inv(e, s) for s in skus}
    qty = {l['sku']: l['ship_qty'] for l in e._items(oid)}
    run.check('포장·출고 후 주문 상태', 'PICKING', e._order(oid)['status'], 'SHIPPED')
    for s in skus:
        run.check(f'{s} 실재고 (−{qty[s]})', b[s]['on_hand'], a[s]['on_hand'], b[s]['on_hand'] - qty[s])


def sc_cutoff_next_day(e, run):
    x, o = _find(e, 'E03')
    run.target(x)
    oid = o['order_id']
    e.resolve(x['exception_id'], '2', 'OPERATOR', '화주 통보: 익일 출고')
    after = e._order(oid)
    run.check('약속출고일', o['promised_ship_date'], after['promised_ship_date'], '2026-10-27')
    run.check('주문 상태 (경고형이라 불변)', o['status'], after['status'], o['status'])
    e.set_clock('2026-10-26 11:40:00')
    e.run_detection()
    n = e._one("SELECT COUNT(*) c FROM exceptions WHERE order_id=? AND rule_id='E03'", (oid,))['c']
    run.check('다음 감지(11:40)에서 다시 감지되지 않음', 1, n, 1)


def sc_cutoff_priority(e, run):
    x, o = _find(e, 'E03')
    run.target(x)
    oid = o['order_id']
    rank_b = next((w['rank'] for w in e.work_queue() if w['order_id'] == oid), None)
    e.resolve(x['exception_id'], '1', 'OPERATOR', '우선 작업 지정')
    run.check('우선 작업', o['priority'], e._order(oid)['priority'], 1)
    rank_a = next((w['rank'] for w in e.work_queue() if w['order_id'] == oid), None)
    run.check('피킹 대기열 순서', rank_b, rank_a, 1)
    run.check('E03 예외', 'OPEN', _exc_status(e, x['exception_id']), 'RESOLVED')
    run.check('주문 상태 (불변)', o['status'], e._order(oid)['status'], o['status'])


SCENARIOS = [
    ('P-01', 'E01', '부분출고 + 잔량 취소', '옵션 1 (잔량 취소)', sc_partial_cancel),
    ('P-02', 'E01', '부분출고 + 잔량 백오더', '옵션 1 (백오더)', sc_partial_backorder),
    ('P-03', 'E01', '입고 후 출고: 입고 대기 → 입고일에 재할당', '옵션 2 → 입고 처리', sc_wait_inbound),
    ('P-04', 'E01', '화주 확인 후 취소', '옵션 3', sc_cancel),
    ('P-05', 'E02', '화주 문의 → 매핑 등록 → 재검증·할당', '옵션 2 → 옵션 1', sc_mapping_inquiry),
    ('P-06', 'E05', '화주 문의 → 주소 수정 → 재검증·할당', '옵션 2 → 옵션 1', sc_address),
    ('P-07', 'E06', '임박 Lot 출고 승인', '옵션 1', sc_near_expiry_approve),
    ('P-08', 'E06', '임박 Lot 거절 → 재고부족 전환', '옵션 2', sc_near_expiry_to_shortage),
    ('P-09', 'E04', '포장 후 취소 → 출고 중단 (송장 취소)', '옵션 1', sc_stop_after_pack),
    ('P-10', 'E04', '피킹 후 취소 → 출고 진행', '옵션 2 → 포장·출고', sc_proceed_after_pick),
    ('P-11', 'E03', '마감 위험 → 익일 출고 전환', '옵션 2', sc_cutoff_next_day),
    ('P-12', 'E03', '마감 위험 → 우선 작업 지정', '옵션 1', sc_cutoff_priority),
]


def run_all(seed_dir=None):
    started = _time.time()
    tmp = tempfile.mkdtemp(prefix='poc-scenarios-')
    results = []
    try:
        for i, (sid, rule, title, action, fn) in enumerate(SCENARIOS):
            e = Engine(os.path.join(tmp, f's{i}.db'), **({'seed_dir': seed_dir} if seed_dir else {}))
            run = Run(sid, rule, title, action)
            try:
                e.replay(CHECKPOINT)
                fn(e, run)
            except Exception as x:  # noqa: BLE001 - 시나리오 실패도 결과로 보여줌
                run.r['error'] = f'{type(x).__name__}: {x}'
            finally:
                e.db.close()
            r = run.r
            r['pass'] = not r['error'] and bool(r['checks']) and all(c['ok'] for c in r['checks'])
            results.append(r)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {'checkpoint': CHECKPOINT, 'scenarios': results, 'pass': all(r['pass'] for r in results),
            'passed': sum(r['pass'] for r in results), 'total': len(results),
            'checks': sum(len(r['checks']) for r in results),
            'seconds': round(_time.time() - started, 1)}
