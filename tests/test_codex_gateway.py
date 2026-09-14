"""Local gateway contract tests against a real, disposable HTTP upstream."""

import base64
import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from claude_switcher import codex_gateway as gateway

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def jwt(**payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'e30.{encoded}.signature'


@pytest.mark.parametrize('token', ['', 'bad', 'a.!.b', 'a.W10.b', None, jwt(exp=10)])
def test_account_id_invalid(token):
    assert gateway.account_id_from_token(token) is None


def test_rewrite_headers():
    token = jwt(**{'https://api.openai.com/auth': {'chatgpt_account_id': 'new-id'}})
    incoming = {'aUtHoRiZaTiOn': 'Bearer old', 'ChatGPT-Account-Id': 'old-id',
                'Host': 'elsewhere', 'Content-Length': '50', 'Transfer-Encoding': 'chunked',
                'Connection': 'keep-alive, X-Private', 'X-Private': 'drop',
                'Keep-Alive': 'timeout=10', 'TE': 'trailers', 'Trailer': 'x',
                'Proxy-Authorization': 'secret', 'Proxy-Authenticate': 'secret',
                'Upgrade': 'h2c', 'X-Other': 'same'}
    result = {k.lower(): v for k, v in gateway.rewrite_headers(incoming, token).items()}
    assert result == {'authorization': f'Bearer {token}', 'chatgpt-account-id': 'new-id', 'x-other': 'same'}
    assert incoming['aUtHoRiZaTiOn'] == 'Bearer old'
    result = {k.lower(): v for k, v in gateway.rewrite_headers(incoming, 'invalid').items()}
    assert result['chatgpt-account-id'] == 'old-id'


def test_headers_without_authorization():
    headers = {'chatgpt-account-id': 'old-id', 'X-Other': 'same'}
    assert gateway.rewrite_headers(headers, 'new') == headers


@pytest.mark.parametrize('headers,expected', [({'Upgrade': 'websocket'}, True),
    ({'upgrade': 'WebSocket', 'connection': 'Upgrade'}, True), ({'Upgrade': 'h2c'}, False), ({}, False)])
def test_websocket_detection(headers, expected):
    assert gateway.is_websocket_upgrade(headers) is expected


@pytest.mark.parametrize('status,body,expected', [(429, b'{"error":{"type":"usage_limit_reached"}}', True),
    (429, b'USAGE_NOT_INCLUDED', True), (429, b'usage_limit_reached not json', True),
    (429, b'{"error":"rate_limit_exceeded"}', False), (200, b'usage_limit_reached', False)])
def test_limit_detection(status, body, expected):
    assert gateway.is_usage_limit(status, body) is expected


@pytest.fixture
def proxy():
    servers = []
    connections = []
    records = []
    behavior = Mock(return_value=(200, b'ok'))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            records.append((self.command, self.path, dict(self.headers), body))
            result = behavior(self)
            if result is not None:
                status, payload = result
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(payload)
                    self.wfile.flush()

        do_POST = do_GET
        do_PATCH = do_GET
        do_HEAD = do_GET
        do_OPTIONS = do_GET

        def log_message(self, *args):
            pass

    try:
        try:
            upstream = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except PermissionError as exc:
            pytest.skip(f'sandbox blocks binding loopback sockets: {exc}')
        upstream.daemon_threads = True
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        servers.append((upstream, thread))
        provider = Mock(return_value=('active@test.com', 'active-token'))
        hook = Mock(return_value=False)
        server = gateway.Gateway('127.0.0.1', 0, provider, hook,
                                 upstream=f'http://127.0.0.1:{upstream.server_port}')
        server.start()

        def connect():
            conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=3)
            connections.append(conn)
            return conn

        yield server, connect, provider, hook, records, behavior
    finally:
        for connection in connections:
            connection.close()
        if 'server' in locals():
            server.stop()
        for upstream, thread in servers:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=3)


