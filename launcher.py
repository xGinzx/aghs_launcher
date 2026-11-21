"""Prepare and host a local Dota Workshop addon. SPDX-License-Identifier: MIT"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from bridge import DestinationPolicy, Dispatcher, LogReader, Mailbox, atomic_bytes
from vpk import Archive

ROOT = Path(__file__).resolve().parent
DEFAULTS = {'workshop_id': '2483181385', 'map': 'aghanim3', 'addon_name': 'aghs_public',
            'game_dir': None, 'port': 27015, 'allowed_origins': []}
OWNER_FILE = '.ahb-owner.json'
LOCK_FILE = '.ahb-operation.lock'


def steam_libraries() -> list[Path]:
    roots = []
    if os.name == 'nt':
        import winreg
        for hive, key, field in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                                 (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath')):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    roots.append(Path(winreg.QueryValueEx(handle, field)[0]))
            except OSError:
                pass
        for variable in ('ProgramFiles(x86)', 'ProgramFiles'):
            if os.environ.get(variable):
                roots.append(Path(os.environ[variable]) / 'Steam')
    libraries = []
    for root in roots:
        libraries.append(root)
        file = root / 'steamapps' / 'libraryfolders.vdf'
        if file.is_file():
            text = file.read_text(encoding='utf-8', errors='replace')
            libraries.extend(Path(value.replace('\\\\', '\\')) for value in re.findall(r'"path"\s*"((?:\\.|[^"\\])*)"', text))
    return list(dict.fromkeys(libraries))


def locate_game(explicit=None) -> Path:
    if explicit:
        game = Path(explicit).expanduser().resolve()
        if not (game / 'dota_addons').is_dir():
            raise ValueError('--game-dir must point to Dota\'s game directory (containing dota_addons)')
        return game
    for library in steam_libraries():
        game = library / 'steamapps' / 'common' / 'dota 2 beta' / 'game'
        if game.is_dir():
            return game.resolve()
    raise ValueError('Dota was not found. Set game_dir in settings.local.json or use --game-dir.')


def locate_workshop(game: Path, workshop_id: str, explicit=None) -> Path:
    if explicit:
        source = Path(explicit).expanduser().resolve()
        if not source.is_file():
            raise ValueError('--source does not point to an existing Workshop VPK')
        return source
    libraries = [game.parents[2]] + [p / 'steamapps' for p in steam_libraries()]
    for steamapps in dict.fromkeys(libraries):
        source = steamapps / 'workshop' / 'content' / '570' / workshop_id / (workshop_id + '.vpk')
        if source.is_file():
            return source.resolve()
    raise ValueError('Workshop VPK not found. Subscribe to the map and wait for Steam to finish downloading it.')


def load_settings(path=None) -> dict:
    path = Path(path) if path else ROOT / 'settings.local.json'
    settings = dict(DEFAULTS)
    if path.is_file():
        additions = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(additions, dict) or additions.keys() - settings.keys():
            raise ValueError('Settings contain unknown keys or are not a JSON object')
        settings.update(additions)
    elif path != ROOT / 'settings.local.json':
        raise ValueError('Settings file not found')
    return settings


def validate_settings(settings):
    if not isinstance(settings['workshop_id'], str) or not re.fullmatch(r'[0-9]{1,20}', settings['workshop_id']):
        raise ValueError('workshop_id must be a numeric string')
    if not isinstance(settings['addon_name'], str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', settings['addon_name']):
        raise ValueError('addon_name must contain letters, digits, and underscores')
    if not isinstance(settings['map'], str) or not re.fullmatch(r'[A-Za-z0-9_/-]{1,128}', settings['map']) or '..' in settings['map']:
        raise ValueError('Invalid map name')
    if type(settings['port']) is not int or not 1 <= settings['port'] <= 65535:
        raise ValueError('Invalid game port')
    if not isinstance(settings['allowed_origins'], list) or any(not isinstance(v, str) for v in settings['allowed_origins']):
        raise ValueError('allowed_origins must be a list of HTTP/HTTPS origins')
    if settings['game_dir'] is not None and not isinstance(settings['game_dir'], str):
        raise ValueError('game_dir must be a path string or null')


def dota_running() -> bool:
    if os.name != 'nt':
        return False
    result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq dota2.exe', '/FO', 'CSV', '/NH'],
                            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Could not check whether Dota is running')
    return any(row and row[0].lower() == 'dota2.exe' for row in csv.reader(result.stdout.splitlines()))


class AddonLock:
    def __init__(self, addon: Path):
        self.path = addon / LOCK_FILE
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            if self.path.stat().st_size == 0:
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another prepare operation or HTTP bridge is using this addon') from None
        return self

    def __exit__(self, *args):
        self.file.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


def patch_files(archive: Archive):
    if any(name in archive.entries for name in ('scripts/vscripts/ahb_bridge.lua', 'scripts/vscripts/ahb_workshop.lua')):
        raise ValueError('The source VPK already contains AHB files; use the original Workshop download')
    return {
        'scripts/vscripts/addon_game_mode.lua': (ROOT / 'lua' / 'entry.lua').read_bytes(),
        'scripts/vscripts/ahb_workshop.lua': archive.read('scripts/vscripts/addon_game_mode.lua'),
        'scripts/vscripts/ahb_bridge.lua': (ROOT / 'lua' / 'http_bridge.lua').read_bytes(),
    }


def prepare(source: Path, addon: Path, workshop_id: str) -> dict:
    addon = Path(addon)
    addon.mkdir(parents=True, exist_ok=True)
    owner = addon / OWNER_FILE
    if not owner.exists() and any(p.name != LOCK_FILE for p in addon.iterdir()):
        raise ValueError('The addon folder contains files not managed by this release. Move it aside or choose another addon_name.')
    previous = json.loads(owner.read_text()) if owner.exists() else {}
    if previous and previous.get('project') != 'aghs-launcher-public':
        raise ValueError('The addon folder belongs to another tool')
    archive = Archive(source)
    replacements = patch_files(archive)
    digest = hashlib.sha256()
    for name, content in sorted(replacements.items()):
        digest.update(name.encode())
        digest.update(content)
    inputs = {'size': archive.stat.st_size, 'mtime_ns': archive.stat.st_mtime_ns,
              'directory_sha256': archive.tree_hash, 'patch_sha256': digest.hexdigest()}
    target = addon / 'pak01_dir.vpk'
    info = archive.read('addoninfo.txt')
    if previous.get('inputs') == inputs and target.is_file() and (addon / 'addoninfo.txt').is_file():
        # Read checksums of the small patched entries, so accidental overwrites do not silently pass.
        installed = Archive(target)
        if all(installed.read(name) == content for name, content in replacements.items()) and (addon / 'addoninfo.txt').read_bytes() == info:
            print('Addon is current. Build ' + previous['build_id'][:16], flush=True)
            return previous
    print('Preparing local addon from the installed Workshop VPK...', flush=True)
    build_id = archive.write_patched(target, replacements)
    atomic_bytes(addon / 'addoninfo.txt', info)
    manifest = {'project': 'aghs-launcher-public', 'schema': 1, 'workshop_id': workshop_id,
                'build_id': build_id, 'inputs': inputs}
    atomic_bytes(owner, (json.dumps(manifest, indent=2) + '\n').encode())
    print('Addon ready. Build ' + build_id[:16], flush=True)
    return manifest


def serve(game: Path, addon: Path, settings: dict, *, launch=False, probe=False):
    token = secrets.token_hex(16)
    mailbox = Mailbox(addon, token)
    mailbox.configure(addon, probe)
    dispatcher = Dispatcher(mailbox, DestinationPolicy(settings['allowed_origins']))
    reader = LogReader(game / 'dota' / 'console.log')
    child = None
    try:
        if launch:
            executable = game / 'bin' / 'win64' / 'dota2.exe'
            if not executable.is_file():
                raise ValueError('Windows Dota executable not found')
            command = [str(executable), '-steam', '-console', '-condebug', '-port', str(settings['port']),
                       '+sv_lan', '0', '+customgamesetup_enable_auto_launch', '0',
                       '+dota_launch_custom_game', settings['addon_name'], settings['map']]
            child = subprocess.Popen(command, cwd=game)
        print('HTTP bridge active. Keep this window open while hosting.', flush=True)
        print(f'Host console: sv_lan 0; customgamesetup_enable_auto_launch 0; dota_launch_custom_game {settings["addon_name"]} {settings["map"]}', flush=True)
        print(f'Friend console: connect HOST_PUBLIC_IP:{settings["port"]}', flush=True)
        while child is None or child.poll() is None:
            for line in reader.read():
                dispatcher.accept(line)
                if b'[AGHS_HTTP_PROBE]' in line:
                    print(line.decode('utf-8', errors='replace'), flush=True)
            dispatcher.drain()
            time.sleep(0.05)
        if child.returncode:
            raise RuntimeError(f'Dota exited with code {child.returncode}')
    except KeyboardInterrupt:
        print('\nStopping HTTP bridge.', flush=True)
    finally:
        dispatcher.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'host', 'bridge'], help='prepare builds files; host launches Dota; bridge waits for a manual map launch')
    parser.add_argument('--config', type=Path, help='Optional local JSON settings')
    parser.add_argument('--game-dir', type=Path)
    parser.add_argument('--source', type=Path, help='Optional explicit original Workshop VPK')
    parser.add_argument('--workshop-id')
    parser.add_argument('--map', dest='map_name')
    parser.add_argument('--addon-name')
    parser.add_argument('--port', type=int)
    parser.add_argument('--allow-origin', action='append')
    parser.add_argument('--probe', action='store_true', help='Request Steam robots.txt to verify a Lua callback')
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        for option, key in ((args.game_dir, 'game_dir'), (args.workshop_id, 'workshop_id'), (args.map_name, 'map'),
                            (args.addon_name, 'addon_name'), (args.port, 'port'), (args.allow_origin, 'allowed_origins')):
            if option is not None:
                settings[key] = str(option) if key == 'game_dir' else option
        validate_settings(settings)
        if args.command in ('host', 'prepare') and dota_running():
            raise ValueError('Quit Dota before preparing files or starting a new host session')
        game = locate_game(settings['game_dir'])
        source = locate_workshop(game, settings['workshop_id'], args.source)
        addon = game / 'dota_addons' / settings['addon_name']
        if args.command == 'bridge':
            installed = addon / OWNER_FILE
            if not installed.is_file():
                raise ValueError('Run Prepare.cmd with Dota closed before starting the bridge')
        with AddonLock(addon):
            if args.command != 'bridge':
                prepare(source, addon, settings['workshop_id'])
            if args.command != 'prepare':
                serve(game, addon, settings, launch=args.command == 'host', probe=args.probe)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print('Error: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
