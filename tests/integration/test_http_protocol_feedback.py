"""Local HTTP wire -> raw metadata -> actual model-result serializer, no model or DB."""
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from workbench_executor.http_sessions import HttpSessionRegistry
from workbench_executor.policy import NetworkPolicy, ScopeRuleSpec
from workbench_executor.settings import ExecutorSettings

from agent_workbench.contracts.enums import OperationStatus
from agent_workbench.contracts.tool import ToolResult
from agent_workbench.tools.http import _protocol_feedback
from agent_workbench.tools.remote import json_text


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.respond()

    def respond(self):
        code = int(self.path.split('/')[2]) if self.path.startswith('/redirect/') else 200
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('X-Tech', 'mock-server')
        self.send_header('Authorization', 'Basic RESPONSE_SECRET')
        if self.path.startswith('/set') or code != 200:
            self.send_header('Set-Cookie', 'sid="COOKIE_SECRET"; Path=/; HttpOnly; SameSite=Lax')
            self.send_header('Set-Cookie', 'other=SECOND_SECRET; Path=/')
        if code != 200:
            self.send_header('Location', '/final?id=22')
        body = (self.command + ' ' + self.path).encode()
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def local_http():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    policy = NetworkPolicy([ScopeRuleSpec(scheme='http', host='127.0.0.1', port=port, allow_private=True)], [], ExecutorSettings(token='test-token'))
    yield f'http://127.0.0.1:{port}', policy
    server.shutdown()
    server.server_close()


def visible(response):
    # Exactly the same projection/serialization used by run_http_request -> ToolMessage.
    return json.loads(json_text(ToolResult(operation_id=uuid4(), status=OperationStatus.SUCCEEDED, summary='HTTP', data=_protocol_feedback(asdict(response)))))['data']


async def test_cookie_creation_reuse_change_and_raw_evidence(local_http):
    base, policy = local_http
    registry = HttpSessionRegistry(policy)
    try:
        first = await registry.request(identity_id='a', method='GET', url=base+'/set')
        second = await registry.request(identity_id='a', method='GET', url=base+'/check')
        third = await registry.request(identity_id='b', method='GET', url=base+'/check', headers={'Cookie': 'sid=DIFFERENT_SECRET', 'Authorization': 'Bearer REQUEST_SECRET'})
        a, b, c = (visible(x) for x in (first, second, third))
        assert a['http_hops'][0]['request_cookies'] == []
        cookies = a['http_hops'][0]['response_cookies']
        assert len(cookies) == 2 and cookies[0]['attributes']['httponly']
        assert cookies[0]['value_id'] == b['http_hops'][0]['request_cookies'][0]['value_id']
        assert cookies[0]['value_id'] != c['http_hops'][0]['request_cookies'][0]['value_id']
        text = json.dumps([a, b, c])
        for secret in ['COOKIE_SECRET', 'SECOND_SECRET', 'DIFFERENT_SECRET', 'REQUEST_SECRET', 'RESPONSE_SECRET']:
            assert secret not in text
        assert ['x-tech', 'mock-server'] in a['response_headers']
        assert len([h for h in a['response_headers'] if h[0] == 'set-cookie']) == 2
        assert 'COOKIE_SECRET' in json.dumps(first.headers)  # Raw immutable evidence unchanged.
    finally:
        await registry.close()


@pytest.mark.parametrize('code', [302, 303, 307])
async def test_redirect_wire_method_location_cookie_and_disabled_follow(local_http, code):
    base, policy = local_http
    registry = HttpSessionRegistry(policy)
    try:
        raw = await registry.request(identity_id='a', method='POST', url=base+f'/redirect/{code}', content='body')
        hops = visible(raw)['http_hops']
        assert [h['status_code'] for h in hops] == [code, 200]
        assert ['location', '/final?id=22'] in hops[0]['headers']
        assert hops[1]['method'] == ('POST' if code == 307 else 'GET')
        assert hops[1]['request_cookies'][0]['value_id'] == hops[0]['response_cookies'][0]['value_id']
        assert raw.final_url.endswith('/final?id=22')
        stopped = await registry.request(identity_id='b', method='POST', url=base+f'/redirect/{code}', max_redirects=0)
        assert stopped.status_code == code and len(stopped.protocol_hops) == 1
    finally:
        await registry.close()


@pytest.mark.parametrize('params', [None, {}])
async def test_original_query_duplicate_and_encoded_parameters_survive(local_http, params):
    base, policy = local_http
    registry = HttpSessionRegistry(policy)
    try:
        raw = await registry.request(identity_id='a', method='GET', url=base+'/parser.php?id=22&tag=a&tag=b&encoded=%26', params=params)
        assert raw.final_url.endswith('?id=22&tag=a&tag=b&encoded=%26')
        assert b'id=22&tag=a&tag=b&encoded=%26' in raw.body
    finally:
        await registry.close()
