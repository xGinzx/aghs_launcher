"""Check a real local Workshop build in a temporary folder. SPDX-License-Identifier: MIT"""
import argparse
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launcher import prepare, patch_files
from vpk import Archive


def verify(source: Path, temp_dir=None):
    original = Archive(source)
    with tempfile.TemporaryDirectory(prefix='ahb_verify_', dir=temp_dir) as folder:
        addon = Path(folder) / 'addon'
        manifest = prepare(source, addon, '2483181385')
        built = Archive(addon / 'pak01_dir.vpk')
        assert built.read('scripts/vscripts/ahb_workshop.lua') == original.read('scripts/vscripts/addon_game_mode.lua')
        modifications = patch_files(original)
        additions = set(modifications) - set(original.entries)
        assert set(built.entries) - set(original.entries) == additions
        assert not set(original.entries) - set(built.entries)
        for name, entry in original.entries.items():
            if name not in modifications:
                assert built.entries[name] == entry
        for name, expected in modifications.items():
            assert built.read(name) == expected
        print(f'Verified {len(original.entries)} original entries; build {manifest["build_id"][:16]}')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--temp-dir', type=Path)
    arguments = parser.parse_args()
    verify(arguments.source, arguments.temp_dir)
