import sys,json,sqlite3,threading,argparse
from pathlib import Path
from types import SimpleNamespace
from selfstruct.store import Store
from selfstruct.research import Scientist,LocalLLM
from selfstruct.experiments import Experiments
parser=argparse.ArgumentParser(description='Offline single-link prompt budget check; never generates or operates a game.')
parser.add_argument('snapshot',type=Path)
parser.add_argument('output',type=Path)
parser.add_argument('--tag',default='after')
parser.add_argument('--url',default='http://127.0.0.1:18766/v1')
parser.add_argument('--job',default='ccb44405')
parser.add_argument('--settings',type=Path,required=True)
args=parser.parse_args()
out=args.output;out.mkdir(parents=True,exist_ok=True);tag=args.tag
db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
source=sqlite3.connect(args.snapshot.resolve().as_uri()+'?mode=ro',uri=True)
source.backup(db);source.close()
j=db.execute("select id,payload from jobs where id like ?",(args.job+"%",)).fetchone()
p=json.loads(j['payload']);jid=j['id']
(out/'original-payload.json').write_text(json.dumps(p,ensure_ascii=False,indent=2),encoding='utf-8')
for field in ('research_calls','visual_rounds','image_bytes'):p[field]=0
for field in ('action_history','visual_history','research_steps','memory_results','inline_claim_receipts'):p[field]=[]
for field in ('research_limit','outcome','repair_response','repair_error','link_end'):p.pop(field,None)
import time
p['research_started']=time.time()
class Captured(BaseException):pass
client=LocalLLM(args.url,'local');client.managed=True;client.vision=True;client.context=8192
client.health=lambda:None
client.capabilities={'input':'vision'}
s=Scientist.__new__(Scientist)
s.store=Store.__new__(Store);s.store.db=db;s.store.lock=threading.RLock();s.store.root=out/'replay-root'
s.store.root.mkdir(exist_ok=True);(s.store.root/'host').mkdir(exist_ok=True)
s.store.reserve=lambda *a:None
s.keys=json.loads(args.settings.read_text(encoding='utf-8'))['keys']
s.experiments=Experiments(s.store,s.keys,None);s.client=client;s.stop_event=threading.Event()
s.activity=SimpleNamespace(update=lambda **kw:None,record=lambda *a:None)
def capture(text,**kw):
    (out/(tag+'-prompt.txt')).write_text(text,encoding='utf-8')
    (out/(tag+'-budget.json')).write_text(json.dumps(p['context_budget'],indent=2),encoding='utf-8')
    raise Captured()
s.complete=capture
try:s.visual_dialogue('',jid,p,2048,lambda _:None)
except Captured:pass
text=(out/(tag+'-prompt.txt')).read_text(encoding='utf-8')
markers=['対象は、','担当リンクの記録','\n根拠IDは','\n担当リンクの現在記録：','\n担当外への','\n参照中の仮説','\n今回の新規','\n状況候補agenda','\n研究履歴：','\n追加観測結果','\n省略情報：','\n今回の手順：']
positions=sorted({0,len(text)}|{text.index(m) for m in markers if m in text})
parts=[]
prev=client.count_tokens('')
for a,b in zip(positions,positions[1:]):
    current=client.count_tokens(text[:b])
    parts.append({'element':text[a:b].lstrip().splitlines()[0][:55],'tokens':current-prev,'chars':b-a})
    prev=current
result={'budget':p['context_budget'],'tokenizer_empty':client.count_tokens(''),'components':parts}
(out/(tag+'-measurement.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
assert len(parts)==13
if tag!='before':
    assert result['budget']['input_count']<=4500
    assert 8192-result['budget']['input_count']-256-2*1024>=1024
for table in ('research_evaluations','research_reports'):
    print(table,db.execute('select count(*) from '+table).fetchone()[0])

