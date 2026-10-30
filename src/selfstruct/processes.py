"""Read-only Windows process/window discovery and lifetime-bound process handles."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
import time


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent: int
    name: str
    exe: str
    created: int


@dataclass(frozen=True)
class ProcessWindow:
    process: ProcessInfo
    hwnd: int
    title: str
    area: int = 0


def _kernel():
    if os.name!='nt':raise OSError('プロセス選択はWindowsで利用できます。')
    api=C.WinDLL('kernel32',use_last_error=True)
    for name,args,result in [
        ('OpenProcess',[W.DWORD,W.BOOL,W.DWORD],W.HANDLE),
        ('CloseHandle',[W.HANDLE],W.BOOL),
        ('GetProcessTimes',[W.HANDLE,*([C.POINTER(W.FILETIME)]*4)],W.BOOL),
        ('QueryFullProcessImageNameW',[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)],W.BOOL),
        ('WaitForSingleObject',[W.HANDLE,W.DWORD],W.DWORD),
        ('GetExitCodeProcess',[W.HANDLE,C.POINTER(W.DWORD)],W.BOOL),
        ('TerminateProcess',[W.HANDLE,W.UINT],W.BOOL),
    ]:
        function=getattr(api,name);function.argtypes,function.restype=args,result
    return api


def _created(api,handle):
    times=[W.FILETIME() for _ in range(4)]
    if not api.GetProcessTimes(handle,*[C.byref(item) for item in times]):return 0
    return (times[0].dwHighDateTime<<32)|times[0].dwLowDateTime


def process_info(pid: int, parent: int=0, name: str='') -> ProcessInfo | None:
    api=_kernel()
    handle=api.OpenProcess(0x1000,False,pid)
    if not handle:return None
    try:
        created=_created(api,handle)
        buffer=C.create_unicode_buffer(32768)
        size=W.DWORD(len(buffer))
        exe=buffer.value if api.QueryFullProcessImageNameW(handle,0,buffer,C.byref(size)) else ''
        return ProcessInfo(pid,parent,name or os.path.basename(exe),exe,created) if created else None
    finally:api.CloseHandle(handle)


def snapshot_processes() -> dict[int,ProcessInfo]:
    api=_kernel()
    class Entry(C.Structure):
        _fields_=[('size',W.DWORD),('usage',W.DWORD),('pid',W.DWORD),('heap',C.c_size_t),
                  ('module',W.DWORD),('threads',W.DWORD),('parent',W.DWORD),('priority',W.LONG),
                  ('flags',W.DWORD),('name',W.WCHAR*260)]
    api.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD]
    api.CreateToolhelp32Snapshot.restype=W.HANDLE
    for name in ('Process32FirstW','Process32NextW'):
        function=getattr(api,name);function.argtypes=[W.HANDLE,C.POINTER(Entry)];function.restype=W.BOOL
    handle=api.CreateToolhelp32Snapshot(2,0)
    if handle in (None,C.c_void_p(-1).value):raise OSError('プロセス一覧を取得できません。')
    result={}
    try:
        entry=Entry(size=C.sizeof(Entry))
        more=api.Process32FirstW(handle,C.byref(entry))
        while more:
            info=process_info(entry.pid,entry.parent,entry.name)
            if info:result[info.pid]=info
            more=api.Process32NextW(handle,C.byref(entry))
    finally:api.CloseHandle(handle)
    return result


def enumerate_windows(processes: dict[int,ProcessInfo] | None=None) -> list[ProcessWindow]:
    processes=snapshot_processes() if processes is None else processes
    user=C.WinDLL('user32',use_last_error=True)
    callback=C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
    for name,args,result in [
        ('EnumWindows',[callback,W.LPARAM],W.BOOL),('IsWindowVisible',[W.HWND],W.BOOL),
        ('GetWindowThreadProcessId',[W.HWND,C.POINTER(W.DWORD)],W.DWORD),
        ('GetWindowTextW',[W.HWND,W.LPWSTR,C.c_int],C.c_int),
        ('GetClientRect',[W.HWND,C.POINTER(W.RECT)],W.BOOL),
    ]:
        function=getattr(user,name);function.argtypes,function.restype=args,result
    windows=[]
    @callback
    def visit(hwnd,_):
        pid=W.DWORD();user.GetWindowThreadProcessId(hwnd,C.byref(pid))
        if pid.value in processes and user.IsWindowVisible(hwnd):
            text=C.create_unicode_buffer(1024);user.GetWindowTextW(hwnd,text,len(text))
            rect=W.RECT();user.GetClientRect(hwnd,C.byref(rect))
            if text.value:
                windows.append(ProcessWindow(processes[pid.value],hwnd,text.value,max(0,rect.right)*max(0,rect.bottom)))
        return True
    user.EnumWindows(visit,0)
    return windows


def discover_targets() -> list[ProcessWindow]:
    processes=snapshot_processes()
    windows=enumerate_windows(processes)
    visible={window.process.pid for window in windows}
    windows.extend(ProcessWindow(info,0,'（ウィンドウ準備中）') for info in processes.values()
                   if info.pid not in visible and info.exe and info.pid!=os.getpid())
    return sorted((item for item in windows if item.process.exe and item.process.pid!=os.getpid()),
                  key=lambda item:(not bool(item.hwnd),item.process.name.casefold(),item.process.pid))


def track_descendants(processes: dict[int,ProcessInfo], known: dict[int,int], launched_after: int) -> None:
    """Remember exited launchers too; never follow children of a recycled PID."""
    changed=True
    while changed:
        changed=False
        for info in processes.values():
            if info.pid in known or info.parent not in known or info.created<launched_after:continue
            parent=processes.get(info.parent)
            if info.created<known[info.parent]:continue
            if parent and parent.created!=known[info.parent] and parent.created<=info.created:continue
            known[info.pid]=info.created;changed=True


class LiveProcess:
    """Hold the exact kernel process object so a recycled PID cannot be adopted."""
    def __init__(self, info: ProcessInfo):
        self.pid,self.created,self.exe=info.pid,info.created,info.exe
        self.api=_kernel()
        self.handle=self.api.OpenProcess(0x1000|0x100000,False,self.pid)
        if not self.handle:raise OSError('選択したプロセスに接続できません。')
        if _created(self.api,self.handle)!=self.created:
            self.close();raise OSError('選択したプロセスは既に別のプロセスへ切り替わっています。')

    def poll(self):
        if not self.handle or self.api.WaitForSingleObject(self.handle,0)==0:
            code=W.DWORD();self.api.GetExitCodeProcess(self.handle,C.byref(code)) if self.handle else None
            return code.value
        return None

    def terminate(self):
        if self.poll() is not None:return
        handle=self.api.OpenProcess(0x1000|1,False,self.pid)
        if not handle:raise OSError('ゲームを終了できません。')
        try:
            if _created(self.api,handle)!=self.created:raise OSError('プロセスの識別情報が変わりました。')
            if not self.api.TerminateProcess(handle,1):raise OSError('ゲームを終了できません。')
        finally:self.api.CloseHandle(handle)

    kill=terminate

    def wait(self,timeout=5):
        if self.handle and self.api.WaitForSingleObject(self.handle,int(timeout*1000))==0x102:
            raise TimeoutError('ゲームの終了待ちが時間切れになりました。')
        return self.poll()

    def close(self):
        if getattr(self,'handle',None):self.api.CloseHandle(self.handle);self.handle=None

    def __del__(self):self.close()
