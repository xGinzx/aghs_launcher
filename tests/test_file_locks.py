"""Regression tests for Windows readers blocking mailbox replacement. SPDX-License-Identifier: MIT"""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from bridge import DestinationPolicy, Dispatcher, Mailbox, Response
from retry_io import atomic_bytes
from support import read_keyvalues


@contextmanager
def windows_reader(path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    guard = threading.Lock()

    def close():
        nonlocal handle
        with guard:
            if handle is not None:
                kernel.CloseHandle(handle)
                handle = None

    try:
        yield close
    finally:
        close()


class AtomicWriteTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Requires native Windows file sharing')
    def test_real_read_handle_blocks_replace_then_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'index.kv'
            candidate = Path(temp) / 'candidate.tmp'
            target.write_bytes(b'previous complete index')
            candidate.write_bytes(b'candidate')
            with windows_reader(target) as release:
                with self.assertRaises(PermissionError) as failure:
                    os.replace(candidate, target)
                self.assertIn(failure.exception.winerror, (5, 32, 33))
                self.assertEqual(target.read_bytes(), b'previous complete index')
                timer = threading.Timer(0.06, release)
                timer.start()
                try:
                    atomic_bytes(target, b'new complete index')
                finally:
                    timer.join()
            self.assertEqual(target.read_bytes(), b'new complete index')

    def test_persistent_denial_preserves_old_contents_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'index.kv'
            target.write_bytes(b'old complete data')
            with patch('retry_io.os.replace', side_effect=PermissionError('locked')):
                with self.assertRaises(PermissionError):
                    atomic_bytes(target, b'new data')
            self.assertEqual(target.read_bytes(), b'old complete data')
            self.assertEqual([p.name for p in Path(temp).iterdir()], ['index.kv'])


class MailboxLockTests(unittest.TestCase):
    def test_index_retried_without_a_second_http_request(self):
        with tempfile.TemporaryDirectory() as temp:
            mailbox = Mailbox(Path(temp), 'a' * 32)
            dispatcher = Dispatcher(mailbox, DestinationPolicy())
            write = mailbox.write

            def block_index(path, data):
                if path.name == 'index.kv':
                    raise PermissionError('reader owns index')
                write(path, data)

            try:
                dispatcher.finished.put(('q123_1', Response(200, b'completed HTTP result')))
                with patch.object(mailbox, 'write', side_effect=block_index), patch('bridge.fetch') as fetch:
                    dispatcher.drain()
                    self.assertTrue(mailbox.index_dirty)
                    self.assertIn('q123_1', mailbox.ready)
                    self.assertNotIn('q123_1', read_keyvalues(mailbox.folder / 'index.kv')['done'])
                    fetch.assert_not_called()
                dispatcher.drain()  # No new HTTP completion is needed to retry the index.
                self.assertFalse(mailbox.index_dirty)
                self.assertEqual(read_keyvalues(mailbox.folder / 'index.kv')['done']['q123_1'], '1')
            finally:
                dispatcher.close()

    def test_reply_kept_until_write_succeeds_and_cleanup_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            mailbox = Mailbox(Path(temp), 'b' * 32)
            write = mailbox.write
            response = Response(200, bytes(range(256)) * 140)

            def block_record(path, data):
                if path.name == 'q123_2.kv':
                    raise PermissionError('record locked')
                write(path, data)

            with patch.object(mailbox, 'write', side_effect=block_record):
                mailbox.publish('q123_2', response)
                self.assertIs(mailbox.pending['q123_2'], response)
                self.assertNotIn('q123_2', mailbox.ready)
            mailbox.flush()
            self.assertFalse(mailbox.pending)
            self.assertEqual(read_keyvalues(mailbox.folder / 'q123_2.kv')['code'], '200')
            with patch('bridge.unlink_file', side_effect=PermissionError('cleanup locked')):
                mailbox.acknowledge('q123_2')
                self.assertTrue(mailbox.cleanup)
            mailbox.flush()
            self.assertFalse(mailbox.cleanup)
            self.assertFalse(mailbox.ready)
            self.assertEqual([p.name for p in mailbox.folder.iterdir()], ['index.kv'])

    def test_timeout_ack_cancels_a_deferred_reply(self):
        with tempfile.TemporaryDirectory() as temp:
            mailbox = Mailbox(Path(temp), 'c' * 32)
            write = mailbox.write

            def block_record(path, data):
                if path.name == 'q123_3.kv':
                    raise PermissionError('record locked')
                write(path, data)

            with patch.object(mailbox, 'write', side_effect=block_record):
                mailbox.publish('q123_3', Response(200, b'already timed out'))
            mailbox.acknowledge('q123_3')
            mailbox.flush()
            self.assertFalse(mailbox.pending)
            self.assertFalse(mailbox.ready)
            self.assertEqual([p.name for p in mailbox.folder.iterdir()], ['index.kv'])


if __name__ == '__main__':
    unittest.main()
