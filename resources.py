"""Patch plain-text DATA in compiled Panorama script resources. SPDX-License-Identifier: MIT"""
import struct


def script_data(resource: bytes):
    if len(resource) < 16:
        raise ValueError('Truncated Panorama resource')
    size, version, resource_version, offset, count = struct.unpack_from('<IHHII', resource)
    table = 8 + offset
    if version != 12 or resource_version != 4 or size != len(resource) or count > 128 or table + count * 12 > len(resource):
        raise ValueError('Unsupported Panorama script resource')
    blocks = []
    for index in range(count):
        position = table + index * 12
        tag, relative, length = struct.unpack_from('<4sII', resource, position)
        begin = position + 4 + relative
        if begin < table + count * 12 or begin + length > len(resource):
            raise ValueError('Invalid Panorama resource block')
        blocks.append((position, tag, begin, length))
    data = [block for block in blocks if block[1] == b'DATA']
    if len(data) != 1:
        raise ValueError('Expected one Panorama script DATA block')
    return table, blocks, resource[data[0][2]:data[0][2] + data[0][3]]


def inject_script(resource: bytes, prefix: bytes) -> bytes:
    table, blocks, original = script_data(resource)
    original.decode('utf-8')
    # Keep the original strict-mode directive as the first statement.
    stripped = original.lstrip()
    if stripped.startswith((b"'use strict';", b'"use strict";')):
        end = len(original) - len(stripped) + len(b"'use strict';")
    else:
        end = 0
    script = original[:end] + b'\n' + prefix + b'\n' + original[end:]
    result = bytearray(resource[:table + len(blocks) * 12])
    for position, tag, begin, length in sorted(blocks, key=lambda block: block[2]):
        result.extend(b'\0' * (-len(result) % 16))
        content = script if tag == b'DATA' else resource[begin:begin + length]
        struct.pack_into('<II', result, position + 4, len(result) - position - 4, len(content))
        result.extend(content)
    struct.pack_into('<I', result, 0, len(result))
    return bytes(result)
