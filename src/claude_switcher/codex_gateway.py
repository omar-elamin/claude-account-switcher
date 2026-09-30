"""Loopback HTTP gateway that uses the live Codex account for each request."""

import asyncio
import base64
import concurrent.futures
import http.client
import io
import json
import os
import queue
import re
import socket
import ssl
import threading
from email.utils import formatdate
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the app's existing tomli dependency.
    import tomli as tomllib

from claude_switcher.config import _atomic_write

CONFIG_PATH = Path.home() / '.codex' / 'config.toml'
MARKER = '# managed by Claude Switcher: Codex gateway'
_CONFIG_KEYS = {'openai_base_url', 'chatgpt_base_url'}
_CONFIG_LOCK = threading.Lock()
_HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
                'te', 'trailer', 'transfer-encoding', 'upgrade'}


def account_id_from_token(token) -> str | None:
    """Decode an account claim, without treating the unverified JWT as authority."""
    try:
        payload = token.split('.')[1]
        data = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        account_id = data['https://api.openai.com/auth']['chatgpt_account_id']
        return account_id if isinstance(account_id, str) else None
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None


def _dropped_headers(headers):
    dropped = _HOP_HEADERS | {'host', 'content-length'}
    for key, value in headers.items():
        if key.lower() == 'connection':
            dropped.update(part.strip().lower() for part in value.split(','))
    return dropped


def rewrite_headers(headers: dict, token: str) -> dict:
    dropped = _dropped_headers(headers)
    result = {key: value for key, value in headers.items() if key.lower() not in dropped}
    # Inject on EVERY request, not only those that already carry Authorization:
    # with a non-default base URL Codex sends its plugin calls (/backend-api/ps/mcp)
    # without any credentials, and chatgpt.com answers 451 unless the bearer is present.
    account_id = account_id_from_token(token)
    result = {key: value for key, value in result.items()
              if key.lower() != 'authorization'
              and not (account_id is not None and key.lower() == 'chatgpt-account-id')}
    result['Authorization'] = f'Bearer {token}'
    if account_id is not None:
        result['chatgpt-account-id'] = account_id
    return result


def is_websocket_upgrade(headers) -> bool:
    return any(key.lower() == 'upgrade' and value.strip().lower() == 'websocket'
               for key, value in headers.items())


def is_usage_limit(status, body_bytes) -> bool:
    return status == 429 and any(value in body_bytes.lower()
                                for value in (b'usage_limit_reached', b'usage_not_included'))


# --- Relay -----------------------------------------------------------------
#
# One asyncio event loop in one daemon thread serves every client connection.
# The blocking callbacks (token_provider, on_usage_limit) and DNS lookups run on
# a small bounded thread pool, so concurrency no longer costs a thread per
# connection. Each read from upstream is forwarded as one client chunk: whatever
# is buffered right now goes out at once, and nothing waits to fill a buffer.

_MAX_LINE = 65536          # same line limit as http.server / http.client
_MAX_HEADERS = 100         # same header count limit as http.client
_CONNECT_TIMEOUT = 30      # upstream TCP connect + TLS handshake; streams have no read timeout
_HANDSHAKE_TIMEOUT = 10    # local TLS handshake
_WORKERS = 8               # daemon threads for token_provider / on_usage_limit / DNS
_STOP_TIMEOUT = 2          # per shutdown phase

# http.client's request validation, so bad input fails the same way (502).
_BAD_METHOD_CHAR = re.compile('[\x00-\x1f]')
_BAD_URL_CHAR = re.compile('[\x00-\x20\x7f]')
_LEGAL_HEADER_NAME = re.compile(rb'[^:\s][^:\r\n]*').fullmatch
_ILLEGAL_HEADER_VALUE = re.compile(rb'\n(?![ \t])|\r(?![ \t\n])').search

_UPSTREAM_ERRORS = (OSError, EOFError, ValueError, asyncio.TimeoutError, http.client.HTTPException)
_TERMINATOR = b'0\r\n\r\n'


