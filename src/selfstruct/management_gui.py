"""Individual and shared LLM management panels for the research console."""
from dataclasses import replace
import json
from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from .individuals import Individuals
from .model_library import ModelLibrary, MANAGED_URL, MissingModel
from .model_catalog import fit


def put(widget, text):
    widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',text);widget.configure(state='disabled')


def stamp(value):
    return time.strftime('%Y-%m-%d %H:%M',time.localtime(value)) if value else '—'


class ManagementUI:
    def _individuals_ui(self):
        self.individuals=Individuals(self.config.root)
        self.individuals.migrate(self.config)
        if not self.config.agent_id and self.individuals.active() and (self.config.resume or self.config.mode=='Experienced'):
            self.config=replace(self.config,agent_id=self.individuals.active())
        self.individual_name=tk.StringVar(value='個体：未選択')
        ttk.Label(self.home,textvariable=self.individual_name,foreground='#a8c9dd').pack(anchor='w',before=self.console)
        panes=ttk.Panedwindow(self.individual_tab,orient='horizontal');panes.pack(fill='both',expand=True)
        left=ttk.Frame(panes);right=ttk.Frame(panes);panes.add(left,weight=2);panes.add(right,weight=3)
        self.individual_tree=ttk.Treeview(left,columns=('created','last','size'),show='tree headings',selectmode='browse')
        self.individual_tree.heading('#0',text='個体名 / ● ロード中');self.individual_tree.column('#0',width=220)
        for key,label in [('created','作成日時'),('last','最終使用'),('size','使用容量')]:
            self.individual_tree.heading(key,text=label);self.individual_tree.column(key,width=135)
        self.individual_tree.pack(fill='both',expand=True);self.individual_tree.bind('<<TreeviewSelect>>',self.show_individual)
        self.individual_detail=tk.Text(right,wrap='word',background='#111920',foreground='#dce5ed',state='disabled');self.individual_detail.pack(fill='both',expand=True)
        controls=ttk.Frame(self.individual_tab);controls.pack(fill='x',pady=8)
        for i,(title,command) in enumerate([('この個体をロード',self.load_individual),('名前変更',self.rename_individual),('複製',self.clone_individual),('NEWBORN',self.newborn),('エクスポート',self.export_individual),('削除',self.delete_individual),('比較',self.compare_individual),('設定を補完',self.review_individual),('容量を更新',self.refresh_individual_sizes)]):
            ttk.Button(controls,text=title,command=command).grid(row=i//5,column=i%5,sticky='ew',padx=2,pady=2)
        self.individual_summaries={};self.refresh_individual_list()
        self.layout.pane('individual-management',panes,default_fraction=.5)

    def selected_individual(self):
        selected=self.individual_tree.selection()
        return selected[0] if selected else ''

    def refresh_individual_list(self):
        selected=self.selected_individual();self.individual_tree.delete(*self.individual_tree.get_children())
        active=self.individuals.active()
        for profile in self.individuals.list():
            identifier=profile['id'];size=self.individual_summaries.get(identifier,{}).get('bytes')
            self.individual_tree.insert('','end',iid=identifier,text=('● ' if identifier==active else '')+profile['name'],values=(stamp(profile['created']),stamp(profile['last_used']),f'{size/1024**3:.3f} GB' if size is not None else '未取得'))
        if selected and self.individual_tree.exists(selected):self.individual_tree.selection_set(selected)
        elif active and self.individual_tree.exists(active):self.individual_tree.selection_set(active)
        profile=next((p for p in self.individuals.list() if p['id']==active),None)
        self.individual_name.set('個体：'+profile['name'] if profile else '個体：未選択')

    def idle_management(self):
        if self.busy:return False
        if self.controller:
            messagebox.showinfo('個体・LLM管理','先にSTOPしてください。経験とチェックポイントの保存完了後に操作できます。',parent=self.window);return False
        return True

    def refresh_individual_sizes(self):
        if self.busy:return
        repo=self.individuals
        def finished(rows):
            self.individual_summaries=rows;self.refresh_individual_list();self.show_individual()
        self.task(lambda:{p['id']:repo.summary(p['id']) for p in repo.list()},finished)

    def show_individual(self,_=None):
        identifier=self.selected_individual()
        if not identifier:return
        p=self.individuals.profile(identifier);s=self.individuals.settings(identifier);r=self.individual_summaries.get(identifier,{})
        c=r.get('counts',{});model=s.get('model_id','');model_name=s.get('model_name') or (self.model_name(model) if hasattr(self,'library') else (model or '未記録'))
        lines=[p['name'],f"作成：{stamp(p['created'])} / 最終使用：{stamp(p['last_used'])}",f"作成方法：{p['creation']} / 親個体：{self.parent_name(p.get('parent'))}",f"使用LLM：{model_name} / 量子化：{s.get('model_quant') or '未記録'}",f"Context：{s.get('llm_context',8192)} / Runtime：llama.cpp {s.get('llm_backend','auto')} / CPU threads：{s.get('llm_threads',0)}",f"接続：{s.get('connection_mode','未記録')} / モード：{s.get('mode','未記録')}",f"ゲーム：{s.get('target_exe') or s.get('game_exe','未記録')}",f"許可キー：{', '.join(s.get('keys',[])) or '未記録'}",f"保存上限：{s.get('quota_bytes',0)/1024**3:g} GB",'']
        if p.get('settings_review_required'):lines.append('旧版で設定が保存されていない個体です。「設定を補完」で当時の設定を確認してください。')
        if r:
            lines.extend([f"経験 {c.get('experience',0)} / 発見 {c.get('discoveries',0)} / 仮説 {c.get('hypotheses',0)}",f"支持 {c.get('supported',0)} / 反証 {c.get('refuted',0)} / 技能 {c.get('skills',0)} / 改善採用 {c.get('improvements',0)}",f"使用容量：{r['bytes']/1024**3:.3f} GB / 累積稼働：{int(p.get('elapsed',0))} 秒",f"\n技術詳細\n内部ID：{identifier}\nAgent Revision：{r['revision']}"])
        else:lines.append('「容量を更新」で研究件数・Revision・使用容量を取得します。')
        put(self.individual_detail,'\n'.join(lines))

    def parent_name(self,identifier):
        if not identifier:return 'なし'
        try:return self.individuals.profile(identifier)['name']
        except OSError:return '削除済み個体'

    def apply_individual(self,config):
        self.config=config;self.external_model_name=config.model_name or config.llm_model;self.config.save(self.config_path)
        values={'game_exe':config.game_exe,'game_args':__import__('subprocess').list2cmdline(config.game_args),'window_title':config.window_title,'keys':','.join(config.keys),'quota':str(config.quota_bytes/1024**3),'llm_url':config.llm_url,'llm_context':str(config.llm_context),'llm_threads':str(config.llm_threads),'research_every':str(config.research_every)}
        for key,value in values.items():self.fields[key].set(value)
        self.connection_method.set(next(k for k,v in self.connection_methods.items() if v==config.connection_mode))
        self.mode.set(config.mode);self.llm_backend.set(config.llm_backend)
        self.selected_process.set(f'対象ゲーム：{config.target_exe or config.game_exe}')
        self.refresh_individual_list();self.refresh_saved_models();self.last_preview=None
        self.preview.delete('all');self.photo=None
        self.metrics.set('個体をロードしました。研究件数は個体管理で確認できます。')
        self.book_text.delete('1.0','end');self.raw_text.delete('1.0','end');self.graph.delete('all');self.mini_graph.delete('all')
        self.input_log.configure(state='normal');self.input_log.delete('1.0','end');self.input_log.configure(state='disabled')
        self.live_trace.configure(state='normal');self.live_trace.delete('1.0','end');self.live_trace.configure(state='disabled')
        self.status.set('個体の設定を読み込みました。LLMを確認しています。')
        self.prepare_individual_llm()

    def load_individual(self):
        identifier=self.selected_individual()
        if not identifier or not self.idle_management():return
        try:config=self.individuals.load(identifier,self.config)
        except ValueError as exc:messagebox.showerror('個体ロード',str(exc),parent=self.window);return
        self.apply_individual(config)

    def prepare_individual_llm(self):
        config=self.config
        if config.llm_url!=MANAGED_URL:
            from .research import LocalLLM
            self.task(lambda:LocalLLM(config.llm_url,config.llm_model).health(),lambda _:self.status.set('個体と外部ローカルLLMの接続を確認しました。STARTで開始できます。'));return
        try:self.library.discover();self.library.get(config.model_id)
        except MissingModel:
            self.missing_model();return
        self.download_cancel.clear()
        self.task(lambda:self.library.prepare(self.llm,config,self.download_cancel),lambda _:self.model_prepared('個体のLLMをスタンバイしました。STARTで開始できます。'))

    def missing_model(self):
        self.status.set('この個体が使用していたLLMが現在保存されていません。')
        dialog=tk.Toplevel(self.window);dialog.title('個体のLLMが見つかりません')
        ttk.Label(dialog,text=self.status.get(),padding=12).pack()
        def search():
            dialog.destroy();self.tabs.select(self.llm_tab)
            self.model_browser.query.set(self.config.model_name or '');self.model_browser.search()
        def choose():dialog.destroy();self.tabs.select(self.llm_tab)
        for label,command in [('同じモデルを検索',search),('別モデルを選択',choose),('キャンセル',dialog.destroy)]:ttk.Button(dialog,text=label,command=command).pack(side='left',padx=5,pady=10)

    def rename_individual(self):
        identifier=self.selected_individual()
        if not identifier or not self.idle_management():return
        name=simpledialog.askstring('個体名','新しい表示名',initialvalue=self.individuals.profile(identifier)['name'],parent=self.window)
        if name:self.individuals.rename(identifier,name);self.refresh_individual_list()

    def clone_individual(self):
        identifier=self.selected_individual()
        if not identifier or not self.idle_management():return
        answer=messagebox.askyesnocancel('個体の複製','完全複製しますか？\n「はい」：経験・研究・状態を完全複製\n「いいえ」：設定のみで新しい個体を作成',parent=self.window)
        if answer is None:return
        def done(new_id):
            self.refresh_individual_list();self.individual_tree.selection_set(new_id);self.status.set('個体を複製しました。ロードする個体を選べます。')
        self.task(lambda:self.individuals.clone(identifier,self.config,settings_only=not answer),done)

    def export_individual(self):
        identifier=self.selected_individual()
        if not identifier or not self.idle_management():return
        path=filedialog.asksaveasfilename(defaultextension='.zip',initialfile=self.individuals.profile(identifier)['name']+'.zip')
        if path:self.task(lambda:self.individuals.export(identifier,Path(path)),lambda _:self.status.set('個体の設定・経験・研究・チェックポイントをエクスポートしました。'))

    def delete_individual(self):
        identifier=self.selected_individual()
        if not identifier or not self.idle_management():return
        if identifier==self.individuals.active():messagebox.showinfo('削除できません','現在ロード中の個体です。別の個体をロードしてください。');return
        if messagebox.askyesno('個体を削除','選択個体の経験・研究・画像・内部状態を削除します。元に戻せません。\n共通LLM本体は残します。削除しますか？',parent=self.window):
            self.task(lambda:self.individuals.delete(identifier),lambda _:self.refresh_individual_list())

    def compare_individual(self):
        identifier=self.selected_individual();active=self.individuals.active()
        if not identifier or not active or self.busy:return
        def display(rows):
            text=[]
            for r in rows:text.append(r['profile']['name']+'\n'+json.dumps(r['counts'],ensure_ascii=False)+f"\n容量 {r['bytes']/1024**3:.3f} GB\nRevision {r['revision']}")
            put(self.individual_detail,'\n\n'.join(text)+'\n\n件数・状態の比較です。能力の優劣を証明する評価ではありません。')
        self.task(lambda:[self.individuals.summary(i) for i in (active,identifier)],display)

    def review_individual(self):
        identifier=self.selected_individual()
        if not identifier or not self.idle_management():return
        if not self.individuals.profile(identifier).get('settings_review_required'):
            messagebox.showinfo('設定を補完','この個体には保存済み設定があります。ロード後に設定タブで変更してください。');return
        if messagebox.askyesno('旧個体の設定を補完','設定タブに、その個体が使用していたゲーム・キーの順序・モード・LLMを合わせましたか？\n現在の設定をこの旧個体へ記録します。過去の設定を自動復元する操作ではありません。',parent=self.window):
            try:
                cfg=self.read_settings();self.individuals.save_settings(identifier,cfg,review=True);self.show_individual()
            except ValueError as exc:messagebox.showerror('設定の確認',str(exc))

    def _llm_ui(self):
        self.library=ModelLibrary(self.config.root)
        ttk.Label(self.llm_tab,text='モデル本体は共通資産です。使用するモデルと推論設定は現在の個体に保存します。',wraplength=1000).pack(anchor='w')
        panes=ttk.Panedwindow(self.llm_tab,orient='horizontal');panes.pack(fill='both',expand=True)
        left=ttk.Frame(panes,width=300);right=ttk.Frame(panes);panes.add(left,weight=1);panes.add(right,weight=3)
        ttk.Label(left,text='保存済みモデル').pack(anchor='w')
        self.saved_models=ttk.Treeview(left,columns=('quant','size','state'),show='tree headings',height=5,selectmode='browse')
        self.saved_models.heading('#0',text='モデル');self.saved_models.column('#0',width=180)
        for k,t in [('quant','量子化'),('size','容量'),('state','状態')]:self.saved_models.heading(k,text=t);self.saved_models.column(k,width=75)
        self.saved_models.pack(fill='both',expand=True);self.saved_models.bind('<<TreeviewSelect>>',self.show_model)
        controls=ttk.Frame(left);controls.pack(fill='x')
        for i,(label,cmd) in enumerate([('スタンバイ',lambda:self.use_saved_model(False)),('使用する',lambda:self.use_saved_model(True)),('停止',self.stop_model),('詳細',self.show_model),('削除',self.delete_model),('更新',self.refresh_saved_models)]):ttk.Button(controls,text=label,command=cmd).grid(row=i//3,column=i%3,sticky='ew')
        self.model_detail=tk.Text(left,height=8,wrap='word',background='#111920',foreground='#dce5ed',state='disabled');self.model_detail.pack(fill='both',expand=True)
        self.model_records={}
        from .model_browser import ModelBrowser
        self.model_browser=ModelBrowser(self,parent=right)
        options=ttk.Frame(self.llm_tab);options.pack(side='bottom',fill='x',pady=5)
        for label,cmd in [('おすすめ',lambda:self.open_models(True)),('手元のGGUF',self.import_model),('外部ローカルLLMへ接続',self.use_external_llm),('取得を中断',self.download_cancel.set)]:ttk.Button(options,text=label,command=cmd).pack(side='left',padx=2)
        settings=ttk.Frame(self.llm_tab);settings.pack(side='bottom',fill='x')
        self.fields['llm_url']=tk.StringVar(value=self.config.llm_url)
        self.fields['llm_context']=tk.StringVar(value=str(self.config.llm_context));self.fields['llm_threads']=tk.StringVar(value=str(self.config.llm_threads));self.llm_backend=tk.StringVar(value=self.config.llm_backend)
        for label,key,width in [('接続先','llm_url',35),('Context','llm_context',8),('CPU threads（0=自動）','llm_threads',4)]:
            ttk.Label(settings,text=label).pack(side='left');entry=ttk.Entry(settings,textvariable=self.fields[key],width=width);entry.pack(side='left',padx=3)
            if key=='llm_url':self.llm_url_entry=entry
        ttk.Combobox(settings,textvariable=self.llm_backend,values=['auto','cpu','vulkan'],state='readonly',width=8).pack(side='left')
        ttk.Button(settings,text='個体へ保存',command=self.save_settings).pack(side='left')
        self.progress=tk.StringVar(value='スタンバイはロード済み・即時利用可能な状態です。同時ロードは1モデルに制限します。')
        ttk.Label(self.llm_tab,textvariable=self.progress,wraplength=1000).pack(side='bottom',fill='x',pady=4)
        self.layout.pane('llm-management',panes,default_fraction=.35)
        panes.pack_forget();panes.pack(fill='both',expand=True)
        self.refresh_saved_models()

    def open_models(self,recommended=False):
        self.tabs.select(self.llm_tab)
        if recommended:
            self.model_browser.query.set('qwen coder');self.model_browser.tags['このPCで推奨'].set(True);self.model_browser.search()

    def model_name(self,identifier):
        state=getattr(self,'model_records',{}).get(identifier)
        if state:return state.get('model_info',{}).get('name',identifier[:12])
        return '未選択' if not identifier else '保存されていないモデル '+identifier[:12]

    def refresh_saved_models(self):
        selected=self.saved_models.selection();self.saved_models.delete(*self.saved_models.get_children())
        self.model_records={r['model_sha256']:r for r in self.library.list()}
        import re
        for identifier,state in self.model_records.items():
            info=state.get('model_info',{});name=info.get('name',identifier[:12]);quant=re.search(r'Q\d(?:_[A-Z0-9]+)*',info.get('file','')+' '+info.get('url','')+' '+name,re.I)
            state_name=self.library.status(state,self.llm,bool(self.controller and self.config.model_id==identifier and self.config.llm_url==MANAGED_URL))
            self.saved_models.insert('','end',iid=identifier,text=name,values=(info.get('quant') or (quant[0].upper() if quant else '不明'),f"{info.get('size',0)/1024**3:.2f} GB",state_name))
        if selected and self.saved_models.exists(selected[0]):self.saved_models.selection_set(selected)
        self.show_model()

    def show_model(self,_=None):
        selected=self.saved_models.selection()
        if not selected:return
        identifier=selected[0];state=self.model_records[identifier];info=state.get('model_info',{})
        size=info.get('size',0);pc=self.performance_monitor.hardware or {};suitability,reason=fit(size,pc)
        text=f"{info.get('name',identifier[:12])}\n作者：{info.get('name','').split('/')[0] or '未記録'}\n量子化：{self.saved_models.set(identifier,'quant')} / 規模：{info.get('parameters','未記録')}\nサイズ：{size/1024**3:.2f} GB\nContext：{state.get('context',8192)}（ロード時）\nRuntime：llama.cpp / {state.get('backend','未準備')}\nライセンス：{info.get('license','要確認')}\n更新日：{info.get('updated',info.get('lastModified','未記録'))}\nPC適合：{suitability}（目安）\n{reason}\nローカル検証：{json.dumps(state.get('validation',{}),ensure_ascii=False)}\n使用個体：{', '.join(self.library.users(identifier)) or 'なし'}"
        put(self.model_detail,text)

    def model_prepared(self,text):
        self.refresh_saved_models();self.progress.set(text);self.status.set(text)

    def use_saved_model(self,use):
        selected=self.saved_models.selection()
        if not selected or not self.idle_management():return
        identifier=selected[0]
        try:config=replace(self.read_settings(),model_id=identifier,llm_url=MANAGED_URL,model_name=self.model_name(identifier),model_quant=self.saved_models.set(identifier,'quant'))
        except ValueError as exc:messagebox.showerror('LLM設定',str(exc));return
        if use and config.agent_id:
            try:self.individuals.save_settings(config.agent_id,config,validate_only=True)
            except ValueError as exc:messagebox.showerror('個体設定',str(exc));return
        self.download_cancel.clear()
        def ready(_):
            if use:
                self.config=config;self.fields['llm_url'].set(MANAGED_URL)
                if config.agent_id:self.individuals.save_settings(config.agent_id,config)
                config.save(self.config_path)
            self.model_prepared('現在の個体に使用モデルを設定しました。' if use else 'モデルをスタンバイしました。個体の使用モデルは変更していません。')
        self.task(lambda:self.library.prepare(self.llm,config,self.download_cancel),ready)

    def stop_model(self):
        if not self.idle_management():return
        self.task(self.llm.stop,lambda _:self.model_prepared('モデルを停止し、VRAMを解放しました。'))

    def delete_model(self):
        selected=self.saved_models.selection()
        if not selected or not self.idle_management():return
        identifier=selected[0];users=self.library.users(identifier)
        if messagebox.askyesno('モデルを削除',f"モデル本体を削除します。個体データは残ります。\n参照個体：{', '.join(users) or 'なし'}\n削除しますか？",parent=self.window):self.task(lambda:self.library.delete(identifier,self.llm),lambda _:self.refresh_saved_models())
