"""Release Tcl variables on the UI thread before worker-triggered collection."""
import tkinter as tk


def bind_cleanup(owner,window):
    def cleanup(event):
        if event.widget!=window or getattr(owner,'_gui_released',False):return
        owner._gui_released=True
        owner.closing=True
        owner.closed=True
        monitor=getattr(owner,'performance_monitor',None)
        if monitor:monitor.stop.set()
        if isinstance(window,tk.Tk):
            try:
                for timer in window.tk.call('after','info'):window.after_cancel(timer)
            except tk.TclError:pass
        else:
            timer=getattr(owner,'poll_id',None)
            if timer:
                try:window.after_cancel(timer)
                except tk.TclError:pass
        if hasattr(owner,'photo'):owner.photo=None
        for name,value in list(vars(owner).items()):
            if isinstance(value,tk.Variable):setattr(owner,name,None)
            elif isinstance(value,dict):
                for key,item in list(value.items()):
                    if isinstance(item,tk.Variable):del value[key]
    window.bind('<Destroy>',cleanup,add='+')