def _reason(status):
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ''


def _chunk(data):
    return b'%x\r\n%b\r\n' % (len(data), data)


class _ClientError(Exception):
    def __init__(self, status, message, command=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.command = command


class _Request:
    __slots__ = ('command', 'path', 'headers', 'close')

    def __init__(self, command, path, headers, close):
        self.command = command
        self.path = path
        self.headers = headers
        self.close = close


async def _read_header_block(reader):
    """Read header lines up to the blank line, with http.client's limits."""
    lines = []
    while True:
        line = await reader.readline()
        if len(line) > _MAX_LINE:
            raise http.client.LineTooLong('header line')
        if line in (b'\r\n', b'\n', b''):
            break
        lines.append(line)
        if len(lines) > _MAX_HEADERS:
            raise http.client.HTTPException(f'got more than {_MAX_HEADERS} headers')
    return http.client.parse_headers(io.BytesIO(b''.join(lines)))


def _parse_request_line(raw):
    """Mirror BaseHTTPRequestHandler.parse_request for protocol_version HTTP/1.1."""
    words = str(raw, 'iso-8859-1').rstrip('\r\n').split()
    if not words:
        return None
    close = True
    version = 'HTTP/0.9'
    if len(words) >= 3:
        version = words[-1]
        try:
            if not version.startswith('HTTP/'):
                raise ValueError
            number = version.split('/', 1)[1].split('.')
            if len(number) != 2 or any(not part.isdigit() or len(part) > 10 for part in number):
                raise ValueError
            number = int(number[0]), int(number[1])
        except (ValueError, IndexError):
            raise _ClientError(400, 'bad request version') from None
        if number >= (1, 1):
            close = False
        if number >= (2, 0):
            raise _ClientError(505, 'invalid http version')
    if not 2 <= len(words) <= 3:
        raise _ClientError(400, 'bad request syntax')
    command, path = words[:2]
    if len(words) == 2:
        close = True
        if command != 'GET':
            raise _ClientError(400, 'bad request syntax', command)
    if path.startswith('//'):
        path = '/' + path.lstrip('/')
    return command, path, version, close


async def _read_request(reader, writer):
    """Return the next request, or None when the client connection should close."""
    try:
        raw = await reader.readline()
    except ValueError:
        raise _ClientError(414, 'request line too long') from None
    if len(raw) > _MAX_LINE:
        raise _ClientError(414, 'request line too long')
    if not raw:
        return None
    parsed = _parse_request_line(raw)
    if parsed is None:
        return None
    command, path, version, close = parsed
    try:
        headers = await _read_header_block(reader)
    except (ValueError, http.client.LineTooLong):
        raise _ClientError(431, 'header line too long', command) from None
    except http.client.HTTPException:
        raise _ClientError(431, 'too many headers', command) from None
    connection = headers.get('Connection', '').lower()
    if connection == 'close':
        close = True
    elif connection == 'keep-alive':
        close = False
    if headers.get('Expect', '').lower() == '100-continue' and version >= 'HTTP/1.1':
        writer.write(b'HTTP/1.1 100 Continue\r\n\r\n')
        await writer.drain()
    return _Request(command, path, headers, close)


def _upstream_request(command, path, parts, headers, body):
    """Serialize the upstream request the way http.client.request() does."""
    if _BAD_METHOD_CHAR.search(command):
        raise ValueError('invalid method')
    if _BAD_URL_CHAR.search(path):
        raise http.client.InvalidURL('invalid path')
    names = {key.lower() for key in headers}
    lines = [f'{command} {path or "/"} HTTP/1.1'.encode('ascii')]
    if 'host' not in names:
        host = parts.hostname
        if ':' in host:
            host = f'[{host}]'
        default_port = 443 if parts.scheme == 'https' else 80
        port = parts.port
        lines.append(b'Host: ' + (host if port in (None, default_port) else f'{host}:{port}')
                     .encode('ascii'))
    if 'accept-encoding' not in names:
        lines.append(b'Accept-Encoding: identity')
    lines.append(b'Content-Length: %d' % len(body))
    for key, value in headers.items():
        name = key.encode('ascii')
        if not _LEGAL_HEADER_NAME(name):
            raise ValueError('invalid header name')
        value = str(value).encode('latin-1')
        if _ILLEGAL_HEADER_VALUE(value):
            raise ValueError('invalid header value')
        lines.append(name + b': ' + value)
    return b'\r\n'.join(lines) + b'\r\n\r\n' + body


def _next_line(buf, pos):
    """Return (line, next position), or (None, pos) when the line is not complete yet."""
    newline = buf.find(b'\n', pos)
    if newline < 0:
        if len(buf) - pos > _MAX_LINE:
            raise http.client.LineTooLong('header line')
        return None, pos
    if newline + 1 - pos > _MAX_LINE:
        raise http.client.LineTooLong('header line')
    return bytes(buf[pos:newline + 1]), newline + 1


def _parse_status_line(line):
    text = str(line, 'iso-8859-1')
    try:
        version, status, _ = text.split(None, 2)
    except ValueError:
        try:
            version, status = text.split(None, 1)
        except ValueError:
            version = ''
    if not version.startswith('HTTP/'):
        raise http.client.BadStatusLine(text)
    try:
        status = int(status)
    except ValueError:
        raise http.client.BadStatusLine(text) from None
    if not 100 <= status <= 999:
        raise http.client.BadStatusLine(text)
    return version, status


def _parse_response_head(buf):
    """Mirror HTTPResponse.begin(): skip 100 Continue, then parse status and headers.

    Return (status, headers, head length), or None while the head is incomplete.
    """
    pos = 0
    while True:
        line, pos = _next_line(buf, pos)
        if line is None:
            return None
        version, status = _parse_status_line(line)
        lines = []
        while True:
            line, pos = _next_line(buf, pos)
            if line is None:
                return None
            if line in (b'\r\n', b'\n'):
                break
            lines.append(line)
            if len(lines) > _MAX_HEADERS:
                raise http.client.HTTPException(f'got more than {_MAX_HEADERS} headers')
        if status != 100:
            break
    if version not in ('HTTP/1.0', 'HTTP/0.9') and not version.startswith('HTTP/1.'):
        raise http.client.UnknownProtocol(version)
    return status, http.client.parse_headers(io.BytesIO(b''.join(lines))), pos


class _ChunkedDecoder:
    """Incremental chunked-body decoder with http.client's tolerances."""

    def __init__(self):
        self._pending = b''
        self._left = None        # None: size line next; >0: data left; 0: chunk CRLF next
        self._trailer = False
        self.done = False

    def feed(self, data):
        buf = self._pending + data if self._pending else data
        self._pending = b''
        out = []
        pos, end = 0, len(buf)
        while pos < end and not self.done:
            left = self._left
            if left:
                take = min(left, end - pos)
                out.append(buf[pos:pos + take])
                pos += take
                self._left = left - take
            elif left == 0:
                if end - pos < 2:
                    break
                pos += 2           # http.client discards the two bytes after chunk data
                self._left = None
            else:
                newline = buf.find(b'\n', pos)
                if newline < 0:
                    if end - pos > _MAX_LINE:
                        raise http.client.LineTooLong('chunk size')
                    break
                line = buf[pos:newline + 1]
                pos = newline + 1
                if len(line) > _MAX_LINE:
                    raise http.client.LineTooLong('chunk size')
                if self._trailer:
                    if line in (b'\r\n', b'\n'):
                        self.done = True
                    continue
                size = int(line.split(b';', 1)[0], 16)
                if size < 0:
                    raise ValueError('negative chunk size')
                if size == 0:
                    self._trailer = True
                else:
                    self._left = size
        if pos < end and not self.done:
            self._pending = buf[pos:]
        return b''.join(out)

    def eof(self):
        # http.client accepts a body that ends inside the trailer section.
        if not self.done and not self._trailer:
            raise http.client.IncompleteRead(b'')
        self.done = True


class _UpstreamProtocol(asyncio.Protocol):
    """One upstream exchange: parse the response head, then deliver the body.

    The body is decoded inside data_received and handed straight to a sink (the
    client connection), so relaying an event costs no task switch. Without a
    sink the body is buffered and returned by `done`.
    """

    def __init__(self, loop, method):
        self._method = method
        self.transport = None
        self.head = loop.create_future()     # (status, headers)
        self.done = loop.create_future()     # buffered body, or None when streamed
        self._buffer = bytearray()
        self._mode = None                    # set by start_body()
        self._sink = None
        self._parts = []
        self._end = None                     # ('eof', None) or ('error', exc) once upstream ends
        self._hold = False                   # head parsed, start_body() not called yet
        self._client_paused = False
        self._reading_paused = False

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data):
        if self.done.done():
            return
        try:
            if self._mode is None:
                self._buffer += data
                if not self.head.done():
                    parsed = _parse_response_head(self._buffer)
                    if parsed is not None:
                        status, headers, end = parsed
                        del self._buffer[:end]
                        self._framing = _framing(self._method, status, headers)
                        self._hold = True
                        self._update_reading()
                        self.head.set_result((status, headers))
                return
            self._feed(data)
        except Exception as exc:
            self._fail(exc)

    def eof_received(self):
        self._ended(('eof', None))

    def connection_lost(self, exc):
        self._ended(('eof', None) if exc is None else ('error', exc))

    def start_body(self, sink=None):
        """Deliver the body to sink(bytes) as it arrives, or buffer it when sink is None."""
        self._sink = sink
        self._mode, self._left = self._framing
        if self._mode == 'chunked':
            self._decoder = _ChunkedDecoder()
        self._hold = False
        try:
            if self._mode == 'length' and not self._left:
                self._finish()
            elif self._buffer:
                leftover = bytes(self._buffer)
                self._buffer.clear()
                self._feed(leftover)
            if self._end is not None:
                self._apply_end()
        except Exception as exc:
            self._fail(exc)
        self._update_reading()

    def client_paused(self, paused):
        self._client_paused = paused
        self._update_reading()

    def _update_reading(self):
        pause = self._hold or self._client_paused
        if pause != self._reading_paused and self.transport is not None \
                and not self.transport.is_closing():
            self._reading_paused = pause
            if pause:
                self.transport.pause_reading()
            else:
                self.transport.resume_reading()

    def _feed(self, data):
        mode = self._mode
        if mode == 'chunked':
            payload = self._decoder.feed(data)
            if payload:
                self._emit(payload)
            if self._decoder.done:
                self._finish()
        elif mode == 'length':
            if len(data) > self._left:
                data = data[:self._left]
            self._left -= len(data)
            self._emit(data)
            if not self._left:
                self._finish()
        else:
            self._emit(data)

    def _emit(self, payload):
        if self._sink is None:
            self._parts.append(payload)
        else:
            self._sink(payload)

    def _finish(self):
        if not self.done.done():
            self.done.set_result(None if self._sink is not None else b''.join(self._parts))

    def _ended(self, end):
        if self._end is not None:
            return
        self._end = end
        if not self.head.done():
            if end[0] == 'error':
                self._fail(end[1])
            else:
                self._fail(http.client.RemoteDisconnected('upstream closed before a response'))
        elif self._mode is not None:
            try:
                self._apply_end()
            except Exception as exc:
                self._fail(exc)

    def _apply_end(self):
        """The upstream connection ended; decide whether the body is complete."""
        if self.done.done():
            return
        kind, exc = self._end
        if kind == 'error':
            raise exc
        if self._mode == 'chunked':
            self._decoder.eof()
            self._finish()
        elif self._mode == 'length':
            raise http.client.IncompleteRead(b'', self._left)
        else:
            self._finish()                      # read-until-close: EOF is the end

    def _fail(self, exc):
        future = self.head if not self.head.done() else self.done
        if not future.done():
            future.set_exception(exc)
            future.exception()                  # retrieved: never logged if nobody awaits it
        if self.transport is not None:
            self.transport.abort()