def test_forward_body_query_and_active_identity(proxy):
    server, connect, provider, hook, records, behavior = proxy
    token = jwt(**{'https://api.openai.com/auth': {'chatgpt_account_id': 'active-id'}})
    provider.return_value = ('active@test.com', token)
    body = b'\x00\xff{"input":"hello"}\r\n'
    conn = connect()
    conn.request('POST', '/backend-api/codex/responses?q=a%2Fb&x=1&x=2', body,
                 {'Authorization': 'Bearer stale', 'chatgpt-account-id': 'stale-id'})
    response = conn.getresponse()
    assert response.status == 200
    assert response.read() == b'ok'
    method, path, headers, received = records[0]
    headers = {k.lower(): v for k, v in headers.items()}
    assert (method, path, received) == ('POST', '/backend-api/codex/responses?q=a%2Fb&x=1&x=2', body)
    assert headers['authorization'] == f'Bearer {token}'
    assert headers['chatgpt-account-id'] == 'active-id'
    assert response.getheader('Transfer-Encoding') == 'chunked'
    assert response.getheader('Content-Length') is None


def test_forward_without_authorization(proxy):
    _, connect, _, hook, records, behavior = proxy
    behavior.return_value = (429, b'usage_limit_reached')
    conn = connect()
    conn.request('POST', '/backend-api/ps/mcp', b'{}', {'chatgpt-account-id': 'keep', 'X-Test': 'keep'})
    response = conn.getresponse()
    assert response.status == 429
    assert response.read() == b'usage_limit_reached'
    headers = {k.lower(): v for k, v in records[0][2].items()}
    assert 'authorization' not in headers
    assert headers['chatgpt-account-id'] == 'keep'
    assert headers['x-test'] == 'keep'
    hook.assert_not_called()


def test_streaming_arrives_before_upstream_finishes(proxy):
    server, _, _, _, _, behavior = proxy
    last_sent = threading.Event()
    finished = threading.Event()

    def stream(handler):
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        for index in range(3):
            if index == 2:
                last_sent.set()
            handler.wfile.write(f'data: {index}\n\n'.encode())
            handler.wfile.flush()
            time.sleep(0.2)
        handler.close_connection = True
        finished.set()

    behavior.side_effect = stream
    with socket.create_connection(('127.0.0.1', server.port), timeout=3) as sock:
        sock.sendall(b'POST /backend-api/codex/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer stale\r\nContent-Length: 0\r\n\r\n')
        received = b''
        while b'data: 0\n\n' not in received:
            chunk = sock.recv(1024)
            assert chunk
            received += chunk
        assert not last_sent.is_set(), 'gateway buffered the SSE response'
        while b'0\r\n\r\n' not in received.split(b'\r\n\r\n', 1)[1][-5:]:
            received += sock.recv(1024)
        assert b'data: 2\n\n' in received
        assert finished.is_set()


def test_websocket_refused_without_upstream(proxy):
    _, connect, provider, _, records, _ = proxy
    conn = connect()
    conn.request('GET', '/backend-api/codex/responses', headers={'Upgrade': 'websocket', 'Connection': 'Upgrade'})
    response = conn.getresponse()
    assert response.status == 426
    assert json.loads(response.read())
    assert records == []
    provider.assert_not_called()


@pytest.mark.parametrize('second_status', [200, 429])
def test_usage_limit_retries_once_with_new_identity(proxy, second_status):
    _, connect, provider, hook, records, behavior = proxy
    provider.side_effect = [('first@test.com', 'first'), ('second@test.com', 'second')]
    hook.return_value = True
    behavior.side_effect = [(429, b'{"error":{"type":"usage_limit_reached"}}'), (second_status, b'retried')]
    conn = connect()
    conn.request('POST', '/responses?repeat=yes', b'same body', {'Authorization': 'Bearer stale'})
    response = conn.getresponse()
    assert response.status == second_status
    assert response.read() == b'retried'
    hook.assert_called_once_with('first@test.com')
    assert provider.call_count == 2
    assert [dict((k.lower(), v) for k, v in r[2].items())['authorization'] for r in records] == ['Bearer first', 'Bearer second']
    assert [(r[0], r[1], r[3]) for r in records] == [('POST', '/responses?repeat=yes', b'same body')] * 2


@pytest.mark.parametrize('body,hook_calls', [(b'usage_limit_reached', 1), (b'usage_not_included', 1),
                                          (b'{"error":"rate_limit_exceeded"}', 0)])
def test_429_relay(proxy, body, hook_calls):
    _, connect, provider, hook, _, behavior = proxy
    behavior.return_value = (429, body)
    conn = connect()
    conn.request('GET', '/usage', headers={'Authorization': 'Bearer old'})
    response = conn.getresponse()
    assert response.status == 429
    assert response.read() == body
    assert hook.call_count == hook_calls
    provider.assert_called_once()


