"""Native message-compatible test window; no foreground keyboard dependency."""
import ctypes as C
from ctypes import wintypes as W

user=C.WinDLL('user32',use_last_error=True)
gdi=C.WinDLL('gdi32',use_last_error=True)
kernel=C.WinDLL('kernel32',use_last_error=True)
Callback=C.WINFUNCTYPE(C.c_ssize_t,W.HWND,W.UINT,W.WPARAM,W.LPARAM)
class WindowClass(C.Structure):
    _fields_=[('style',W.UINT),('procedure',Callback),('class_extra',C.c_int),('window_extra',C.c_int),
              ('instance',W.HINSTANCE),('icon',W.HICON),('cursor',W.HANDLE),('background',W.HBRUSH),
              ('menu',W.LPCWSTR),('name',W.LPCWSTR)]
class Paint(C.Structure):
    _fields_=[('dc',W.HDC),('erase',W.BOOL),('rect',W.RECT),('restore',W.BOOL),('update',W.BOOL),('reserved',C.c_byte*32)]
for func,args,result in [
    (user.DefWindowProcW,[W.HWND,W.UINT,W.WPARAM,W.LPARAM],C.c_ssize_t),
    (user.RegisterClassW,[C.POINTER(WindowClass)],W.ATOM),
    (user.CreateWindowExW,[W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,C.c_int,C.c_int,C.c_int,C.c_int,W.HWND,W.HMENU,W.HINSTANCE,C.c_void_p],W.HWND),
    (user.GetClientRect,[W.HWND,C.POINTER(W.RECT)],W.BOOL),
    (user.FillRect,[W.HDC,C.POINTER(W.RECT),W.HBRUSH],C.c_int),
    (gdi.CreateSolidBrush,[W.DWORD],W.HBRUSH),
    (gdi.DeleteObject,[W.HANDLE],W.BOOL),
    (user.InvalidateRect,[W.HWND,C.c_void_p,W.BOOL],W.BOOL),
    (user.BeginPaint,[W.HWND,C.POINTER(Paint)],W.HDC),
    (user.EndPaint,[W.HWND,C.POINTER(Paint)],W.BOOL),
    (user.ShowWindow,[W.HWND,C.c_int],W.BOOL),
    (user.UpdateWindow,[W.HWND],W.BOOL),
    (user.DestroyWindow,[W.HWND],W.BOOL),
    (user.GetMessageW,[C.POINTER(W.MSG),W.HWND,W.UINT,W.UINT],W.BOOL),
    (user.TranslateMessage,[C.POINTER(W.MSG)],W.BOOL),
    (user.DispatchMessageW,[C.POINTER(W.MSG)],C.c_ssize_t),
    (kernel.GetModuleHandleW,[W.LPCWSTR],W.HINSTANCE),
]:func.argtypes,func.restype=args,result
counter=0
def draw(hwnd,dc):
    rect=W.RECT()
    user.GetClientRect(hwnd,C.byref(rect))
    brush=gdi.CreateSolidBrush(0x2040c0 if counter%2 else 0x403020)
    try:user.FillRect(dc,C.byref(rect),brush)
    finally:gdi.DeleteObject(brush)
@Callback
def procedure(hwnd,message,wparam,lparam):
    global counter
    if message==0x100:  # WM_KEYDOWN, regardless of OS foreground focus.
        counter+=1
        user.InvalidateRect(hwnd,None,True)
        return 0
    if message==0x318:  # WM_PRINTCLIENT
        draw(hwnd,wparam)
        return 0
    if message==0xF:  # WM_PAINT
        paint=Paint()
        dc=user.BeginPaint(hwnd,C.byref(paint))
        try:draw(hwnd,dc)
        finally:user.EndPaint(hwnd,C.byref(paint))
        return 0
    if message==0x10:
        user.DestroyWindow(hwnd)
        return 0
    if message==2:
        user.PostQuitMessage(0)
        return 0
    return user.DefWindowProcW(hwnd,message,wparam,lparam)

if __name__=='__main__':
    instance=kernel.GetModuleHandleW(None)
    definition=WindowClass(procedure=procedure,instance=instance,name='SelfStructMessageWindow')
    if not user.RegisterClassW(C.byref(definition)):raise C.WinError(C.get_last_error())
    hwnd=user.CreateWindowExW(0,definition.name,'SelfStruct Message Test',0xcf0000,100,100,420,340,None,None,instance,None)
    if not hwnd:raise C.WinError(C.get_last_error())
    user.ShowWindow(hwnd,4)
    user.UpdateWindow(hwnd)
    message=W.MSG()
    while user.GetMessageW(C.byref(message),None,0,0)>0:
        user.TranslateMessage(C.byref(message))
        user.DispatchMessageW(C.byref(message))
