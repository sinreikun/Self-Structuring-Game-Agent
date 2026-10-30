"""Versioned layout registry; unknown UI IDs and malformed entries are harmless."""
import json
import os
from pathlib import Path

from .config import write_json


def displays(window):
    if os.name=='nt':
        import ctypes as c
        from ctypes import wintypes as w
        result=[]
        callback=c.WINFUNCTYPE(w.BOOL,w.HANDLE,w.HDC,c.POINTER(w.RECT),w.LPARAM)
        def visit(monitor,dc,rect,data):
            r=rect.contents;result.append((r.left,r.top,r.right-r.left,r.bottom-r.top));return True
        c.windll.user32.EnumDisplayMonitors(None,None,callback(visit),0)
        if result:return result
    return [(0,0,window.winfo_screenwidth(),window.winfo_screenheight())]


def fit_geometry(value, screens, default=(1150,800)):
    try:
        width,height,x,y=(value[k] for k in ('width','height','x','y'))
        if any(type(n)!=int or abs(n)>100000 for n in (width,height,x,y)): raise ValueError()
        if width<300 or height<200:raise ValueError()
    except (KeyError,ValueError,TypeError): width,height=default;x,y=screens[0][:2]
    # Pick the display with greatest intersection, or the primary if detached.
    def overlap(r):
        a,b,w,h=r;return max(0,min(x+width,a+w)-max(x,a))*max(0,min(y+height,b+h)-max(y,b))
    screen=max(screens,key=overlap)
    if not overlap(screen):screen=screens[0]
    a,b,w,h=screen
    width=min(width,w);height=min(height,h)
    x=max(a,min(x,a+w-width));y=max(b,min(y,b+h-height))
    return width,height,x,y


class Layout:
    def __init__(self,path: Path):
        self.path=path;self.widgets={};self.entries={}
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if data.get('schema')==1 and isinstance(data.get('elements'),dict):self.entries=data['elements']
        except (OSError,ValueError,AttributeError):pass

    def window(self,id,widget):
        self.widgets[id]=('window',widget)
        def remember(event):
            if event.widget==widget and widget.state()=='normal':
                self.entries[id]={'width':widget.winfo_width(),'height':widget.winfo_height(),'x':widget.winfo_x(),'y':widget.winfo_y()}
        widget.bind('<Configure>',remember,add='+')
        entry=self.entries.get(id,{})
        width,height,x,y=fit_geometry(entry,displays(widget))
        widget.minsize(min(900,width),min(650,height))
        widget.geometry(f'{width}x{height}{x:+d}{y:+d}')
        if isinstance(entry,dict) and entry.get('maximized'):
            try:widget.state('zoomed')
            except Exception:pass

    def pane(self,id,widget):
        self.widgets[id]=('pane',widget)
        def restore():
            entry=self.entries.get(id,{})
            if not isinstance(entry,dict):return
            positions=entry.get('sashes',[])
            if not isinstance(positions,list):return
            extent=widget.winfo_height() if str(widget.cget('orient'))=='vertical' else widget.winfo_width()
            if not positions and len(widget.panes())==2:positions=[max(80,extent-180)]
            for i,value in enumerate(positions[:max(0,len(widget.panes())-1)]):
                if type(value)==int:
                    try:widget.sashpos(i,max(50,min(value,max(50,extent-50))))
                    except Exception:pass
        widget.after(150,restore)

    def notebook(self,id,widget):
        self.widgets[id]=('notebook',widget)
        entry=self.entries.get(id,{})
        if isinstance(entry,dict) and type(entry.get('selected'))==int and 0<=entry['selected']<len(widget.tabs()):widget.select(entry['selected'])

    def save(self):
        entries={}
        for id,(kind,w) in self.widgets.items():
            try:
                if kind=='window':
                    previous=self.entries.get(id,{})
                    if w.state()=='zoomed' and isinstance(previous,dict) and previous:
                        entries[id]={**previous,'maximized':True}
                    else:entries[id]=dict(width=w.winfo_width(),height=w.winfo_height(),x=w.winfo_x(),y=w.winfo_y(),maximized=w.state()=='zoomed',visible=w.state()!='withdrawn')
                elif kind=='pane': entries[id]={'sashes':[w.sashpos(i) for i in range(len(w.panes())-1)]}
                elif kind=='panel':entries[id]={'visible':w.layout_visible,'collapsed':w.layout_collapsed}
                else:entries[id]={'selected':w.index(w.select())}
            except Exception:continue
        write_json(self.path,{'schema':1,'elements':entries})

    def panel(self,id,widget,default_visible=True):
        """Register future floating/collapsible panels without depending on their class."""
        entry=self.entries.get(id,{})
        if not isinstance(entry,dict):entry={}
        visible=entry.get('visible',default_visible)
        collapsed=entry.get('collapsed',False)
        widget.layout_visible=visible if type(visible)==bool else default_visible
        widget.layout_collapsed=collapsed if type(collapsed)==bool else False
        self.widgets[id]=('panel',widget)
        return widget.layout_visible,widget.layout_collapsed
