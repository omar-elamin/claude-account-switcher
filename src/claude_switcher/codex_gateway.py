"""Loopback HTTP gateway that uses the live Codex account for each request."""

import base64
import http.client
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    if any(key.lower() == 'authorization' for key in headers):
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


class Gateway:
    def __init__(self, host, port, token_provider, on_usage_limit, upstream='https://chatgpt.com'):
        self.host = host
        self.port = port
        self.token_provider = token_provider
        self.on_usage_limit = on_usage_limit
        self.upstream = upstream
        self._server = None
        self._thread = None

    def start(self):
        if self._server is not None:
            return
        server = ThreadingHTTPServer((self.host, self.port), _Handler)
        server.daemon_threads = True
        server.gateway = self
        self._server = server
        self.port = server.server_port
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join()
            self._server = None
            self._thread = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        # Paths and upstream errors can contain private data too.
        pass

    def __getattr__(self, name):
        if name.startswith('do_'):
            return self._forward
        raise AttributeError(name)

    def _json_error(self, status, message):
        body = json.dumps({'error': message}, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD':
            self.wfile.write(body)
            self.wfile.flush()

    def _forward(self):
        gateway = self.server.gateway
        headers = dict(self.headers)
        if is_websocket_upgrade(headers):
            self._json_error(426, 'use the streaming HTTP transport')
            return
        # Only Content-Length request framing is supported; reject ambiguity.
        lengths = self.headers.get_all('Content-Length', [])
        try:
            if len(lengths) > 1 or self.headers.get('Transfer-Encoding'):
                raise ValueError
            length = int(lengths[0]) if lengths else 0
            if length < 0:
                raise ValueError
        except ValueError:
            self._json_error(400, 'invalid request body framing')
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._json_error(400, 'incomplete request body')
            return
        authenticated = any(key.lower() == 'authorization' for key in headers)
        response_started = False
        try:
            for attempt in range(2):
                try:
                    identity = gateway.token_provider()
                    if not identity:
                        raise ValueError
                    email, token = identity
                    if not email or not token:
                        raise ValueError
                except Exception:
                    self._json_error(503, 'no active codex account')
                    return
                upstream = urlsplit(gateway.upstream)
                connection_type = (http.client.HTTPSConnection if upstream.scheme == 'https'
                                   else http.client.HTTPConnection)
                connection = connection_type(upstream.hostname, upstream.port, timeout=30)
                try:
                    connection.connect()
                    connection.sock.settimeout(None)
                    connection.request(self.command, self.path, body=body,
                                       headers=rewrite_headers(headers, token))
                    response = connection.getresponse()
                    buffered = response.read() if response.status == 429 else None
                    if (attempt == 0 and authenticated and buffered is not None
                            and is_usage_limit(response.status, buffered)):
                        try:
                            switched = gateway.on_usage_limit(email)
                        except Exception:
                            switched = False
                        if switched:
                            continue
                    response_started = True
                    self.send_response(response.status)
                    response_headers = response.getheaders()
                    dropped = _dropped_headers(dict(response_headers))
                    for key, value in response_headers:
                        if key.lower() not in dropped:
                            self.send_header(key, value)
                    has_body = self.command != 'HEAD' and response.status not in (204, 304) and response.status >= 200
                    if has_body:
                        self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    if has_body:
                        if buffered is not None:
                            for offset in range(0, len(buffered), 8192):
                                self._chunk(buffered[offset:offset + 8192])
                        else:
                            # read(n) waits to fill n bytes, delaying SSE. read1 returns
                            # whatever is available after one underlying socket read.
                            while chunk := response.read1(8192):
                                self._chunk(chunk)
                        self.wfile.write(b'0\r\n\r\n')
                        self.wfile.flush()
                    return
                finally:
                    connection.close()
        except (OSError, http.client.HTTPException, ValueError):
            if response_started:
                # A partial response cannot be replaced by a second HTTP status.
                self.close_connection = True
            else:
                self._json_error(502, 'upstream connection failed')

    def _chunk(self, data):
        self.wfile.write(f'{len(data):x}\r\n'.encode() + data + b'\r\n')
        self.wfile.flush()


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
                     f'openai_base_url = "http://127.0.0.1:{port}/backend-api/codex"\n'
                     f'chatgpt_base_url = "http://127.0.0.1:{port}/backend-api/"\n').encode()
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
