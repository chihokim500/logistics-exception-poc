"""물류 PoC 웹 서버 (Python 표준 라이브러리만 사용).

실행: python app/server.py [--port 8000] [--db data/logistics.db]
브라우저: http://localhost:8000
"""
import argparse
import json
import mimetypes
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import ROOT, BadRequest, Conflict, Engine, NotFound  # noqa: E402
import scenarios  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
ENGINE = None
ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(f'^{pattern}$'), fn))
        return fn
    return deco


# ---------------------------------------------------------------- 조회
@route('GET', '/api/summary')
def summary(q, body):
    return ENGINE.summary()


@route('GET', '/api/orders')
def orders(q, body):
    return ENGINE.list_orders(q.get('status'), q.get('client'), q.get('q'))


@route('GET', '/api/work-queue')
def work_queue(q, body):
    return ENGINE.work_queue()


@route('GET', r'/api/orders/(?P<oid>[^/]+)')
def order(q, body, oid):
    return ENGINE.order_detail(oid)


@route('GET', '/api/inventory')
def inventory(q, body):
    return ENGINE.inventory(q.get('client'), q.get('q'))


@route('GET', '/api/products')
def products(q, body):
    return ENGINE.products(q.get('client'))


@route('GET', '/api/exceptions')
def exceptions(q, body):
    return ENGINE.list_exceptions(q.get('status', 'open'), q.get('rule'))


@route('GET', r'/api/exceptions/(?P<eid>[^/]+)')
def exception(q, body, eid):
    return ENGINE.exception_detail(eid)


@route('GET', '/api/policies')
def policies(q, body):
    return list(ENGINE.policies.values())


@route('GET', '/api/verify')
def verify(q, body):
    return ENGINE.verify()


@route('GET', '/api/verify/scenarios')
def verify_scenarios(q, body):
    return scenarios.run_all(ENGINE.seed_dir)  # 임시 DB에서 실행, 현재 DB는 건드리지 않음


# ---------------------------------------------------------------- 변경 (운영자 승인 화면에서만 호출)
@route('POST', '/api/orders/import')
def import_orders(q, body):
    return ENGINE.import_orders_csv(body if isinstance(body, str) else body.get('csv', ''), 'OPERATOR')


@route('POST', r'/api/orders/(?P<oid>[^/]+)/allocate')
def allocate(q, body, oid):
    return ENGINE.allocate(oid, body.get('actor') or 'OPERATOR')


@route('POST', r'/api/orders/(?P<oid>[^/]+)/transition')
def transition(q, body, oid):
    return ENGINE.transition(oid, body.get('to'), body.get('actor') or 'OPERATOR', body.get('carrier'))


@route('POST', r'/api/orders/(?P<oid>[^/]+)/cancel-request')
def cancel_request(q, body, oid):
    return ENGINE.cancel_request(oid, body.get('actor') or 'CHANNEL')


@route('POST', r'/api/orders/(?P<oid>[^/]+)/confirm-restock')
def confirm_restock(q, body, oid):
    return ENGINE.confirm_restock(oid, body.get('actor') or 'OPERATOR')


@route('POST', r'/api/exceptions/(?P<eid>[^/]+)/resolve')
def resolve(q, body, eid):
    return ENGINE.resolve(eid, str(body.get('option', '')), body.get('actor') or 'OPERATOR',
                          body.get('reason') or '', body.get('params') or {})


@route('POST', '/api/sim/reset')
def sim_reset(q, body):
    ENGINE.reset()
    return ENGINE.summary()


@route('POST', '/api/sim/replay')
def sim_replay(q, body):
    return ENGINE.replay(body.get('until') or None)


@route('POST', '/api/sim/clock')
def sim_clock(q, body):
    return {'clock': ENGINE.set_clock(body.get('clock'))}


@route('POST', '/api/sim/run-detection')
def sim_detect(q, body):
    return ENGINE.run_detection('OPERATOR')


@route('POST', '/api/sim/receive-inbound')
def sim_inbound(q, body):
    return ENGINE.process_inbound('OPERATOR')


class Handler(BaseHTTPRequestHandler):
    server_version = 'LogisticsPoC/0.1'

    def log_message(self, fmt, *args):
        if '/api/' in (args[0] if args else '') and not str(args[0]).startswith('GET'):
            sys.stderr.write('%s %s\n' % (self.log_date_time_string(), fmt % args))

    def _send(self, code, payload, ctype='application/json; charset=utf-8'):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urlparse(self.path)
        if not url.path.startswith('/api/'):
            return self._static(url.path) if method == 'GET' else self._send(405, {'error': 'Method Not Allowed'})
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = {}
        if method == 'POST':
            raw = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode('utf-8-sig')
            if 'json' in (self.headers.get('Content-Type') or ''):
                try:
                    body = json.loads(raw or '{}')
                except json.JSONDecodeError:
                    return self._send(400, {'error': 'JSON 형식 오류'})
            else:
                body = raw
        for m, pat, fn in ROUTES:
            match = pat.match(url.path)
            if m == method and match:
                try:
                    return self._send(200, fn(q, body, **match.groupdict()))
                except Conflict as e:
                    return self._send(409, {'error': str(e), 'logged_as': 'E07'})
                except BadRequest as e:
                    return self._send(400, {'error': str(e)})
                except NotFound as e:
                    return self._send(404, {'error': str(e)})
                except Exception as e:  # noqa: BLE001 - 데모 서버: 원인을 화면에 보여줌
                    traceback.print_exc()
                    return self._send(500, {'error': f'{type(e).__name__}: {e}'})
        return self._send(404, {'error': f'없는 API: {method} {url.path}'})

    def _static(self, path):
        rel = 'index.html' if path in ('', '/') else path.lstrip('/')
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, {'error': 'not found'})
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype.endswith('javascript'):
            ctype += '; charset=utf-8'
        with open(full, 'rb') as f:
            self._send(200, f.read(), ctype)

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')


def main():
    global ENGINE
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8000)))
    ap.add_argument('--db', default=os.path.join(ROOT, 'data', 'logistics.db'))
    args = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    ENGINE = Engine(args.db)
    httpd = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'물류 PoC 서버: http://localhost:{args.port}  (DB: {args.db})  종료: Ctrl+C', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
