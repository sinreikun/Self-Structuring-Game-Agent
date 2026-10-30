"""Lifetime-bound Windows job for our application tree; never adopts external apps."""
import ctypes as c
from ctypes import wintypes as w
import os
import subprocess
import time


class OwnedProcess:
    def __init__(self,args,env,retain_job=False):
        self.retain_job=retain_job
        self.api=c.WinDLL('kernel32',use_last_error=True)
        class Basic(c.Structure):
            _fields_=[('process_time',c.c_longlong),('job_time',c.c_longlong),('flags',w.DWORD),
                ('minimum',c.c_size_t),('maximum',c.c_size_t),('active',w.DWORD),
                ('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(c.Structure):
            _fields_=[(name,c.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Limits(c.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',c.c_size_t),('job_memory',c.c_size_t),
                     ('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
        class Accounting(c.Structure):
            _fields_=[(n,c.c_longlong) for n in ('user','kernel','period_user','period_kernel')]+[(n,w.DWORD) for n in ('faults','total','active','terminated')]
        self.Limits,self.Accounting=Limits,Accounting
        for name,arguments,result in [
            ('CreateJobObjectW',[c.c_void_p,w.LPCWSTR],w.HANDLE),
            ('SetInformationJobObject',[w.HANDLE,c.c_int,c.c_void_p,w.DWORD],w.BOOL),
            ('AssignProcessToJobObject',[w.HANDLE,w.HANDLE],w.BOOL),
            ('TerminateJobObject',[w.HANDLE,w.UINT],w.BOOL),
            ('QueryInformationJobObject',[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p],w.BOOL),
            ('CloseHandle',[w.HANDLE],w.BOOL),
        ]:
            function=getattr(self.api,name);function.argtypes=arguments;function.restype=result
        self.job=self.api.CreateJobObjectW(None,None)
        if not self.job:raise OSError('Application containment unavailable')
        limits=Limits();limits.basic.flags=0x2000  # KILL_ON_JOB_CLOSE until health acknowledgment.
        if not self.api.SetInformationJobObject(self.job,9,c.byref(limits),c.sizeof(limits)):
            self.api.CloseHandle(self.job);self.job=None;raise OSError('Application containment unavailable')
        try:
            self.child=subprocess.Popen(args,env=env)
            if not self.api.AssignProcessToJobObject(self.job,w.HANDLE(int(self.child._handle))):
                self.child.terminate();self.child.wait(timeout=10)
                raise OSError('Could not contain application process')
        except BaseException:
            self.api.CloseHandle(self.job);self.job=None;raise
        self.pid=self.child.pid

    def poll(self):return self.child.poll()
    def wait(self,timeout=10):return self.child.wait(timeout=timeout)

    def detach(self):
        if self.retain_job:return
        limits=self.Limits()
        if not self.api.SetInformationJobObject(self.job,9,c.byref(limits),c.sizeof(limits)):
            raise OSError('Could not detach healthy application')
        self.api.CloseHandle(self.job);self.job=None

    def terminate_tree(self):
        if not self.job:return
        if not self.api.TerminateJobObject(self.job,1):raise OSError('Could not stop application tree')
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            accounting=self.Accounting()
            if not self.api.QueryInformationJobObject(self.job,1,c.byref(accounting),c.sizeof(accounting),None):raise OSError('Could not inspect application tree')
            if not accounting.active:
                self.child.wait(timeout=10)
                self.api.CloseHandle(self.job);self.job=None;return
            time.sleep(.05)
        raise TimeoutError('Application tree did not stop')


def start_application(args,env,retain_job=False):
    return OwnedProcess(args,env,retain_job) if os.name=='nt' else subprocess.Popen(args,env=env)
