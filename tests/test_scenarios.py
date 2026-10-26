"""설계초안 9장 테스트 시나리오 (TS-01 ~ TS-15) 자동화.

실행: python -m unittest discover -s tests -v   (24_물류PoC 폴더에서)
"""
import csv
import io
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))
from engine import BadRequest, Conflict, Engine  # noqa: E402

BASE = date(2026, 10, 26)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.e = Engine(os.path.join(self.tmp, 'test.db'))
        self.e.set_clock('2026-10-26 10:00:00')
        self.n = 0

    def tearDown(self):
        self.e.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 도우미
    def stock(self, sku, lots):
        """sku의 재고를 [(수량, 유통기한까지 남은 일수 또는 None)]로 바꿉니다."""
        loc = self.e._one('SELECT location_code FROM inventory WHERE sku=? LIMIT 1', (sku,))['location_code']
        with self.e.tx():
            self.e._x('DELETE FROM inventory WHERE sku=?', (sku,))
            for i, (q, d) in enumerate(lots):
                exp = (BASE + timedelta(days=d)).isoformat() if d is not None else None
                self.e._x('INSERT INTO inventory(sku, location_code, lot_no, expiry_date, on_hand_qty) '
                          'VALUES (?, ?, ?, ?, ?)', (sku, loc, f'T{i}-{d}', exp, q))
            self.e._x('DELETE FROM inbound_schedule WHERE sku=?', (sku,))

    def inbound(self, sku, days, qty):
        with self.e.tx():
            self.e._x('INSERT INTO inbound_schedule(inbound_id, sku, expected_date, qty) VALUES (?, ?, ?, ?)',
                      (f'IB-T{self.n}{sku}', sku, (BASE + timedelta(days=days)).isoformat(), qty))

    def order(self, client, lines, channel=None, zipcode='06236', address='서울특별시 테스트로 1',
              promised_days=0):
        """lines: [(sku, 수량)] 또는 [('RAW:코드', 수량)]. 주문을 업로드하고 order_id를 돌려줍니다."""
        self.n += 1
        oid = f'T-{self.n:03d}'
        if channel is None:
            sku = next(s for s, _ in lines if not s.startswith('RAW:'))
            channel = self.e._one('SELECT channel FROM channel_product_map WHERE sku=? ORDER BY channel LIMIT 1',
                                  (sku,))['channel']
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['order_id', 'client_id', 'channel', 'ordered_at', 'recipient', 'zipcode', 'address',
                    'promised_ship_date', 'line_no', 'channel_product_code', 'product_name', 'qty'])
        for i, (s, q) in enumerate(lines, 1):
            if s.startswith('RAW:'):
                code = s[4:]
            else:
                code = self.e._one('SELECT channel_product_code c FROM channel_product_map WHERE sku=? AND channel=?',
                                   (s, channel))['c']
            w.writerow([oid, client, channel, f'2026-10-26 09:{self.n:02d}:00', '테*트', zipcode, address,
                        (BASE + timedelta(days=promised_days)).isoformat(), i, code, s, q])
        self.e.import_orders_csv(buf.getvalue(), 'TEST')
        return oid

    def status(self, oid):
        return self.e._order(oid)['status']

    def rules(self, oid, status=None):
        sql = 'SELECT rule_id FROM exceptions WHERE order_id=?' + (' AND status=?' if status else '')
        return sorted(r['rule_id'] for r in self.e._q(sql, (oid, status) if status else (oid,)))

    def exc(self, oid, rule):
        return self.e._one('SELECT * FROM exceptions WHERE order_id=? AND rule_id=? ORDER BY exception_id DESC',
                           (oid, rule))

    def options(self, oid, rule):
        return {o['code']: o for o in self.e._options(self.exc(oid, rule))}

    def inv(self, sku):
        return self.e._one('SELECT SUM(on_hand_qty) on_hand, SUM(allocated_qty) allocated, '
                           'SUM(on_hand_qty-allocated_qty-hold_qty) available FROM inventory WHERE sku=?', (sku,))

    def ship(self, oid):
        for to in ('PICKING', 'PACKED', 'SHIPPED'):
            self.e.transition(oid, to, 'W01')


