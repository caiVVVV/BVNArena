"""Render only the client belonging to the bound process into a private bitmap."""
import ctypes as C
from ctypes import wintypes as W
import os
import struct

from PIL import Image


def _declare(dll, name, result, *arguments):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, arguments
    return fn


class WindowCapture:
    def __init__(self, pid, hwnd=None):
        if os.name != "nt" or pid <= 0:
            raise OSError("需要有效的 Windows 游戏进程")
        self.pid, self.requested, self.hwnd = pid, hwnd, None
        user = C.WinDLL("user32", use_last_error=True)
        gdi = C.WinDLL("gdi32", use_last_error=True)
        self.rect = _declare(user, "GetClientRect", W.BOOL, W.HWND, C.POINTER(W.RECT))
        self.window_pid = _declare(user, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
        self.visible = _declare(user, "IsWindowVisible", W.BOOL, W.HWND)
        self.minimized = _declare(user, "IsIconic", W.BOOL, W.HWND)
        self.enum_callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self.enum = _declare(user, "EnumWindows", W.BOOL, self.enum_callback, W.LPARAM)
        self.print_client = _declare(user, "PrintWindow", W.BOOL, W.HWND, W.HDC, W.UINT)
        self.create_dc = _declare(gdi, "CreateCompatibleDC", W.HDC, W.HDC)
        self.create_bitmap = _declare(gdi, "CreateDIBSection", W.HBITMAP, W.HDC, W.LPVOID, W.UINT, C.POINTER(W.LPVOID), W.HANDLE, W.DWORD)
        self.select = _declare(gdi, "SelectObject", W.HANDLE, W.HDC, W.HANDLE)
        self.delete = _declare(gdi, "DeleteObject", W.BOOL, W.HANDLE)
        self.delete_dc = _declare(gdi, "DeleteDC", W.BOOL, W.HDC)
        self.flush = _declare(gdi, "GdiFlush", W.BOOL)
        self.dpi = _declare(user, "SetThreadDpiAwarenessContext", W.HANDLE, W.HANDLE)
        self.dc = self.bitmap = self.old = None
        self.size = (0, 0)

    def _belongs(self, hwnd):
        owner = W.DWORD()
        return hwnd and self.window_pid(hwnd, C.byref(owner)) and owner.value == self.pid

    def find(self):
        if self.requested:
            return self.requested if self._belongs(self.requested) else None
        if self._belongs(self.hwnd):
            return self.hwnd
        choices = []

        @self.enum_callback
        def collect(hwnd, _):
            if self._belongs(hwnd) and self.visible(hwnd):
                area = W.RECT()
                if self.rect(hwnd, C.byref(area)) and area.right > 0 and area.bottom > 0:
                    choices.append((area.right * area.bottom, hwnd))
            return True

        self.enum(collect, 0)
        self.hwnd = max(choices)[1] if choices else None
        return self.hwnd

    def close(self):
        if self.dc:
            if self.old:
                self.select(self.dc, self.old)
            if self.bitmap:
                self.delete(self.bitmap)
            self.delete_dc(self.dc)
        self.dc = self.bitmap = self.old = None
        self.size = (0, 0)

    def grab(self):
        hwnd = self.find()
        if not hwnd or self.minimized(hwnd):
            raise OSError("未找到游戏窗口或窗口已最小化")
        previous = self.dpi(C.c_void_p(-4))
        try:
            area = W.RECT()
            if not self.rect(hwnd, C.byref(area)):
                raise OSError("读取游戏客户区失败")
            w, h = area.right, area.bottom
            if not (1 <= w <= 8192 and 1 <= h <= 8192 and w * h <= 16777216):
                raise OSError("游戏客户区尺寸无效")
            if self.size != (w, h):
                self.close()
                self.dc = self.create_dc(None)
                if not self.dc:
                    raise OSError("创建窗口采样缓冲失败")
                header = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
                self.pixels = C.c_void_p()
                self.bitmap = self.create_bitmap(self.dc, header, 0, C.byref(self.pixels), None, 0)
                if not self.bitmap:
                    self.close()
                    raise OSError("创建窗口位图失败")
                self.old = self.select(self.dc, self.bitmap)
                self.size = w, h
            if not self.print_client(hwnd, self.dc, 3):
                raise OSError("游戏窗口未返回画面")
            self.flush()
            return Image.frombytes("RGB", (w, h), C.string_at(self.pixels, 4*w*h), "raw", "BGRX")
        finally:
            if previous:
                self.dpi(previous)
