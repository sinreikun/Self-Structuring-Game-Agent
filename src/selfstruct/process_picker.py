"""Japanese process/window selector; never activates or modifies listed apps."""
import queue
import threading
import tkinter as tk
from tkinter import messagebox,ttk

from .processes import discover_targets,process_info


class ProcessPicker:
    def __init__(self,parent,on_select):
        self.on_select=on_select
        self.window=tk.Toplevel(parent)
        self.window.title('起動中のプロセスからゲームを選択')
        self.window.geometry('820x480')
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.closed=False
        self.poll_id=None
        self.results=queue.Queue()
        self.items=[]
        self.search=tk.StringVar()
        ttk.Label(self.window,text='ゲーム本体のプロセスまたはウィンドウを選択してください。STOP後も接続先は終了しません。').pack(anchor='w',padx=12,pady=10)
        ttk.Entry(self.window,textvariable=self.search).pack(fill='x',padx=12)
        self.search.trace_add('write',lambda *_:self.render())
        self.tree=ttk.Treeview(self.window,columns=('name','pid','title'),show='headings',selectmode='browse')
        for column,title,width in (('name','アプリ名',210),('pid','プロセスID',90),('title','ウィンドウ',460)):
            self.tree.heading(column,text=title);self.tree.column(column,width=width)
        self.tree.pack(fill='both',expand=True,padx=12,pady=10)
        self.tree.bind('<Double-1>',lambda _:self.select())
        buttons=ttk.Frame(self.window);buttons.pack(fill='x',padx=12,pady=(0,12))
        self.refresh_button=ttk.Button(buttons,text='一覧を更新',command=self.refresh)
        self.refresh_button.pack(side='left')
        ttk.Button(buttons,text='このプロセスに接続',command=self.select).pack(side='right')
        self.refresh()

    def refresh(self):
        self.refresh_button.configure(state='disabled')
        def scan():
            try:self.results.put((discover_targets(),None))
            except Exception as error:self.results.put(([],str(error)))
        threading.Thread(target=scan,daemon=True).start()
        self.poll_id=self.window.after(100,self.poll)

    def poll(self):
        if self.closed:return
        try:items,error=self.results.get_nowait()
        except queue.Empty:
            self.poll_id=self.window.after(100,self.poll);return
        self.poll_id=None
        self.refresh_button.configure(state='normal')
        if error:messagebox.showerror('一覧を取得できません',error,parent=self.window)
        self.items=items;self.render()

    def render(self):
        self.tree.delete(*self.tree.get_children())
        query=self.search.get().casefold()
        for index,item in enumerate(self.items):
            if query and query not in f'{item.process.name} {item.process.pid} {item.title}'.casefold():continue
            self.tree.insert('', 'end',iid=str(index),values=(item.process.name,item.process.pid,item.title))

    def select(self):
        selected=self.tree.selection()
        if not selected:return
        item=self.items[int(selected[0])]
        current=process_info(item.process.pid)
        if current is None or current.created!=item.process.created:
            messagebox.showinfo('プロセスが終了しました','一覧を更新して選択し直してください。',parent=self.window)
            self.refresh();return
        self.on_select(item)
        self.close()

    def close(self):
        self.closed=True
        if self.poll_id:self.window.after_cancel(self.poll_id)
        self.window.destroy()