def _framing(method, status, headers):
    """Body framing, decided the way http.client.HTTPResponse.begin() does."""
    if method == 'HEAD':
        return 'length', 0
    encoding = headers.get('transfer-encoding')
    if encoding and encoding.lower() == 'chunked':
        return 'chunked', None
    length = None
    declared = headers.get('content-length')
    if declared:
        try:
            length = int(declared)
        except ValueError:
            length = None
        else:
            if length < 0:
                length = None
    if status in (204, 304) or 100 <= status < 200:
        length = 0
    if length is not None:
        return 'length', length
    return 'close', None


async def _json_error(writer, command, status, message):
    body = json.dumps({'error': message}, separators=(',', ':')).encode()
    head = (f'HTTP/1.1 {status} {_reason(status)}\r\n'
            f'Date: {formatdate(usegmt=True)}\r\n'
            'Content-Type: application/json\r\n'
            f'Content-Length: {len(body)}\r\n'
            'Connection: close\r\n\r\n').encode('latin-1')
    try:
        writer.write(head if command == 'HEAD' else head + body)
        await writer.drain()
    except (OSError, RuntimeError):
        pass


class _WorkerPool:
    """A bounded pool of daemon threads for blocking calls made from the event loop.

    Not concurrent.futures: its executors register an interpreter-exit hook that
    joins every worker, so one blocked token refresh would stop the app quitting.
    These threads are daemons that nothing joins.
    """

    def __init__(self, size, name):
        self._size = size
        self._name = name
        self._jobs = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._threads = []
        self._idle = 0
        self._closed = False

    def run(self, fn, *args):
        """Run fn(*args) on a worker; return an asyncio future for its result."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            if self._closed:
                raise RuntimeError('worker pool is shut down')
            self._jobs.put((loop, future, fn, args))
            # Reserve an idle worker for the job, or start one while below the limit.
            # At the limit the job waits in the queue for the next free worker.
            if self._idle:
                self._idle -= 1
            elif len(self._threads) < self._size:
                thread = threading.Thread(target=self._work, daemon=True,
                                          name=f'{self._name}-{len(self._threads)}')
                self._threads.append(thread)
                thread.start()
        return future

    def _work(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            loop, future, fn, args = job
            if not future.done():   # a cancelled request needs no call
                try:
                    result, error = fn(*args), None
                except BaseException as exc:
                    result, error = None, exc
                try:
                    loop.call_soon_threadsafe(_deliver, future, result, error)
                except RuntimeError:
                    pass            # the loop is closed; nobody is waiting
            with self._lock:
                self._idle += 1

    def shutdown(self):
        """Stop accepting work and let idle workers exit. Never waits for a call."""
        with self._lock:
            self._closed = True
            try:
                while True:
                    self._jobs.get_nowait()
            except queue.Empty:
                pass
            for _ in self._threads:
                self._jobs.put(None)


def _deliver(future, result, error):
    if future.done():
        return
    if error is None:
        future.set_result(result)
    else:
        future.set_exception(error)


async def _connect(loop, pool, host, port):
    """socket.create_connection() without asyncio's default thread-pool resolver."""
    try:   # an IP literal needs no lookup, so no worker thread
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST)
    except socket.gaierror:
        infos = await pool.run(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    error = None
    for family, kind, proto, _, address in infos:
        sock = socket.socket(family, kind, proto)
        try:
            sock.setblocking(False)
            await loop.sock_connect(sock, address)
            return sock
        except OSError as exc:
            sock.close()
            error = exc
        except BaseException:
            sock.close()
            raise
    raise error if error is not None else OSError('getaddrinfo returned no addresses')


class _ClientProtocol(asyncio.StreamReaderProtocol):
    """A client connection. Cancels its in-flight request when the client goes away."""

    def __init__(self, gateway):
        self._gateway = gateway
        self.task = None
        self.busy = False
        self.gone = False
        self.transport = None
        self.upstream = None          # the _UpstreamProtocol streaming into this client
        self.writing_paused = False
        self.reader = asyncio.StreamReader(limit=_MAX_LINE + 1)
        super().__init__(self.reader, self._connected)

    def _connected(self, reader, writer):
        self.transport = writer.transport
        self._gateway._connections.add(self)
        self.task = asyncio.get_running_loop().create_task(
            self._gateway._serve(self, reader, writer))

    def _peer_gone(self):
        # While a request is in flight nothing reads from the client, so its EOF
        # is the only sign that the stream should end and upstream be released.
        self.gone = True
        if self.busy and self.task is not None:
            self.task.cancel()

    def eof_received(self):
        self._peer_gone()
        return super().eof_received()

    def connection_lost(self, exc):
        self._peer_gone()
        self._gateway._connections.discard(self)
        super().connection_lost(exc)

    def pause_writing(self):
        super().pause_writing()
        self.writing_paused = True
        if self.upstream is not None:
            self.upstream.client_paused(True)       # backpressure: stop reading upstream

    def resume_writing(self):
        super().resume_writing()
        self.writing_paused = False
        if self.upstream is not None:
            self.upstream.client_paused(False)

    def abort(self):
        if self.task is not None:
            self.task.cancel()
        if self.transport is not None:
            self.transport.abort()


class Gateway:
    def __init__(self, host, port, token_provider, on_usage_limit,
                 upstream='https://chatgpt.com', ssl_context=None, upstream_ssl_context=None):
        self.host = host
        self.port = port
        self.token_provider = token_provider
        self.on_usage_limit = on_usage_limit
        self.upstream = upstream
        self.ssl_context = ssl_context
        self.upstream_ssl_context = upstream_ssl_context
        self._lifecycle = threading.Lock()
        self._loop = None
        self._thread = None
        self._server = None
        self._sock = None
        self._pool = None
        self._upstream_ssl = None
        self._connections = set()

    def start(self):
        with self._lifecycle:
            if self._loop is not None:
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, self.port))
                sock.listen(128)
                sock.setblocking(False)
                upstream_ssl = self.upstream_ssl_context
                if upstream_ssl is None:
                    # What http.client.HTTPSConnection uses by default.
                    upstream_ssl = ssl.create_default_context()
                    upstream_ssl.set_alpn_protocols(['http/1.1'])
            except BaseException:
                sock.close()
                raise
            pool = _WorkerPool(_WORKERS, 'codex-gateway-worker')
            loop = asyncio.new_event_loop()
            loop.set_exception_handler(lambda loop, context: None)   # never log request data
            ready = concurrent.futures.Future()
            self._upstream_ssl = upstream_ssl
            self._pool = pool
            self._connections = set()
            thread = threading.Thread(target=self._run, args=(loop, sock, ready),
                                      name='codex-gateway', daemon=True)
            thread.start()
            try:
                self._server = ready.result(timeout=10)
            except BaseException:
                if loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=_STOP_TIMEOUT)
                sock.close()
                pool.shutdown()
                raise
            self.port = sock.getsockname()[1]
            self._sock = sock
            self._loop = loop
            self._thread = thread

    def _run(self, loop, sock, ready):
        asyncio.set_event_loop(loop)
        try:
            try:
                server = loop.run_until_complete(loop.create_server(
                    lambda: _ClientProtocol(self), sock=sock, ssl=self.ssl_context,
                    ssl_handshake_timeout=_HANDSHAKE_TIMEOUT if self.ssl_context else None))
            except BaseException as exc:
                ready.set_exception(exc)
                return
            ready.set_result(server)
            loop.run_forever()
        finally:
            try:
                tasks = asyncio.all_tasks(loop)
                for task in tasks:
                    task.cancel()
                if tasks:
                    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            finally:
                loop.close()

    def stop(self):
        with self._lifecycle:
            loop, thread, server, sock = self._loop, self._thread, self._server, self._sock
            if loop is None:
                return
            self._loop = self._thread = self._server = self._sock = None
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(server), loop).result(_STOP_TIMEOUT)
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass   # the loop already closed
            thread.join(timeout=_STOP_TIMEOUT)
            sock.close()   # normally already closed by server.close(); frees the port regardless
            # Blocking callbacks may still be running; do not wait for them.
            self._pool.shutdown()
            self._pool = None

    async def _shutdown(self, server):
        server.close()                              # stop listening; frees the port
        abort_clients = getattr(server, 'abort_clients', None)
        if abort_clients is not None:
            abort_clients()                         # 3.13+: also unfinished TLS handshakes
        connections = list(self._connections)
        tasks = [conn.task for conn in connections if conn.task is not None]
        for conn in connections:
            try:
                conn.abort()
            except Exception:
                pass
        if tasks:
            await asyncio.wait(tasks, timeout=1)

    async def _serve(self, conn, reader, writer):
        graceful = False
        try:
            while True:
                try:
                    request = await _read_request(reader, writer)
                except _ClientError as exc:
                    await _json_error(writer, exc.command, exc.status, exc.message)
                    request = None
                if request is None:
                    break
                keep_alive = await self._respond(conn, request, reader, writer)
                if not keep_alive or request.close:
                    break
            graceful = True
        except Exception:
            pass    # never log: paths, headers and bodies can hold private data
        finally:
            if graceful:
                writer.close()
            else:
                writer.transport.abort()

    async def _respond(self, conn, request, reader, writer):
        """Serve one request. Return False when the client connection must close."""
        command = request.command
        headers = dict(request.headers)
        if is_websocket_upgrade(headers):
            await _json_error(writer, command, 426, 'use the streaming HTTP transport')
            return False
        # Only Content-Length request framing is supported; reject ambiguity.
        lengths = request.headers.get_all('Content-Length', [])
        try:
            if len(lengths) > 1 or request.headers.get('Transfer-Encoding'):
                raise ValueError
            length = int(lengths[0]) if lengths else 0
            if length < 0:
                raise ValueError
        except ValueError:
            await _json_error(writer, command, 400, 'invalid request body framing')
            return False
        try:
            body = await reader.readexactly(length)
        except asyncio.IncompleteReadError:
            await _json_error(writer, command, 400, 'incomplete request body')
            return False
        if conn.gone:
            return False
        conn.busy = True
        try:
            return await self._forward(conn, request, headers, body, writer)
        finally:
            conn.busy = False

    async def _open_upstream(self, loop, command, parts, https):
        sock = await _connect(loop, self._pool, parts.hostname,
                              parts.port or (443 if https else 80))
        try:
            return await loop.create_connection(
                lambda: _UpstreamProtocol(loop, command), sock=sock,
                ssl=self._upstream_ssl if https else None,
                server_hostname=parts.hostname if https else None)
        except BaseException:
            sock.close()
            raise

    async def _forward(self, conn, request, headers, body, writer):
        loop = asyncio.get_running_loop()
        command = request.command
        authenticated = any(key.lower() == 'authorization' for key in headers)
        started = False
        try:
            for attempt in range(2):
                try:
                    identity = await self._pool.run(self.token_provider)
                    if not identity:
                        raise ValueError
                    email, token = identity
                    if not email or not token:
                        raise ValueError
                except Exception:
                    await _json_error(writer, command, 503, 'no active codex account')
                    return False
                parts = urlsplit(self.upstream)
                if not parts.hostname:
                    raise ValueError('invalid upstream')
                https = parts.scheme == 'https'
                payload = _upstream_request(command, request.path, parts,
                                            rewrite_headers(headers, token), body)
                transport, response = await asyncio.wait_for(
                    self._open_upstream(loop, command, parts, https), _CONNECT_TIMEOUT)
                try:
                    transport.write(payload)
                    status, response_headers = await response.head
                    buffered = None
                    if status == 429:
                        response.start_body()
                        buffered = await response.done
                    if (attempt == 0 and authenticated and buffered is not None
                            and is_usage_limit(status, buffered)):
                        try:
                            switched = await self._pool.run(self.on_usage_limit, email)
                        except Exception:
                            switched = False
                        if switched:
                            continue
                    started = True
                    # Only upstream's Date/Server are sent; nothing is added but framing.
                    items = response_headers.items()
                    dropped = _dropped_headers(dict(items))
                    lines = [f'HTTP/1.1 {status} {_reason(status)}\r\n']
                    lines += [f'{key}: {value}\r\n' for key, value in items
                              if key.lower() not in dropped]
                    has_body = command != 'HEAD' and status not in (204, 304) and status >= 200
                    if has_body:
                        lines.append('Transfer-Encoding: chunked\r\n')
                    lines.append('\r\n')
                    head = ''.join(lines).encode('latin-1')
                    if not has_body:
                        writer.write(head)
                    elif buffered is not None:
                        writer.write(head + (_chunk(buffered) if buffered else b'') + _TERMINATOR)
                    else:
                        writer.write(head)
                        await writer.drain()
                        # Each upstream read becomes one client chunk, written from
                        # data_received; client backpressure pauses upstream reads.
                        client = writer.transport
                        conn.upstream = response
                        response.client_paused(conn.writing_paused)
                        response.start_body(lambda data: client.write(_chunk(data)))
                        await response.done
                        writer.write(_TERMINATOR)
                    await writer.drain()
                    return True
                finally:
                    conn.upstream = None
                    transport.abort()
        except _UPSTREAM_ERRORS:
            if started:
                # A partial response cannot be replaced by a second HTTP status.
                return False
            await _json_error(writer, command, 502, 'upstream connection failed')
            return False


