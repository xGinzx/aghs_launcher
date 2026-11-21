"""HTTP and transport integration tests. SPDX-License-Identifier: MIT"""
import http.server
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bridge import DestinationPolicy, Dispatcher, LogReader, Mailbox, Request, Response, decode_fields, encode_fields, fetch
from support import HTTPFixture, read_keyvalues


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), HTTPFixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'
        cls.policy = DestinationPolicy(test_loopback=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_http_status_parameters_and_raw_body(self):
        result = fetch(Request('GET', self.origin + '/missing?old=1', parameters=[(b'q', 'x & \u96ea'.encode())]), self.policy)
        self.assertEqual((result.code, result.body), (404, b'/missing?old=1&q=x+%26+%E9%9B%AA'))
        result = fetch(Request('POST', self.origin + '/', parameters=[(b'a', b'x+y')]), self.policy)
        self.assertEqual((result.code, result.body), (201, b'a=x%2By'))
        body = b'\x00\xff\r\n\x80'
        self.assertEqual(fetch(Request('POST', self.origin, raw=True, body=body), self.policy).body, body)

    def test_redirect_compression_and_timeout(self):
        self.assertEqual(fetch(Request('GET', self.origin + '/redirect'), self.policy).body, b'/arrived')
        self.assertEqual(fetch(Request('GET', self.origin + '/gzip'), self.policy).body, b'compressed fixture')
        self.assertEqual(fetch(Request('GET', self.origin + '/slow', total_ms=40), self.policy).error, 'timeout')

    def test_policy_blocks_private_and_redirected_private_addresses(self):
        self.assertEqual(fetch(Request('GET', self.origin), DestinationPolicy()).code, 0)
        with self.assertRaises(ValueError):
            DestinationPolicy(['https://example.org']).resolve(self.origin)
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::ffff:127.0.0.1'):
            with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', (address, 80))]):
                with self.assertRaises(ValueError):
                    DestinationPolicy().resolve('http://fixture.invalid')

    def test_fragmented_request_and_ack_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            mailbox = Mailbox(Path(temp), 'a' * 32)
            dispatcher = Dispatcher(mailbox, self.policy)
            packet = encode_fields([b'GET', (self.origin + '/binary').encode(), b'2000', b'1000', b'form', b'', b'0', b'1', b'long', b'x' * 800])
            wire = packet.hex()
            pieces = [wire[n:n + 768] for n in range(0, len(wire), 768)]
            for token in ('b' * 32, mailbox.token):
                for i in reversed(range(len(pieces))):
                    dispatcher.accept(f'prefix [AGHS_HTTP_REQUEST] {token}:q123_1:{i + 1}/{len(pieces)}:{pieces[i]}'.encode())
                if token[0] == 'b':
                    self.assertFalse(dispatcher.active)
            deadline = time.monotonic() + 3
            while not mailbox.ready and time.monotonic() < deadline:
                dispatcher.drain()
                time.sleep(0.01)
            dispatcher.close()
            record = read_keyvalues(mailbox.folder / 'q123_1.kv')
            self.assertEqual(record['code'], '200')
            self.assertGreater(int(record['pages']), 1)
            dispatcher.accept(f'[AGHS_HTTP_ACK] {mailbox.token}:q123_1'.encode())
            self.assertFalse(mailbox.ready)
            self.assertEqual([p.name for p in mailbox.folder.iterdir()], ['index.kv'])


class FramingTests(unittest.TestCase):
    def test_binary_envelopes_and_rejection(self):
        values = [b'', bytes(range(256)), 'text \u96ea'.encode()]
        encoded = encode_fields(values)
        self.assertEqual(decode_fields(encoded), values)
        for invalid in (encoded[:-1], encoded + b'x', b'AHB1ffff', b'AHB10001zzzzzzzz'):
            with self.assertRaises(ValueError):
                decode_fields(invalid)

    def test_request_validation(self):
        base = [b'POST', b'https://example.org', b'1000', b'1000', b'form', b'', b'0', b'0']
        self.assertEqual(Request.decode(encode_fields(base)).method, 'POST')
        for index, value in ((0, b'CONNECT'), (2, b'0'), (4, b'other'), (5, b'unexpected')):
            values = base.copy()
            values[index] = value
            with self.assertRaises(ValueError):
                Request.decode(encode_fields(values))
        invalid_header = base[:6] + [b'1', b'Host', b'other.invalid', b'0']
        with self.assertRaises(ValueError):
            Request.decode(encode_fields(invalid_header))

    def test_log_rotation_and_partial_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'console.log'
            path.write_bytes(b'old data\n')
            reader = LogReader(path)
            self.assertEqual(reader.read(), [])
            with path.open('ab') as stream:
                stream.write(b'new par')
            self.assertEqual(reader.read(), [])
            with path.open('ab') as stream:
                stream.write(b't\n')
            self.assertEqual(reader.read(), [b'new part'])
            path.write_bytes(b'fresh\n')
            self.assertEqual(reader.read(), [b'fresh'])


if __name__ == '__main__':
    unittest.main()
