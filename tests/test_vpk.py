"""Synthetic archive and incremental preparation tests. SPDX-License-Identifier: MIT"""
import json
import os
from pathlib import Path
import tempfile
import unittest

from launcher import AddonLock, DEFAULTS, OWNER_FILE, prepare, validate_settings
from support import make_vpk
from vpk import Archive


class ArchiveTests(unittest.TestCase):
    def test_patch_preserves_original_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contents = make_vpk(root / 'source.vpk')
            original_bytes = (root / 'source.vpk').read_bytes()
            source = Archive(root / 'source.vpk')
            changes = {'scripts/vscripts/new.lua': b'return 1', 'addoninfo.txt': b'new info'}
            build_id = source.write_patched(root / 'patched.vpk', changes)
            result = Archive(root / 'patched.vpk')
            self.assertEqual(len(build_id), 64)
            for name, data in contents.items():
                self.assertEqual(result.read(name), changes.get(name, data))
            self.assertEqual(result.read('scripts/vscripts/new.lua'), b'return 1')
            self.assertEqual((root / 'source.vpk').read_bytes(), original_bytes)

    def test_corruption_and_failed_build_preserve_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_vpk(root / 'source.vpk')
            source = Archive(root / 'source.vpk')
            data = bytearray(source.path.read_bytes())
            data[-1] ^= 1
            source.path.write_bytes(data)
            with self.assertRaises(ValueError):
                Archive(source.path).read('maps/fixture.bin')
            (root / 'destination.vpk').write_bytes(b'previous good build')
            source.path.write_bytes(data[:source.data_offset])
            with self.assertRaises(ValueError):
                source.write_patched(root / 'destination.vpk', {'new.txt': b'hello'})
            self.assertEqual((root / 'destination.vpk').read_bytes(), b'previous good build')
            self.assertFalse((root / 'destination.vpk.building').exists())

    def test_prepare_rebuilds_same_size_update_and_keeps_paths_out_of_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contents = make_vpk(root / 'source.vpk')
            addon = root / 'addon'
            with AddonLock(addon):
                first = prepare(root / 'source.vpk', addon, '123')
                stamp = (addon / 'pak01_dir.vpk').stat().st_mtime_ns
                self.assertEqual(prepare(root / 'source.vpk', addon, '123')['build_id'], first['build_id'])
                self.assertEqual((addon / 'pak01_dir.vpk').stat().st_mtime_ns, stamp)
                source_stamp = (root / 'source.vpk').stat()
                contents['maps/fixture.bin'] = b'UPDATED!! game data\x00\xff'
                self.assertEqual(len(contents['maps/fixture.bin']), len(b'unchanged game data\x00\xff'))
                make_vpk(root / 'source.vpk', contents)
                os.utime(root / 'source.vpk', ns=(source_stamp.st_atime_ns, source_stamp.st_mtime_ns))
                second = prepare(root / 'source.vpk', addon, '123')
                self.assertNotEqual(first['build_id'], second['build_id'])
            manifest = (addon / OWNER_FILE).read_text()
            self.assertNotIn(str(root), manifest)
            self.assertEqual(json.loads(manifest)['project'], 'aghs-launcher-public')

    def test_unmanaged_addon_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_vpk(root / 'source.vpk')
            addon = root / 'addon'
            addon.mkdir()
            (addon / 'existing.txt').write_text('keep')
            with self.assertRaises(ValueError):
                prepare(root / 'source.vpk', addon, '123')
            self.assertEqual((addon / 'existing.txt').read_text(), 'keep')

    def test_settings_reject_console_and_path_injection(self):
        for key, value in (('addon_name', '../other'), ('map', 'map;quit'), ('port', 0), ('workshop_id', '../123')):
            settings = dict(DEFAULTS, **{key: value})
            with self.assertRaises(ValueError):
                validate_settings(settings)


if __name__ == '__main__':
    unittest.main()
