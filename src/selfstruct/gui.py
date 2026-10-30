from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageChops, ImageTk

from .config import Config, default_root
from .engine import Controller
from .presentation import error_message, prose, system_info
from .resources import ManagedLLM

UI_FONT = "Yu Gothic UI" if os.name == "nt" else "Noto Sans CJK JP"
MONO_FONT = "Consolas" if os.name == "nt" else "Noto Sans Mono CJK JP"


class App:
    def __init__(self, window: tk.Tk, health_marker: Path | None = None):
        self.window = window
        self.config_path = default_root() / "host" / "settings.json"
        self.config = Config.load(self.config_path)
        self.controller: Controller | None = None
        self.llm = ManagedLLM(Path(self.config.root), self.config.quota_bytes)
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.closing = False
        self.close_pending = False
        self.stop_pending = False
        self.download_cancel = threading.Event()
        self.health_marker = health_marker
        window.title("自己発達ゲームAI — 自律研究室")
        window.geometry("1150x800")
        window.minsize(900, 650)
        window.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure('.',font=(UI_FONT,10))
        style.configure("TLabel", font=(UI_FONT, 10))
        style.configure("TButton", padding=8, font=(UI_FONT, 10))
        style.configure("Title.TLabel", font=(UI_FONT, 20, "bold"))
        window.configure(background='#141a20')
        style.configure('.',background='#1c252e',foreground='#dce5ed',fieldbackground='#111920',bordercolor='#354451')
        style.configure('TButton',background='#2d414e',foreground='#e9f0f5')
        style.map('TButton',background=[('active','#3e5969')])
        style.configure('TNotebook.Tab',padding=(16,8),background='#22313c')
        style.map('TNotebook.Tab',background=[('selected','#3b5362')])
        style.map('TCombobox',fieldbackground=[('readonly','#111920')],foreground=[('readonly','#dce5ed')],selectbackground=[('readonly','#273d4c')])
        style.configure('TEntry',fieldbackground='#111920',insertcolor='#dce5ed')
        style.configure('Treeview',fieldbackground='#111920',background='#111920',foreground='#dce5ed')
        from .layout import Layout
        self.layout=Layout(default_root()/'host'/'layout.json')
        self.layout.window('main',window)
        self.last_preview=None
        self.graph_nodes=set();self.graph_new={}
        self.tabs = ttk.Notebook(window)
        self.tabs.pack(fill="both", expand=True, padx=6, pady=6)
        self.home = ttk.Frame(self.tabs, padding=4)
        self.setup = ttk.Frame(self.tabs, padding=16)
        self.book = ttk.Frame(self.tabs, padding=16)
        self.details = ttk.Frame(self.tabs, padding=16)
        for frame, title in ((self.home, "研究室"), (self.setup, "初回設定"), (self.book, "研究ノート"), (self.details, "開発者詳細")):
            self.tabs.add(frame, text=title)
        self._home()
        self._setup()
        launcher_status=default_root()/'runtime'/'launcher-status.json'
        if launcher_status.is_file():
            try:
                message=json.loads(launcher_status.read_text(encoding='utf-8')).get('message','')
                if message:self.activity.set(prose(message))
            except (OSError,ValueError):pass
        self.note_category=tk.StringVar(value='現在')
        categories=ttk.Combobox(self.book,textvariable=self.note_category,values=['現在','発見','仮説','支持','反証','自己改善'],state='readonly')
        categories.pack(anchor='w',pady=(0,10));categories.bind('<<ComboboxSelected>>',self.refresh_text)
        self.book_text = tk.Text(self.book, wrap="word", font=(UI_FONT, 11),background='#111920',foreground='#dce5ed',insertbackground='#dce5ed')
        self.book_text.pack(fill="both", expand=True)
        self.layout.panel("research-notes",self.book_text)
        ttk.Button(self.details,text='研究記録を保存（診断提出用とは別）',command=self.export_research).pack(anchor='w',pady=(0,8))
        self.raw_text = tk.Text(self.details, wrap="none", font=(MONO_FONT, 10))
        self.raw_text.pack(fill="both", expand=True)
        self.capabilities=ttk.Frame(self.tabs,padding=16)
        self.tabs.add(self.capabilities,text='能力接続図')
        self.graph=tk.Canvas(self.capabilities,background='#111920',highlightthickness=0)
        self.graph.pack(fill='both',expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", self.refresh_text)
        self.layout.notebook('main-tabs',self.tabs)
        if not self.config.game_exe:
            self.tabs.select(self.setup)
        elif self.config.auto_run:
            window.after(1000, self.start)
        window.after(200, self.poll)
        if health_marker:
            from .sandbox import Sandbox
            seed = Path(__file__).with_name("assets").joinpath("seed.wat").read_text(encoding="utf-8")
            Sandbox(seed).action(1, 2)
            # Verify the retained database before acknowledging an application update.
            import sqlite3
            for db_path in Path(self.config.root).glob("experiments/*/host/experience.sqlite3"):
                with sqlite3.connect(db_path) as db:
                    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("Database integrity check failed")
            window.after(1000, lambda: health_marker.write_text("healthy"))

    def _home(self):
        self.status = tk.StringVar(value="初回設定を確認してSTARTを押してください。")
        self.console = ttk.Panedwindow(self.home, orient='horizontal')
        self.console.pack(fill='both', expand=True)
        screen=ttk.Frame(self.console)
        readout=ttk.Frame(self.console,padding=8,width=340)
        readout.pack_propagate(False)
        self.console.add(screen,weight=4);self.console.add(readout,weight=0)
        if 'lab-horizontal' not in self.layout.entries:
            self.console.after(180,lambda:self.console.sashpos(0,max(450,self.console.winfo_width()-350)))
        self.layout.pane('lab-horizontal',self.console)
        self.layout.panel('lab-game-screen',screen)
        self.layout.panel('lab-activity',readout)
        self.view_mode=tk.StringVar(value='現在の画面')
        ttk.Combobox(screen,textvariable=self.view_mode,state='readonly',values=['現在の画面','入力前後の比較','差分画像'],width=22).pack(anchor='w')
        self.preview=tk.Canvas(screen,background='#06090c',highlightthickness=1,highlightbackground='#63717e')
        self.preview.pack(fill='both',expand=True)
        self.preview.create_text(24,30,anchor='nw',fill='#91a5b3',text='ゲーム画面は接続後に表示されます。')
        self.llm_status=tk.StringVar(value='○ LLM 待機')
        self.llm_label=ttk.Label(readout,textvariable=self.llm_status)
        self.llm_label.pack(anchor='w')
        self.current_activity=tk.StringVar(value='現在：停止中\n研究：観測を待っています\n直前：—\n次：STARTで開始')
        ttk.Label(readout,textvariable=self.current_activity,wraplength=320).pack(fill='x',pady=6)
        self.timeline=tk.StringVar(value='最近のイベントは開始後に表示されます。')
        ttk.Label(readout,textvariable=self.timeline,wraplength=320,foreground='#a8bbc9').pack(fill='x',pady=6)
        self.adoption=tk.StringVar()
        ttk.Label(readout,textvariable=self.adoption,wraplength=320,foreground='#7fd2af').pack(fill='x')
        streams=ttk.Panedwindow(readout,orient='horizontal');streams.pack(fill='both',expand=True)
        for name,title,width in [('input_log','入力ログ',18),('live_trace','LIVE TRACE',27)]:
            frame=ttk.Frame(streams);streams.add(frame,weight=1)
            ttk.Label(frame,text=title).pack(anchor='w')
            widget=tk.Text(frame,height=10,width=width,background='#06090c',foreground='#91a5b3',font=(MONO_FONT,9),state='disabled',wrap='none',borderwidth=0)
            widget.pack(fill='both',expand=True);widget.tag_configure('fresh',foreground='#dceee7')
            setattr(self,name,widget);self.layout.panel('lab-'+name,frame)
        self.layout.pane('lab-streams',streams)
        ttk.Label(readout,text='自作器官接続図').pack(anchor='w',pady=(8,0))
        self.mini_graph=tk.Canvas(readout,background='#06090c',height=160,highlightthickness=0)
        self.mini_graph.pack(fill='both',expand=True)
        self.mini_graph.create_text(10,20,anchor='nw',fill='#91a5b3',text='Agentからの自己記述を待っています')
        self.mini_graph.bind('<MouseWheel>',lambda e:self.mini_graph.yview_scroll(-1 if e.delta>0 else 1,'units'))
        self.layout.panel('lab-capability-graph',self.mini_graph)
        self.mini_graph.bind('<Button-1>',lambda _:self.tabs.select(self.capabilities))
        self.pc_performance=tk.StringVar(value='PC負荷を取得中…')
        ttk.Label(readout,textvariable=self.pc_performance,wraplength=320).pack(fill='x',pady=6)
        footer=ttk.Frame(self.home);footer.pack(fill='x',pady=(8,0))
        for title,command in [('START',self.start),('PAUSE',self.pause),('STOP',self.stop),('NEWBORN',self.newborn),('診断ZIPをコピー',self.export)]:
            ttk.Button(footer,text=title,command=command).pack(side='left',padx=(0,4))
        self.metrics=tk.StringVar(value='経験 0　発見 0　仮説 0　支持 0　反証 0　技能 0　自己改善 0　世代 1')
        ttk.Label(self.home,textvariable=self.metrics).pack(anchor='e')
        storage=ttk.Frame(self.home);storage.pack(fill='x')
        ttk.Label(storage,textvariable=self.status).pack(side='left')
        self.storage_label=tk.StringVar(value='保存領域 —')
        ttk.Style().configure('Storage.Horizontal.TProgressbar',background='#64ab8b')
        self.storage_bar=ttk.Progressbar(storage,length=180,maximum=100,style="Storage.Horizontal.TProgressbar")
        self.storage_bar.pack(side='right')
        ttk.Label(storage,textvariable=self.storage_label).pack(side='right',padx=8)
        self.activity=tk.StringVar()
        ttk.Label(self.home,textvariable=self.activity,wraplength=1050).pack(anchor='w')
        self.trace_sequence=0
        from .performance import PerformanceMonitor
        self.performance_monitor=PerformanceMonitor(Path(self.config.root))
        self.performance_monitor.start()

    def _setup(self):
        ttk.Label(self.setup, text="はじめの設定", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self.fields: dict[str, tk.StringVar] = {}
        self.connection_methods={'実行ファイルから起動':'launch','ランチャーから起動':'launcher','起動中のプロセスに接続':'attach'}
        self.connection_method=tk.StringVar(value=next(label for label,value in self.connection_methods.items() if value==self.config.connection_mode))
        ttk.Label(self.setup,text='接続方法').grid(row=1,column=0,sticky='w')
        ttk.Combobox(self.setup,textvariable=self.connection_method,values=list(self.connection_methods),state='readonly',width=34).grid(row=1,column=1,sticky='w',padx=8)
        ttk.Button(self.setup,text='起動中から選択',command=self.choose_process).grid(row=1,column=2)
        self.selected_process=tk.StringVar(value=f'接続先プロセス: {Path(self.config.target_exe).name}' if self.config.target_exe else 'ランチャーを使う場合は「ランチャーから起動」を選び、ゲーム本体の起動を待ちます。')
        ttk.Label(self.setup,textvariable=self.selected_process,wraplength=950).grid(row=2,column=0,columnspan=3,sticky='w',pady=5)
        entries = [("game_exe", "ゲーム・ランチャーの実行ファイル", self.config.game_exe), ("window_title", "ウィンドウ名の一部（空欄可）", self.config.window_title),
                   ("game_args", "起動オプション（空欄可）", __import__('subprocess').list2cmdline(self.config.game_args)),
                   ("root", "作業・保存フォルダ", self.config.root), ("keys", "許可キー（カンマ区切り・Num0〜Num9対応）", ",".join(self.config.keys)),
                   ("quota", "保存容量の上限（GB）", str(self.config.quota_bytes / 1024**3)),
                   ("llm_url", "ローカル推論接続先（詳細設定）", self.config.llm_url)]
        for row, (name, label, value) in enumerate(entries, 3):
            ttk.Label(self.setup, text=label).grid(row=row, column=0, sticky="w", pady=5)
            var = self.fields[name] = tk.StringVar(value=value)
            entry=ttk.Entry(self.setup, textvariable=var, width=65)
            entry.grid(row=row, column=1, sticky="ew", padx=8)
            if name=="llm_url":self.llm_url_entry=entry
            if name in {"game_exe", "root"}:
                ttk.Button(self.setup, text="選択", command=lambda n=name: self.browse(n)).grid(row=row, column=2)
        self.setup.columnconfigure(1, weight=1)
        row = len(entries) + 3
        self.mode = tk.StringVar(value=self.config.mode)
        ttk.Label(self.setup, text="開始方法").grid(row=row, column=0, sticky="w")
        ttk.Combobox(self.setup, textvariable=self.mode, state="readonly", values=["Newborn", "Experienced", "Guided"]).grid(row=row, column=1, sticky="w", padx=8)
        ttk.Label(self.setup, text="Newborn：新しい個体 ／ Experienced：前回の続き ／ Guided：人間知識を区別する比較用個体", wraplength=1000).grid(row=row+1, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(self.setup, text="設定を保存", command=self.save_settings).grid(row=row+2, column=1, sticky="w", padx=8)
        ttk.Separator(self.setup).grid(row=row+3, column=0, columnspan=3, sticky="ew", pady=14)
        info = system_info()
        ttk.Label(self.setup, text=f"PCの目安：空きメモリ {info['ram']:.1f}GB ／ 空き容量 {info['free']:.1f}GB ／ CPU推論に対応").grid(row=row+4, column=0, columnspan=3, sticky="w")
        ttk.Label(self.setup,text='ローカルLLM — 用途やPCに合うGGUFを探して導入できます。').grid(row=row+5,column=0,columnspan=3,sticky='w',pady=8)
        choices=ttk.Frame(self.setup);choices.grid(row=row+6,column=0,columnspan=3,sticky='w')
        for label,command in [('おすすめモデルを使う',lambda:self.open_models(True)),('モデルを検索する',self.open_models),('手元のGGUFを使う',self.import_model),('外部ローカルLLMへ接続',self.use_external_llm)]:
            ttk.Button(choices,text=label,command=command).pack(side='left',padx=3)
        self.progress=tk.StringVar(value='モデルのライセンス・容量を確認してから導入してください。')
        ttk.Label(self.setup,textvariable=self.progress,wraplength=950).grid(row=row+7,column=0,columnspan=3,sticky='w',pady=8)
        ttk.Button(self.setup,text='取得を中断',command=self.download_cancel.set).grid(row=row+8,column=2)
        ttk.Button(self.setup,text='公式更新を確認',command=self.check_update).grid(row=row+8,column=1,sticky='w')

    def open_models(self,recommended=False):
        from .model_browser import ModelBrowser
        ModelBrowser(self,recommended)

    def use_external_llm(self):
        if self.fields['llm_url'].get()=='http://127.0.0.1:8766/v1':self.fields['llm_url'].set('http://127.0.0.1:1234/v1')
        self.llm_url_entry.focus_set();self.llm_url_entry.selection_range(0,'end')
        self.progress.set('ローカル推論接続先を入力して設定を保存してください。STARTで接続を確認します。')

    def import_model(self):
        path=filedialog.askopenfilename(filetypes=[('GGUFモデル','*.gguf')])
        if path:self.install_model({'local_path':path,'name':Path(path).name})

    def browse(self, name):
        path = filedialog.askdirectory() if name == "root" else filedialog.askopenfilename(filetypes=[("実行ファイル", "*.exe"), ("すべて", "*.*")])
        if path:
            self.fields[name].set(path)
            if name=='game_exe':
                self.config=replace(self.config,target_pid=0,target_created=0,target_exe='',target_hwnd=0)
                self.connection_method.set('実行ファイルから起動')
                self.selected_process.set('実行ファイルから起動します。ランチャーの場合は接続方法を切り替えてください。')

    def choose_process(self):
        if self.controller or self.busy:
            messagebox.showinfo('接続先の変更','先にSTOPしてから接続先を変更してください。');return
        from .process_picker import ProcessPicker
        ProcessPicker(self.window,self.selected_target)

    def selected_target(self,item):
        self.config=replace(self.config,target_pid=item.process.pid,target_created=item.process.created,
                            target_exe=item.process.exe,target_hwnd=item.hwnd)
        self.connection_method.set('起動中のプロセスに接続')
        if not self.fields['game_exe'].get():self.fields['game_exe'].set(item.process.exe)
        self.fields['window_title'].set('')
        self.selected_process.set(f'{item.process.name} — {item.title}（プロセスID {item.process.pid}）。STOP後もゲームは終了しません。')

    def read_settings(self) -> Config:
        from .windows import parse_arguments
        args = parse_arguments(self.fields["game_args"].get())
        result = replace(self.config, root=self.fields["root"].get(), game_exe=self.fields["game_exe"].get(),
                         connection_mode=self.connection_methods[self.connection_method.get()],
                         game_args=args, window_title=self.fields["window_title"].get(),
                         keys=[x.strip().upper() for x in self.fields["keys"].get().split(",") if x.strip()],
                         quota_bytes=int(float(self.fields["quota"].get()) * 1024**3), llm_url=self.fields["llm_url"].get(),
                         mode=self.mode.get())
        result.validate(require_game=False)
        return result

    def save_settings(self):
        try:
            if self.controller:
                raise ValueError("設定を変える前にSTOPしてください。")
            self.config = self.read_settings()
            self.config.save(self.config_path)
            self.status.set("設定を保存しました。")
        except Exception as exc:
            messagebox.showerror("設定を確認してください", error_message(exc))

    def task(self, operation, done=None):
        if self.busy:
            return
        self.busy = True
        def worker():
            try:
                result = operation()
                self.events.put(("done", (done, result)))
            except Exception as exc:
                import traceback
                self.events.put(("error", (error_message(exc), traceback.format_exc())))
        threading.Thread(target=worker, daemon=True).start()

    def install_model(self, selected):
        if self.controller or self.busy:
            return
        try:
            self.config = self.read_settings()
            self.config.save(self.config_path)
        except Exception as exc:
            messagebox.showerror("設定を確認してください", error_message(exc))
            return
        self.download_cancel.clear()
        def install():
            self.llm.stop()
            self.llm = ManagedLLM(Path(self.config.root), self.config.quota_bytes)
            def progress(done, total):
                self.events.put(("progress", f"必要ファイルを取得中 {done / max(total, 1):.0%}"))
            self.llm.install(selected, progress, self.download_cancel)
            self.events.put(("managed_ready", None))
        self.progress.set("配布情報を確認しています。")
        self.task(install, lambda _: self.progress.set("導入と推論試験が完了しました。STARTで始められます。"))

    def start(self):
        if self.busy:
            return
        if self.controller and not self.controller.stopped.is_set():
            self.controller.start()
            return
        previous = self.controller
        self.controller = None
        def operation():
            if previous:
                previous.close()
            config = self.read_snapshot
            config.validate()
            if self.llm.root != Path(config.root):
                self.llm.stop()
                self.llm = ManagedLLM(Path(config.root), config.quota_bytes)
            if config.llm_url == "http://127.0.0.1:8766/v1":
                self.llm.start()
            from .research import LocalLLM
            LocalLLM(config.llm_url, config.llm_model).health()
            controller = Controller(config)
            if config.llm_url == "http://127.0.0.1:8766/v1" and self.llm.state_path.exists():
                def recover_llm():
                    self.llm.stop()
                    self.llm.start(cancel=controller.scientist.stop_event)
                controller.scientist.recover_llm = recover_llm
            self.config = replace(config, resume=True, auto_run=True)
            self.config.save(self.config_path)
            controller.start()
            return controller
        try:
            self.read_snapshot = self.read_settings()
        except Exception as exc:
            messagebox.showerror("設定を確認してください", error_message(exc))
            return
        self.status.set("ローカル推論とゲームの接続を確認しています。")
        self.task(operation, self.started)

    def started(self, controller):
        self.controller = controller
        self.graph_stamp=None
        self.trace_sequence=0
        self.tabs.select(self.home)

    def pause(self):
        if self.controller:
            self.controller.pause()

    def stop(self):
        self.config = replace(self.config, auto_run=False)
        self.config.save(self.config_path)
        if self.controller:
            self.controller.stop()
            if self.busy:
                self.stop_pending = True
                return
            controller = self.controller
            # Keep its live inference snapshot visible until shutdown completes.
            self.task(controller.close, self.closed_controller)

    def closed_controller(self, _):
        self.controller = None
        self.llm_status.set('○ LLM 待機')
        self.llm_label.configure(foreground='#9aabb9')
        self.status.set("停止しました。経験と研究内容を保存しました。")

    def newborn(self):
        if self.busy:
            return
        if self.controller:
            messagebox.showinfo("新しい個体", "先にSTOPしてください。前の個体のデータは保持されます。")
            return
        self.config = replace(self.config, mode="Newborn", resume=False, auto_run=False)
        self.mode.set("Newborn")
        self.config.save(self.config_path)
        self.status.set("次のSTARTで、ゲーム固有の経験を持たない新しい個体を作成します。")

    def export(self):
        if self.busy:return
        from .diagnostics import create_diagnostics,copy_file
        ctrl=self.controller
        snapshot=ctrl.activity.snapshot() if ctrl else None
        config=self.config
        gui={'tab':self.tabs.index(self.tabs.select()),'state':self.status.get()}
        def generate():
            store=ctrl.store if ctrl else None
            owned=None
            if store is None:
                state=Path(config.root)/'host'/'experiment.json'
                if state.is_file():
                    metadata=json.loads(state.read_text(encoding='utf-8'))
                    candidate=(Path(config.root)/'experiments'/metadata['id']).resolve()
                    if candidate.is_relative_to((Path(config.root)/'experiments').resolve()):
                        from .store import Store
                        owned=Store(candidate,config.quota_bytes);owned.account_root=Path(config.root);store=owned
            try:return create_diagnostics(Path(config.root),store=store,config=config,activity=snapshot,gui=gui)
            finally:
                if owned:owned.close()
        def copied(path):
            try:
                copy_file(path,self.window.winfo_id())
                self.activity.set('診断ZIPを作成してコピーしました。添付欄へ貼り付けられます。')
            except OSError:
                self.activity.set('診断ZIPを作成しましたが、クリップボードへのコピーに失敗しました。')
            self.raw_text.insert('end',f'診断ZIP: {path}\n')
        self.task(generate,copied)

    def export_research(self):
        if not self.controller or self.busy:return
        path=filedialog.asksaveasfilename(defaultextension='.zip',initialfile='研究記録.zip')
        if path:
            store=self.controller.store
            self.task(lambda:store.export_research(Path(path)),lambda _:self.activity.set('研究DBと世代情報を保存しました。提出用には診断ZIPをご利用ください。'))

    def refresh_text(self, _=None):
        if not self.controller:
            return
        if self.tabs.select() == str(self.book):
            self.book_text.delete("1.0", "end")
            category=self.note_category.get()
            ctrl=self.controller
            if category=='現在':
                a=ctrl.activity.snapshot()
                text=f'研究ノート — 現在\n\n現在の注目：{a.purpose}\n現在の研究：{a.experiment}\n最近の変化：{a.change:.3f}\n未確認：画面変化の意味と因果関係\n次の実験：{a.next}'
                hypotheses=ctrl.store.note_rows('仮説',3)
                text+='\n\n現在の仮説\n'+'\n'.join(prose(r['text']) for r in hypotheses)
            elif category=='自己改善':text='\n\n'.join(prose(r['summary']) for r in ctrl.store.reports())
            else:text='\n\n'.join(prose(r['text']) for r in ctrl.store.note_rows(category))
            self.book_text.insert('1.0',text or 'まだ記録はありません。')
        if self.tabs.select()==str(self.capabilities):
            self.render_graph()
        if self.tabs.select() == str(self.details):
            self.raw_text.delete("1.0", "end")
            self.raw_text.insert("1.0", self.controller.revisions.source() + "\n\n" + json.dumps(self.controller.store.reports(), ensure_ascii=False, indent=2))
            path = Path(self.config.root) / "host" / "host.log"
            if path.exists():
                with path.open("rb") as file:
                    file.seek(max(0, path.stat().st_size - 65536))
                    self.raw_text.insert("end", "\n" + file.read().decode(errors="replace"))

    def render_graph(self):
        self.graph.delete('all')
        path=self.controller.revisions.root/self.controller.agent_id/'capabilities.json'
        if not path.exists():
            self.graph.create_text(30,30,anchor='nw',fill='#dce5ed',text='Agentからの能力自己記述を待っています。Hostから能力やゲームの意味は与えません。')
            return
        data=json.loads(path.read_text(encoding='utf-8'))
        positions={n['id']:(150+(i%3)*290,70+(i//3)*130) for i,n in enumerate(data['nodes'])}
        for a,b in data['edges']:
            self.graph.create_line(*positions[a],*positions[b],fill='#698ea5',arrow='last')
        for node in data['nodes']:
            x,y=positions[node['id']]
            box=self.graph.create_rectangle(x-125,y-40,x+125,y+40,fill='#294c48' if node.get('active') else '#172630',outline='#668398')
            label=self.graph.create_text(x,y,text=node['description']+'\n'+node['role']+f" ／ {node['uses']}回",width=235,fill='#e1eaf0')
            def show(_,item=node):
                lines=[item['description'],f"状態：{item.get('status','未報告')}",f"役割：{item['role']}",f"入力：{item['input']}",f"出力：{item['output']}",f"使用回数：{item['uses']}",f"有効性：{item['effectiveness']}",f"作成世代：{item.get('created_generation','未報告')}"]
                messagebox.showinfo('Agentの能力自己記述','\n'.join(lines),parent=self.window)
            self.graph.tag_bind(box,'<Button-1>',show);self.graph.tag_bind(label,'<Button-1>',show)

    def render_streams(self,a):
        fresh=[e for e in a.trace if e.sequence>self.trace_sequence]
        if fresh:self.trace_sequence=fresh[-1].sequence
        for widget,rows in [(self.live_trace,a.trace[-35:]),(self.input_log,tuple(e for e in a.trace if e.kind=='ACT')[-10:])]:
            widget.configure(state='normal');widget.delete('1.0','end')
            for event in rows:
                stamp=time.strftime('%H:%M:%S',time.localtime(event.at))
                widget.insert('end',f'{stamp} {event.kind} {event.text}\n','fresh' if time.time()-event.at<1 else '')
            widget.see('end');widget.configure(state='disabled')

    def render_mini_graph(self):
        canvas=self.mini_graph
        path=self.controller.revisions.root/self.controller.agent_id/'capabilities.json'
        try:
            stamp=(str(path),path.stat().st_mtime_ns,canvas.winfo_width(),any(time.time()-at<20 for at in self.graph_new.values()))
            if stamp==getattr(self,'graph_stamp',None):return
            self.graph_stamp=stamp;data=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            canvas.delete('all');canvas.create_text(10,20,anchor='nw',fill='#91a5b3',text='Agentからの自己記述を待っています');return
        canvas.delete('all')
        nodes=data.get('nodes',[]);width=max(120,canvas.winfo_width())
        identities={n['id'] for n in nodes}
        for identity in identities-self.graph_nodes:self.graph_new[identity]=time.time()
        self.graph_nodes=identities
        positions={n['id']:(width/2,25+i*42) for i,n in enumerate(nodes)}
        for source,target in data.get('edges',[]):
            if source in positions and target in positions:canvas.create_line(*positions[source],*positions[target],fill='#607b8a',arrow='last')
        for node in nodes:
            x,y=positions[node['id']];active=node.get('active',False)
            canvas.create_rectangle(10,y-15,width-10,y+15,fill='#294c48' if active else '#172630',outline='#9cdfba' if time.time()-self.graph_new.get(node['id'],0)<20 else '#668398')
            canvas.create_text(x,y,text=node.get('description','')[:30],fill='#dce5ed',width=width-30)
        canvas.configure(scrollregion=canvas.bbox('all'))

    def check_update(self):
        self.status.set('固定Launcherから起動すると、起動時に公式更新を確認します。経験とAgent世代は引き継ぎます。')

    def poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "done":
                    self.busy = False
                    callback, value = data
                    if callback:
                        callback(value)
                elif kind == "error":
                    self.busy = False
                    self.status.set(data[0])
                    self.raw_text.insert("end", data[1] + "\n")
                elif kind == "managed_ready":
                    self.fields["llm_url"].set("http://127.0.0.1:8766/v1")
                    self.config=replace(self.config,llm_url="http://127.0.0.1:8766/v1",llm_model="local")
                    self.config.save(self.config_path)
                elif kind == "progress":
                    self.progress.set(data)
        except queue.Empty:
            pass
        if not self.busy and self.close_pending:
            self.close_pending = False
            self.close()
        elif not self.busy and self.stop_pending:
            self.stop_pending = False
            self.stop()
        self.pc_performance.set(self.performance_monitor.text())
        ctrl = self.controller
        if ctrl:
            game=ctrl.game
            if game and getattr(game,'process',None) and getattr(game,'target_exe',''):
                identity=(game.target_pid,game.target_created,game.target_exe,game.hwnd)
                saved=(self.config.target_pid,self.config.target_created,self.config.target_exe,self.config.target_hwnd)
                if identity!=saved:
                    self.config=replace(self.config,target_pid=identity[0],target_created=identity[1],target_exe=identity[2],target_hwnd=identity[3])
                    self.config.save(self.config_path)
            a=ctrl.activity.snapshot()
            self.status.set(ctrl.status + f" ／ 稼働 {int(time.monotonic()-ctrl.started)}秒")
            while not ctrl.messages.empty():self.activity.set(prose(ctrl.messages.get_nowait()))
            self.llm_status.set(('●' if a.llm=='推論中' else '○')+' LLM '+a.llm)
            self.llm_label.configure(foreground={'推論中':'#7fe0ae','評価中':'#e5bf72','復旧中':'#e5bf72','エラー':'#ee9191'}.get(a.llm,'#9aabb9'))
            self.current_activity.set(f'現在：{a.phase}\n研究：{a.experiment}\n直前：{a.action} / {a.duration*1000:.0f}ms / 変化 {a.change:.3f}\n次：{a.next}')
            self.render_streams(a)
            self.render_mini_graph()
            self.timeline.set('\n'.join(e.text for e in a.events[-3:]))
            self.adoption.set(a.adoption if time.time()-a.adoption_at<20 else '')
            telemetry=ctrl.telemetry;c=telemetry.get('counts',{});storage=telemetry.get('storage',{})
            self.metrics.set(f"経験 {c.get('experience',ctrl.steps)}　発見 {c.get('discoveries',0)}　仮説 {c.get('hypotheses',0)}　支持 {c.get('supported',0)}　反証 {c.get('refuted',0)}　技能 {c.get('skills',0)}　自己改善 {c.get('improvements',0)}　世代 {ctrl.generation_number}")
            if storage:
                text=f"保存領域 {storage['used']/1024**3:.2f} / {storage['quota']/1024**3:g}GB"
                if storage['stage']!='通常':text+=' ／ 保存領域を自動整理しています'
                self.storage_label.set(text)
                self.storage_bar["value"]=100*storage["used"]/max(1,storage["quota"])
                ttk.Style().configure("Storage.Horizontal.TProgressbar",background="#df9870" if self.storage_bar["value"]>=80 else "#64ab8b")
            if ctrl.latest and (ctrl.steps,self.view_mode.get(),self.preview.winfo_width(),self.preview.winfo_height(),a.attention)!=self.last_preview:
                self.last_preview=(ctrl.steps,self.view_mode.get(),self.preview.winfo_width(),self.preview.winfo_height(),a.attention)
                image = ctrl.latest.copy()
                if ctrl.latest_before and self.view_mode.get() == "差分画像":
                    image = ImageChops.difference(ctrl.latest_before.resize(image.size), image)
                elif ctrl.latest_before and self.view_mode.get() == "入力前後の比較":
                    before = ctrl.latest_before.resize(image.size)
                    combined = Image.new("RGB", (image.width * 2, image.height))
                    combined.paste(before)
                    combined.paste(image, (image.width, 0))
                    image = combined
                image.thumbnail((max(100, self.preview.winfo_width()), max(100, self.preview.winfo_height())))
                self.photo = ImageTk.PhotoImage(image)
                self.preview.delete('all')
                x=(self.preview.winfo_width()-image.width)//2;y=(self.preview.winfo_height()-image.height)//2
                self.preview.create_image(x,y,anchor='nw',image=self.photo)
                if a.attention and self.view_mode.get()=='現在の画面':
                    left,top,right,bottom=a.attention
                    sx=image.width/ctrl.latest.width;sy=image.height/ctrl.latest.height
                    self.preview.create_rectangle(x+left*sx,y+top*sy,x+right*sx,y+bottom*sy,outline='#80d9be',width=2)
        if not self.closing:
            self.window.after(500, self.poll)

    def close(self):
        self.download_cancel.set()
        if self.busy:
            self.close_pending = True
            self.status.set("処理を安全に終了してから閉じます。")
            return
        controller = self.controller
        self.controller = None
        if controller:
            controller.stop()
        def finish():
            if controller:
                controller.close()
            self.llm.stop()
        self.task(finish, lambda _: self.destroy())

    def destroy(self):
        self.performance_monitor.stop.set()
        self.layout.save()
        self.closing = True
        self.window.destroy()
