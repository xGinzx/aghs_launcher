"""Minimal checked reader and patch writer for self-contained Valve VPKs.

SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import struct
import zlib


@dataclass(frozen=True)
class Entry:
    crc: int
    archive: int
    offset: int
    length: int
    preload: bytes = b''


class Archive:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.stat = self.path.stat()
        with self.path.open('rb') as stream:
            header = stream.read(12)
            if len(header) != 12:
                raise ValueError('Truncated VPK header')
            signature, version, directory_bytes = struct.unpack('<III', header)
            if signature != 0x55AA1234 or version not in (1, 2) or directory_bytes > 64 * 1024 * 1024:
                raise ValueError('Unsupported VPK header')
            if version == 2:
                extra = stream.read(16)
                if len(extra) != 16:
                    raise ValueError('Truncated VPK v2 header')
                self.data_size, archive_hashes, other_hashes, signature_bytes = struct.unpack('<4I', extra)
                self.data_offset = 28 + directory_bytes
                if self.data_offset + self.data_size + archive_hashes + other_hashes + signature_bytes > self.stat.st_size:
                    raise ValueError('VPK sections exceed file size')
            else:
                self.data_offset = 12 + directory_bytes
                self.data_size = self.stat.st_size - self.data_offset
                if self.data_size < 0:
                    raise ValueError('VPK directory exceeds file size')
            self.tree = stream.read(directory_bytes)
        if len(self.tree) != directory_bytes:
            raise ValueError('Truncated VPK directory')
        self.tree_hash = hashlib.sha256(self.tree).hexdigest()
        self.entries = {}
        cursor = 0

        def text():
            nonlocal cursor
            end = self.tree.find(b'\0', cursor)
            if end < 0:
                raise ValueError('Unterminated VPK directory string')
            value = self.tree[cursor:end].decode('utf-8')
            cursor = end + 1
            return value

        while extension := text():
            while folder := text():
                while name := text():
                    if cursor + 18 > len(self.tree):
                        raise ValueError('Truncated VPK entry')
                    crc, initial, archive, offset, length, terminator = struct.unpack_from('<IHHIIH', self.tree, cursor)
                    cursor += 18
                    if terminator != 0xFFFF or cursor + initial > len(self.tree):
                        raise ValueError('Invalid VPK entry')
                    preload = self.tree[cursor:cursor + initial]
                    cursor += initial
                    path = (folder + '/' if folder != ' ' else '') + name + ('.' + extension if extension != ' ' else '')
                    if path in self.entries or any(part in ('', '..', '.') for part in path.split('/')) or '\\' in path:
                        raise ValueError('Invalid or duplicate VPK path')
                    if archive == 0x7FFF and offset + length > self.data_size:
                        raise ValueError('VPK entry exceeds data section')
                    self.entries[path] = Entry(crc, archive, offset, length, preload)
        if cursor != len(self.tree):
            raise ValueError('Unexpected directory suffix')

    def read(self, name: str) -> bytes:
        entry = self.entries[name]
        if entry.archive != 0x7FFF:
            raise ValueError('This launcher requires a self-contained Workshop VPK')
        with self.path.open('rb') as stream:
            stream.seek(self.data_offset + entry.offset)
            data = entry.preload + stream.read(entry.length)
        if len(data) != len(entry.preload) + entry.length or zlib.crc32(data) != entry.crc:
            raise ValueError(f'Invalid VPK entry checksum: {name}')
        return data

    def write_patched(self, target: Path, replacements: dict[str, bytes]) -> str:
        target = Path(target)
        if target.resolve() == self.path.resolve():
            raise ValueError('The Workshop archive cannot be overwritten')
        entries = dict(self.entries)
        appended = bytearray()
        for name, content in replacements.items():
            entries[name] = Entry(zlib.crc32(content), 0x7FFF, self.data_size + len(appended), len(content))
            appended.extend(content)
        if self.data_size + len(appended) >= 2 ** 32:
            raise ValueError('VPK data exceeds the 32-bit format limit')
        tree = directory(entries)
        temporary = target.with_name(target.name + '.building')
        original_hash = hashlib.sha256()
        try:
            with self.path.open('rb') as source, temporary.open('wb') as output:
                output.write(struct.pack('<7I', 0x55AA1234, 2, len(tree), self.data_size + len(appended), 0, 0, 0))
                output.write(tree)
                source.seek(self.data_offset)
                remaining = self.data_size
                while remaining:
                    block = source.read(min(remaining, 8 * 1024 * 1024))
                    if not block:
                        raise ValueError('Workshop data was truncated during the build')
                    original_hash.update(block)
                    output.write(block)
                    remaining -= len(block)
                output.write(appended)
            current = self.path.stat()
            if (current.st_size, current.st_mtime_ns) != (self.stat.st_size, self.stat.st_mtime_ns):
                raise ValueError('Workshop download changed during the build; wait for Steam and try again')
            verified = Archive(temporary)
            for name, content in replacements.items():
                if verified.read(name) != content:
                    raise ValueError('Patched archive verification failed')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        identity = self.tree_hash + original_hash.hexdigest()
        for name, content in sorted(replacements.items()):
            identity += name + hashlib.sha256(content).hexdigest()
        return hashlib.sha256(identity.encode()).hexdigest()


def directory(entries: dict[str, Entry]) -> bytes:
    groups = {}
    for path, entry in entries.items():
        folder, _, filename = path.rpartition('/')
        name, dot, extension = filename.rpartition('.')
        if not dot:
            name, extension = filename, ' '
        if entry.archive != 0x7FFF:
            raise ValueError('Split VPK archives are not supported')
        groups.setdefault(extension, {}).setdefault(folder or ' ', {})[name] = entry
    output = bytearray()
    for extension, folders in sorted(groups.items()):
        output.extend(extension.encode() + b'\0')
        for folder, files in sorted(folders.items()):
            output.extend(folder.encode() + b'\0')
            for name, entry in sorted(files.items()):
                output.extend(name.encode() + b'\0')
                output.extend(struct.pack('<IHHIIH', entry.crc, len(entry.preload), entry.archive, entry.offset, entry.length, 0xFFFF))
                output.extend(entry.preload)
            output.extend(b'\0')
        output.extend(b'\0')
    output.extend(b'\0')
    return bytes(output)
