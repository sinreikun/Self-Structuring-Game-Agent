"""Independent Tk model browser; all remote work stays outside Tk callbacks."""
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk,messagebox

from .model_catalog import ModelCatalog,TAGS,files,parameters,license_id,fit,PERMISSIVE
from .presentation import error_message,system_info


class ModelBrowser:
    def __init__(self,app,recommended=False):
        self.app=app;self.catalog=ModelCatalog();self.events=queue.Queue();self.rows=[];self.current=None;self.running=False;self.selection=0
        self.window=tk.Toplevel(app.window);self.window.title('SelfStruct — GGUFモデルブラウザ');self.window.geometry('1120x860');self.window.minsize(900,700)
        app.layout.window('model-browser',self.window)
        self.pc=app.performance_monitor.hardware or system_info()
        top=ttk.Frame(self.window,padding=10);top.pack(fill='x')
        self.query=tk.StringVar(value='qwen coder' if recommended else '')
        ttk.Entry(top,textvariable=self.query,width=50).pack(side='left',fill='x',expand=True)
        ttk.Button(top,text='検索',command=self.search).pack(side='left');self.window.bind('<Return>',lambda _:self.search())
        tags=ttk.Frame(self.window,padding=(10,0));tags.pack(fill='x');self.tags={}
        row=0
        for group,labels in TAGS.items():
            ttk.Label(tags,text=group,width=10).grid(row=row,column=0,sticky='w')
            container=ttk.Frame(tags);container.grid(row=row,column=1,sticky='w')
            for index,label in enumerate(labels):
                var=tk.BooleanVar();self.tags[label]=var
                ttk.Checkbutton(container,text=label,variable=var,style='Toolbutton').grid(row=index//7,column=index%7,sticky='w',padx=1,pady=2)
            row+=1
        if recommended:self.tags['このPCで推奨'].set(True)
        ttk.Label(self.window,text=f'CPU：{system_info()["cpu"]} / 空きRAM {self.pc.get("ram",0):.1f}GB / ディスク {self.pc.get("free",0):.1f}GB',wraplength=1050).pack(fill='x',padx=10)
        self.status=tk.StringVar(value='GGUF配布メタデータで検索します。適合度は目安で、速度や研究成功を保証しません。')
        ttk.Label(self.window,textvariable=self.status,wraplength=1050).pack(fill='x',padx=10,pady=6)
        panes=ttk.Panedwindow(self.window,orient='vertical');panes.pack(fill='both',expand=True,padx=10)
        upper=ttk.Frame(panes);lower=ttk.Frame(panes);panes.add(upper,weight=1);panes.add(lower,weight=1)
        columns=('author','scale','updated','downloads','license','quants','size','fit')
        self.tree=ttk.Treeview(upper,columns=columns,show='tree headings',height=7)
        self.tree.heading('#0',text='モデル名 / GGUF');self.tree.column('#0',width=280)
        for key,label in zip(columns,['作者','規模','更新日','DL数','ライセンス','量子化','ファイル容量','PC適合']):
            self.tree.heading(key,text=label);self.tree.column(key,width=105,stretch=False)
        scroll=ttk.Scrollbar(upper,orient='horizontal',command=self.tree.xview);self.tree.configure(xscrollcommand=scroll.set)
        self.tree.pack(fill='both',expand=True);scroll.pack(fill='x');self.tree.bind('<<TreeviewSelect>>',self.select)
        self.detail=tk.Text(lower,height=8,wrap='word',background='#10171d',foreground='#dce5ed',state='disabled');self.detail.pack(fill='both',expand=True)
        self.file=tk.StringVar();self.file_choice=ttk.Combobox(lower,textvariable=self.file,state='readonly');self.file_choice.pack(fill='x',pady=6)
        self.file_choice.bind('<<ComboboxSelected>>',self.file_selected)
        self.file_status=tk.StringVar();ttk.Label(lower,textvariable=self.file_status,wraplength=1000).pack(fill='x')
        self.ack=tk.BooleanVar();ttk.Checkbutton(lower,text='配布元のモデルカード・ライセンス条件を確認しました',variable=self.ack).pack(anchor='w')
        ttk.Button(lower,text='選択した量子化を導入・推論試験',command=self.install).pack(anchor='e',pady=8)
        ttk.Label(self.window,text='ライセンス絞り込みは既知の寛容ライセンス識別子に基づく目安です。独自条件はモデルカードで確認してください。\n高性能＝14B以上の規模目安。VRAM条件は容量比較であり、現在の管理RuntimeはCPU版です。',wraplength=1050).pack(fill='x',padx=10,pady=6)
        app.layout.pane('model-browser-panes',panes)
        self.window.after(100,self.poll)
        if recommended:self.search()

    def worker(self,kind,operation):
        def run():
            try:self.events.put((kind,operation()))
            except Exception as exc:self.events.put(('error',error_message(exc)))
        threading.Thread(target=run,name='model-browser',daemon=True).start()

    def search(self):
        if self.running:return
        self.running=True;self.selection+=1;self.current=None;self.file_choice['values']=();self.file.set('');self.ack.set(False)
        self.status.set('Hugging Faceの公開GGUF情報を検索中…')
        query=self.query.get();tags={k for k,v in self.tags.items() if v.get()}
        # Multiword search on the API is literal, so recommend a family and filter purpose locally.
        if query.lower()=='qwen coder':query='qwen';tags.add('コード生成')
        self.worker('search',lambda:self.catalog.search(query,tags,self.pc))

    def select(self,_=None):
        selected=self.tree.selection()
        if not selected or self.running:return
        self.selection+=1;generation=self.selection;self.current=None;self.ack.set(False);self.file.set('');self.file_choice['values']=()
        repo=self.rows[int(selected[0])]['id'];self.status.set('GGUFファイルとモデルカードを取得中…')
        def detail():
            data=self.catalog.detail(repo)
            # README is bounded and rendered as plain text, never HTML or executable content.
            from .resources import check_url,TRUSTED_HOSTS,SafeRedirect
            from urllib.request import Request,build_opener
            from urllib.parse import quote
            url=f'https://huggingface.co/{repo}/raw/{data["sha"]}/README.md';check_url(url,TRUSTED_HOSTS)
            try:
                with build_opener(SafeRedirect(TRUSTED_HOSTS)).open(Request(url,headers={'User-Agent':'SelfStruct'}),timeout=20) as reply:
                    data['readme']=reply.read(24000).decode('utf-8',errors='replace')
            except OSError:data['readme']='モデルカードを取得できません。配布元でライセンスを確認してください。'
            data['readme']=re.sub(r'\A---\s*\n.*?\n---\s*\n','',data.get('readme',''),count=1,flags=re.S)
            return generation,data
        self.worker('detail',detail)

    def poll(self):
        if not self.window.winfo_exists():return
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='error':self.running=False;self.status.set(value)
                elif kind=='search':
                    self.running=False;self.rows=value;self.tree.delete(*self.tree.get_children())
                    for i,row in enumerate(value):
                        scale=parameters(row);entries=files(row);size=min((x['size'] for x in entries if x['size']),default=0)
                        self.tree.insert('', 'end',iid=str(i),text=row['id']+' / GGUF',values=(row['id'].split('/')[0],f'{scale:g}B' if scale else '不明',str(row.get('lastModified',''))[:10],row.get('downloads',0),license_id(row),', '.join(sorted({x['quant'] for x in entries})) or '選択して取得',f'{size/1024**3:.2f}GB〜' if size else '選択して取得',fit(size,self.pc)[0]))
                    self.status.set(f'{len(value)}件 / 最大40候補内で絞り込み。条件を変えて検索できます。')
                elif kind=='detail':
                    generation,data=value
                    if generation!=self.selection:continue
                    self.current=data;self.available=sorted([x for x in files(data) if not x['split']],key=lambda x:(x['quant']!='Q4_K_M',x['size']))
                    self.file_labels={f'{x["quant"]} / {x["size"]/1024**3:.2f}GB / {x["file"]}':x for x in self.available}
                    self.file_choice['values']=list(self.file_labels)
                    if self.file_labels:self.file.set(next(iter(self.file_labels)))
                    self.detail.configure(state='normal');self.detail.delete('1.0','end')
                    from .model_catalog import matches
                    purposes=[tag for tag in TAGS['用途'][:5] if matches(data,{tag},self.pc)]
                    text=f'{data["id"]}\n作者：{data["id"].split("/")[0]} / 更新：{data.get("lastModified", "不明")}\nライセンス：{license_id(data)} / 用途の目安（名称・配布者タグ）：{"、".join(purposes) or "未確認"}\n配布元：https://huggingface.co/{data["id"]}\n\nREADME概要（冒頭）\n{data.get("readme", "")[:12000]}'
                    state_path=self.app.llm.state_path
                    if state_path.exists():
                        import json
                        try:
                            state=json.loads(state_path.read_text(encoding='utf-8'))
                            info=state.get('model_info',{})
                            if info.get('name')==data['id']:
                                validation=state.get('validation',{})
                                text+='\n\nこのPCでの導入後検証：'+('起動・最小推論 成功' if validation.get('minimal_inference') else '未記録')+' / 日本語品質・JSON・コード生成は未評価'
                        except (OSError,ValueError):pass
                    self.detail.insert('1.0',text);self.detail.configure(state='disabled');self.file_selected()
                    self.status.set('詳細を取得しました。量子化・容量・ライセンスを確認してください。分割GGUFは導入対象外です。')
                    selected=self.tree.selection()
                    if selected:
                        entry=self.file_labels.get(self.file.get());size=entry['size'] if entry else 0
                        self.tree.set(selected[0],'quants',', '.join(sorted({x['quant'] for x in self.available})))
                        self.tree.set(selected[0],'size',f'{size/1024**3:.2f}GB');self.tree.set(selected[0],'fit',fit(size,self.pc)[0])
        except queue.Empty:pass
        self.window.after(100,self.poll)

    def file_selected(self,_=None):
        entry=getattr(self,'file_labels',{}).get(self.file.get())
        if entry:
            label,reason=fit(entry['size'],self.pc);self.file_status.set(f'{label}（目安）— {reason}')

    def install(self):
        entry=getattr(self,'file_labels',{}).get(self.file.get())
        if not self.current or not entry:return
        if not self.ack.get():messagebox.showinfo('配布条件','モデルカードとライセンスを確認し、確認欄を選んでください。',parent=self.window);return
        if not entry['sha256']:messagebox.showerror('完全性情報なし','SHA-256がないファイルは導入できません。',parent=self.window);return
        self.app.install_model({'repo':self.current['id'],'file':entry['file'],'name':self.current['id'],'license_ack':True})
        self.app.tabs.select(self.app.setup)
        self.status.set('初回設定画面で導入進捗を確認できます。')
