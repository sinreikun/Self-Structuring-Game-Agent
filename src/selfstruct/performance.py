"""Low frequency host metrics, sampled off the Tk and Agent threads."""
import shutil
import subprocess
import threading
from pathlib import Path


class PerformanceMonitor:
    def __init__(self,root:Path):
        self.root=root;self.stop=threading.Event();self.value='PC負荷を取得中…';self.hardware={}

    def start(self):
        threading.Thread(target=self.run,name='pc-metrics',daemon=True).start()

    def text(self):return self.value

    def run(self):
        import psutil
        adapters=windows_adapters()
        psutil.cpu_percent()
        while not self.stop.wait(2):
            try:
                memory=psutil.virtual_memory();disk=shutil.disk_usage(self.root if self.root.exists() else self.root.parent)
                gpu='GPU 使用率・VRAM 未取得'
                devices=[]
                try:
                    result=subprocess.run(['nvidia-smi','--query-gpu=name,utilization.gpu,memory.used,memory.total','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=2,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    for line in result.stdout.splitlines()[:8]:
                        name,usage,used,total=[v.strip() for v in line.rsplit(',',3)]
                        devices.append({'name':name,'vram_gb':float(total)/1024})
                        gpu=f'{name} {usage}% / VRAM {float(used)/1024:.1f}/{float(total)/1024:.1f}GB'
                except (OSError,ValueError,subprocess.TimeoutExpired):pass
                if not devices and adapters:
                    devices=adapters
                    gpu=' / '.join(f'{g["name"]} VRAM {g["vram_gb"]:.1f}GB' for g in devices)+'（使用率 未取得）'
                self.hardware={'ram':memory.available/1024**3,'total_ram':memory.total/1024**3,'free':disk.free/1024**3,'gpus':devices}
                self.value=f'PC CPU {psutil.cpu_percent():.0f}%  RAM {memory.used/1024**3:.1f}/{memory.total/1024**3:.1f}GB\n{gpu}\nディスク空き {disk.free/1024**3:.1f}GB'
            except OSError:self.value='PC負荷を取得できませんでした'


def windows_adapters():
    """DXGI dedicated memory, including AMD/Intel; no WMI 32-bit VRAM truncation."""
    import os
    if os.name!='nt':return []
    import ctypes as c
    from ctypes import wintypes as w
    class GUID(c.Structure):
        _fields_=[('a',w.DWORD),('b',w.WORD),('d',w.WORD),('e',c.c_ubyte*8)]
    class LUID(c.Structure):_fields_=[('low',w.DWORD),('high',w.LONG)]
    class Description(c.Structure):
        _fields_=[('name',w.WCHAR*128),('vendor',w.UINT),('device',w.UINT),('subsystem',w.UINT),('revision',w.UINT),('dedicated',c.c_size_t),('system',c.c_size_t),('shared',c.c_size_t),('luid',LUID),('flags',w.UINT)]
    def method(ptr,index,result,*args):
        table=c.cast(ptr,c.POINTER(c.POINTER(c.c_void_p))).contents
        return c.WINFUNCTYPE(result,c.c_void_p,*args)(table[index])
    result=[];factory=c.c_void_p()
    iid=GUID(0x770aae78,0xf26f,0x4dba,(c.c_ubyte*8)(0xa8,0x29,0x25,0x3c,0x83,0xd1,0xb3,0x87))
    try:
        create=c.WinDLL('dxgi').CreateDXGIFactory1
        create.argtypes=[c.POINTER(GUID),c.POINTER(c.c_void_p)];create.restype=w.LONG
        if create(c.byref(iid),c.byref(factory))<0:return []
        for index in range(16):
            adapter=c.c_void_p()
            if method(factory,12,w.LONG,w.UINT,c.POINTER(c.c_void_p))(factory,index,c.byref(adapter))<0:break
            try:
                desc=Description()
                if method(adapter,10,w.LONG,c.POINTER(Description))(adapter,c.byref(desc))>=0 and not desc.flags&2:
                    result.append({'name':desc.name,'vram_gb':desc.dedicated/1024**3})
            finally:method(adapter,2,w.ULONG)(adapter)
    except (OSError,ValueError):pass
    finally:
        if factory.value:method(factory,2,w.ULONG)(factory)
    return result