class Scenarios(Base):
    def test_ts01_normal_flow(self):
        """TS-01 정상 출고: 실재고 -3, 할당 0, 상태 이력 6건(수집 포함)."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 3)])
        self.assertEqual(self.status(oid), 'CONFIRMED')
        self.e.allocate(oid)
        self.assertEqual(self.inv('C01-001')['allocated'], 3)
        self.ship(oid)
        self.assertEqual(self.status(oid), 'SHIPPED')
        self.assertEqual(self.inv('C01-001')['on_hand'], 7)
        self.assertEqual(self.inv('C01-001')['allocated'], 0)
        hist = self.e._q("SELECT * FROM audit_log WHERE entity='order' AND entity_id=? AND field='status'", (oid,))
        self.assertEqual([h['after_value'] for h in hist],
                         ['RECEIVED', 'CONFIRMED', 'ALLOCATED', 'PICKING', 'PACKED', 'SHIPPED'])
        ship = self.e._one('SELECT * FROM shipments WHERE order_id=?', (oid,))
        self.assertTrue(ship['tracking_no'].startswith('CJ'))

    def test_ts02_shortage_partial_allowed(self):
        """TS-02 재고부족 + 부분출고 허용 화주(C01): E01, 보류, 옵션1 활성."""
        self.stock('C01-001', [(6, None)])
        oid = self.order('C01', [('C01-001', 10)])
        self.e.allocate(oid)
        self.assertEqual(self.status(oid), 'ON_HOLD')
        self.assertEqual(self.rules(oid), ['E01'])
        opts = self.options(oid, 'E01')
        self.assertTrue(opts['1']['enabled'])
        self.assertFalse(opts['2']['enabled'])
        self.assertTrue(opts['3']['enabled'])
        self.assertEqual(self.inv('C01-001')['allocated'], 0, '전량 할당 원칙: 부족하면 할당하지 않음')

    def test_ts02b_partial_ship(self):
        """E01 옵션1 부분출고: 출고수량 6으로 줄이고 할당 → 출고."""
        self.stock('C01-001', [(6, None)])
        oid = self.order('C01', [('C01-001', 10)])
        self.e.allocate(oid)
        self.e.resolve(self.exc(oid, 'E01')['exception_id'], '1', 'OP', '화주 동의')
        self.assertEqual(self.status(oid), 'ALLOCATED')
        line = self.e._one('SELECT * FROM order_items WHERE order_id=?', (oid,))
        self.assertEqual((line['ship_qty'], line['short_qty'], line['short_action']), (6, 4, 'CANCEL'))
        self.ship(oid)
        self.assertEqual(self.inv('C01-001')['on_hand'], 0)

    def test_ts02c_partial_backorder(self):
        """E01 옵션1 + 백오더: 잔량 4개로 새 주문을 만들고, 재고가 없으니 E01로 보류."""
        self.stock('C01-001', [(6, None)])
        oid = self.order('C01', [('C01-001', 10)])
        self.e.allocate(oid)
        self.e.resolve(self.exc(oid, 'E01')['exception_id'], '1', 'OP', '', {'remainder': 'BACKORDER'})
        bo = self.e._one('SELECT * FROM orders WHERE parent_order_id=?', (oid,))
        self.assertEqual(bo['order_id'], f'{oid}-B1')
        self.assertEqual(self.e._one('SELECT qty FROM order_items WHERE order_id=?', (bo['order_id'],))['qty'], 4)
        self.assertEqual(bo['status'], 'ON_HOLD')
        self.assertEqual(self.rules(bo['order_id'], 'OPEN'), ['E01'])
        with self.assertRaises(BadRequest):
            oid2 = self.order('C01', [('C01-001', 1)])
            self.e.allocate(oid2)
            self.e.resolve(self.exc(oid2, 'E01')['exception_id'], '1', 'OP', '', {'remainder': 'KEEP'})

    def test_e01_cancel(self):
        """E01 옵션3 취소: 취소, 할당 변화 없음."""
        self.stock('C02-001', [(6, 20)])
        oid = self.order('C02', [('C02-001', 10)])
        self.e.allocate(oid)
        self.e.resolve(self.exc(oid, 'E01')['exception_id'], '3', 'OP', '화주 확인')
        self.assertEqual(self.status(oid), 'CANCELLED')
        self.assertEqual(self.inv('C02-001')['allocated'], 0)

    def test_ts03_shortage_partial_denied_with_inbound(self):
        """TS-03 부분출고 불허(C02) + 약속출고일 전 입고예정: 옵션1 비활성(사유), 옵션2 활성."""
        self.stock('C02-001', [(6, 20)])
        self.inbound('C02-001', 1, 12)
        oid = self.order('C02', [('C02-001', 10)], promised_days=2)
        self.e.allocate(oid)
        opts = self.options(oid, 'E01')
        self.assertFalse(opts['1']['enabled'])
        self.assertIn('불허', opts['1']['reason'])
        self.assertTrue(opts['2']['enabled'])

    def test_ts04_unmapped_code(self):
        """TS-04 매핑 누락: E02 → 매핑 등록 후 재검증·할당."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('RAW:SS-NEW-001', 2)], channel='스마트스토어')
        self.assertEqual(self.status(oid), 'ON_HOLD')
        self.assertEqual(self.rules(oid), ['E02'])
        eid = self.exc(oid, 'E02')['exception_id']
        with self.assertRaises(BadRequest):
            self.e.resolve(eid, '1', 'OP', '', {'mappings': {'SS-NEW-001': 'C02-001'}})  # 다른 화주 SKU
        self.e.resolve(eid, '1', 'OP', '', {'mappings': {'SS-NEW-001': 'C01-001'}})
        self.assertEqual(self.status(oid), 'ALLOCATED')
        self.assertIsNotNone(self.e._one("SELECT 1 FROM channel_product_map WHERE channel_product_code='SS-NEW-001'"))

    def test_ts05_bad_address(self):
        """TS-05 주소 오류: E05 → 잘못된 수정은 거부, 올바른 수정이면 진행."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 1)], zipcode='1234')
        self.assertEqual(self.rules(oid), ['E05'])
        eid = self.exc(oid, 'E05')['exception_id']
        with self.assertRaises(BadRequest):
            self.e.resolve(eid, '1', 'OP', '', {'zipcode': '12a45', 'address': '서울'})
        self.e.resolve(eid, '1', 'OP', '', {'zipcode': '04524', 'address': '서울특별시 중구 세종대로 1'})
        self.assertEqual(self.status(oid), 'ALLOCATED')

    def test_ts06_cutoff_risk(self):
        """TS-06 마감 위험: 경고만 하고 상태 유지, 같은 날 중복 감지 없음."""
        self.stock('C02-001', [(10, 20)])
        oid = self.order('C02', [('C02-001', 1)])
        self.e.allocate(oid)
        self.e.set_clock('2026-10-26 11:25:00')
        self.assertEqual(self.e.run_detection()['new_exceptions'], [])
        self.e.set_clock('2026-10-26 11:35:00')
        self.assertEqual(len(self.e.run_detection()['new_exceptions']), 1)
        self.assertEqual(self.status(oid), 'ALLOCATED')
        self.e.resolve(self.exc(oid, 'E03')['exception_id'], '1', 'OP')
        self.assertEqual(self.e._order(oid)['priority'], 1)
        self.e.set_clock('2026-10-26 11:45:00')
        self.assertEqual(self.e.run_detection()['new_exceptions'], [], '처리 후에도 같은 날 다시 감지하지 않음')

    def test_ts07_cancel_after_picking_stop(self):
        """TS-07 피킹 후 취소 → 출고 중단: 주문은 바로 취소, 재고는 원위치 확인 후에 해제, 실재고 불변."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 3)])
        self.e.allocate(oid)
        self.e.transition(oid, 'PICKING', 'W01')
        self.e.cancel_request(oid)
        self.assertEqual(self.status(oid), 'ON_HOLD')
        self.assertEqual(self.rules(oid), ['E04'])
        self.assertEqual(self.e._order(oid)['cancel_result'], 'PENDING')
        self.e.resolve(self.exc(oid, 'E04')['exception_id'], '1', 'OP')
        o = self.e._order(oid)
        self.assertEqual((o['status'], o['cancel_requested'], o['cancel_result']), ('CANCELLED', 0, 'ACCEPTED'))
        self.assertEqual(self.inv('C01-001')['allocated'], 3, '상품이 돌아오기 전에는 다른 주문에 할당되지 않음')
        self.assertEqual(self.inv('C01-001')['available'], 7)
        check = next(c for c in self.e.verify()['checks'] if c['name'] == '취소 주문 재고')
        self.assertTrue(check['ok'], '원위치 대기(RETURNING)는 정상 상태')
        self.e.confirm_restock(oid, 'W01')
        self.assertEqual(self.inv('C01-001')['allocated'], 0)
        self.assertEqual(self.inv('C01-001')['available'], 10)
        self.assertEqual(self.inv('C01-001')['on_hand'], 10)
        with self.assertRaises(BadRequest):
            self.e.confirm_restock(oid, 'W01')

    def test_ts07b_cancel_after_packing_proceed(self):
        """E04 옵션2 출고 진행: 포장완료 상태로 복귀 후 출고 가능."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 3)])
        self.e.allocate(oid)
        self.e.transition(oid, 'PICKING', 'W01')
        self.e.transition(oid, 'PACKED', 'W01')
        self.e.cancel_request(oid)
        self.e.resolve(self.exc(oid, 'E04')['exception_id'], '2', 'OP')
        o = self.e._order(oid)
        self.assertEqual((o['status'], o['cancel_requested'], o['cancel_result']), ('PACKED', 0, 'REJECTED'),
                         '출고 진행을 결정하면 취소요청 플래그를 내리고 결과를 "거절"로 남김')
        self.e.transition(oid, 'SHIPPED', 'W01')
        self.assertEqual(self.inv('C01-001')['on_hand'], 7)
        check = next(c for c in self.e.verify()['checks'] if c['name'] == '취소요청 처리 상태')
        self.assertTrue(check['ok'])

    def test_ts08_cancel_before_work(self):
        """TS-08 할당 후 즉시 취소: 예외 없이 취소, 할당 해제."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 3)])
        self.e.allocate(oid)
        self.e.cancel_request(oid)
        self.assertEqual(self.status(oid), 'CANCELLED')
        self.assertEqual(self.rules(oid), [])
        self.assertEqual(self.inv('C01-001')['allocated'], 0)

    def test_ts09_cancel_after_shipped_blocked(self):
        """TS-09 출고 후 취소: 409 차단, E07 기록, 상태 불변."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 3)])
        self.e.allocate(oid)
        self.ship(oid)
        with self.assertRaises(Conflict):
            self.e.cancel_request(oid)
        self.assertEqual(self.status(oid), 'SHIPPED')
        self.assertEqual(self.rules(oid), ['E07'])

    def test_ts10_double_ship_blocked(self):
        """TS-10 중복 출고: 차단, 실재고 추가 차감 없음."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 3)])
        self.e.allocate(oid)
        self.ship(oid)
        with self.assertRaises(Conflict):
            self.e.transition(oid, 'SHIPPED', 'W01')
        self.assertEqual(self.inv('C01-001')['on_hand'], 7)

    def test_ts10b_skip_step_blocked(self):
        """전이표에 없는 전이(할당 → 포장완료)는 차단."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 3)])
        self.e.allocate(oid)
        with self.assertRaises(Conflict):
            self.e.transition(oid, 'PACKED', 'W01')
        with self.assertRaises(Conflict):
            self.e.transition(oid, 'CANCELLED', 'W01')
        self.assertEqual(self.status(oid), 'ALLOCATED')
        self.assertEqual(self.rules(oid), ['E07', 'E07'])

    def test_ts11_fefo(self):
        """TS-11 FEFO: 유통기한이 빠른 Lot(D+10)부터 할당."""
        self.stock('C02-001', [(10, 30), (10, 10)])
        oid = self.order('C02', [('C02-001', 3)])
        self.e.allocate(oid)
        a = self.e._one('SELECT i.expiry_date FROM allocations a JOIN inventory i ON i.inventory_id=a.inventory_id '
                        'WHERE a.order_id=?', (oid,))
        self.assertEqual(a['expiry_date'], (BASE + timedelta(days=10)).isoformat())

    def test_ts11b_skip_near_expiry_lot(self):
        """기준(7일) 미만 Lot은 건너뛰고 다음 Lot에서 할당."""
        self.stock('C02-001', [(10, 3), (10, 20)])
        oid = self.order('C02', [('C02-001', 3)])
        self.e.allocate(oid)
        a = self.e._one('SELECT i.expiry_date FROM allocations a JOIN inventory i ON i.inventory_id=a.inventory_id '
                        'WHERE a.order_id=?', (oid,))
        self.assertEqual(a['expiry_date'], (BASE + timedelta(days=20)).isoformat())

    def test_ts12_near_expiry_only(self):
        """TS-12 임박 Lot만 남음: E06 → 옵션1 승인 시 임박 Lot으로 할당."""
        self.stock('C02-001', [(5, 3)])
        oid = self.order('C02', [('C02-001', 2)])
        self.e.allocate(oid)
        self.assertEqual(self.rules(oid), ['E06'])
        self.e.resolve(self.exc(oid, 'E06')['exception_id'], '1', 'OP', '화주 승인')
        self.assertEqual(self.status(oid), 'ALLOCATED')
        self.assertEqual(self.inv('C02-001')['allocated'], 2)

    def test_ts12b_near_expiry_to_shortage(self):
        """E06 옵션2: 재고부족(E01)으로 전환, 보류 유지."""
        self.stock('C02-001', [(5, 3)])
        oid = self.order('C02', [('C02-001', 2)])
        self.e.allocate(oid)
        self.e.resolve(self.exc(oid, 'E06')['exception_id'], '2', 'OP')
        self.assertEqual(self.status(oid), 'ON_HOLD')
        self.assertEqual(self.rules(oid, 'OPEN'), ['E01'])

    def test_ts13_competing_orders(self):
        """TS-13 같은 재고 경쟁: 먼저 들어온 주문만 할당, 가용재고는 음수가 되지 않음."""
        self.stock('C01-001', [(5, None)])
        x = self.order('C01', [('C01-001', 3)])
        y = self.order('C01', [('C01-001', 3)])
        self.e.allocate_all()
        self.assertEqual(self.status(x), 'ALLOCATED')
        self.assertEqual(self.status(y), 'ON_HOLD')
        self.assertEqual(self.rules(y), ['E01'])
        self.assertEqual(self.inv('C01-001')['available'], 2)

    def test_ts14_resolve_returns_to_flow(self):
        """TS-14 예외 해결 후 복귀: 입고 대기 → 입고 처리 → 재할당, 예외 RESOLVED, 이력 기록."""
        self.stock('C01-001', [(6, None)])
        self.inbound('C01-001', 0, 10)
        oid = self.order('C01', [('C01-001', 10)])
        self.e.allocate(oid)
        eid = self.exc(oid, 'E01')['exception_id']
        self.assertTrue(self.options(oid, 'E01')['2']['enabled'])
        self.e.resolve(eid, '2', 'OP', '오후 입고분 사용')
        self.assertEqual(self.status(oid), 'ON_HOLD', '승인만으로는 아직 입고되지 않은 재고를 할당하지 않음')
        self.assertEqual(self.e._order(oid)['waiting_inbound'], 1)
        self.assertEqual(self.inv('C01-001')['on_hand'], 6)
        self.assertFalse(self.options(oid, 'E01')['2']['enabled'], '이미 입고 대기 중')
        r = self.e.process_inbound('OP')
        self.assertIn(oid, r['released'])
        self.assertEqual(self.status(oid), 'ALLOCATED')
        self.assertEqual(self.e._one('SELECT status FROM exceptions WHERE exception_id=?', (eid,))['status'], 'RESOLVED')
        hist = [h['after_value'] for h in self.e._q(
            "SELECT * FROM audit_log WHERE entity='order' AND entity_id=? AND field='status' ORDER BY log_id", (oid,))]
        self.assertEqual(hist[-3:], ['ON_HOLD', 'CONFIRMED', 'ALLOCATED'])
        self.assertEqual(self.inv('C01-001')['on_hand'], 16)

    def test_inbound_waits_until_expected_date(self):
        """입고예정일(D+1) 전에는 입고 처리를 해도 할당하지 않고, 당일이 되면 재할당."""
        self.stock('C02-001', [(6, 20)])
        self.inbound('C02-001', 1, 12)
        oid = self.order('C02', [('C02-001', 10)], promised_days=2)
        self.e.allocate(oid)
        self.e.resolve(self.exc(oid, 'E01')['exception_id'], '2', 'OP')
        mine = f'IB-T{self.n - 1}C02-001'  # inbound()가 만든 ID (order() 호출 전 번호)
        r = self.e.process_inbound('OP')
        self.assertNotIn(mine, r['received'], '입고예정일 전에는 입고하지 않음')
        self.assertEqual(self.status(oid), 'ON_HOLD')
        self.e.set_clock('2026-10-27 09:00:00')
        r = self.e.process_inbound('OP')
        self.assertIn(mine, r['received'])
        self.assertEqual(self.status(oid), 'ALLOCATED')
        self.assertEqual(self.exc(oid, 'E01')['status'], 'RESOLVED')
        self.assertEqual(self.inv('C02-001')['on_hand'], 18)

    def test_stop_after_pack_cancels_label(self):
        """E04 출고 중단: 발행된 송장도 취소 처리, 검증의 '취소 주문 송장' 항목 통과."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 3)])
        self.e.allocate(oid)
        self.e.transition(oid, 'PICKING', 'W01')
        self.e.transition(oid, 'PACKED', 'W01')
        self.e.cancel_request(oid)
        self.e.resolve(self.exc(oid, 'E04')['exception_id'], '1', 'OP')
        sh = self.e._one('SELECT * FROM shipments WHERE order_id=?', (oid,))
        self.assertIsNotNone(sh['tracking_no'])
        self.assertIsNotNone(sh['cancelled_at'])
        check = next(c for c in self.e.verify()['checks'] if c['name'] == '취소 주문 송장')
        self.assertTrue(check['ok'])

    def test_overlapping_e02_e05(self):
        """한 주문에 매핑 누락과 주소 오류가 함께: 둘 다 처리해야 진행."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('RAW:SS-NEW-002', 1)], channel='스마트스토어', zipcode='')
        self.assertEqual(self.rules(oid), ['E02', 'E05'])
        self.e.resolve(self.exc(oid, 'E02')['exception_id'], '1', 'OP', '', {'mappings': {'SS-NEW-002': 'C01-001'}})
        self.assertEqual(self.status(oid), 'ON_HOLD', 'E05가 남아 있으면 계속 보류')
        self.e.resolve(self.exc(oid, 'E05')['exception_id'], '1', 'OP', '',
                       {'zipcode': '04524', 'address': '서울특별시 중구 세종대로 110'})
        self.assertEqual(self.status(oid), 'ALLOCATED')

    def test_inquiry_then_resolve(self):
        """E05 옵션2 화주 문의: 처리중(IN_PROGRESS)으로 바뀌고 보류 유지 → 이후 옵션1로 완료."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 1)], zipcode='1234')
        eid = self.exc(oid, 'E05')['exception_id']
        self.e.resolve(eid, '2', 'OP', '화주 문의')
        self.assertEqual(self.exc(oid, 'E05')['status'], 'IN_PROGRESS')
        self.assertEqual(self.status(oid), 'ON_HOLD')
        self.e.resolve(eid, '1', 'OP', '', {'zipcode': '04524', 'address': '서울특별시 중구 세종대로 110'})
        self.assertEqual(self.status(oid), 'ALLOCATED')

    def test_cutoff_next_day(self):
        """E03 옵션2 익일 전환: 약속출고일 +1, 같은 날 다시 감지하지 않음."""
        self.stock('C02-001', [(10, 20)])
        oid = self.order('C02', [('C02-001', 1)])
        self.e.allocate(oid)
        self.e.set_clock('2026-10-26 11:35:00')
        self.e.run_detection()
        self.e.resolve(self.exc(oid, 'E03')['exception_id'], '2', 'OP')
        self.assertEqual(self.e._order(oid)['promised_ship_date'], '2026-10-27')
        self.e.set_clock('2026-10-26 11:45:00')
        self.assertEqual(self.e.run_detection()['new_exceptions'], [])

    # ---------------------------------------------------------------- 코드 검토 회귀 (v0.3)
    def test_dup_sku_lines_partial(self):
        """같은 SKU가 두 라인(4+4)이고 가용 5: 부분출고 시 4+1 출고, 잔량 3, 재할당 성공."""
        self.stock('C01-001', [(5, None)])
        oid = self.order('C01', [('C01-001', 4), ('C01-001', 4)])
        self.e.allocate(oid)
        self.assertEqual(self.rules(oid), ['E01'])
        short = self.e._short_lines(oid)
        self.assertEqual([(s['line_no'], s['usable']) for s in short], [(2, 1)], '뒤 라인은 앞 라인이 쓴 재고를 뺀 나머지만 봄')
        self.e.resolve(self.exc(oid, 'E01')['exception_id'], '1', 'OP', '', {'remainder': 'CANCEL'})
        self.assertEqual(self.status(oid), 'ALLOCATED')
        self.assertEqual([(l['ship_qty'], l['short_qty']) for l in self.e._items(oid)], [(4, 0), (1, 3)])
        self.assertEqual(self.inv('C01-001')['available'], 0)
        self.assertEqual(self.rules(oid, 'OPEN'), [])

    def test_dup_sku_lines_no_overcommit(self):
        """같은 SKU 두 라인(4+4), 가용 8: 라인마다 4씩 할당, 초과 할당 없음."""
        self.stock('C01-001', [(8, None)])
        oid = self.order('C01', [('C01-001', 4), ('C01-001', 4)])
        self.e.allocate(oid)
        self.assertEqual(self.status(oid), 'ALLOCATED')
        per_line = self.e._q('SELECT line_no, SUM(qty) q FROM allocations WHERE order_id=? GROUP BY line_no', (oid,))
        self.assertEqual([(r['line_no'], r['q']) for r in per_line], [(1, 4), (2, 4)])
        check = next(c for c in self.e.verify()['checks'] if c['name'] == '라인별 할당 수량')
        self.assertTrue(check['ok'])

    def test_dup_sku_inbound_gap_summed(self):
        """같은 SKU 두 라인이 부족하면 부족분을 합쳐 입고예정과 비교 (C02, 가용 5, 4+4)."""
        self.stock('C02-001', [(5, 20)])
        self.inbound('C02-001', 1, 2)  # 부족 3 > 입고 2 → 불가
        oid = self.order('C02', [('C02-001', 4), ('C02-001', 4)], promised_days=2)
        self.e.allocate(oid)
        self.assertFalse(self.options(oid, 'E01')['2']['enabled'])
        self.inbound('C02-001', 1, 1)  # 합계 3 = 부족 3 → 가능
        self.assertTrue(self.options(oid, 'E01')['2']['enabled'])

    def test_mapping_is_per_client(self):
        """두 화주가 같은 채널·같은 상품코드를 써도 매핑이 서로 덮어쓰지 않음."""
        c01 = self.e._one("SELECT * FROM channel_product_map WHERE client_id='C01' AND channel='쿠팡' ORDER BY sku LIMIT 1")
        code = c01['channel_product_code']
        self.stock(c01['sku'], [(10, None)])
        self.stock('C02-001', [(10, 20)])
        o2 = self.order('C02', [('RAW:' + code, 1)], channel='쿠팡')
        self.assertEqual(self.rules(o2), ['E02'], 'C01의 코드는 C02 매핑이 아님')
        self.e.resolve(self.exc(o2, 'E02')['exception_id'], '1', 'OP', '', {'mappings': {code: 'C02-001'}})
        self.assertEqual(self.status(o2), 'ALLOCATED')
        o1 = self.order('C01', [('RAW:' + code, 1)], channel='쿠팡')
        self.assertEqual(self.status(o1), 'CONFIRMED', 'C01 매핑이 그대로 남아 있음')
        self.assertEqual(self.e._items(o1)[0]['sku'], c01['sku'])
        rows = self.e._q("SELECT client_id, sku FROM channel_product_map WHERE channel='쿠팡' AND channel_product_code=? "
                         'ORDER BY client_id', (code,))
        self.assertEqual([(r['client_id'], r['sku']) for r in rows], [('C01', c01['sku']), ('C02', 'C02-001')])

    def test_expired_lot_is_not_near_expiry(self):
        """유통기한 경과(D-1)·당일(D+0) Lot은 가용에서 제외 → E06이 아니라 E01."""
        for days in (-1, 0):
            self.stock('C02-001', [(5, days)])
            oid = self.order('C02', [('C02-001', 2)])
            self.e.allocate(oid)
            self.assertEqual(self.rules(oid), ['E01'], f'잔여 {days}일 Lot')

    def test_e06_approval_never_uses_expired_lot(self):
        """E06 승인은 최소 잔여일만 완화: 경과 Lot(D-1)은 건너뛰고 임박 Lot(D+3)만 할당."""
        self.stock('C02-001', [(5, -1), (5, 3)])
        oid = self.order('C02', [('C02-001', 2)])
        self.e.allocate(oid)
        self.assertEqual(self.rules(oid), ['E06'])
        self.e.resolve(self.exc(oid, 'E06')['exception_id'], '1', 'OP', '화주 승인')
        exp = self.e._one('SELECT i.expiry_date FROM allocations a JOIN inventory i ON i.inventory_id=a.inventory_id '
                          'WHERE a.order_id=?', (oid,))['expiry_date']
        self.assertEqual(exp, (BASE + timedelta(days=3)).isoformat())
        check = next(c for c in self.e.verify()['checks'] if c['name'] == '만료 Lot 할당')
        self.assertTrue(check['ok'])

    # ---------------------------------------------------------------- 코드 검토 2차 (v0.4)
    def test_cancel_before_pick_releases_immediately(self):
        """피킹 전 취소는 상품을 꺼내지 않았으므로 원위치 확인 없이 바로 재고 해제."""
        self.stock('C01-001', [(10, None)])
        oid = self.order('C01', [('C01-001', 3)])
        self.e.allocate(oid)
        self.e.cancel_request(oid)
        o = self.e._order(oid)
        self.assertEqual((o['status'], o['cancel_result']), ('CANCELLED', 'ACCEPTED'))
        self.assertEqual(self.inv('C01-001')['allocated'], 0)
        self.assertFalse(any(a['action'] == 'confirm-restock' for a in self.e.order_detail(oid)['actions']))

    def test_priority_first_in_work_queue(self):
        """E03 '우선 작업 지정' 주문은 피킹 대기열과 주문 목록 맨 앞, 할당도 먼저."""
        self.stock('C01-001', [(10, None)])
        a = self.order('C01', [('C01-001', 1)])
        b = self.order('C01', [('C01-001', 1)])  # 나중 주문
        self.e.allocate_all()
        self.assertEqual([o['order_id'] for o in self.e.work_queue()][:2], [a, b], '기본은 주문시각 순')
        self.e.set_clock('2026-10-26 13:35:00')
        self.e.run_detection()
        self.e.resolve(self.exc(b, 'E03')['exception_id'], '1', 'OP')
        q = self.e.work_queue()
        self.assertEqual((q[0]['order_id'], q[0]['rank']), (b, 1))
        self.assertEqual(self.e.list_orders()[0]['order_id'], b)

    def test_priority_allocates_first(self):
        """같은 재고를 두고 경쟁하면 우선 작업 주문이 먼저 할당."""
        self.stock('C01-001', [(3, None)])
        x = self.order('C01', [('C01-001', 3)])
        y = self.order('C01', [('C01-001', 3)])
        with self.e.tx():
            self.e._x('UPDATE orders SET priority=1 WHERE order_id=?', (y,))
        self.e.allocate_all()
        self.assertEqual((self.status(x), self.status(y)), ('ON_HOLD', 'ALLOCATED'))

    def test_work_queue_cutoff_order(self):
        """우선순위가 같으면 오늘 출고분 중 화주 마감이 빠른 주문(C02 12:00)이 먼저."""
        self.stock('C01-001', [(10, None)])
        self.stock('C02-001', [(10, 20)])
        a = self.order('C01', [('C01-001', 1)])   # 먼저 들어왔지만 마감 14:00
        b = self.order('C02', [('C02-001', 1)])   # 마감 12:00
        self.e.allocate_all()
        self.assertEqual([o['order_id'] for o in self.e.work_queue()][:2], [b, a])

    def _csv(self, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['order_id', 'client_id', 'channel', 'ordered_at', 'recipient', 'zipcode', 'address',
                    'promised_ship_date', 'line_no', 'channel_product_code', 'product_name', 'qty'])
        w.writerows(rows)
        return buf.getvalue()

    def test_csv_inconsistent_order_rejected(self):
        """같은 주문번호 행들의 화주·채널이 다르거나 라인번호 중복·수량 오류면 그 주문만 거부, 나머지는 등록."""
        m = self.e._one("SELECT * FROM channel_product_map WHERE client_id='C01' AND channel='쿠팡' LIMIT 1")
        code = m['channel_product_code']
        ok = ['T-OK', 'C01', '쿠팡', '2026-10-26 09:00:00', '정*상', '06236', '서울', '2026-10-26', 1, code, '', 1]
        mixed = [['T-MIX', 'C01', '쿠팡', '2026-10-26 09:00:00', '혼*합', '06236', '서울', '2026-10-26', 1, code, '', 1],
                 ['T-MIX', 'C02', '자사몰', '2026-10-26 09:00:00', '혼*합', '06236', '서울', '2026-10-26', 2, code, '', 1]]
        dup = [['T-DUP', 'C01', '쿠팡', '2026-10-26 09:00:00', '중*복', '06236', '서울', '2026-10-26', 1, code, '', 1],
               ['T-DUP', 'C01', '쿠팡', '2026-10-26 09:00:00', '중*복', '06236', '서울', '2026-10-26', 1, code, '', 0]]
        bad = [['T-BAD', 'C09', '쿠팡', '2026/10/26', '오*류', '06236', '서울', '내일', 'x', code, '', 'two']]
        r = self.e.import_orders_csv(self._csv([ok] + mixed + dup + bad))
        self.assertEqual(r['created'], 1)
        rej = {x['order_id']: x['errors'] for x in r['rejected']}
        self.assertEqual(set(rej), {'T-MIX', 'T-DUP', 'T-BAD'})
        self.assertTrue(any('화주 값이 행마다 다름' in e for e in rej['T-MIX']))
        self.assertTrue(any('채널 값이 행마다 다름' in e for e in rej['T-MIX']))
        self.assertTrue(any('라인번호 1 중복' in e for e in rej['T-DUP']))
        self.assertTrue(any('수량은 1 이상' in e for e in rej['T-DUP']))
        self.assertEqual(len(rej['T-BAD']), 5, '화주·주문시각·약속출고일·라인번호·수량')
        self.assertIsNone(self.e._one("SELECT 1 FROM orders WHERE order_id='T-MIX'"))
        self.assertEqual(self.status('T-OK'), 'CONFIRMED')

    # ---------------------------------------------------------------- 재검토 (v0.5)
    def _allow_partial(self, cid):
        with self.e.tx():
            self.e._x('UPDATE clients SET partial_ship_allowed=1 WHERE client_id=?', (cid,))

    def test_e01_e06_combo_keeps_approval(self):
        """A 부족(E01) + B 임박(E06): E06 승인 후 E01 부분출고 → A 2개+잔량 2, B 승인한 3개 출고 (총 5개)."""
        self._allow_partial('C02')
        self.stock('C02-001', [(2, 20)])
        self.stock('C02-002', [(3, 3)])
        oid = self.order('C02', [('C02-001', 4), ('C02-002', 3)])
        self.e.allocate(oid)
        self.assertEqual(self.rules(oid), ['E01', 'E06'])
        self.e.resolve(self.exc(oid, 'E06')['exception_id'], '1', 'OP', '화주 승인')
        self.assertEqual(self.status(oid), 'ON_HOLD', 'E01이 남아 있으면 계속 보류')
        self.e.resolve(self.exc(oid, 'E01')['exception_id'], '1', 'OP', '', {'remainder': 'CANCEL'})
        self.assertEqual(self.status(oid), 'ALLOCATED')
        self.assertEqual([(l['sku'], l['ship_qty'], l['short_qty']) for l in self.e._items(oid)],
                         [('C02-001', 2, 2), ('C02-002', 3, 0)])
        self.assertEqual(self.e._one('SELECT SUM(qty) q FROM allocations WHERE order_id=?', (oid,))['q'], 5)

    def test_e01_first_then_e06(self):
        """순서를 바꿔 E01 부분출고를 먼저 해도 E06 라인은 그대로 두고, 이후 E06 승인으로 진행."""
        self._allow_partial('C02')
        self.stock('C02-001', [(2, 20)])
        self.stock('C02-002', [(3, 3)])
        oid = self.order('C02', [('C02-001', 4), ('C02-002', 3)])
        self.e.allocate(oid)
        self.e.resolve(self.exc(oid, 'E01')['exception_id'], '1', 'OP', '', {'remainder': 'CANCEL'})
        self.assertEqual([(l['ship_qty'], l['short_qty']) for l in self.e._items(oid)], [(2, 2), (3, 0)],
                         'E01 처리가 E06 라인(B)을 잔량 처리하지 않음')
        self.assertEqual(self.status(oid), 'ON_HOLD')
        self.e.resolve(self.exc(oid, 'E06')['exception_id'], '1', 'OP')
        self.assertEqual(self.status(oid), 'ALLOCATED')

    def test_e06_reject_then_partial(self):
        """E06 거절 → B는 재고부족(E01)으로 넘어가고, 부분출고하면 B 전량 잔량, A만 출고."""
        self._allow_partial('C02')
        self.stock('C02-001', [(4, 20)])
        self.stock('C02-002', [(3, 3)])
        oid = self.order('C02', [('C02-001', 4), ('C02-002', 3)])
        self.e.allocate(oid)
        self.e.resolve(self.exc(oid, 'E06')['exception_id'], '2', 'OP')
        self.e.resolve(self.exc(oid, 'E01')['exception_id'], '1', 'OP', '', {'remainder': 'CANCEL'})
        self.assertEqual([(l['ship_qty'], l['short_qty']) for l in self.e._items(oid)], [(4, 0), (0, 3)])
        self.assertEqual(self.status(oid), 'ALLOCATED')

    def test_ship_blocked_when_lot_expired_after_allocation(self):
        """10-26 할당(유통기한 11-03) → 11-04 출고 시도: 출고 직전 재검증으로 차단, 실재고 불변."""
        self.stock('C02-001', [(5, 8)])
        oid = self.order('C02', [('C02-001', 1)])
        self.e.allocate(oid)
        self.ship_until_packed(oid)
        self.e.set_clock('2026-11-04 09:00:00')
        with self.assertRaises(Conflict) as cm:
            self.e.transition(oid, 'SHIPPED', 'W01')
        self.assertIn('만료', str(cm.exception))
        self.assertEqual(self.status(oid), 'PACKED')
        self.assertEqual(self.inv('C02-001')['on_hand'], 5)
        check = next(c for c in self.e.verify()['checks'] if c['name'] == '만료 Lot 출고')
        self.assertTrue(check['ok'])

    def test_ship_blocked_below_min_days_unless_approved(self):
        """할당 땐 잔여 8일(기준 7일 충족) → 출고일엔 잔여 3일: 미승인 라인은 차단, E06 승인 라인은 출고."""
        self.stock('C02-001', [(5, 8)])
        oid = self.order('C02', [('C02-001', 1)])
        self.e.allocate(oid)
        self.ship_until_packed(oid)
        self.e.set_clock('2026-10-31 09:00:00')
        with self.assertRaises(Conflict):
            self.e.transition(oid, 'SHIPPED', 'W01')
        self.e.set_clock('2026-10-26 10:30:00')  # 승인 라인 확인용 주문은 다시 10-26에 할당
        self.stock('C02-002', [(5, 3)])
        o2 = self.order('C02', [('C02-002', 1)])
        self.e.allocate(o2)
        self.e.resolve(self.exc(o2, 'E06')['exception_id'], '1', 'OP')
        self.ship_until_packed(o2)
        self.e.set_clock('2026-10-27 09:00:00')
        self.e.transition(o2, 'SHIPPED', 'W01')
        self.assertEqual(self.status(o2), 'SHIPPED')

    def test_partial_allowed_when_one_item_has_zero_stock(self):
        """A 3개(가용 3) + B 2개(가용 0): 부분출고 가능 → A 3개 출고, B 2개 백오더."""
        self.stock('C01-001', [(3, None)])
        self.stock('C01-002', [(0, None)])
        oid = self.order('C01', [('C01-001', 3), ('C01-002', 2)])
        self.e.allocate(oid)
        self.assertTrue(self.options(oid, 'E01')['1']['enabled'])
        self.e.resolve(self.exc(oid, 'E01')['exception_id'], '1', 'OP', '', {'remainder': 'BACKORDER'})
        self.assertEqual(self.status(oid), 'ALLOCATED')
        self.assertEqual([(l['ship_qty'], l['short_qty']) for l in self.e._items(oid)], [(3, 0), (0, 2)])
        bo = self.e._one('SELECT * FROM orders WHERE parent_order_id=?', (oid,))
        self.assertEqual(self.e._one('SELECT sku, qty FROM order_items WHERE order_id=?', (bo['order_id'],))['qty'], 2)

    def test_partial_disabled_when_nothing_shippable(self):
        """주문 전체에서 지금 출고할 수 있는 수량이 0이면 부분출고 비활성."""
        self.stock('C01-001', [(0, None)])
        oid = self.order('C01', [('C01-001', 3)])
        self.e.allocate(oid)
        opt = self.options(oid, 'E01')['1']
        self.assertFalse(opt['enabled'])
        self.assertIn('주문 전체', opt['reason'])

    def test_mapping_owner_enforced_by_db(self):
        """C01 상품코드를 C02 SKU에 잇는 매핑은 DB(복합 FK)에서 거부."""
        import sqlite3
        c01 = self.e._one("SELECT * FROM channel_product_map WHERE client_id='C01' LIMIT 1")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.e.tx():
                self.e._x('UPDATE channel_product_map SET sku=? WHERE client_id=? AND channel=? AND channel_product_code=?',
                          ('C02-001', 'C01', c01['channel'], c01['channel_product_code']))

    def test_mapping_owner_checked_at_order_validation(self):
        """제약을 우회해 들어온 잘못된 매핑이 있어도 주문 검증에서 E02로 막음 (다른 화주 상품 출고 방지)."""
        c01 = self.e._one("SELECT * FROM channel_product_map WHERE client_id='C01' AND channel='쿠팡' LIMIT 1")
        self.e.db.execute('PRAGMA foreign_keys = OFF')
        self.e.db.execute('UPDATE channel_product_map SET sku=? WHERE client_id=? AND channel=? AND channel_product_code=?',
                          ('C02-001', 'C01', '쿠팡', c01['channel_product_code']))
        self.e.db.execute('PRAGMA foreign_keys = ON')
        oid = self.order('C01', [('RAW:' + c01['channel_product_code'], 1)], channel='쿠팡')
        self.assertEqual(self.rules(oid), ['E02'])
        detail = __import__('json').loads(self.exc(oid, 'E02')['detail'])
        self.assertEqual(detail['owner_mismatch'], [f'{c01["channel_product_code"]}→C02-001'])

    def test_master_load_rejects_bad_mapping(self):
        """시드 매핑 CSV에 화주가 다른 SKU가 있으면 적재 단계에서 거부."""
        import shutil as sh
        bad = os.path.join(self.tmp, 'seed')
        sh.copytree(self.e.seed_dir, bad)
        path = os.path.join(bad, 'channel_product_map.csv')
        with open(path, encoding='utf-8-sig') as f:
            rows = f.read().splitlines()
        cols = rows[1].split(',')
        cols[3] = 'C02-001' if cols[2] != 'C02' else 'C01-001'
        rows[1] = ','.join(cols)
        with open(path, 'w', encoding='utf-8-sig') as f:
            f.write('\n'.join(rows) + '\n')
        with self.assertRaises(ValueError) as cm:
            Engine(os.path.join(self.tmp, 'bad.db'), seed_dir=bad)
        self.assertIn('매핑 화주와 SKU 소유 화주가 다름', str(cm.exception))

    def ship_until_packed(self, oid):
        self.e.transition(oid, 'PICKING', 'W01')
        self.e.transition(oid, 'PACKED', 'W01')

    def test_resolution_scenarios(self):
        """처리 시나리오 12개 (검증 탭과 같은 내용): 전부 통과."""
        import scenarios
        r = scenarios.run_all()
        failed = [(s['id'], s['error'], [c for c in s['checks'] if not c['ok']]) for s in r['scenarios'] if not s['pass']]
        self.assertEqual(failed, [])
        self.assertEqual(r['total'], 12)

    def test_disabled_option_rejected(self):
        """선택할 수 없는 옵션은 서버에서도 거부 (화면 우회 방지)."""
        self.stock('C02-001', [(6, 20)])
        oid = self.order('C02', [('C02-001', 10)])
        self.e.allocate(oid)
        with self.assertRaises(BadRequest):
            self.e.resolve(self.exc(oid, 'E01')['exception_id'], '1', 'OP')
        self.assertEqual(self.status(oid), 'ON_HOLD')

    def test_ts15_full_replay_matches_labels(self):
        """TS-15 시드 300건 전체 재생: 정답 라벨과 전부 일치, 정합성 체크 전부 통과."""
        r = self.e.replay()
        self.assertEqual(r['conflicts'], [])
        v = self.e.verify()
        failed = [c for c in v['checks'] if not c['ok']]
        self.assertEqual(failed, [])
        self.assertEqual(v['mismatches'], [])
        self.assertTrue(v['pass'])


if __name__ == '__main__':
    unittest.main()
