"""AHB/1 framing, HTTP client, and on-disk response mailbox.

SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import http.client
import ipaddress
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import time
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
import zlib

from retry_io import atomic_bytes, unlink_file

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
MAX_ENVELOPE = MAX_RESPONSE + 128 * 1024
LOG_HEX_CHARS = 768
PAGE_CHUNKS = 64
CHUNK_BYTES = 384
REQUEST_LINE = re.compile(rb'\[AGHS_HTTP_REQUEST\] ([0-9a-f]{32}):(q[0-9a-f]+_[0-9a-f]+):(\d+)/(\d+):([0-9a-f]+)\s*$')
ACK_LINE = re.compile(rb'\[AGHS_HTTP_ACK\] ([0-9a-f]{32}):(q[0-9a-f]+_[0-9a-f]+)\s*$')
METHODS = {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}
MANAGED_HEADERS = {'host', 'content-length', 'connection', 'transfer-encoding', 'upgrade', 'proxy-authorization', 'proxy-connection'}
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")


def encode_fields(values: list[bytes]) -> bytes:
    if len(values) > 1024:
        raise ValueError('Too many fields')
    return b'AHB1' + f'{len(values):04x}'.encode() + b''.join(f'{len(value):08x}'.encode() + value for value in values)


def decode_fields(data: bytes, limit: int = MAX_ENVELOPE) -> list[bytes]:
    if len(data) > limit or not re.fullmatch(rb'AHB1[0-9a-f]{4}', data[:8]):
        raise ValueError('Invalid envelope')
    count, offset, fields = int(data[4:8], 16), 8, []
    if count > 1024:
        raise ValueError('Too many fields')
    for _ in range(count):
        size = data[offset:offset + 8]
        if not re.fullmatch(rb'[0-9a-f]{8}', size):
            raise ValueError('Invalid field size')
        length = int(size, 16)
        offset += 8
        if offset + length > len(data):
            raise ValueError('Truncated envelope')
        fields.append(data[offset:offset + length])
        offset += length
    if offset != len(data):
        raise ValueError('Trailing envelope data')
    return fields


@dataclass
class Request:
    method: str
    url: str
    total_ms: int = 60000
    idle_ms: int = 30000
    raw: bool = False
    body: bytes = b''
    headers: dict[str, str] = field(default_factory=dict)
    parameters: list[tuple[bytes, bytes]] = field(default_factory=list)

    @classmethod
    def decode(cls, payload: bytes) -> Request:
        values = decode_fields(payload, MAX_REQUEST)
        if len(values) < 8:
            raise ValueError('Missing fields')
        method, url = values[0].decode('ascii'), values[1].decode('utf-8')
        total, idle = int(values[2]), int(values[3])
        if method not in METHODS or len(url) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in url):
            raise ValueError('Invalid method or URL')
        if not 1 <= total <= 120000 or not 1 <= idle <= 120000 or values[4] not in (b'raw', b'form'):
            raise ValueError('Invalid request options')
        raw, body = values[4] == b'raw', values[5]
        position = 6

        def pairs(maximum):
            nonlocal position
            if position >= len(values):
                raise ValueError('Missing pair count')
            count = int(values[position])
            position += 1
            if not 0 <= count <= maximum or position + 2 * count > len(values):
                raise ValueError('Invalid pair count')
            result = list(zip(values[position:position + 2 * count:2], values[position + 1:position + 2 * count:2]))
            position += 2 * count
            return result

        headers = {}
        for name, value in pairs(64):
            key = name.decode('ascii').lower()
            if not HEADER_NAME.fullmatch(key) or key in MANAGED_HEADERS or len(value) > 8192 or any(c in value for c in (b'\r', b'\n', b'\x00')):
                raise ValueError('Invalid header')
            headers[key] = value.decode('latin-1')
        parameters = pairs(128)
        if any(len(name) > 1024 for name, _ in parameters):
            raise ValueError('Parameter name too long')
        if position != len(values) or (parameters and (raw or method not in ('GET', 'HEAD', 'POST'))):
            raise ValueError('Invalid parameters')
        if (raw and method not in ('POST', 'PUT', 'PATCH')) or (not raw and body):
            raise ValueError('Invalid request body')
        return cls(method, url, total, idle, raw, body, headers, parameters)


@dataclass
class Response:
    code: int = 0
    body: bytes = b''
    headers: list[tuple[str, str]] = field(default_factory=list)
    error: str = ''
    elapsed_ms: int = 0

    def envelope(self) -> bytes:
        values = [str(len(self.headers)).encode()]
        for name, value in self.headers:
            values.extend((name.encode('ascii'), value.encode('latin-1')))
        values.append(self.body)
        return encode_fields(values)


def origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError('Unsupported URL')
    if any(ord(c) < 33 or ord(c) == 127 for c in url) or len(url) > 8192:
        raise ValueError('Invalid URL characters')
    host = parts.hostname.encode('idna').decode('ascii').lower()
    return parts.scheme, host, parts.port or (443 if parts.scheme == 'https' else 80)


class DestinationPolicy:
    def __init__(self, allowed_origins=(), *, test_loopback=False):
        self.allowed = {origin(value) for value in allowed_origins}
        self.test_loopback = test_loopback

    def resolve(self, url):
        scheme, host, port = origin(url)
        if self.allowed and (scheme, host, port) not in self.allowed:
            raise ValueError('Destination is outside the configured allowlist')
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise OSError('No destination address')
        for _, _, _, _, address in addresses:
            ip = ipaddress.ip_address(address[0].split('%')[0])
            # IPv4-mapped IPv6 must satisfy the IPv4 policy too.
            if getattr(ip, 'ipv4_mapped', None):
                ip = ip.ipv4_mapped
            if not ip.is_global and not (self.test_loopback and ip.is_loopback):
                raise ValueError('Private or reserved destinations are disabled')
        return scheme, host, port, addresses[0][4][0]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPS(_PinnedHTTP):
    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch(request: Request, policy: DestinationPolicy) -> Response:
    started = time.monotonic()
    deadline = started + request.total_ms / 1000

    def timeout():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Absolute deadline elapsed')
        return min(remaining, request.idle_ms / 1000)

    try:
        url, headers = request.url, dict(request.headers)
        payload = request.body if request.raw else None
        parameters = urlencode(request.parameters).encode('ascii')
        if parameters:
            if request.method in ('GET', 'HEAD'):
                parsed = urlsplit(url)
                url = urlunsplit(parsed._replace(query=parsed.query + ('&' if parsed.query else '') + parameters.decode()))
            else:
                payload = parameters
                headers.setdefault('content-type', 'application/x-www-form-urlencoded')
        if request.method == 'POST' and payload is None:
            payload = b''
        headers.setdefault('user-agent', 'AghsLauncher/1')
        headers.setdefault('accept-encoding', 'identity')
        for hop in range(4):
            scheme, host, port, address = policy.resolve(url)
            connection_type = _PinnedHTTPS if scheme == 'https' else _PinnedHTTP
            connection = connection_type(host, port, address, timeout())
            try:
                connection.connect()
                connection.sock.settimeout(timeout())
                parts = urlsplit(url)
                target = parts.path or '/'
                if parts.query:
                    target += '?' + parts.query
                connection.request(request.method, target, body=payload, headers=headers)
                connection.sock.settimeout(timeout())
                network_socket = connection.sock
                reply = connection.getresponse()
                with reply:
                    location = reply.getheader('Location')
                    next_url = urljoin(url, location) if location else None
                    if (hop < 3 and request.method in ('GET', 'HEAD') and reply.status in (301, 302, 303, 307, 308)
                            and next_url and origin(next_url) == origin(url)):
                        url = next_url
                        continue
                    result_headers = reply.getheaders()
                    if len(result_headers) > 100 or sum(len(k) + len(v) for k, v in result_headers) > 65536:
                        raise ValueError('Response headers exceed limit')
                    body = bytearray()
                    while True:
                        network_socket.settimeout(timeout())
                        block = reply.read1(min(65536, MAX_RESPONSE + 1 - len(body)))
                        if not block:
                            break
                        body.extend(block)
                        if len(body) > MAX_RESPONSE:
                            raise ValueError('Response exceeds limit')
                    encoding = reply.getheader('Content-Encoding', '').lower()
                    if encoding in ('gzip', 'deflate'):
                        decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15)
                        decoded = decoder.decompress(body, MAX_RESPONSE + 1)
                        if len(decoded) > MAX_RESPONSE or not decoder.eof:
                            raise ValueError('Invalid or oversized compressed response')
                        body = decoded
                    return Response(reply.status, bytes(body), result_headers, elapsed_ms=int((time.monotonic() - started) * 1000))
            finally:
                connection.close()
        raise ValueError('Redirect limit exceeded')
    except (TimeoutError, socket.timeout):
        return Response(error='timeout', elapsed_ms=int((time.monotonic() - started) * 1000))
    except Exception:
        # Request URLs, headers, and exception text may contain account information.
        return Response(error='request_failed', elapsed_ms=int((time.monotonic() - started) * 1000))


def keyvalues(data: dict, depth=0) -> str:
    lines = []
    for key, value in data.items():
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', str(key)):
            raise ValueError('Invalid KeyValues key')
        if isinstance(value, dict):
            lines.extend([' ' * depth + f'"{key}"', ' ' * depth + '{', keyvalues(value, depth + 2), ' ' * depth + '}'])
        else:
            value = str(value)
            if any(c in value for c in '\r\n\x00"\\'):
                raise ValueError('Invalid KeyValues value')
            lines.append(' ' * depth + f'"{key}" "{value}"')
    return '\n'.join(lines)


class Mailbox:
    def __init__(self, addon: Path, token: str):
        if not re.fullmatch('[0-9a-f]{32}', token):
            raise ValueError('Invalid mailbox token')
        self.token = token
        self.relative = f'scripts/ahb_{token}'
        self.folder = addon / self.relative
        self.folder.mkdir(parents=True, exist_ok=True)
        self.ready: dict[str, str] = {}
        self.files: dict[str, list[Path]] = {}
        self.pending: dict[str, Response] = {}
        self.cleanup: set[Path] = set()
        self.index_dirty = True
        self.last_write_warning = float('-inf')
        self.index()

    @staticmethod
    def write(path: Path, data: dict):
        atomic_bytes(path, (keyvalues({'ahb': data}) + '\n').encode('ascii'))

    def index(self):
        self.index_dirty = True
        try:
            self.write(self.folder / 'index.kv', {'token': self.token, 'done': self.ready})
        except PermissionError:
            self.write_blocked()
        else:
            self.index_dirty = False

    def write_blocked(self):
        now = time.monotonic()
        if now - self.last_write_warning >= 10:
            print('Local HTTP file write blocked; retaining replies and retrying.', flush=True)
            self.last_write_warning = now

    def configure(self, addon: Path, probe=False):
        self.write(addon / 'scripts' / 'aghs_http_bridge.txt', {'token': self.token, 'probe': int(probe)})

    def publish(self, request_id: str, response: Response):
        if not re.fullmatch(r'q[0-9a-f]+_[0-9a-f]+', request_id):
            raise ValueError('Invalid request ID')
        self.pending[request_id] = response
        self.flush()

    def flush(self):
        for request_id, response in list(self.pending.items()):
            try:
                self.write_response(request_id, response)
            except PermissionError:
                self.write_blocked()
                continue
            del self.pending[request_id]
        if self.index_dirty:
            self.index()
        for path in list(self.cleanup):
            try:
                unlink_file(path)
            except PermissionError:
                self.write_blocked()
            else:
                self.cleanup.discard(path)

    def write_response(self, request_id: str, response: Response):
        payload = response.envelope()
        if len(payload) > MAX_ENVELOPE:
            response = Response(error='response_too_large')
            payload = response.envelope()
        chunks = [payload[n:n + CHUNK_BYTES] for n in range(0, len(payload), CHUNK_BYTES)]
        page_count = (len(chunks) + PAGE_CHUNKS - 1) // PAGE_CHUNKS
        self.files[request_id] = [self.folder / f'{request_id}_{page}.kv' for page in range(page_count)] + [self.folder / f'{request_id}.kv']
        for page in range(page_count):
            path = self.folder / f'{request_id}_{page}.kv'
            self.write(path, {'token': self.token, 'request': request_id, 'page': page,
                              'data': {f'b{i}': chunk.hex() for i, chunk in enumerate(chunks[page * PAGE_CHUNKS:(page + 1) * PAGE_CHUNKS])}})
        path = self.folder / f'{request_id}.kv'
        self.write(path, {'token': self.token, 'request': request_id, 'code': response.code,
                          'error': response.error, 'elapsed_ms': response.elapsed_ms,
                          'length': len(payload), 'chunks': len(chunks), 'pages': page_count})
        self.ready[request_id] = '1'
        self.index_dirty = True

    def acknowledge(self, request_id: str):
        self.pending.pop(request_id, None)
        if request_id in self.ready:
            self.ready.pop(request_id)
            self.index_dirty = True
        self.cleanup.update(self.files.pop(request_id, []))
        self.flush()


class Dispatcher:
    def __init__(self, mailbox: Mailbox, policy: DestinationPolicy):
        self.mailbox, self.policy = mailbox, policy
        self.partial = {}
        self.seen = OrderedDict()
        self.active = set()
        self.cancelled = set()
        self.finished = queue.Queue()
        self.workers = ThreadPoolExecutor(max_workers=4)

    def _remember(self, request_id):
        self.seen[request_id] = None
        while len(self.seen) > 4096:
            self.seen.popitem(last=False)

    def accept(self, line: bytes):
        ack = ACK_LINE.search(line)
        if ack and ack[1].decode() == self.mailbox.token:
            rid = ack[2].decode()
            if rid in self.active:
                self.cancelled.add(rid)
            self.partial.pop(rid, None)
            self._remember(rid)
            self.mailbox.acknowledge(rid)
            return
        match = REQUEST_LINE.search(line)
        if not match or match[1].decode() != self.mailbox.token:
            return
        if len(match[3]) > 5 or len(match[4]) > 5:
            return
        rid, part, total = match[2].decode(), int(match[3]), int(match[4])
        if len(rid) > 96 or rid in self.seen or not 1 <= part <= total <= (2 * MAX_REQUEST + LOG_HEX_CHARS - 1) // LOG_HEX_CHARS:
            return
        if len(match[5]) > LOG_HEX_CHARS or len(match[5]) % 2:
            return
        now = time.monotonic()
        self.partial = {k: v for k, v in self.partial.items() if now - v[0] < 120}
        if rid not in self.partial:
            if len(self.partial) >= 16:
                return
            self.partial[rid] = (now, total, {})
        _, expected, pieces = self.partial[rid]
        if total != expected or (part in pieces and pieces[part] != match[5]):
            return
        pieces[part] = match[5]
        if len(pieces) < total:
            return
        self.partial.pop(rid)
        self._remember(rid)
        try:
            request = Request.decode(bytes.fromhex(b''.join(pieces[i] for i in range(1, total + 1)).decode()))
        except (ValueError, UnicodeError):
            self.mailbox.publish(rid, Response(error='invalid_request'))
            return
        if len(self.active) >= 16 or len(self.mailbox.ready) + len(self.mailbox.pending) >= 32:
            self.mailbox.publish(rid, Response(error='busy'))
            return
        self.active.add(rid)
        task = self.workers.submit(fetch, request, self.policy)
        task.add_done_callback(lambda done, key=rid: self.finished.put((key, done.result())))

    def drain(self):
        while True:
            try:
                rid, result = self.finished.get_nowait()
            except queue.Empty:
                self.mailbox.flush()
                return
            self.active.discard(rid)
            if rid in self.cancelled:
                self.cancelled.discard(rid)
                continue
            self.mailbox.publish(rid, result)
            print(f'HTTP {rid}: status {result.code}, {len(result.body)} bytes' + (f', {result.error}' if result.error else ''), flush=True)

    def close(self):
        self.workers.shutdown(wait=True)
        self.drain()


class LogReader:
    """Start at EOF and handle replacement/truncation between Dota launches."""
    def __init__(self, path: Path):
        self.path = path
        stat = path.stat() if path.exists() else None
        self.position = stat.st_size if stat else 0
        self.identity = (stat.st_dev, stat.st_ino) if stat else None
        self.incomplete = b''

    def read(self):
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity != self.identity or stat.st_size < self.position:
                self.position, self.incomplete = 0, b''
            self.identity = identity
            with self.path.open('rb') as stream:
                stream.seek(self.position)
                data = stream.read(1024 * 1024)
                self.position = stream.tell()
        except FileNotFoundError:
            self.identity = None
            return []
        lines = (self.incomplete + data).split(b'\n')
        self.incomplete = lines.pop()
        if len(self.incomplete) > 4096:
            self.incomplete = b''
        return lines