@pytest.mark.parametrize('raises', [False, True])
def test_missing_token_is_503(proxy, raises):
    _, connect, provider, _, records, _ = proxy
    provider.return_value = None
    if raises:
        provider.side_effect = RuntimeError('private details')
    conn = connect()
    conn.request('GET', '/')
    response = conn.getresponse()
    assert response.status == 503
    assert json.loads(response.read()) == {'error': 'no active codex account'}
    assert records == []


def test_upstream_down_is_502(proxy):
    server, connect, _, _, _, _ = proxy
    # macOS times out on a bound, non-listening socket. Close it first so
    # the upstream connection is refused, rather than waiting for 30 seconds.
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    server.upstream = f'http://127.0.0.1:{port}'
    conn = connect()
    conn.request('GET', '/')
    response = conn.getresponse()
    assert response.status == 502
    assert json.loads(response.read())


@pytest.mark.parametrize('method', ['HEAD', 'PATCH', 'OPTIONS'])
def test_other_methods(proxy, method):
    _, connect, _, _, records, _ = proxy
    conn = connect()
    conn.request(method, '/models')
    response = conn.getresponse()
    assert response.status == 200
    assert response.read() == (b'' if method == 'HEAD' else b'ok')
    assert records[0][0] == method


def test_config_roundtrip_preserves_unmanaged_bytes_and_backup(tmp_path):
    path = tmp_path / 'config.toml'
    original = (b'# personal settings\r\nmodel = "gpt-5"\r\n'
                b'openai_base_url = "https://old.example"\r\n'
                b'chatgpt_base_url = "https://old.example/"\r\n'
                b'features = [\r\n  "a",\r\n  "b",\r\n]\r\n\r\n'
                b'[mcp_servers.x]\r\ncommand = "tool"\r\nargs = ["a", "b"]\r\n'
                b'openai_base_url = "leave this table key"\r\n')
    path.write_bytes(original)
    unmanaged = b''.join(line for line in original.splitlines(keepends=True)
                         if line not in [b'openai_base_url = "https://old.example"\r\n',
                                         b'chatgpt_base_url = "https://old.example/"\r\n'])
    assert not gateway.gateway_configured(path)
    gateway.enable_config(8790, path)
    first = path.read_bytes()
    parsed = tomllib.loads(first.decode())
    assert parsed['openai_base_url'] == 'http://127.0.0.1:8790/backend-api/codex'
    assert parsed['chatgpt_base_url'] == 'http://127.0.0.1:8790/backend-api/'
    assert first.endswith(unmanaged)
    assert first.index(b'openai_base_url') < first.index(b'[mcp_servers')
    assert path.stat().st_mode & 0o777 == 0o600
    backup = path.with_name('config.toml.claude-switcher.bak')
    assert backup.read_bytes() == original
    assert gateway.gateway_configured(path)
    gateway.enable_config(8790, path)
    assert path.read_bytes() == first
    gateway.enable_config(8791, path)
    assert backup.read_bytes() == original
    gateway.disable_config(path)
    assert path.read_bytes() == unmanaged
    assert not gateway.gateway_configured(path)
    assert 'openai_base_url' not in tomllib.loads(path.read_text())
    gateway.disable_config(path)
    assert path.read_bytes() == unmanaged


def test_config_missing_and_invalid(tmp_path):
    path = tmp_path / 'new' / 'config.toml'
    gateway.enable_config(8790, path)
    assert gateway.gateway_configured(path)
    assert len(tomllib.loads(path.read_text())) == 2
    path.write_text('invalid = [')
    with pytest.raises(RuntimeError):
        gateway.enable_config(8790, path)
    assert path.read_text() == 'invalid = ['


def test_config_multiline_values_are_untouched(tmp_path):
    path = tmp_path / 'config.toml'
    original = 'description = """\n[mcp_servers.fake]\nopenai_base_url = fake\n"""\narray = [\n[1, 2],\n[3, 4]\n]\nopenai_base_url = "old"\n[mcp_servers.real]\ncommand = "x"\n'
    path.write_text(original)
    gateway.enable_config(8790, path)
    assert tomllib.loads(path.read_text())['openai_base_url'].startswith('http://127.')
    gateway.disable_config(path)
    assert path.read_text() == original.replace('openai_base_url = "old"\n', '')
