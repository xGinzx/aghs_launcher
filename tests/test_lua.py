"""Run the public Lua adapter against the Python bridge. SPDX-License-Identifier: MIT"""
import http.server
from pathlib import Path
import tempfile
import threading
import time
import unittest

from bridge import DestinationPolicy, Dispatcher, Mailbox, Request, Response
from support import HTTPFixture, read_keyvalues

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(LuaRuntime, 'Optional development dependency lupa is not installed')
class LuaAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), HTTPFixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'.encode()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addon = Path(self.temporary.name)
        self.mailbox = Mailbox(self.addon, 'c' * 32)
        self.mailbox.configure(self.addon)
        self.dispatcher = Dispatcher(self.mailbox, DestinationPolicy(test_loopback=True))
        self.lua = LuaRuntime(encoding=None, unpack_returned_tuples=True)
        self.output = []
        self.globals = self.lua.globals()
        self.globals[b'print_Engine'] = lambda value: self.output.append(value)

        def read(path):
            file = self.addon / path.decode()
            if not file.exists():
                return None

            def byte_keys(value):
                return {key.encode(): byte_keys(item) if isinstance(item, dict) else item.encode() for key, item in value.items()}

            return self.lua.table_from(byte_keys(read_keyvalues(file)), recursive=True)

        self.globals[b'LoadKeyValues'] = read
        self.lua.execute(b'''
            fixture_time = 123
            RealTime = function() return fixture_time end
            CreateHTTPRequest = function() return nil end
            CreateHTTPRequestScriptVM = function() return nil end
            local entity = {}
            function entity:SetContextThink(name, callback, interval) fixture_think = callback end
            GameRules = { GetGameModeEntity = function() return entity end }
        ''')
        self.adapter = self.lua.execute((ROOT / 'lua' / 'http_bridge.lua').read_bytes())
        self.adapter.install()

    def tearDown(self):
        self.dispatcher.close()
        self.temporary.cleanup()

    def pump(self, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while self.output:
                self.dispatcher.accept(self.output.pop(0))
            self.dispatcher.drain()
            self.adapter.poll()
            if self.globals[b'fixture_result'] is not None:
                while self.output:
                    self.dispatcher.accept(self.output.pop(0))
                return self.globals[b'fixture_result']
            time.sleep(0.01)
        self.fail('Lua callback was not invoked')

    def test_raw_binary_request_and_callback(self):
        self.globals[b'fixture_url'] = self.origin + b'/echo'
        self.globals[b'fixture_body'] = b'\x00\xff\r\n\x80body'
        self.lua.execute(b'''
            local r = CreateHTTPRequestScriptVM('POST', fixture_url)
            assert(r:SetHTTPRequestRawPostBody('application/octet-stream', fixture_body))
            assert(r:Send(function(reply) fixture_result = reply end))
            assert(not r:Send(function() end))
            assert(not r:SetHTTPRequestHeaderValue('late', 'value'))
        ''')
        result = self.pump()
        self.assertEqual(result[b'StatusCode'], 201)
        self.assertEqual(result[b'Body'], self.globals[b'fixture_body'])
        self.assertFalse(self.mailbox.ready)

    def test_large_paged_response_is_exact(self):
        self.globals[b'fixture_url'] = self.origin + b'/binary'
        self.lua.execute(b"assert(CreateHTTPRequest('GET', fixture_url):Send(function(r) fixture_result = r end))")
        result = self.pump()
        self.assertEqual(result[b'StatusCode'], 200)
        self.assertEqual(result[b'Body'], bytes(range(256)) * 2300)
        self.assertEqual(result[b'Headers'][b'X-Fixture'], b'public-test')

    def test_unicode_parameters_and_header_validation(self):
        self.globals[b'fixture_url'] = self.origin + b'/query'
        self.globals[b'fixture_value'] = 'a & \u96ea'.encode()
        self.globals[b'fixture_bad_header'] = b'bad\r\nvalue'
        self.lua.execute(b'''
            local r = CreateHTTPRequest('GET', fixture_url)
            assert(not r:SetHTTPRequestHeaderValue('Host', 'other.invalid'))
            assert(not r:SetHTTPRequestHeaderValue('X-Test', fixture_bad_header))
            assert(r:SetHTTPRequestGetOrPostParameter('q', fixture_value))
            assert(r:Send(function(reply) fixture_result = reply end))
        ''')
        self.assertEqual(self.pump()[b'Body'], b'/query?q=a+%26+%E9%9B%AA')

    def test_timeout_invokes_callback_once(self):
        self.globals[b'fixture_url'] = self.origin
        self.lua.execute(b'''
            fixture_calls = 0
            local r = CreateHTTPRequest('GET', fixture_url)
            assert(r:SetHTTPRequestAbsoluteTimeoutMS(1))
            r:Send(function(reply) fixture_calls = fixture_calls + 1; fixture_result = reply end)
            fixture_time = fixture_time + 1
        ''')
        self.adapter.poll()
        self.adapter.poll()
        self.assertEqual(self.globals[b'fixture_calls'], 1)
        self.assertEqual(self.globals[b'fixture_result'][b'Error'], b'timeout')

    def test_native_factory_and_activate_return_values(self):
        # A new module instance installs around a functioning native constructor.
        self.lua.execute(b"native_object = {}; CreateHTTPRequest = function() return native_object end")
        adapter = self.lua.execute((ROOT / 'lua' / 'http_bridge.lua').read_bytes())
        adapter.install()
        self.lua.execute(b"assert(CreateHTTPRequest('GET', 'https://example.org') == native_object)")
        self.globals[b'fixture_adapter'] = adapter
        self.lua.execute(b'''
            package.preload['ahb_bridge'] = function() return fixture_adapter end
            package.preload['ahb_workshop'] = function()
                Activate = function(a) fixture_argument = a; return 'one', nil, 'three' end
                return 'workshop return'
            end
        ''')
        self.assertEqual(self.lua.execute((ROOT / 'lua' / 'entry.lua').read_bytes()), b'workshop return')
        self.assertEqual(self.lua.eval(b"function() return Activate('argument') end")(), (b'one', None, b'three'))
        self.assertEqual(self.globals[b'fixture_argument'], b'argument')

    def test_corrupt_response_becomes_error(self):
        self.globals[b'fixture_url'] = self.origin
        self.lua.execute(b"CreateHTTPRequest('GET', fixture_url):Send(function(r) fixture_result = r end)")
        frame = next(line for line in self.output if b'[AGHS_HTTP_REQUEST]' in line)
        rid = frame.split(b':')[1].decode()
        self.mailbox.publish(rid, Response(200, b'body'))
        page = self.mailbox.folder / (rid + '_0.kv')
        page.write_text(page.read_text().replace('41484231', 'zzzzzzzz'))
        self.adapter.poll()
        self.assertEqual(self.globals[b'fixture_result'][b'Error'], b'invalid_response')


if __name__ == '__main__':
    unittest.main()
