"""Windows kernel mutex scoped to the canonical data directory."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path


class SingleInstance:
    def __init__(self, directory):
        canonical = os.path.normcase(str(Path(directory).resolve()))
        digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        self.name = 'Local\\STT.Data.' + digest
        self.handle = None
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def acquire(self):
        if self.handle is not None:
            return True
        handle = self.kernel.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            self.kernel.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def show_existing_instance():
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user.MessageBoxW.restype = ctypes.c_int
    user.MessageBoxW(None, '这个数据目录的 STT 已在运行，请使用已有窗口。', 'STT 已在运行', 0x40)
