"""Gateway streaming, concurrency and lifecycle tests over real loopback sockets.

The fake upstream is an asyncio server on one known thread, so the tests can
count the gateway's own threads and observe when the gateway closes its
upstream connection.
"""

import asyncio
import http.client
import json
import hashlib
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from claude_switcher import codex_gateway as gateway
from claude_switcher import codex_tls

# Gateway threads allowed while serving many streams: the worker pool (8), the
# event loop thread, and a small constant. A thread per connection exceeds it.
MAX_GATEWAY_THREADS = 8 + 1 + 2


def _chunk(data):
    return b'%x\r\n%b\r\n' % (len(data), data)


class Upstream:
    """A disposable HTTP/1.1 upstream running on its own event loop thread."""

    def __init__(self, respond, ssl_context=None):
        self.respond = respond
        self.ssl_context = ssl_context
        self.requests = []
        self.closed = []          # monotonic times at which the gateway closed a stream
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self._ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.release = asyncio.Event()
        self.server = self.loop.run_until_complete(asyncio.start_server(
            self._handle, '127.0.0.1', 0, ssl=self.ssl_context))
        self.port = self.server.sockets[0].getsockname()[1]
        self._ready.set()
        self.loop.run_forever()
        tasks = asyncio.all_tasks(self.loop)
        for task in tasks:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        self.server.close()
        self.loop.close()

    async def _handle(self, reader, writer):
        try:
            line = await reader.readline()
            headers = {}
            while (raw := await reader.readline()) not in (b'\r\n', b''):
                key, value = raw.decode('latin-1').split(':', 1)
                headers[key.strip().lower()] = value.strip()
            body = await reader.readexactly(int(headers.get('content-length', 0)))
            path = line.decode('latin-1').split()[1]
            self.requests.append((path, headers, body))
            await self.respond(self, path, reader, writer)
            # Flush everything before closing; abort() would drop buffered bytes.
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 5)
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            writer.transport.abort()

    async def wait_for_gateway_close(self, reader):
        try:
            while await reader.read(65536):
                pass
        except OSError:
            pass
        self.closed.append(time.monotonic())

    def open_release(self):
        self.loop.call_soon_threadsafe(self.release.set)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture(scope='module')
def certs(tmp_path_factory):
    root = tmp_path_factory.mktemp('certs')
    return (codex_tls.ensure_certificate(directory=root / 'gateway'),
            codex_tls.ensure_certificate(directory=root / 'upstream'))


@pytest.fixture
def harness(certs):
    gateway_paths, _ = certs
    upstreams, gateways, sockets = [], [], []

    def upstream(respond, ssl_context=None):
        try:
            server = Upstream(respond, ssl_context)
        except PermissionError as exc:
            pytest.skip(f'sandbox blocks binding loopback sockets: {exc}')
        upstreams.append(server)
        return server

    def start(up, tls=True, scheme='http', **kwargs):
        server = gateway.Gateway(
            '127.0.0.1', 0, Mock(return_value=('active@test.com', 'active-token')),
            Mock(return_value=False), upstream=f'{scheme}://127.0.0.1:{up.port}',
            ssl_context=codex_tls.ssl_context(gateway_paths) if tls else None, **kwargs,
        )
        server.start()
        gateways.append(server)
        return server

    def client_context():
        return ssl.create_default_context(cafile=str(gateway_paths.ca))

    def raw(server, tls=True, timeout=5):
        sock = socket.create_connection(('127.0.0.1', server.port), timeout=timeout)
        if tls:
            sock = client_context().wrap_socket(sock, server_hostname='127.0.0.1')
        sockets.append(sock)
        return sock

    def https(server, timeout=10):
        return http.client.HTTPSConnection('127.0.0.1', server.port, timeout=timeout,
                                           context=client_context())

    yield upstream, start, raw, https
    for sock in sockets:
        sock.close()
    for server in gateways:
        server.stop()
    for server in upstreams:
        server.stop()


def _request(path, body=b'', extra=''):
    return (f'POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer stale\r\n'
            f'{extra}Content-Length: {len(body)}\r\n\r\n').encode() + body


def _read_until(sock, marker, received=b''):
    while marker not in received:
        data = sock.recv(65536)
        assert data, 'connection closed before the expected bytes arrived'
        received += data
    return received


