"""Shared media operations / exclusive cleanup, across threads and processes.

No lock upgrade: post-delivery cleanup runs after releasing the reader guard.
"""
from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import stat
import threading

_local = threading.local()


@contextmanager
def media_guard(data_dir: Path, *, exclusive=False, blocking=True):
    path = data_dir / "result-retention.lock"
    key = str(path.absolute())
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = {}
    if key in held:
        if exclusive and not held[key]:
            yield False
        else:
            yield True
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("Unsafe media lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Unsafe media lock")
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            class OVERLAPPED(ctypes.Structure):
                _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                            ("hEvent", wintypes.HANDLE)]
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            lock = kernel.LockFileEx
            lock.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,
                             wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(OVERLAPPED)]
            lock.restype = wintypes.BOOL
            handle = msvcrt.get_osfhandle(fd)
            overlapped = OVERLAPPED()
            acquired = bool(lock(handle, (2 if exclusive else 0) | (0 if blocking else 1),
                                 0, 1, 0, ctypes.byref(overlapped)))
            if not acquired and (blocking or ctypes.get_last_error() != 33):
                raise RuntimeError("Media lock unavailable")
        else:
            import fcntl
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                            | (0 if blocking else fcntl.LOCK_NB))
                acquired = True
            except BlockingIOError:
                acquired = False
        if acquired:
            held[key] = exclusive
        yield acquired
    finally:
        if acquired:
            held.pop(key, None)
            if os.name == "nt":
                unlock = kernel.UnlockFileEx
                unlock.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,
                                   wintypes.DWORD,ctypes.POINTER(OVERLAPPED)]
                unlock.restype = wintypes.BOOL
                unlock(handle, 0, 1, 0, ctypes.byref(overlapped))
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def media_operation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with media_guard(self.settings.data_dir):
            return method(self, *args, **kwargs)
    return guarded
