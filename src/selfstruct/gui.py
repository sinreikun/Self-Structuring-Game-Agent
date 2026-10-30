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
from .resources import Catalog, ManagedLLM


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
        style.configure("TLabel", font=("Yu Gothic UI", 10))
        style.configure("TButton", padding=8, font=("Yu Gothic UI", 10))
        style.configure("Title.TLabel", font=("Yu Gothic UI", 20, "bold"))
        self.tabs = ttk.Notebook(window)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=12)
        self.home = ttk.Frame(self.tabs, padding=16)
        self.setup = ttk.Frame(self.tabs, padding=16)
        self.book = ttk.Frame(self.tabs, padding=16)
        self.details = ttk.Frame(self.tabs, padding=16)
        for frame, title in ((self.home, "研究室"), (self.setup, "初回設定"), (self.book, "研究ノート"), (self.details, "開発者詳細")):
            self.tabs.add(frame, text=title)
        self._home()
        self._setup()
        self.book_text = tk.Text(self.book, wrap="word", font=("Yu Gothic UI", 11))
        self.book_text.pack(fill="both", expand=True)
        self.raw_text = tk.Text(self.details, wrap="none", font=("Consolas", 10))
        self.raw_text.pack(fill="both", expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", self.refresh_text)
        if not self.config.game_exe:
            self.tabs.select(self.setup)
        elif self.config.auto_run:
            window.after(1000, self.start)
        window.after(200, self.poll)
        if health_marker:
            from .sandbox import Sandbox
            seed = Path(__file__).with_name("assets").joinpath("seed.wat").read_text()
            Sandbox(seed).action(1, 2)
            # Verify the retained database before acknowledging an application update.
            import sqlite3
            for db_path in Path(self.config.root).glob("experiments/*/host/experience.sqlite3"):
                with sqlite3.connect(db_path) as db:
                    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("Database integrity check failed")
            window.after(1000, lambda: health_marker.write_text("healthy"))

    def _home(self):
        ttk.Label(self.home, text="画面から、理解を育てる。", style="Title.TLabel").pack(anchor="w")
        self.status = tk.StringVar(value="初回設定を確認してSTARTを押してください。")
        ttk.Label(self.home, textvariable=self.status, wraplength=1050).pack(anchor="w", pady=8)
        toolbar = ttk.Frame(self.home)
        toolbar.pack(fill="x", pady=8)
        for title, command in (("START", self.start), ("PAUSE", self.pause), ("STOP", self.stop),
                               ("NEWBORN", self.newborn), ("EXPORT", self.export)):
            ttk.Button(toolbar, text=title, command=command).pack(side="left", padx=(0, 8))
        ttk.Label(toolbar, text="緊急停止：F12", foreground="#953535").pack(side="right")
        self.metrics = tk.StringVar(value="経験 0　仮説 0　支持 0　反証 0　技能 0　自己改善 0")
        ttk.Label(self.home, textvariable=self.metrics).pack(anchor="w", pady=8)
        self.view_mode = tk.StringVar(value="現在の画面")
        selection = ttk.Combobox(self.home, textvariable=self.view_mode, state="readonly", values=["現在の画面", "入力前後の比較", "差分画像"])
        selection.pack(anchor="w")
        self.preview = ttk.Label(self.home, text="対象ゲームの画面は開始後に表示されます。", anchor="center", background="#162027", foreground="#c9d7dd")
        self.preview.pack(fill="both", expand=True, pady=12)
        self.activity = tk.StringVar(value="画面の意味やキーの意味を、最初から与えることはありません。")
        ttk.Label(self.home, textvariable=self.activity, wraplength=1050).pack(anchor="w")
        ttk.Label(self.home, text="通常はゲームを前面にして観測します。他のウィンドウへ切り替えると入力を解放して待機します。", wraplength=1050).pack(anchor="w", pady=8)

    def _setup(self):
        ttk.Label(self.setup, text="はじめの設定", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self.fields: dict[str, tk.StringVar] = {}
        entries = [("game_exe", "対象ゲーム", self.config.game_exe), ("window_title", "ウィンドウ名の一部（空欄可）", self.config.window_title),
                   ("game_args", "起動オプション（空欄可）", __import__('subprocess').list2cmdline(self.config.game_args)),
                   ("root", "作業・保存フォルダ", self.config.root), ("keys", "許可キー（カンマ区切り）", ",".join(self.config.keys)),
                   ("quota", "保存容量の上限（GB）", str(self.config.quota_bytes / 1024**3)),
                   ("llm_url", "ローカル推論接続先（詳細設定）", self.config.llm_url)]
        for row, (name, label, value) in enumerate(entries, 1):
            ttk.Label(self.setup, text=label).grid(row=row, column=0, sticky="w", pady=5)
            var = self.fields[name] = tk.StringVar(value=value)
            ttk.Entry(self.setup, textvariable=var, width=65).grid(row=row, column=1, sticky="ew", padx=8)
            if name in {"game_exe", "root"}:
                ttk.Button(self.setup, text="選択", command=lambda n=name: self.browse(n)).grid(row=row, column=2)
        self.setup.columnconfigure(1, weight=1)
        row = len(entries) + 1
        self.mode = tk.StringVar(value=self.config.mode)
        ttk.Label(self.setup, text="開始方法").grid(row=row, column=0, sticky="w")
        ttk.Combobox(self.setup, textvariable=self.mode, state="readonly", values=["Newborn", "Experienced", "Guided"]).grid(row=row, column=1, sticky="w", padx=8)
        ttk.Label(self.setup, text="Newborn：新しい個体 ／ Experienced：前回の続き ／ Guided：人間知識を区別する比較用個体", wraplength=1000).grid(row=row+1, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(self.setup, text="設定を保存", command=self.save_settings).grid(row=row+2, column=1, sticky="w", padx=8)
        ttk.Separator(self.setup).grid(row=row+3, column=0, columnspan=3, sticky="ew", pady=14)
        info = system_info()
        ttk.Label(self.setup, text=f"PCの目安：空きメモリ {info['ram']:.1f}GB ／ 空き容量 {info['free']:.1f}GB ／ CPU推論に対応").grid(row=row+4, column=0, columnspan=3, sticky="w")
        catalog = Catalog(Path(self.config.root) / "host" / "catalog.json")
        self.models = catalog.data["models"]
        suggested = max((x for x in self.models if x["ram_gb"] <= info["ram"]), key=lambda x: x["ram_gb"], default=self.models[0])
        self.model = tk.StringVar(value=suggested["name"])
        ttk.Label(self.setup, text="ローカルLLM").grid(row=row+5, column=0, sticky="w", pady=8)
        ttk.Combobox(self.setup, textvariable=self.model, values=[x["name"] for x in self.models], state="readonly", width=50).grid(row=row+5, column=1, sticky="w", padx=8)
        ttk.Label(self.setup, text="選ぶだけで公式配布元から推論エンジンとモデルを取得・検証します。既存のローカル接続先も利用できます。", wraplength=950).grid(row=row+6, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(self.setup, text="選択したLLMを自動導入", command=self.install_model).grid(row=row+7, column=1, sticky="w", padx=8)
        ttk.Button(self.setup, text="取得を中断", command=self.download_cancel.set).grid(row=row+7, column=2)
        self.progress = tk.StringVar(value="モデルの再配布条件：Apache-2.0 ／ 推論エンジン：MIT")
        ttk.Label(self.setup, textvariable=self.progress, wraplength=900).grid(row=row+8, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(self.setup, text="公式更新を確認", command=self.check_update).grid(row=row+9, column=1, sticky="w", padx=8)

    def browse(self, name):
        path = filedialog.askdirectory() if name == "root" else filedialog.askopenfilename(filetypes=[("実行ファイル", "*.exe"), ("すべて", "*.*")])
        if path:
            self.fields[name].set(path)

    def read_settings(self) -> Config:
        from .windows import parse_arguments
        args = parse_arguments(self.fields["game_args"].get())
        result = replace(self.config, root=self.fields["root"].get(), game_exe=self.fields["game_exe"].get(),
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

    def install_model(self):
        if self.controller or self.busy:
            return
        try:
            self.config = self.read_settings()
            self.config.save(self.config_path)
        except Exception as exc:
            messagebox.showerror("設定を確認してください", error_message(exc))
            return
        selected = next(x for x in self.models if x["name"] == self.model.get())
        self.download_cancel.clear()
        def install():
            self.llm.stop()
            self.llm = ManagedLLM(Path(self.config.root), self.config.quota_bytes)
            def progress(done, total):
                self.events.put(("progress", f"必要ファイルを取得中 {done / max(total, 1):.0%}"))
            self.llm.install(selected, progress, self.download_cancel)
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
            self.controller = None
            self.task(controller.close, self.closed_controller)

    def closed_controller(self, _):
        self.controller = None
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
        if not self.controller:
            messagebox.showinfo("診断情報", "実験開始後に利用できます。保存済みの個体はExperiencedで開けます。")
            return
        path = filedialog.asksaveasfilename(defaultextension=".zip", initialfile="研究記録.zip")
        if path:
            self.task(lambda: self.controller.store.export(Path(path)), lambda _: self.activity.set("研究ノートと診断情報を書き出しました。"))

    def refresh_text(self, _=None):
        if not self.controller:
            return
        if self.tabs.select() == str(self.book):
            self.book_text.delete("1.0", "end")
            self.book_text.insert("1.0", prose(self.controller.store.book()))
        if self.tabs.select() == str(self.details):
            self.raw_text.delete("1.0", "end")
            self.raw_text.insert("1.0", self.controller.revisions.source() + "\n\n" + json.dumps(self.controller.store.reports(), ensure_ascii=False, indent=2))
            path = Path(self.config.root) / "host" / "host.log"
            if path.exists():
                with path.open("rb") as file:
                    file.seek(max(0, path.stat().st_size - 65536))
                    self.raw_text.insert("end", "\n" + file.read().decode(errors="replace"))

    def check_update(self):
        if self.controller or self.busy:
            return
        from . import __version__
        from .release import UPDATE_KEY, UPDATE_URL
        from .update import Updater
        updater = Updater(Path(self.config.root), self.config.quota_bytes, UPDATE_KEY, UPDATE_URL.format(channel=self.config.channel))
        def ready(prepared):
            if prepared is None:
                self.status.set("現在の版は最新です。")
            elif messagebox.askyesno("本体の更新", "検証済みの更新を取得しました。アプリを再起動して更新しますか？"):
                updater.handoff(prepared)
                self.close()
        self.task(lambda: updater.prepare(self.config.channel, __version__), ready)

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
        ctrl = self.controller
        if ctrl:
            self.status.set(ctrl.status + f" ／ 稼働 {int(time.monotonic() - ctrl.started)}秒 ／ 世代 {len(list(ctrl.revisions.root.glob('*/agent.wat')))}")
            while not ctrl.messages.empty():
                self.activity.set(prose(ctrl.messages.get_nowait()))
            known = ctrl.store.knowledge_rows()
            reports = ctrl.store.reports()
            self.metrics.set(f"経験 {ctrl.steps}　発見 {sum(r['kind']=='observation' for r in known)}　仮説 {sum(r['kind']=='hypothesis' for r in known)}　支持 {sum(r['validation'] in {'supported','reproduced'} for r in known)}　反証 {sum(r['validation']=='refuted' for r in known)}　技能 {sum(r['kind']=='skill' for r in known)}　自己改善 {sum(r['adopted'] for r in reports)}")
            if ctrl.latest:
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
                self.preview.configure(image=self.photo, text="")
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
        self.closing = True
        self.window.destroy()