def _connection_ends(sock, within):
    """True when the peer closes (EOF, reset or TLS error) within the time limit."""
    sock.settimeout(within)
    deadline = time.monotonic() + within
    try:
        while time.monotonic() < deadline:
            if not sock.recv(65536):
                return True
    except socket.timeout:
        return False
    except (OSError, ssl.SSLError):
        return True
    return False


def _wait(predicate, within):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


async def _sse_head(writer):
    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                 b'Transfer-Encoding: chunked\r\n\r\n')
    await writer.drain()


def test_twenty_concurrent_streams_share_bounded_threads(harness):
    upstream_factory, start, _, https = harness
    events = 30

    async def respond(up, path, reader, writer):
        client = path.rsplit('/', 1)[1]
        await _sse_head(writer)
        writer.write(_chunk(f'data: {client}-0\n\n'.encode()))
        await writer.drain()
        await up.release.wait()
        for index in range(1, events):
            writer.write(_chunk(f'data: {client}-{index}\n\n'.encode()))
            await writer.drain()
        writer.write(b'0\r\n\r\n')
        await writer.drain()

    up = upstream_factory(respond)
    baseline = set(threading.enumerate())
    server = start(up)
    first_seen = threading.Semaphore(0)
    results, errors = {}, []

    def client(index):
        try:
            conn = https(server)
            conn.request('POST', f'/backend-api/codex/responses/{index}', b'{}',
                         {'Authorization': 'Bearer stale'})
            response = conn.getresponse()
            assert response.status == 200
            received = b''
            while f'{index}-0\n\n'.encode() not in received:
                received += response.read1(65536)
            first_seen.release()
            results[index] = received + response.read()
            conn.close()
        except Exception as exc:   # surfaced below
            errors.append(exc)
            first_seen.release()

    clients = [threading.Thread(target=client, args=(index,), daemon=True) for index in range(20)]
    for thread in clients:
        thread.start()
    try:
        for _ in clients:
            assert first_seen.acquire(timeout=15), 'not every stream started'
        assert not errors, errors
        gateway_threads = set(threading.enumerate()) - baseline - set(clients)
        assert len(gateway_threads) <= MAX_GATEWAY_THREADS, (
            f'{len(gateway_threads)} gateway threads for 20 streams: '
            f'{sorted(thread.name for thread in gateway_threads)}')
    finally:
        up.open_release()
        for thread in clients:
            thread.join(15)
    assert not errors, errors
    for index in range(20):
        expected = ''.join(f'data: {index}-{n}\n\n' for n in range(events)).encode()
        assert results[index] == expected


@pytest.mark.parametrize('tls', [True, False])
def test_client_disconnect_closes_upstream(harness, tls):
    upstream_factory, start, raw, _ = harness

    async def respond(up, path, reader, writer):
        await _sse_head(writer)
        writer.write(_chunk(b'data: first\n\n'))
        await writer.drain()
        # Stay silent, as a model does while it thinks, until the gateway lets go.
        await up.wait_for_gateway_close(reader)

    up = upstream_factory(respond)
    server = start(up, tls=tls)
    sock = raw(server, tls=tls)
    sock.sendall(_request('/backend-api/codex/responses'))
    _read_until(sock, b'data: first')
    sock.close()
    disconnected = time.monotonic()
    assert _wait(lambda: up.closed, 2.5), 'gateway kept the upstream stream open'
    assert up.closed[0] - disconnected < 2


@pytest.mark.parametrize('framing', ['chunked', 'length'])
def test_upstream_death_mid_stream_closes_client(harness, framing):
    upstream_factory, start, raw, _ = harness

    async def respond(up, path, reader, writer):
        if framing == 'chunked':
            await _sse_head(writer)
            writer.write(_chunk(b'data: first\n\n'))
        else:
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\ndata: first\n\n')
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.transport.abort()

    up = upstream_factory(respond)
    server = start(up)
    sock = raw(server)
    sock.sendall(_request('/backend-api/codex/responses'))
    received = _read_until(sock, b'data: first')
    started = time.monotonic()
    sock.settimeout(3)
    try:
        while data := sock.recv(65536):
            received += data
    except (OSError, ssl.SSLError):
        pass
    assert time.monotonic() - started < 3, 'client connection hung after upstream died'
    # A cut stream must not look complete: no terminating zero-length chunk.
    assert not received.endswith(b'\r\n0\r\n\r\n')