def _parse_config(raw):
    try:
        return tomllib.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError('Codex config.toml is not valid TOML') from exc


def _without_managed_config(raw):
    """Keep complete TOML statements intact, including multiline strings/arrays."""
    _parse_config(raw)
    kept = []
    statement = b''
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not statement and line.lstrip().startswith(b'['):
            # The remainder is table content, where these names are unrelated.
            return b''.join(kept) + b''.join(lines[index:])
        statement += line
        try:
            tomllib.loads(statement.decode('utf-8'))
        except tomllib.TOMLDecodeError:
            continue
        first = statement.splitlines()[0].strip()
        key = first.partition(b'=')[0].strip().decode('utf-8')
        if first != MARKER.encode() and key not in _CONFIG_KEYS:
            kept.append(statement)
        statement = b''
    return b''.join(kept)


def _edit_config(path, port=None):
    path = Path(path)
    with _CONFIG_LOCK:
        original = path.read_bytes() if path.exists() else b''
        cleaned = _without_managed_config(original)
        if port is not None:
            block = (f'{MARKER}\n'
                     f'openai_base_url = "https://127.0.0.1:{port}/backend-api/codex"\n'
                     f'chatgpt_base_url = "https://127.0.0.1:{port}/backend-api/"\n').encode()
            cleaned = block + cleaned
        _parse_config(cleaned)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = path.with_name(path.name + '.claude-switcher.bak')
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(original)
        _atomic_write(path, cleaned, mode=0o600)
        parsed = _parse_config(path.read_bytes())
        if port is not None and not _CONFIG_KEYS.issubset(parsed):
            raise RuntimeError('Codex gateway config keys are missing')


def enable_config(port, path=CONFIG_PATH):
    _edit_config(path, port)


def disable_config(path=CONFIG_PATH):
    _edit_config(path)


def gateway_configured(path=CONFIG_PATH):
    try:
        return MARKER.encode() in Path(path).read_bytes()
    except FileNotFoundError:
        return False
