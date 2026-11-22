"""Atomic mailbox writes with bounded file-sharing retries. SPDX-License-Identifier: MIT"""
import os
from pathlib import Path
import tempfile
import time


def retry_access(operation, timeout=0.3):
    """Retry sharing/access races without changing permissions or truncating readers' files."""
    deadline = time.monotonic() + timeout
    delay = 0.005
    while True:
        try:
            return operation()
        except PermissionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.05)


def atomic_bytes(path: Path, data: bytes):
    path = Path(path)
    # Per-write names prevent another writer or a stale .tmp file from being reused.
    descriptor, name = retry_access(lambda: tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        retry_access(lambda: os.replace(temporary, path))
    finally:
        try:
            retry_access(lambda: temporary.unlink(missing_ok=True), timeout=0.05)
        except PermissionError:
            # A scanner may briefly own the unique temporary file as well.
            # Cleanup must not mask the write result or replace the destination.
            pass


def unlink_file(path: Path):
    retry_access(lambda: path.unlink(missing_ok=True))
