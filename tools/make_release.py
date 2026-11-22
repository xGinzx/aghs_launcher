"""Create a deterministic source ZIP from an explicit allowlist. SPDX-License-Identifier: MIT"""
import argparse
import hashlib
from pathlib import Path
import zipfile

FILES = (
    '.gitignore', 'LICENSE', 'README.md', 'PROTOCOL.md', 'PROVENANCE.md',
    'Host Aghs.cmd', 'Prepare.cmd', 'settings.example.json',
    'launcher.py', 'bridge.py', 'vpk.py', 'retry_io.py', 'resources.py',
    'lua/entry.lua', 'lua/http_bridge.lua', 'lua/ui_transport.lua', 'panorama/ui_transport.js',
    'tests/support.py', 'tests/test_bridge.py', 'tests/test_vpk.py', 'tests/test_lua.py', 'tests/test_file_locks.py',
    'tests/test_ui_transport.py', 'tests/ui_receiver_runner.js',
    'tools/make_release.py', 'tools/verify_workshop.py',
)


def create(root: Path, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        if not (root / name).is_file():
            raise ValueError('Missing release file: ' + name)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in FILES:
            info = zipfile.ZipInfo('aghs_launcher_public/' + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (root / name).read_bytes())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(digest + '  ' + output.name + '\n', encoding='ascii')
    return digest


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=root / 'dist' / 'aghs_launcher_public.zip')
    args = parser.parse_args()
    digest = create(root, args.output)
    print(f'Created {args.output.name}: {len(FILES)} source files, SHA-256 {digest}')
