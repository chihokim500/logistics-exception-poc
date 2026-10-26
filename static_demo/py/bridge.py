"""정적 데모 브리지 (브라우저 안의 Pyodide에서 실행).

server.py의 라우트(ROUTES)를 그대로 쓰고, HTTP 대신 함수 호출로 요청을 처리합니다.
엔진·검증 로직은 서버 버전과 같은 코드입니다. DB는 메모리에 두므로 새로고침하면 처음 상태로 돌아갑니다.
"""
import json
import sys
import traceback
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, '/home/pyodide/poc/app')

import engine  # noqa: E402
import scenarios  # noqa: E402
import server  # noqa: E402
from engine import BadRequest, Conflict, Engine, NotFound  # noqa: E402

# 처리 시나리오 검증도 파일 대신 메모리 DB에서 실행 (시나리오마다 새 DB)
scenarios.Engine = lambda _path, **kw: Engine(':memory:', **kw)
server.ENGINE = Engine(':memory:')


def _json(code, payload):
    return code, json.dumps(payload, ensure_ascii=False)


def handle(method, path, body, ctype):
    url = urlparse(path)
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    if method != 'POST':
        body = {}
    elif 'json' in (ctype or ''):
        try:
            body = json.loads(body or '{}')
        except json.JSONDecodeError:
            return _json(400, {'error': 'JSON 형식 오류'})
    for m, pat, fn in server.ROUTES:
        match = pat.match(url.path)
        if m == method and match:
            try:
                return _json(200, fn(q, body, **match.groupdict()))
            except Conflict as e:
                return _json(409, {'error': str(e), 'logged_as': 'E07'})
            except BadRequest as e:
                return _json(400, {'error': str(e)})
            except NotFound as e:
                return _json(404, {'error': str(e)})
            except Exception as e:  # noqa: BLE001 - 데모: 원인을 화면에 보여줌
                traceback.print_exc()
                return _json(500, {'error': f'{type(e).__name__}: {e}'})
    return _json(404, {'error': f'없는 API: {method} {url.path}'})