def test_stop_with_active_streams_is_prompt_and_restartable(harness):
    upstream_factory, start, raw, https = harness

    async def respond(up, path, reader, writer):
        if path == '/after-restart':
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
            await writer.drain()
            return
        await _sse_head(writer)
        writer.write(_chunk(b'data: first\n\n'))
        await writer.drain()
        await up.wait_for_gateway_close(reader)

    up = upstream_factory(respond)
    server = start(up)
    port = server.port
    streams = []
    for _ in range(3):
        sock = raw(server)
        sock.sendall(_request('/backend-api/codex/responses'))
        _read_until(sock, b'data: first')
        streams.append(sock)
    idle = raw(server)                   # a finished request on a kept-alive connection
    idle.sendall(_request('/after-restart'))
    _read_until(idle, b'0\r\n\r\n')
    stalled = socket.create_connection(('127.0.0.1', port), timeout=5)   # never handshakes
    time.sleep(0.3)                      # accepted, now waiting in the TLS handshake

    started = time.monotonic()
    server.stop()
    assert time.monotonic() - started < 3, 'stop() waited on active streams'
    for sock in streams + [idle]:
        assert _connection_ends(sock, 2), 'a client connection outlived stop()'
    if sys.version_info >= (3, 13):      # Server.abort_clients() reaches TLS handshakes
        assert _connection_ends(stalled, 2), 'a stalled handshake outlived stop()'
    stalled.close()
    assert _wait(lambda: len(up.closed) == 3, 2), 'stop() left upstream streams open'
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(('127.0.0.1', port), timeout=2).close()

    server.start()
    assert server.port == port
    conn = https(server)
    conn.request('GET', '/after-restart')
    response = conn.getresponse()
    assert (response.status, response.read()) == (200, b'ok')
    conn.close()


@pytest.mark.parametrize('upstream_tls', [False, True])
def test_many_tiny_events_arrive_intact_and_in_order(harness, certs, upstream_tls):
    upstream_factory, start, _, https = harness
    _, upstream_paths = certs
    events = [b'data: {"type":"response.output_text.delta","n":%05d,"d":"xy"}\n\n' % index
              for index in range(5000)]
    assert all(55 <= len(event) <= 65 for event in events)

    async def respond(up, path, reader, writer):
        await _sse_head(writer)
        for index, event in enumerate(events):
            writer.write(_chunk(event))
            if index % 50 == 0:
                await writer.drain()
        writer.write(b'0\r\n\r\n')
        await writer.drain()

    kwargs = {}
    if upstream_tls:
        up = upstream_factory(respond, ssl_context=codex_tls.ssl_context(upstream_paths))
        kwargs['upstream_ssl_context'] = ssl.create_default_context(cafile=str(upstream_paths.ca))
        server = start(up, scheme='https', **kwargs)
    else:
        up = upstream_factory(respond)
        server = start(up)
    conn = https(server)
    conn.request('POST', '/backend-api/codex/responses', b'{}', {'Authorization': 'Bearer stale'})
    response = conn.getresponse()
    assert response.status == 200
    assert response.read() == b''.join(events)
    conn.close()
    path, headers, body = up.requests[0]
    assert (path, body) == ('/backend-api/codex/responses', b'{}')
    assert headers['authorization'] == 'Bearer active-token'
    assert headers['host'] == ('127.0.0.1:%d' % up.port)


def test_upstream_tls_is_verified(harness, certs):
    upstream_factory, start, _, https = harness
    _, upstream_paths = certs

    async def respond(up, path, reader, writer):
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
        await writer.drain()

    up = upstream_factory(respond, ssl_context=codex_tls.ssl_context(upstream_paths))
    server = start(up, scheme='https')   # default trust store does not know the test CA
    conn = https(server)
    conn.request('GET', '/')
    response = conn.getresponse()
    assert response.status == 502
    assert json.loads(response.read()) == {'error': 'upstream connection failed'}
    assert up.requests == []


BODY_PARTS = [b'data: one\n\n', b'data: two\n\n', b'x' * 70000, b'data: last\n\n']


