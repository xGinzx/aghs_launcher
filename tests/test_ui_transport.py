"""Lua-to-JavaScript UI delivery and resource tests. SPDX-License-Identifier: MIT"""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import unittest

from resources import inject_script, script_data

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[1]


class ResourceTests(unittest.TestCase):
    def test_patched_script_preserves_metadata_and_strict_mode(self):
        metadata, script = b'metadata with relative fields', b"'use strict';\nvar fixture = 1;"
        header = struct.pack('<IHHII', 48 + len(metadata) + len(script), 12, 4, 8, 2)
        data = header + struct.pack('<4sII', b'RED2', 28, len(metadata))
        data += struct.pack('<4sII', b'DATA', 16 + len(metadata), len(script))
        data += b'\0' * 8 + metadata + script
        patched = inject_script(data, b'fixture_transport();')
        _, blocks, result = script_data(patched)
        self.assertTrue(result.startswith(b"'use strict';\nfixture_transport();"))
        self.assertIn(b'var fixture = 1;', result)
        record = next(b for b in blocks if b[1] == b'RED2')
        self.assertEqual(patched[record[2]:record[2] + record[3]], metadata)


@unittest.skipUnless(LuaRuntime and shutil.which('node'), 'Optional Lua runtime and Node.js required')
class UITransportTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(encoding=None, unpack_returned_tuples=True)
        self.messages = []
        self.lua.globals()[b'capture'] = self.capture
        self.lua.execute(b'''
            RealTime=function() return 10 end
            IsServer=function() return true end
            print_Engine=function() end
            fixture_player={IsNull=function() return false end, GetTeamNumber=function() return 2 end}
            local entity={SetContextThink=function(self,name,fn) fixture_think=fn end}
            GameRules={GetGameModeEntity=function()return entity end}
            PlayerResource={GetPlayer=function(self,id) if id==0 then return fixture_player end end}
            CustomGameEventManager={
                Send_ServerToPlayer=function(self,player,name,data) capture(name,data) end,
                RegisterListener=function(self,name,fn) return 17 end
            }
        ''')
        self.transport = self.lua.execute((ROOT / 'lua' / 'ui_transport.lua').read_bytes())
        self.transport.install()

    def capture(self, name, data):
        def native(value):
            if isinstance(value, bytes): return value.decode('utf-8')
            if hasattr(value, 'items'): return {str(native(k)): native(v) for k, v in value.items()}
            return value
        self.messages.append({'name': name.decode(), 'data': native(data)})

    def pump(self):
        sizes = []
        for _ in range(5000):
            before = len(self.messages)
            pending = self.transport.pump()
            sizes.append(sum(len(json.dumps(m).encode()) for m in self.messages[before:]))
            if pending is None:
                return sizes
        self.fail('UI queue failed to drain')

    def receive(self, messages=None, unsubscribe=False, event='fixture_inventory'):
        result = subprocess.run(['node', str(ROOT / 'tests' / 'ui_receiver_runner.js')],
                                input=json.dumps({'event': event, 'messages': messages or self.messages,
                                                  'unsubscribe': unsubscribe}), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_large_inventory_is_chunked_and_reassembled_without_loss(self):
        self.lua.globals()[b'fixture_text'] = ('quoted " slash \\ Unicode \u96ea ' * 22000).encode()
        self.lua.execute(b'''
            fixture_data={text=fixture_text, flags={enabled=true}, [1]={id=123}, empty={}}
            CustomGameEventManager:Send_ServerToPlayer(fixture_player,'fixture_inventory',fixture_data)
            fixture_data.text='changed after enqueue'
        ''')
        sizes = self.pump()
        self.assertGreater(len(self.messages), 100)
        self.assertGreater(len(sizes), 10)
        self.assertTrue(all(m['name'] == 'ahb_ui_fragment' for m in self.messages))
        self.assertLess(max(len(json.dumps(m).encode()) for m in self.messages), 1600)
        # Deliberately reorder fragments and repeat them to check assembly and replay suppression.
        result = self.receive(list(reversed(self.messages)) + self.messages)
        self.assertFalse(result['errors'])
        self.assertEqual(len(result['received']), 1)
        payload = result['received'][0]
        self.assertEqual(payload['text'], self.lua.globals()[b'fixture_text'].decode())
        self.assertEqual(payload['1'], {'id': 123})
        self.assertEqual(payload['empty'], {})
        self.assertTrue(payload['flags']['enabled'])

    def test_small_events_are_snapshotted_and_native_methods_still_forward(self):
        self.lua.execute(b'''
            local data={value=1}
            CustomGameEventManager:Send_ServerToPlayer(fixture_player,'fixture_inventory',data)
            data.value=2
            assert(CustomGameEventManager:RegisterListener('event',function()end)==17)
        ''')
        self.assertFalse(self.messages)
        self.pump()
        self.assertEqual(self.messages, [{'name':'fixture_inventory','data':{'value':1}}])
        self.assertEqual(self.receive()['received'], [{'value':1}])
        self.assertEqual(self.receive(unsubscribe=True)['received'], [])

    def test_incomplete_and_invalid_transfers_do_not_reach_ui(self):
        self.lua.globals()[b'fixture_text'] = b'x' * 10000
        self.lua.execute(b"CustomGameEventManager:Send_ServerToPlayer(fixture_player,'fixture_inventory',{text=fixture_text})")
        self.pump()
        self.assertEqual(self.receive(self.messages[:-1])['received'], [])
        malformed = json.loads(json.dumps(self.messages))
        malformed[0]['data']['hex'] = 'zz'
        result = self.receive(malformed)
        self.assertEqual(result['received'], [])
        self.assertTrue(result['errors'])

    def test_invalid_players_are_removed_before_sending(self):
        self.lua.execute(b'''
            CustomGameEventManager:Send_ServerToPlayer(fixture_player,'fixture_inventory',{value=1})
            fixture_player.IsNull=function()return true end
        ''')
        self.pump()
        self.assertFalse(self.messages)

    def test_pending_full_state_requests_coalesce_to_latest_snapshot(self):
        self.lua.execute(b'''
            local first={text=string.rep('a',10000), version=1}
            CustomGameEventManager:Send_ServerToPlayer(fixture_player,'GetPlayerRelicData_feedback',first)
            for i=1,20 do
                CustomGameEventManager:Send_ServerToPlayer(fixture_player,'GetPlayerRelicData_feedback',
                    {version=2,text=string.rep('b',10000)})
            end
        ''')
        self.pump()
        self.assertEqual(len({m['data']['id'] for m in self.messages}), 1)
        received = self.receive(event='GetPlayerRelicData_feedback')['received']
        self.assertEqual(received, [{'version':2,'text':'b'*10000}])

    def test_scroll_transfer_finishes_before_large_inventory(self):
        self.lua.execute(b'''
            CustomGameEventManager:Send_ServerToPlayer(fixture_player,'fixture_inventory',{text=string.rep('r',520000)})
            CustomGameEventManager:Send_ServerToPlayer(fixture_player,'fixture_scrolls',{text=string.rep('s',16000)})
        ''')
        for _ in range(50):
            self.transport.pump()
            scrolls = [m['data'] for m in self.messages if m['data']['event']=='fixture_scrolls']
            if scrolls and scrolls[-1]['part']==scrolls[-1]['total']:
                break
        else:
            self.fail('Scrolls waited behind the full inventory')
        inventory = [m['data'] for m in self.messages if m['data']['event']=='fixture_inventory']
        self.assertLess(inventory[-1]['part'],inventory[-1]['total'])
        self.assertEqual(self.receive(event='fixture_scrolls')['received'],[{'text':'s'*16000}])
        self.pump()

    def test_inflight_snapshot_keeps_one_pending_copy_for_new_subscribers(self):
        self.lua.execute(b'''
            local data={text=string.rep('x',30000)}
            CustomGameEventManager:Send_ServerToPlayer(fixture_player,'GetPlayerRelicData_feedback',data)
        ''')
        self.transport.pump()
        self.lua.execute(b'''
            for i=1,20 do
                CustomGameEventManager:Send_ServerToPlayer(fixture_player,'GetPlayerRelicData_feedback',
                    {text=string.rep('x',30000)})
            end
        ''')
        self.pump()
        ids=[]
        for message in self.messages:
            key=message['data']['id']
            if key not in ids: ids.append(key)
        self.assertEqual(len(ids),2)
        self.assertEqual(len(self.receive(event='GetPlayerRelicData_feedback')['received']),2)


if __name__ == '__main__':
    unittest.main()
