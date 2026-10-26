"""포트폴리오용 시연 화면 캡처 (Edge 헤드리스, 2배 해상도).

사용: 서버를 띄운 뒤  python scripts/capture.py [--base http://localhost:8000]
결과: 포트폴리오_구성안/캡처/*.png
캡처하면서 서버 DB를 초기화·재생하므로, 끝나면 초기화 상태로 되돌립니다.
"""
import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, '..', '포트폴리오_구성안', '캡처')
EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'


def call(base, path, body=None):
    req = urllib.request.Request(base + path, method='POST' if body is not None else 'GET',
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # 409 차단 테스트는 실패가 정상
        return {'status': e.code, **json.loads(e.read())}


def shot(base, name, url, profile, w=1440, h=900, budget=6000):
    path = os.path.abspath(os.path.join(OUT, name + '.png'))
    if os.path.exists(path):
        os.remove(path)
    for attempt in range(3):  # Edge가 이전 프로세스의 프로필 잠금으로 가끔 저장에 실패함 → 새 프로필로 재시도
        prof = tempfile.mkdtemp(prefix='edge-', dir=profile)
        subprocess.run([EDGE, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
                        f'--user-data-dir={prof}', f'--window-size={w},{h}', '--force-device-scale-factor=2',
                        f'--virtual-time-budget={budget}', f'--screenshot={path}', base + url],
                       capture_output=True, timeout=180)
        for _ in range(60):  # Edge 실행기가 먼저 끝나고 파일은 조금 뒤에 써지는 경우가 있음
            if os.path.exists(path) and os.path.getsize(path) > 60000:  # 빈 화면은 약 20KB
                break
            time.sleep(0.5)
        if os.path.exists(path) and os.path.getsize(path) > 60000:
            time.sleep(0.5)
            break
    else:
        raise RuntimeError(f'캡처 실패: {name}')
    print('저장', name, os.path.getsize(path) // 1024, 'KB')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://localhost:8000')
    base = ap.parse_args().base
    os.makedirs(OUT, exist_ok=True)
    profile = tempfile.mkdtemp(prefix='edge-capture-')

    # A: 11:30 시점 (C02 마감 30분 전)
    call(base, '/api/sim/reset', {})
    call(base, '/api/sim/replay', {'until': '2026-10-26 11:30:00'})
    exc = call(base, '/api/exceptions?status=all')
    first = {}
    for e in exc:
        first.setdefault(e['rule_id'], e['exception_id'])
    e01_c02 = next(e['exception_id'] for e in exc if e['rule_id'] == 'E01' and e['client_id'] == 'C02'
                   and e['promised_ship_date'] > '2026-10-26')
    e01_order = next(e['order_id'] for e in exc if e['exception_id'] == e01_c02)
    shot(base, '01_대시보드_예외큐', '/#dashboard', profile)
    shot(base, '02_E01_처리옵션', f'/?exc={e01_c02}&opt=2#dashboard', profile)
    shot(base, '03_E02_매핑등록', f'/?exc={first["E02"]}&opt=1#dashboard', profile)
    shot(base, '04_E04_피킹후취소', f'/?exc={first["E04"]}#dashboard', profile)
    shot(base, '05_재고_Lot_FEFO', '/?client=C02&inv=C02-008#inventory', profile)

    # E03 '우선 작업 지정' → 피킹 대기열 맨 앞, 포장 후 취소 '출고 중단' → 원위치 대기
    e03 = next(e for e in exc if e['rule_id'] == 'E03')
    call(base, f'/api/exceptions/{e03["exception_id"]}/resolve', {'option': '1', 'actor': 'OPERATOR',
                                                                  'reason': '마감 전 우선 처리'})
    shot(base, '11_피킹대기열_우선작업', '/?view=queue#orders', profile)
    packed = next(e for e in exc if e['rule_id'] == 'E04'
                  and call(base, f'/api/orders/{e["order_id"]}')['order']['hold_from_status'] == 'PACKED')
    call(base, f'/api/exceptions/{packed["exception_id"]}/resolve', {'option': '1', 'actor': 'OPERATOR',
                                                                     'reason': '포장 해체 지시'})
    shot(base, '12_출고중단_원위치대기', f'/?order={packed["order_id"]}#orders', profile)

    # B: E01 '입고 후 출고' 승인 → 입고 대기 → 다음날 입고 처리 → 재할당
    call(base, f'/api/exceptions/{e01_c02}/resolve',
         {'option': '2', 'actor': 'OPERATOR', 'reason': '화주 확인: 10/27 입고분으로 출고'})
    shot(base, '06b_주문상세_입고대기', f'/?order={e01_order}#orders', profile)
    call(base, '/api/sim/clock', {'clock': '2026-10-27 09:00:00'})
    call(base, '/api/sim/receive-inbound', {})
    shot(base, '06_주문상세_처리이력', f'/?order={e01_order}#orders', profile)
    call(base, '/api/sim/clock', {'clock': '2026-10-26 11:30:00'})

    # 차단 테스트: 출고완료 주문에 취소요청 → 409 + E07
    shipped = call(base, '/api/orders?status=SHIPPED')[0]['order_id']
    call(base, f'/api/orders/{shipped}/cancel-request', {'actor': 'CHANNEL'})
    shot(base, '07_출고후취소_차단', f'/?order={shipped}#orders', profile)

    # C: 끝까지 재생 → 검증
    call(base, '/api/sim/reset', {})
    call(base, '/api/sim/replay', {'until': ''})
    shot(base, '08_검증_PASS', '/#verify', profile, h=1000)
    shot(base, '09_대시보드_종료시점', '/#dashboard', profile)
    shot(base, '10_처리시나리오_검증', '/?scen=1#verify', profile, h=1000, budget=30000)

    call(base, '/api/sim/reset', {})
    print('완료:', os.path.abspath(OUT))


if __name__ == '__main__':
    main()