@pytest.mark.parametrize('framing', ['chunked', 'close', 'length'])
def test_upstream_body_framings_are_relayed(harness, framing):
    upstream_factory, start, _, https = harness

    async def respond(up, path, reader, writer):
        if framing == 'chunked':
            writer.write(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n'
                         b'X-Kept: yes\r\n\r\n')
            for part in BODY_PARTS:
                # Chunk extensions and upper-case hex, split across writes.
                framed = b'%X;ext=1\r\n%b\r\n' % (len(part), part)
                writer.write(framed[:3])
                await writer.drain()
                await asyncio.sleep(0.01)
                writer.write(framed[3:])
                await writer.drain()
            writer.write(b'0\r\nX-Trailer: dropped\r\n\r\n')
        elif framing == 'close':
            writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nX-Kept: yes\r\n\r\n')
            for part in BODY_PARTS:
                writer.write(part)
                await writer.drain()
                await asyncio.sleep(0.01)
            writer.close()
        else:
            total = sum(map(len, BODY_PARTS))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nX-Kept: yes\r\n\r\n' % total)
            for part in BODY_PARTS:
                writer.write(part)
                await writer.drain()
                await asyncio.sleep(0.01)
        await writer.drain()

    up = upstream_factory(respond)
    server = start(up)
    conn = https(server)
    for attempt in range(2):     # the client connection stays usable afterwards
        conn.request('POST', f'/r{attempt}', b'{}', {'Authorization': 'Bearer stale'})
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader('X-Kept') == 'yes'
        assert response.getheader('Transfer-Encoding') == 'chunked'
        assert response.getheader('Content-Length') is None
        assert response.getheader('X-Trailer') is None
        assert response.read() == b''.join(BODY_PARTS)
    conn.close()
    assert [request[0] for request in up.requests] == ['/r0', '/r1']


def test_keep_alive_serves_sequential_requests_on_one_connection(harness):
    upstream_factory, start, raw, _ = harness

    async def respond(up, path, reader, writer):
        payload = path.encode()
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%b' % (len(payload), payload))
        await writer.drain()

    up = upstream_factory(respond)
    server = start(up, tls=False)
    sock = raw(server, tls=False)
    for index in range(3):
        sock.sendall(_request(f'/call/{index}', b'body'))
        received = _read_until(sock, b'\r\n0\r\n\r\n')
        assert received.startswith(b'HTTP/1.1 200 OK\r\n')
        assert f'/call/{index}'.encode() in received
    assert [(r[0], r[2]) for r in up.requests] == [(f'/call/{i}', b'body') for i in range(3)]


def test_expect_continue_is_answered_before_the_body(harness):
    upstream_factory, start, raw, _ = harness

    async def respond(up, path, reader, writer):
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
        await writer.drain()

    up = upstream_factory(respond)
    server = start(up, tls=False)
    sock = raw(server, tls=False)
    sock.sendall(b'POST /x HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\n'
                 b'Content-Length: 5\r\n\r\n')
    assert _read_until(sock, b'\r\n\r\n') == b'HTTP/1.1 100 Continue\r\n\r\n'
    sock.sendall(b'hello')
    assert b'\r\n\r\n2\r\nok\r\n0\r\n\r\n' in _read_until(sock, b'0\r\n\r\n')
    assert up.requests[0][2] == b'hello'


@pytest.mark.parametrize('request_bytes,status,message', [
    (b'POST /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n', 400,
     'invalid request body framing'),
    (b'POST /x HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nab', 400,
     'invalid request body framing'),
    (b'POST /x HTTP/1.1\r\nContent-Length: -1\r\n\r\n', 400, 'invalid request body framing'),
    (b'POST /x HTTP/1.1\r\nContent-Length: 10\r\n\r\nshort', 400, 'incomplete request body'),
])
def test_bad_request_framing_is_rejected_without_upstream(harness, request_bytes, status, message):
    upstream_factory, start, raw, _ = harness

    async def respond(up, path, reader, writer):
        raise AssertionError('upstream must not be contacted')

    up = upstream_factory(respond)
    server = start(up, tls=False)
    sock = raw(server, tls=False)
    sock.sendall(request_bytes)
    if message == 'incomplete request body':
        sock.shutdown(socket.SHUT_WR)
    received = b''
    while data := sock.recv(65536):
        received += data
    head, _, body = received.partition(b'\r\n\r\n')
    assert head.startswith(f'HTTP/1.1 {status} '.encode())
    assert b'\r\nConnection: close' in head
    assert json.loads(body) == {'error': message}
    assert up.requests == []


