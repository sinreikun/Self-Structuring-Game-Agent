"""Windows-only, PID-scoped experimental I/O. No game semantics."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import multiprocessing
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


class WindowMinimized(GameUnavailable):
    pass


def _capture_worker(connection):
    """PrintWindow may block inside a game's renderer, so contain it in a process."""
    api = object.__new__(WindowsGame)
    api.user = C.WinDLL('user32', use_last_error=True)
    api.gdi = C.WinDLL('gdi32', use_last_error=True)
    api._declare()
    api.user.SetProcessDPIAware()
    try:
        while True:
            hwnd, pid = connection.recv()
            try:
                image = api._render(hwnd,pid)
                connection.send(('image',image.size,image.tobytes()))
            except Exception as error:
                connection.send(('error',str(error)))
    except (EOFError,OSError):
        pass
    finally:
        connection.close()


class CaptureProcess:
    def __init__(self):
        self.process = None
        self.connection = None
        self.timeout = 2.0

    def capture(self, hwnd: int, pid: int) -> Image.Image:
        starting = self.process is None or not self.process.is_alive()
        if starting:
            self.close()
            context = multiprocessing.get_context('spawn')
            self.connection, child = context.Pipe()
            self.process = context.Process(target=_capture_worker,args=(child,),daemon=True)
            self.process.start()
            child.close()
        try:
            self.connection.send((hwnd,pid))
            if not self.connection.poll(max(5.0,self.timeout) if starting else self.timeout):
                raise TimeoutError('対象ゲームの画面取得が応答しません。')
            result = self.connection.recv()
            if result[0]!='image':
                raise GameUnavailable(result[1])
            return Image.frombytes('RGB',result[1],result[2])
        except (OSError,EOFError,TimeoutError) as error:
            self.close()
            raise GameUnavailable('対象ゲームの画面取得が応答しません。') from error

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=3)
            self.process.close()
            self.process = None


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
        self.last_input_target = None
        self.generation = 0
        self.held: set[int] = set()
        self.held_targets: dict[int,int] = {}
        self.capturer = CaptureProcess()
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
            (u.IsChild, [W.HWND,W.HWND], W.BOOL),
            (u.GetWindow, [W.HWND,W.UINT], W.HWND),
            (u.GetGUIThreadInfo, [W.DWORD,C.c_void_p], W.BOOL),
            (u.PostMessageW, [W.HWND,W.UINT,W.WPARAM,W.LPARAM], W.BOOL),
            (u.MapVirtualKeyW, [W.UINT,W.UINT], W.UINT),
            (u.GetClientRect, [W.HWND, C.POINTER(W.RECT)], W.BOOL),
            (u.GetDC, [W.HWND], W.HDC), (u.ReleaseDC, [W.HWND, W.HDC], C.c_int),
            (u.PrintWindow, [W.HWND, W.HDC, W.UINT], W.BOOL),
            (g.CreateCompatibleDC, [W.HDC], W.HDC),
            (g.CreateCompatibleBitmap, [W.HDC, C.c_int, C.c_int], W.HBITMAP),
            (g.SelectObject, [W.HDC, W.HANDLE], W.HANDLE),
            (g.DeleteObject, [W.HANDLE], W.BOOL), (g.DeleteDC, [W.HDC], W.BOOL),
            (g.GetDIBits, [W.HDC, W.HBITMAP, W.UINT, W.UINT, C.c_void_p, C.c_void_p, W.UINT], C.c_int),
        ]
        for func, args, result in definitions:
            func.argtypes, func.restype = args, result

    def start(self) -> None:
        if self.process is None or self.process.poll() is not None:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 4  # SW_SHOWNOACTIVATE: do not request foreground focus.
            self.process = subprocess.Popen([self.exe, *self.args], cwd=str(Path(self.exe).parent), shell=False,
                                            startupinfo=startup)
            self.generation += 1
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                self.connect()
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

    def check(self) -> None:
        if not self.process or self.process.poll() is not None or not self.hwnd or not self.user.IsWindow(self.hwnd):
            raise GameUnavailable("対象ゲームとの接続が失われました。")
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(self.hwnd, C.byref(pid))
        if pid.value != self.process.pid:
            raise GameUnavailable("対象ウィンドウの所有者が変わりました。")
        if self.user.IsIconic(self.hwnd):
            raise WindowMinimized('対象ゲームが最小化されています。元の大きさに戻すと自動で継続します。')

    def _owns(self, hwnd) -> bool:
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(hwnd,C.byref(pid))
        return bool(self.process and pid.value==self.process.pid and self.user.IsWindow(hwnd))

    def _input_target(self):
        class ThreadInfo(C.Structure):
            _fields_ = [('size',W.DWORD),('flags',W.DWORD),('active',W.HWND),('focus',W.HWND),
                        ('capture',W.HWND),('menu',W.HWND),('move',W.HWND),('caret',W.HWND),('rect',W.RECT)]
        info = ThreadInfo(size=C.sizeof(ThreadInfo))
        thread = self.user.GetWindowThreadProcessId(self.hwnd,None)
        if self.user.GetGUIThreadInfo(thread,C.byref(info)) and info.focus and self._owns(info.focus):
            if info.focus==self.hwnd or self.user.IsChild(self.hwnd,info.focus):
                self.last_input_target = info.focus
                return info.focus
        if self.last_input_target and self._owns(self.last_input_target) and (self.last_input_target==self.hwnd or self.user.IsChild(self.hwnd,self.last_input_target)):
            return self.last_input_target
        target = self.hwnd
        for _ in range(16):
            child = self.user.GetWindow(target,5)  # GW_CHILD, within the target PID only.
            if not child or not self._owns(child):
                break
            target = child
        self.last_input_target = target
        return target

    def _send(self, vk: int, up: bool) -> None:
        target = self.held_targets.get(vk) if up else self._input_target()
        if not target or not self._owns(target):
            if up:
                self.held_targets.pop(vk,None)
                return
            raise GameUnavailable('対象ゲームの入力先が失われました。')
        scan = self.user.MapVirtualKeyW(vk,0) & 0xff
        flags = 1 | (scan<<16) | ((1<<24) if vk in {0x25,0x26,0x27,0x28} else 0)
        if up:
            flags |= (1<<30) | (1<<31)
        if not self.user.PostMessageW(target,0x101 if up else 0x100,vk,flags):
            raise GameUnavailable("入力を送信できません。ゲームとアプリの権限を確認してください。")
        if up:
            self.held_targets.pop(vk,None)
        else:
            self.held_targets[vk] = target

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
        self._input_target()
        image = self.capturer.capture(self.hwnd,self.process.pid)
        self.check()
        return image

    def _render(self, hwnd: int, expected_pid: int) -> Image.Image:
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(hwnd,C.byref(pid))
        if not self.user.IsWindow(hwnd) or pid.value!=expected_pid:
            raise GameUnavailable('対象ウィンドウの所有者が変わりました。')
        rect = W.RECT()
        if not self.user.GetClientRect(hwnd, C.byref(rect)):
            raise GameUnavailable("画面の大きさを取得できません。")
        width, height = rect.right, rect.bottom
        if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width*height>3840*2160:
            raise GameUnavailable("画面が最小化されているか大きすぎます。")
        dc = self.user.GetDC(hwnd)
        mem = self.gdi.CreateCompatibleDC(dc)
        bmp = self.gdi.CreateCompatibleBitmap(dc, width, height)
        old = self.gdi.SelectObject(mem, bmp)
        try:
            # Render the target itself; never substitute visible desktop pixels.
            if not dc or not mem or not bmp or not self.user.PrintWindow(hwnd,mem,3):
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
            self.user.GetWindowThreadProcessId(hwnd,C.byref(pid))
            if pid.value!=expected_pid:
                raise GameUnavailable('対象ウィンドウの所有者が変わりました。')
            return Image.frombytes("RGB", (width, height), buffer.raw, "raw", "BGRX")
        finally:
            self.gdi.SelectObject(mem, old)
            self.gdi.DeleteObject(bmp)
            self.gdi.DeleteDC(mem)
            self.user.ReleaseDC(hwnd, dc)

    def restart(self) -> None:
        self.stop()
        self.start()

    def stop(self) -> None:
        self.release()
        self.capturer.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.hwnd = None
        self.last_input_target = None
