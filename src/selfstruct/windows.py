"""Windows-only, PID-scoped experimental I/O. No game semantics."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import subprocess
import threading
import time

from PIL import Image

KEY_CODES = {**{chr(k): k for k in range(65, 91)}, **{str(k): 48 + k for k in range(10)},
             "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27, "ENTER": 0x0D,
             "SPACE": 0x20, "ESC": 0x1B, "TAB": 0x09,
             **{f"F{k}": 0x6F + k for k in range(1, 12)}}


class GameUnavailable(RuntimeError):
    pass


class FocusLost(GameUnavailable):
    pass


def parse_arguments(text: str) -> list[str]:
    if not text.strip():
        return []
    if os.name != "nt":
        import shlex
        return shlex.split(text)
    shell = C.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [W.LPCWSTR, C.POINTER(C.c_int)]
    shell.CommandLineToArgvW.restype = C.POINTER(W.LPWSTR)
    count = C.c_int()
    values = shell.CommandLineToArgvW("game " + text, C.byref(count))
    if not values:
        raise ValueError("起動オプションを読み取れません。")
    try:
        return [values[index] for index in range(1, count.value)]
    finally:
        kernel = C.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [C.c_void_p]
        kernel.LocalFree(values)


class WindowsGame:
    def __init__(self, exe: str, args: list[str], title: str, keys: list[str]):
        if os.name != "nt":
            raise OSError("ゲームの画面操作はWindowsで利用できます。")
        self.exe, self.args, self.title = exe, args, title
        self.keys = set(keys)
        self.process: subprocess.Popen | None = None
        self.hwnd = None
        self.generation = 0
        self.held: set[int] = set()
        self.release_at = 0.0
        self.lock = threading.RLock()
        self.user = C.WinDLL("user32", use_last_error=True)
        self.gdi = C.WinDLL("gdi32", use_last_error=True)
        self._declare()
        self.user.SetProcessDPIAware()

    def _declare(self) -> None:
        u, g = self.user, self.gdi
        self.enum_callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        definitions = [
            (u.EnumWindows, [self.enum_callback, W.LPARAM], W.BOOL),
            (u.GetWindowThreadProcessId, [W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            (u.IsWindowVisible, [W.HWND], W.BOOL), (u.IsWindow, [W.HWND], W.BOOL),
            (u.IsIconic, [W.HWND], W.BOOL), (u.GetForegroundWindow, [], W.HWND),
            (u.GetWindowTextW, [W.HWND, W.LPWSTR, C.c_int], C.c_int),
            (u.SetForegroundWindow, [W.HWND], W.BOOL),
            (u.GetClientRect, [W.HWND, C.POINTER(W.RECT)], W.BOOL),
            (u.GetDC, [W.HWND], W.HDC), (u.ReleaseDC, [W.HWND, W.HDC], C.c_int),
            (u.PrintWindow, [W.HWND, W.HDC, W.UINT], W.BOOL),
            (g.CreateCompatibleDC, [W.HDC], W.HDC),
            (g.CreateCompatibleBitmap, [W.HDC, C.c_int, C.c_int], W.HBITMAP),
            (g.SelectObject, [W.HDC, W.HANDLE], W.HANDLE),
            (g.BitBlt, [W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.HDC, C.c_int, C.c_int, W.DWORD], W.BOOL),
            (g.DeleteObject, [W.HANDLE], W.BOOL), (g.DeleteDC, [W.HDC], W.BOOL),
            (g.GetDIBits, [W.HDC, W.HBITMAP, W.UINT, W.UINT, C.c_void_p, C.c_void_p, W.UINT], C.c_int),
        ]
        for func, args, result in definitions:
            func.argtypes, func.restype = args, result

    def start(self) -> None:
        if self.process is None or self.process.poll() is not None:
            self.process = subprocess.Popen([self.exe, *self.args], cwd=str(Path(self.exe).parent), shell=False)
            self.generation += 1
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                self.connect()
                self.user.SetForegroundWindow(self.hwnd)
                return
            except GameUnavailable:
                if self.process.poll() is not None:
                    raise GameUnavailable("対象ゲームが終了しました。実行ファイルを確認してください。")
                time.sleep(0.2)
        raise GameUnavailable("対象ゲームの画面が見つかりません。ウィンドウ名を確認してください。")

    def connect(self) -> None:
        if not self.process or self.process.poll() is not None:
            raise GameUnavailable("対象ゲームが終了しています。")
        found = []

        @self.enum_callback
        def visit(hwnd, _):
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, C.byref(pid))
            if pid.value == self.process.pid and self.user.IsWindowVisible(hwnd):
                text = C.create_unicode_buffer(1024)
                self.user.GetWindowTextW(hwnd, text, 1024)
                if self.title.casefold() in text.value.casefold():
                    found.append(hwnd)
            return True

        self.user.EnumWindows(visit, 0)
        if not found:
            raise GameUnavailable("対象ゲームの画面を再検出しています。")
        self.hwnd = found[0]

    def check(self, foreground: bool = True) -> None:
        if not self.process or self.process.poll() is not None or not self.hwnd or not self.user.IsWindow(self.hwnd):
            raise GameUnavailable("対象ゲームとの接続が失われました。")
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(self.hwnd, C.byref(pid))
        if pid.value != self.process.pid:
            raise GameUnavailable("対象ウィンドウの所有者が変わりました。")
        if foreground and (self.user.GetForegroundWindow() != self.hwnd or self.user.IsIconic(self.hwnd)):
            raise FocusLost("対象ゲームが前面にありません。入力を解放して待機しています。")

    def _send(self, vk: int, up: bool) -> None:
        # sizeof(INPUT) must include MOUSEINPUT (40 bytes on x64), even for keyboards.
        class Mouse(C.Structure):
            _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("data", W.DWORD), ("flags", W.DWORD),
                        ("time", W.DWORD), ("extra", C.c_size_t)]
        class Keyboard(C.Structure):
            _fields_ = [("vk", W.WORD), ("scan", W.WORD), ("flags", W.DWORD), ("time", W.DWORD), ("extra", C.c_size_t)]
        class Payload(C.Union):
            _fields_ = [("mouse", Mouse), ("key", Keyboard)]
        class Input(C.Structure):
            _fields_ = [("type", W.DWORD), ("payload", Payload)]
        extended = 1 if vk in {0x25, 0x26, 0x27, 0x28} else 0
        inp = Input(type=1, payload=Payload(key=Keyboard(vk=vk, flags=extended | (2 if up else 0))))
        self.user.SendInput.argtypes = [W.UINT, C.POINTER(Input), C.c_int]
        self.user.SendInput.restype = W.UINT
        if self.user.SendInput(1, C.byref(inp), C.sizeof(inp)) != 1:
            raise GameUnavailable("入力を送信できません。ゲームとアプリの権限を確認してください。")

    def release(self) -> None:
        with self.lock:
            error = None
            for vk in tuple(self.held):
                try:
                    self._send(vk, True)
                except Exception as exc:
                    error = exc
                else:
                    self.held.discard(vk)
            if error:
                raise error

    def release_due(self):
        if self.held and time.monotonic() >= self.release_at:
            self.release()

    def act(self, key: str, seconds: float, cancel: threading.Event, on_sent=None) -> None:
        if key not in self.keys or not 0.01 <= seconds <= 0.5:
            raise ValueError("許可されていない入力です。")
        try:
            with self.lock:
                if cancel.is_set():
                    return
                self.check()
                vk = KEY_CODES[key]
                self.held.add(vk)
                self.release_at = time.monotonic() + seconds
                self._send(vk, False)
            if on_sent:
                on_sent(time.time())
            end = self.release_at
            while time.monotonic() < end and not cancel.wait(0.01):
                self.check()
        finally:
            self.release()

    def capture(self) -> Image.Image:
        self.check()
        rect = W.RECT()
        if not self.user.GetClientRect(self.hwnd, C.byref(rect)):
            raise GameUnavailable("画面の大きさを取得できません。")
        width, height = rect.right, rect.bottom
        if not 1 <= width <= 8192 or not 1 <= height <= 8192:
            raise GameUnavailable("画面が最小化されているか大きすぎます。")
        dc = self.user.GetDC(self.hwnd)
        mem = self.gdi.CreateCompatibleDC(dc)
        bmp = self.gdi.CreateCompatibleBitmap(dc, width, height)
        old = self.gdi.SelectObject(mem, bmp)
        try:
            # Visible client capture does not send a blocking WM_PRINT to the game.
            if not dc or not mem or not bmp or not self.gdi.BitBlt(mem, 0, 0, width, height, dc, 0, 0, 0x00CC0020 | 0x40000000):
                raise GameUnavailable("このゲームの画面取得方式に対応していません。")
            class Header(C.Structure):
                _fields_ = [("size", W.DWORD), ("width", W.LONG), ("height", W.LONG), ("planes", W.WORD),
                            ("bits", W.WORD), ("compression", W.DWORD), ("image_size", W.DWORD),
                            ("x", W.LONG), ("y", W.LONG), ("used", W.DWORD), ("important", W.DWORD)]
            info = Header(size=40, width=width, height=-height, planes=1, bits=32)
            buffer = C.create_string_buffer(width * height * 4)
            # GetDIBits requires the bitmap to be deselected from the drawing DC.
            self.gdi.SelectObject(mem, old)
            if self.gdi.GetDIBits(mem, bmp, 0, height, buffer, C.byref(info), 0) != height:
                raise GameUnavailable("画面の読み取りに失敗しました。")
            self.check()
            return Image.frombytes("RGB", (width, height), buffer.raw, "raw", "BGRX")
        finally:
            self.gdi.SelectObject(mem, old)
            self.gdi.DeleteObject(bmp)
            self.gdi.DeleteDC(mem)
            self.user.ReleaseDC(self.hwnd, dc)

    def restart(self) -> None:
        self.stop()
        self.start()

    def stop(self) -> None:
        self.release()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.hwnd = None