def test_slow_client_holds_back_a_fast_upstream(harness):
    upstream_factory, start, raw, _ = harness
    size = 48 * 1024 * 1024
    block = bytes(range(256)) * 256          # 64 KiB
    finished = threading.Event()

    async def respond(up, path, reader, writer):
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % size)
        for _ in range(size // len(block)):
            writer.write(block)
            await writer.drain()
        finished.set()

    up = upstream_factory(respond)
    server = start(up)
    sock = raw(server, timeout=10)
    sock.sendall(_request('/big'))
    received = _read_until(sock, b'\r\n\r\n')
    time.sleep(1)                             # the client is not reading
    assert not finished.is_set(), 'gateway buffered the whole body instead of pausing upstream'
    head, _, rest = received.partition(b'\r\n\r\n')
    assert b'Transfer-Encoding: chunked' in head
    buffered = bytearray(rest)
    while not buffered.endswith(b'\r\n0\r\n\r\n'):
        data = sock.recv(1 << 20)
        assert data
        buffered += data
    body, position = bytearray(), 0
    while True:
        newline = buffered.index(b'\r\n', position)
        length = int(buffered[position:newline], 16)
        if not length:
            break
        body += buffered[newline + 2:newline + 2 + length]
        position = newline + 2 + length + 2
    assert finished.is_set()
    assert len(body) == size
    assert hashlib.sha256(body).digest() == hashlib.sha256(block * (size // len(block))).digest()


EXIT_PROBE = r"""
import importlib.util, socket, ssl, sys, threading
spec = importlib.util.spec_from_file_location('gateway_under_test', sys.argv[1])
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
entered = threading.Event()

def token_provider():               # a token refresh or `security` call that never returns
    entered.set()
    threading.Event().wait()

context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(sys.argv[2], sys.argv[3])
server = gateway.Gateway('127.0.0.1', 0, token_provider, lambda email: False,
                         upstream='http://127.0.0.1:9', ssl_context=context)
server.start()
client = ssl.create_default_context(cafile=sys.argv[4]).wrap_socket(
    socket.create_connection(('127.0.0.1', server.port)), server_hostname='127.0.0.1')
client.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
assert entered.wait(5), 'token_provider was not called'
server.stop()
client.close()
print('main returned', flush=True)
"""


def test_process_exits_while_a_blocking_callback_is_stuck(certs):
    # Quitting the app must not wait for a token_provider call that never returns.
    gateway_paths, _ = certs
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [os.path.dirname(os.path.dirname(os.path.abspath(codex_tls.__file__))),
         os.environ.get('PYTHONPATH', '')]))
    process = subprocess.Popen(
        [sys.executable, '-c', EXIT_PROBE, gateway.__file__, str(gateway_paths.cert),
         str(gateway_paths.key), str(gateway_paths.ca)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        output, errors = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        output, errors = process.communicate()
        pytest.fail(f'process did not exit within 5 s after stop(): {output}{errors}')
    assert 'main returned' in output, errors
    assert process.returncode == 0, errors


def test_dns_names_resolve_without_extra_threads(harness):
    upstream_factory, start, _, https = harness

    async def respond(up, path, reader, writer):
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
        await writer.drain()

    up = upstream_factory(respond)
    server = gateway.Gateway(
        '127.0.0.1', 0, Mock(return_value=('active@test.com', 'active-token')),
        Mock(return_value=False), upstream=f'http://localhost:{up.port}')
    server.start()
    try:
        before = set(threading.enumerate())
        conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=10)
        conn.request('GET', '/by-name')
        response = conn.getresponse()
        assert (response.status, response.read()) == (200, b'ok')
        conn.close()
        assert up.requests[0][1]['host'] == f'localhost:{up.port}'
        # asyncio's default executor (threads named asyncio_N) must never start:
        # its interpreter-exit hook would join them.
        extra = set(threading.enumerate()) - before
        assert not [thread.name for thread in extra if thread.name.startswith('asyncio_')]
    finally:
        server.stop()
