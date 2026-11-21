"""Synthetic fixtures shared by tests. SPDX-License-Identifier: MIT"""
import http.server
from pathlib import Path
import re
import struct
import time
import zlib

from vpk import Entry, directory


class HTTPFixture(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Deliberate timeout tests close their temporary client connections.
            pass

    def do_GET(self):
        status, body = 200, self.path.encode('ascii')
        if self.path.startswith('/binary'):
            body = bytes(range(256)) * 2300
        elif self.path.startswith('/gzip'):
            import gzip
            body = gzip.compress(b'compressed fixture')
        elif self.path.startswith('/missing'):
            status = 404
        elif self.path.startswith('/redirect'):
            status, body = 302, b''
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Fixture', 'public-test')
        if self.path.startswith('/gzip'):
            self.send_header('Content-Encoding', 'gzip')
        if self.path.startswith('/redirect'):
            self.send_header('Location', '/arrived')
        self.end_headers()
        if self.path.startswith('/slow'):
            time.sleep(0.2)
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.send_response(201)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def read_keyvalues(path: Path):
    tokens = re.findall(r'"([^"\n]*)"|([{}])', path.read_text('ascii'))
    position = 0

    def mapping(nested=False):
        nonlocal position
        result = {}
        while position < len(tokens):
            key, symbol = tokens[position]
            position += 1
            if symbol == '}':
                if not nested:
                    raise ValueError('Unexpected closing brace')
                return result
            if symbol:
                raise ValueError('Expected a key')
            value, symbol = tokens[position]
            position += 1
            result[key] = mapping(True) if symbol == '{' else value
        if nested:
            raise ValueError('Missing closing brace')
        return result

    return mapping()['ahb']


def make_vpk(path: Path, contents=None):
    contents = contents or {'addoninfo.txt': b'fixture addon',
                            'scripts/vscripts/addon_game_mode.lua': b'function Activate() return 7 end',
                            'maps/fixture.bin': b'unchanged game data\x00\xff'}
    entries, payload = {}, bytearray()
    for name, data in contents.items():
        entries[name] = Entry(zlib.crc32(data), 0x7FFF, len(payload), len(data))
        payload.extend(data)
    tree = directory(entries)
    path.write_bytes(struct.pack('<7I', 0x55AA1234, 2, len(tree), len(payload), 0, 0, 0) + tree + payload)
    return contents
