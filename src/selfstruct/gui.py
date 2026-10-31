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


from .management_gui import ManagementUI


class App(ManagementUI):
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
        self.starting = False
        self.last_ui_error = None
        self.diagnostic_busy = False
        self.diagnostic_done_until = 0.
        self.last_record_refresh = 0.
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
        style.map('Toolbutton',background=[('selected','#45675b'),('active','#3e5969')])
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
        self.individual_tab = ttk.Frame(self.tabs, padding=12)
        self.setup = ttk.Frame(self.tabs, padding=16)
        self.llm_tab = ttk.Frame(self.tabs, padding=10)
        self.book = ttk.Frame(self.tabs, padding=16)
        self.capabilities = ttk.Frame(self.tabs, padding=16)
        self.details = ttk.Frame(self.tabs, padding=16)
        for frame, title in ((self.home,'研究室'),(self.individual_tab,'個体管理'),(self.setup,'設定'),(self.llm_tab,'LLM管理'),(self.book,'研究ノート'),(self.capabilities,'能力接続図'),(self.details,'開発者詳細')):
            self.tabs.add(frame,text=title)
        self._home()
        self._setup()
        self._individuals_ui()
        self._llm_ui()
        from .gui_lifecycle import bind_cleanup
        bind_cleanup(self,window)
        launcher_status=default_root()/'runtime'/'launcher-status.json'
        if launcher_status.is_file():
            try:
                message=json.loads(launcher_status.read_text(encoding='utf-8')).get('message','')
                if message:self.activity.set(prose(message))
            except (OSError,ValueError):pass
        self.record_scope=tk.StringVar(value='個体：未ロード')
        for frame in (self.book,self.capabilities,self.details):
            ttk.Label(frame,textvariable=self.record_scope,foreground='#a8c9dd').pack(anchor='w',pady=(0,8))
        self.note_category=tk.StringVar(value='現在')
        categories=ttk.Combobox(self.book,textvariable=self.note_category,values=['現在','研究履歴','実験','発見','仮説','検証中','支持','反証','判定不能','自己改善'],state='readonly')
        categories.pack(anchor='w',pady=(0,10));categories.bind('<<ComboboxSelected>>',self.refresh_text)
        self.book_text = tk.Text(self.book, wrap="word", font=(UI_FONT, 11),background='#111920',foreground='#dce5ed',insertbackground='#dce5ed')
        self.book_text.pack(fill="both", expand=True)
        self.layout.panel("research-notes",self.book_text)
        ttk.Button(self.details,text='研究記録を保存（診断提出用とは別）',command=self.export_research).pack(anchor='w',pady=(0,8))
        self.raw_text = tk.Text(self.details, wrap="none", font=(MONO_FONT, 10))
        self.raw_text.pack(fill="both", expand=True)
        self.graph=tk.Canvas(self.capabilities,background='#111920',highlightthickness=0)
        self.graph.pack(fill='both',expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", self.refresh_text)
        self.layout.notebook('main-tabs',self.tabs)
        if not self.config.game_exe:
            self.tabs.select(self.setup)
        elif self.config.auto_run:
            window.after(1000, self.start)
        self.refresh_text()
        self.update_control_buttons()
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
        self.status = tk.StringVar(value="設定を確認してSTARTを押してください。")
        self.console = ttk.Panedwindow(self.home, orient='horizontal')
        self.console.pack(fill='both', expand=True)
        screen=ttk.Frame(self.console)
        readout=ttk.Frame(self.console,padding=8,width=340)
        readout.pack_propagate(False)
        self.console.add(screen,weight=4);self.console.add(readout,weight=0)
        self.layout.pane('lab-horizontal',self.console,default_tail=350)
        self.layout.panel('lab-game-screen',screen)
        self.layout.panel('lab-activity',readout)
        self.view_mode=tk.StringVar(value='現在の画面')
        ttk.Combobox(screen,textvariable=self.view_mode,state='readonly',values=['現在の画面','入力前後の比較','差分画像','AIが調べている画面'],width=22).pack(anchor='w')
        self.visual_attention=tk.StringVar(value='AI注目領域：要求待ち')
        ttk.Label(screen,textvariable=self.visual_attention,wraplength=700,foreground='#a8c9dd').pack(anchor='w')
        self.preview=tk.Canvas(screen,background='#06090c',highlightthickness=1,highlightbackground='#63717e')
        self.preview.pack(fill='both',expand=True)
        self.preview.create_text(24,30,anchor='nw',fill='#91a5b3',text='ゲーム画面は接続後に表示されます。')
        self.pc_performance=tk.StringVar(value='PC負荷を取得中…')
        ttk.Label(readout,textvariable=self.pc_performance,wraplength=320).pack(side='bottom',fill='x',pady=6)
        self.llm_status=tk.StringVar(value='○ LLM 待機')
        self.llm_label=ttk.Label(readout,textvariable=self.llm_status)
        self.llm_label.pack(anchor='w')
        self.active_model=tk.StringVar(value='使用モデル：未接続')
        ttk.Label(readout,textvariable=self.active_model,wraplength=320,foreground='#a8c9dd').pack(fill='x',pady=4)
        self.current_activity=tk.StringVar(value='現在：停止中\n研究：観測を待っています\n直前：—\n次：STARTで開始')
        ttk.Label(readout,textvariable=self.current_activity,wraplength=320).pack(fill='x',pady=6)
        self.timeline=tk.StringVar(value='最近のイベントは開始後に表示されます。')
        ttk.Label(readout,textvariable=self.timeline,wraplength=320,foreground='#a8bbc9').pack(fill='x',pady=6)
        self.evaluation_status=tk.StringVar(value='評価：変化あり・なしの独立比較を待っています')
        ttk.Label(readout,textvariable=self.evaluation_status,wraplength=320,foreground='#a8c9dd').pack(fill='x',pady=4)
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
        footer=ttk.Frame(self.home);footer.pack(fill='x',pady=(8,0))
        self.control_buttons={}
        for title,command in [('START',self.start),('PAUSE',self.pause),('STOP',self.stop),('診断ZIPをコピー',self.export)]:
            button=ttk.Button(footer,text=title,command=command)
            button.pack(side='left',padx=(0,4));self.control_buttons[title]=button
        self.control_state=tk.StringVar(value='停止中')
        ttk.Label(footer,textvariable=self.control_state).pack(side='left',padx=12)
        self.control_help=ttk.Label(self.home,text='START：開始／再開　PAUSE：ゲーム操作を休止（研究は継続）　STOP：操作・研究を終了して保存',font=(UI_FONT,9),foreground='#a8bbc9',wraplength=1050)
        self.control_help.pack(anchor='w')
        self.diagnostic_help=ttk.Label(self.home,text='診断ZIP：症状が出たらSTOP前に採取。稼働中／PAUSE中でも可。状態と記録をコピーし、停止・リセットはしません。',font=(UI_FONT,9),foreground='#a8bbc9',wraplength=1050)
        self.diagnostic_help.pack(anchor='w',pady=(2,0))
        self.home.bind('<Configure>',lambda event:[label.configure(wraplength=max(280,event.width-20)) for label in (self.control_help,self.diagnostic_help)])
        for name,color in [('Running','#459d76'),('Paused','#d2a44e'),('Stopped','#a95f67'),('Working','#7d8daf'),('Copied','#459d76')]:
            style=ttk.Style(self.window);style.configure(name+'.TButton',background=color,foreground='#101820',font=(UI_FONT,10,'bold'))
            style.map(name+'.TButton',background=[('disabled',color),('active',color)],foreground=[('disabled','#26333c')])
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
        ttk.Label(self.setup,text='個体設定 — 現在ロード中の個体へ保存されます',style='Title.TLabel').grid(row=0,column=0,columnspan=3,sticky='w',pady=8)
        self.fields={}
        self.connection_methods={'実行ファイルから起動':'launch','ランチャーから起動':'launcher','起動中のプロセスに接続':'attach'}
        self.connection_method=tk.StringVar(value=next(k for k,v in self.connection_methods.items() if v==self.config.connection_mode))
        ttk.Label(self.setup,text='接続方法').grid(row=1,column=0,sticky='w')
        ttk.Combobox(self.setup,textvariable=self.connection_method,values=list(self.connection_methods),state='readonly',width=34).grid(row=1,column=1,sticky='w',padx=8)
        ttk.Button(self.setup,text='起動中から選択',command=self.choose_process).grid(row=1,column=2)
        self.selected_process=tk.StringVar(value=f'対象ゲーム：{self.config.target_exe or self.config.game_exe}')
        ttk.Label(self.setup,textvariable=self.selected_process,wraplength=950).grid(row=2,column=0,columnspan=3,sticky='w')
        entries=[('game_exe','起動・自動再起動用のゲーム／ランチャー',self.config.game_exe),('game_args','起動オプション',__import__('subprocess').list2cmdline(self.config.game_args)),('window_title','対象ウィンドウ（空欄可）',self.config.window_title),('keys','許可キー（末尾追加なら個体を継続可能）',','.join(self.config.keys)),('quota','保存容量上限（GB / 共通資産を含む総使用量）',str(self.config.quota_bytes/1024**3)),('research_every','研究間隔（経験件数）',str(self.config.research_every))]
        for row,(key,label,value) in enumerate(entries,3):
            ttk.Label(self.setup,text=label).grid(row=row,column=0,sticky='w',pady=5)
            self.fields[key]=tk.StringVar(value=value);ttk.Entry(self.setup,textvariable=self.fields[key],width=65).grid(row=row,column=1,sticky='ew',padx=8)
            if key=='game_exe':ttk.Button(self.setup,text='選択',command=lambda:self.browse('game_exe')).grid(row=row,column=2)
        row=len(entries)+3
        self.mode=tk.StringVar(value=self.config.mode)
        ttk.Label(self.setup,text='知識モード').grid(row=row,column=0,sticky='w')
        ttk.Combobox(self.setup,textvariable=self.mode,values=['Newborn','Experienced','Guided'],state='readonly').grid(row=row,column=1,sticky='w',padx=8)
        ttk.Label(self.setup,text='個体の切替・新規作成は個体管理へ。LLM・Context・Runtimeの変更はLLM管理へ。').grid(row=row+1,column=0,columnspan=3,sticky='w',pady=8)
        ttk.Separator(self.setup).grid(row=row+2,column=0,columnspan=3,sticky='ew',pady=10)
        ttk.Label(self.setup,text='アプリ共通設定 — 個体を切り替えても維持します').grid(row=row+3,column=0,columnspan=3,sticky='w')
        self.fields['root']=tk.StringVar(value=self.config.root)
        ttk.Label(self.setup,text='共通モデル・個体の保存先').grid(row=row+4,column=0,sticky='w')
        ttk.Entry(self.setup,textvariable=self.fields['root'],width=65).grid(row=row+4,column=1,sticky='ew',padx=8)
        ttk.Button(self.setup,text='選択',command=lambda:self.browse('root')).grid(row=row+4,column=2)
        self.update_channel=tk.StringVar(value=self.config.channel)
        ttk.Label(self.setup,text='更新チャンネル').grid(row=row+5,column=0,sticky='w')
        ttk.Combobox(self.setup,textvariable=self.update_channel,values=['Stable','Development'],state='readonly').grid(row=row+5,column=1,sticky='w',padx=8)
        ttk.Label(self.setup,text='テーマ・ウィンドウ位置・レイアウトはアプリ共通で保持します。').grid(row=row+6,column=0,columnspan=3,sticky='w',pady=8)
        ttk.Button(self.setup,text='設定を保存',command=self.save_settings).grid(row=row+7,column=1,sticky='w',padx=8)
        ttk.Button(self.setup,text='公式更新を確認',command=self.check_update).grid(row=row+7,column=2)
        self.setup.columnconfigure(1,weight=1)

    def use_external_llm(self):
        if self.fields['llm_url'].get()=='http://127.0.0.1:8766/v1':self.fields['llm_url'].set('http://127.0.0.1:1234/v1')
        self.tabs.select(self.llm_tab)
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
        self.selected_process.set(f'{item.process.name} — {item.title}（プロセスID {item.process.pid}）。STOP後もゲームは終了しません。ゲーム終了時は設定した実行ファイルから自動再起動します。')

    def read_settings(self, *, allow_root_change=False) -> Config:
        from .windows import parse_arguments
        args = parse_arguments(self.fields["game_args"].get())
        result = replace(self.config, root=self.fields["root"].get(), game_exe=self.fields["game_exe"].get(),
                         connection_mode=self.connection_methods[self.connection_method.get()],
                         game_args=args, window_title=self.fields["window_title"].get(),
                         keys=[x.strip().upper() for x in self.fields["keys"].get().split(",") if x.strip()],
                         quota_bytes=int(float(self.fields["quota"].get()) * 1024**3), llm_url=self.fields["llm_url"].get(),
                         mode=self.mode.get(),channel=self.update_channel.get(),
                         llm_context=int(self.fields["llm_context"].get()),llm_threads=int(self.fields["llm_threads"].get()),
                         llm_backend=self.llm_backend.get(),research_every=int(self.fields["research_every"].get()))
        result.validate(require_game=False)
        if not allow_root_change and Path(result.root).resolve()!=Path(self.config.root).resolve():
            raise ValueError('共通保存先を変更した場合は、先に設定を保存してください。')
        return result

    def save_settings(self):
        try:
            if self.busy:raise ValueError("処理中です。完了またはSTOP後に設定を保存してください。")
            if self.controller:
                raise ValueError("設定を変える前にSTOPしてください。")
            config=self.read_settings(allow_root_change=True)
            if Path(config.root).resolve()!=Path(self.config.root).resolve():
                self.llm.stop()
                from .individuals import Individuals
                from .model_library import ModelLibrary
                self.individuals=Individuals(config.root);self.individuals.migrate(config)
                config=replace(config,agent_id='',model_id='',resume=False,auto_run=False)
                self.library=ModelLibrary(config.root);self.llm=ManagedLLM(Path(config.root),config.quota_bytes)
                self.performance_monitor.stop.set()
                from .performance import PerformanceMonitor
                self.performance_monitor=PerformanceMonitor(Path(config.root));self.performance_monitor.start()
            if config.agent_id:self.individuals.save_settings(config.agent_id,config)
            self.config=config
            self.config.save(self.config_path)
            self.refresh_individual_list();self.refresh_saved_models()
            self.status.set("設定を保存しました。現在の個体設定とアプリ共通設定を更新しました。")
        except Exception as exc:
            messagebox.showerror("設定を確認してください", error_message(exc))

    def task(self, operation, done=None):
        if self.busy:
            return
        import gc
        gc.collect()
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
            config=self.read_settings()
            if config.agent_id:self.individuals.save_settings(config.agent_id,config)
            self.config=config
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
            from .model_library import ModelLibrary
            library=ModelLibrary(self.config.root);library.discover()
            state=json.loads(self.llm.state_path.read_text(encoding='utf-8'))
            self.events.put(("managed_ready", state['model_sha256']))
        self.progress.set("配布情報を確認しています。")
        self.task(install, lambda _: self.progress.set("導入と推論試験が完了しました。STARTで始められます。"))

    def start(self):
        if self.busy:
            return
        if self.controller and not self.controller.stopped.is_set():
            self.controller.start()
            self.update_control_buttons()
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
                if config.model_id or self.llm.state_path.is_file():
                    identifier=self.library.prepare(self.llm,config,self.download_cancel)
                    info=self.library.get(identifier).get('model_info',{})
                    config=replace(config,model_id=identifier,model_name=info.get('name',''),model_quant=info.get('quant','未記録'))
                else:
                    self.llm.start(cancel=self.download_cancel)
            from .research import LocalLLM
            client=LocalLLM(config.llm_url,config.llm_model)
            client.health()
            if self.download_cancel.is_set():raise InterruptedError('開始を中断しました。')
            self.external_model_name=getattr(client,"loaded_model",config.llm_model)
            controller = Controller(config)
            if config.llm_url == "http://127.0.0.1:8766/v1" and self.llm.state_path.exists():
                def recover_llm():
                    self.llm.stop()
                    self.library.prepare(self.llm,controller.config,controller.scientist.stop_event)
                controller.scientist.recover_llm = recover_llm
            self.config = replace(controller.config, resume=True, auto_run=True)
            self.config.save(self.config_path)
            controller.start()
            return controller
        try:
            self.read_snapshot = self.read_settings()
        except Exception as exc:
            messagebox.showerror("設定を確認してください", error_message(exc))
            return
        if self.read_snapshot.model_id and self.read_snapshot.llm_url=='http://127.0.0.1:8766/v1':
            from .model_library import MissingModel
            try:self.library.get(self.read_snapshot.model_id)
            except MissingModel:self.missing_model();return
        self.download_cancel.clear()
        self.status.set("ローカル推論とゲームの接続を確認しています。")
        self.starting=True
        self.task(operation, self.started)
        self.update_control_buttons()

    def started(self, controller):
        self.starting=False
        self.controller = controller
        if self.download_cancel.is_set():
            self.stop();return
        self.graph_stamp=None
        self.trace_sequence=0
        self.refresh_individual_list();self.refresh_saved_models()
        self.tabs.select(self.home)
        self.update_control_buttons()

    def pause(self):
        if self.controller:
            self.controller.pause()
            self.update_control_buttons()

    def stop(self):
        self.starting=False
        self.download_cancel.set()
        self.update_control_buttons()
        self.config = replace(self.config, auto_run=False)
        self.config.save(self.config_path)
        if self.controller:
            self.controller.stop()
            self.update_control_buttons()
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
        self.refresh_individual_list();self.refresh_saved_models()
        self.status.set("停止しました。経験と研究内容を保存しました。")
        self.graph_stamp=None
        self.refresh_text();self.render_mini_graph();self.update_control_buttons()

    def newborn(self):
        if self.busy:
            return
        if self.controller:
            messagebox.showinfo("新しい個体", "先にSTOPしてください。前の個体のデータは保持されます。")
            return
        try:
            config=replace(self.read_settings(),mode='Newborn',resume=False,auto_run=False)
            identifier=self.individuals.create(config,parent=self.individuals.active() or None)
            config=replace(config,agent_id=identifier,resume=True)
            self.apply_individual(config)
        except Exception as exc:
            messagebox.showerror('NEWBORN',error_message(exc),parent=self.window)

    def update_control_buttons(self):
        ctrl=self.controller
        running=bool(ctrl and not ctrl.closed and not ctrl.stopped.is_set())
        paused=bool(running and ctrl.paused.is_set())
        stopping=bool(ctrl and ctrl.stopped.is_set() and not ctrl.closed)
        selected='START' if self.starting else 'PAUSE' if paused else 'START' if running else 'STOP'
        styles={'START':'Running.TButton','PAUSE':'Paused.TButton','STOP':'Stopped.TButton'}
        for key in ('START','PAUSE','STOP'):
            button=self.control_buttons[key]
            button.configure(style='Working.TButton' if key=='START' and self.starting else styles[key] if key==selected else 'TButton')
            disabled=(key=='START' and (self.busy or (running and not paused))) or (key=='PAUSE' and (not running or paused)) or (key=='STOP' and not ctrl and not self.starting)
            button.state(['disabled'] if disabled else ['!disabled'])
        diagnostic=self.control_buttons['診断ZIPをコピー']
        diagnostic.configure(style='Working.TButton' if self.diagnostic_busy else 'Copied.TButton' if time.monotonic()<self.diagnostic_done_until else 'TButton',text='診断ZIPを作成中…' if self.diagnostic_busy else '診断ZIPをコピー')
        diagnostic.state(['disabled'] if self.busy else ['!disabled'])
        self.control_state.set('開始準備中' if self.starting else '停止・保存処理中' if stopping else 'ゲーム操作は一時停止中／研究は継続' if paused else '実行中' if running else '処理中／ゲーム操作は停止' if self.busy else '停止中／保存記録を閲覧できます')

    def export(self):
        if self.busy:return
        from .diagnostics import create_diagnostics,copy_file
        from .inspection import IndividualView
        ctrl=self.controller
        config=self.config
        snapshot=ctrl.snapshot() if ctrl else None
        gui={'tab':self.tabs.index(self.tabs.select()),'state':self.status.get(),'snapshot_kind':'live' if ctrl else 'saved','loaded_individual':config.agent_id}
        self.diagnostic_busy=True;self.update_control_buttons()
        def generate():
            with IndividualView(config,ctrl) as view:
                cfg=replace(config,agent_id=view.identifier)
                return create_diagnostics(Path(cfg.root),store=view.store,config=cfg,activity=snapshot if ctrl else view.activity(),gui={**gui,'loaded_individual':view.identifier})
        def copied(path):
            self.diagnostic_busy=False
            try:
                copy_file(path,self.window.winfo_id())
                self.diagnostic_done_until=time.monotonic()+3
                self.activity.set('診断ZIPを作成してコピーしました。添付欄へ貼り付けられます。')
            except OSError:
                self.activity.set('診断ZIPは保存済みですが、コピーに失敗しました：'+str(path))
            self.update_control_buttons()
        self.task(generate,copied)
        self.update_control_buttons()

    def export_research(self):
        if self.busy:return
        from .inspection import IndividualView
        path=filedialog.asksaveasfilename(defaultextension='.zip',initialfile='研究記録.zip')
        if path:
            config=self.config;ctrl=self.controller
            def save():
                with IndividualView(config,ctrl) as view:
                    if not view.store:raise ValueError('この個体には保存された研究記録がありません。')
                    view.store.export_research(Path(path))
            self.task(save,lambda _:self.activity.set('ロード済み個体の研究記録を保存しました。提出用には診断ZIPをご利用ください。'))

    def refresh_text(self, _=None):
        if self.tabs.select()==str(self.individual_tab) and not self.busy:
            self.refresh_individual_sizes()
        if self.tabs.select()==str(self.llm_tab):
            self.refresh_saved_models()
        from .inspection import IndividualView
        from .diagnostics import diagnostic_json
        try:
            with IndividualView(self.config,self.controller) as view:
                name=view.repo.profile(view.identifier)['name'] if view.identifier else '未ロード'
                live=bool(view.controller)
                self.record_scope.set('個体：'+name+' ／ '+(view.activity().get('phase','実行中')+'の記録' if live else '保存済み記録（STARTせず閲覧できます）'))
                if self.tabs.select()==str(self.book):
                    category=self.note_category.get();store=view.store
                    if category=='現在':
                        a=view.activity()
                        text='研究ノート — 現在'+('' if live else '（保存済み）')+'\n\n状態：'+a.get('phase','停止中')+'\n現在の注目：'+a.get('purpose','未記録')+'\n現在の研究：'+a.get('experiment','まだ研究記録はありません。')+'\n最近の変化：'+str(a.get('change','未記録'))+'\n未確認：画面変化の意味と因果関係\n次の実験：'+a.get('next','STARTで研究を開始します。')
                        text+='\n\n現在の仮説\n'+'\n'.join(prose(r['text']) for r in store.note_rows('仮説',3)) if store else ''
                    elif category=='研究履歴':
                        from .diagnostics import diagnostic_job_payload
                        text='\n\n'.join('Job '+r['id']+' ／ '+r['state']+'\n'+json.dumps(json.loads(diagnostic_job_payload(r['payload'])),ensure_ascii=False,indent=2) for r in store.research_history()) if store else ''
                    elif category=='自己改善':text='\n\n'.join(prose(r['summary']) for r in store.reports()) if store else ''
                    else:text='\n\n'.join(prose(r['text']) for r in store.note_rows(category)) if store else ''
                    self.set_record_text(self.book_text,text or 'まだ記録はありません。')
                if self.tabs.select()==str(self.details):
                    text=view.source() or 'まだAgent世代は作成されていません。'
                    if view.store:
                        text+='\n\n直近の評価（要約。全記録は保存できます）\n'+'\n'.join(diagnostic_json(r) for r in view.store.reports()[:5])
                    path=view.root/'host'/'host.log' if view.root else None
                    if path and path.is_file():
                        with path.open('rb') as f:f.seek(max(0,path.stat().st_size-65536));text+='\n'+f.read().decode(errors='replace')
                    if self.last_ui_error and self.last_ui_error[0]==self.config.agent_id:text+='\n\n直近の画面操作エラー\n'+self.last_ui_error[1]
                    self.set_record_text(self.raw_text,text)
            if self.tabs.select()==str(self.capabilities):self.render_graph()
        except (OSError,ValueError,__import__('sqlite3').Error) as error:
            self.record_scope.set('ロード済み個体の記録を取得できません：'+str(error))
            self.set_record_text(self.book_text,'記録を取得できません。個体管理でロード中の個体をご確認ください。')
            self.set_record_text(self.raw_text,str(error));self.graph.delete('all')

    def set_record_text(self,widget,text):
        if widget.get('1.0','end-1c')==text:return
        position=widget.yview()[0]
        widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',text);widget.configure(state='disabled')
        widget.yview_moveto(position)

    def capability_graph(self):
        from .inspection import IndividualView
        with IndividualView(self.config,self.controller) as view:return view.graph()

    def render_graph(self):
        self.graph.delete('all')
        data=self.capability_graph()
        if not data['nodes']:
            self.graph.create_text(30,30,anchor='nw',fill='#dce5ed',text='Agentからの能力自己記述を待っています。Hostから能力やゲームの意味は与えません。')
            return
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
        path=str(self.config.agent_id)
        try:
            data=self.capability_graph()
            stamp=(str(path),json.dumps(data,sort_keys=True),canvas.winfo_width(),any(time.time()-at<20 for at in self.graph_new.values()))
            if stamp==getattr(self,'graph_stamp',None):return
            self.graph_stamp=stamp
        except (OSError,ValueError,__import__('sqlite3').Error):
            canvas.delete('all');canvas.create_text(10,20,anchor='nw',fill='#91a5b3',text='Agentからの自己記述を待っています');return
        canvas.delete('all')
        if not data.get('nodes'):
            canvas.create_text(10,20,anchor='nw',fill='#91a5b3',text='Agentからの自己記述を待っています');return
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
        if self.closing:return
        try:
            while True:
                if self.closing:return
                kind, data = self.events.get_nowait()
                if kind == "done":
                    self.busy = False
                    callback, value = data
                    if callback:
                        callback(value)
                elif kind == "error":
                    self.busy = False
                    self.diagnostic_busy=False
                    self.starting=False
                    self.last_ui_error=(self.config.agent_id,data[1])
                    self.status.set(data[0])
                    self.activity.set(data[0])
                elif kind == "managed_ready":
                    self.refresh_saved_models()
                    self.fields["llm_url"].set("http://127.0.0.1:8766/v1")
                    self.config=replace(self.config,llm_url="http://127.0.0.1:8766/v1",llm_model="local",model_id=data,model_name=self.model_name(data),model_quant=self.model_records.get(data,{}).get('model_info',{}).get('quant','未記録'))
                    if self.config.agent_id:self.individuals.save_settings(self.config.agent_id,self.config)
                    self.refresh_saved_models()
                    self.config.save(self.config_path)
                elif kind == "progress":
                    self.progress.set(data)
        except queue.Empty:
            pass
        if self.closing:return
        if not self.busy and self.close_pending:
            self.close_pending = False
            self.close()
        elif not self.busy and self.stop_pending:
            self.stop_pending = False
            self.stop()
        self.update_control_buttons()
        if time.monotonic()-self.last_record_refresh>2:
            self.last_record_refresh=time.monotonic()
            if self.tabs.select() in (str(self.book),str(self.capabilities),str(self.details)):self.refresh_text()
        self.pc_performance.set(self.performance_monitor.text())
        info=self.llm.current_info
        if self.config.llm_url!='http://127.0.0.1:8766/v1':
            self.active_model.set('使用モデル：'+getattr(self,'external_model_name',self.config.llm_model)+' / 外部ローカル接続（GPU使用は接続先で管理）')
        elif info:
            layers=info.get('gpu_layers',0)
            mode=f"GPU / VRAM使用 / {layers}/{info.get('total_layers','?')}層" if layers else ('GPUオフロード未確認' if info.get('backend')=='vulkan' else 'CPU推論')
            if info.get('status')!='利用可能':mode='未稼働'
            live_id=(self.llm.active_state or {}).get('model_sha256','')
            prefix='使用モデル：' if self.controller and not self.controller.stopped.is_set() else 'ロード済み：'
            mismatch=bool(self.config.model_id and live_id and live_id!=self.config.model_id)
            extra=('\n個体の指定：'+(self.config.model_name or self.config.model_id[:12])+'（未準備）') if mismatch else ''
            self.active_model.set(prefix+info['name']+'\n推論：'+mode+' / '+info.get('status','')+extra+('（GPU起動失敗のためCPUへ復帰）' if info.get('gpu_error') else ''))
        else:self.active_model.set('使用モデル：未接続')
        if hasattr(self,'saved_models'):
            for identifier,state in self.model_records.items():
                if self.saved_models.exists(identifier):self.saved_models.set(identifier,'state',self.library.status(state,self.llm,bool(self.controller and not self.controller.stopped.is_set() and self.config.model_id==identifier and self.config.llm_url=='http://127.0.0.1:8766/v1')))
        ctrl = self.controller
        if not ctrl:self.render_mini_graph()
        if ctrl:
            game=ctrl.game
            if game and getattr(game,'process',None) and getattr(game,'target_exe',''):
                identity=(game.target_pid,game.target_created,game.target_exe,game.hwnd)
                saved=(self.config.target_pid,self.config.target_created,self.config.target_exe,self.config.target_hwnd)
                if identity!=saved:
                    self.config=replace(self.config,target_pid=identity[0],target_created=identity[1],target_exe=identity[2],target_hwnd=identity[3])
                    self.config.save(self.config_path)
            a=ctrl.snapshot()
            display_status='停止・保存処理中' if ctrl.stopped.is_set() and not ctrl.closed else '一時停止中' if ctrl.paused.is_set() else ctrl.status
            self.status.set(display_status + f" ／ 稼働 {int(time.monotonic()-ctrl.started)}秒")
            while not ctrl.messages.empty():self.activity.set(prose(ctrl.messages.get_nowait()))
            self.llm_status.set(('●' if a.llm=='推論中' else '○')+' LLM '+a.llm)
            self.llm_label.configure(foreground={'推論中':'#7fe0ae','評価中':'#e5bf72','復旧中':'#e5bf72','エラー':'#ee9191'}.get(a.llm,'#9aabb9'))
            self.current_activity.set(f'現在：{a.phase}\n研究：{a.experiment}\n直前：{a.action} / {a.duration*1000:.0f}ms / 変化 {a.change:.3f}\n次：{a.next}')
            self.render_streams(a)
            self.render_mini_graph()
            self.timeline.set('\n'.join(e.text for e in a.events[-3:]))
            self.adoption.set(a.adoption if time.time()-a.adoption_at<20 else '')
            telemetry=ctrl.telemetry;c=telemetry.get('counts',{});storage=telemetry.get('storage',{})
            evaluation=telemetry.get('evaluation',{})
            measured=evaluation.get('candidate',{})
            if not measured and evaluation.get('live'):measured=evaluation['live'].get('agent',{})
            if measured.get('classes'):
                groups=measured['classes'];loss=measured.get('balanced_loss')
                self.evaluation_status.set(f"評価：変化 {groups['changed']['n']} / なし {groups['unchanged']['n']}\n均等損失："+(f'{loss:.3f}' if loss is not None else '比較不足')+f"\n見逃し {groups['changed']['misses']} / 誤反応 {groups['unchanged'].get('false_positives',0)}"+'\n'+evaluation.get('reason',''))
            elif evaluation:self.evaluation_status.set('評価：旧形式または候補の検査段階。能力向上は未確認です。')
            self.metrics.set(f"経験 {c.get('experience',ctrl.steps)}　発見 {c.get('discoveries',0)}　仮説 {c.get('hypotheses',0)}　支持 {c.get('supported',0)}　反証 {c.get('refuted',0)}　技能 {c.get('skills',0)}　自己改善 {c.get('improvements',0)}　世代 {ctrl.generation_number}")
            if storage:
                text=f"保存領域 {storage['used']/1024**3:.2f} / {storage['quota']/1024**3:g}GB"
                if storage['stage']!='通常':text+=' ／ 保存領域を自動整理しています'
                self.storage_label.set(text)
                self.storage_bar["value"]=100*storage["used"]/max(1,storage["quota"])
                ttk.Style().configure("Storage.Horizontal.TProgressbar",background="#df9870" if self.storage_bar["value"]>=80 else "#64ab8b")
            if a.attention:
                self.visual_attention.set(f'AI注目領域：{a.attention_source or "現在画面"} ／ {list(a.attention)}')
            preview_key=(ctrl.steps,self.view_mode.get(),self.preview.winfo_width(),self.preview.winfo_height(),a.attention,a.attention_source,a.attention_size,a.attention_reference)
            if ctrl.latest and preview_key!=self.last_preview:
                self.last_preview=preview_key
                image = ctrl.latest.copy()
                viewing_attention=self.view_mode.get()=='AIが調べている画面'
                if viewing_attention:
                    try:
                        event,phase,observation=a.attention_reference
                        source={'event':event,'phase':phase} if event else {'observation':observation}
                        image=ctrl.visual.eyes.source(source)[0]
                    except (ValueError,OSError,TypeError):
                        image=None
                elif ctrl.latest_before and self.view_mode.get() == "差分画像":
                    image = ImageChops.difference(ctrl.latest_before.resize(image.size), image)
                elif ctrl.latest_before and self.view_mode.get() == "入力前後の比較":
                    before = ctrl.latest_before.resize(image.size)
                    combined = Image.new("RGB", (image.width * 2, image.height))
                    combined.paste(before)
                    combined.paste(image, (image.width, 0))
                    image = combined
                self.preview.delete('all')
                if image is None:
                    self.preview.create_text(24,30,anchor='nw',fill='#91a5b3',text='対象の視覚Evidenceは未取得または整理済みです。')
                else:
                    source_width,source_height=image.size
                    image.thumbnail((max(100, self.preview.winfo_width()), max(100, self.preview.winfo_height())))
                    self.photo = ImageTk.PhotoImage(image)
                    x=(self.preview.winfo_width()-image.width)//2;y=(self.preview.winfo_height()-image.height)//2
                    self.preview.create_image(x,y,anchor='nw',image=self.photo)
                    can_overlay=viewing_attention or (a.attention_source in ('','現在画面') and self.view_mode.get()=='現在の画面' and (not a.attention_size or a.attention_size==ctrl.latest.size))
                    if a.attention and can_overlay:
                        left,top,right,bottom=a.attention
                        sx=image.width/source_width;sy=image.height/source_height
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
