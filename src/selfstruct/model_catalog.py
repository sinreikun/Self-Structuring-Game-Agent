"""Public GGUF discovery; metadata is untrusted and never executable code."""
import re
from urllib.parse import quote, urlencode
from .resources import fetch_json

PERMISSIVE={'apache-2.0','mit','bsd-2-clause','bsd-3-clause','isc','cc0-1.0'}
QUANTS=('Q4','Q5','Q6','Q8')
TAGS={
 '用途':['日本語','コード生成','推論','Instruct','長文対応','軽量','高性能'],
 'PC適合':['このPCで推奨','VRAM内に収まりやすい','RAM内に収まりやすい','CPU利用可能','〜4GB','〜8GB','〜12GB','〜16GB','16GB超'],
 'モデル規模':['〜3B','7B前後','14B前後','30B前後','70B以上'],
 '量子化':list(QUANTS),
 '配布条件':['ライセンス確認済','商用利用可','再配布可','要確認'],
 '並び順':['最近更新','人気','ダウンロード数が多い','新着']}


def repo_path(repo):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+',repo) or '..' in repo:raise ValueError('モデル配布元が不正です。')
    return quote(repo,safe='/')


def parameters(data):
    count=(data.get('safetensors') or {}).get('total') or (data.get('gguf') or {}).get('total')
    if isinstance(count,(int,float)):return count/1e9
    match=re.search(r'(\d+(?:\.\d+)?)\s*[bB](?:\b|[-_])',data.get('id',''))
    return float(match[1]) if match else None


def quantization(name):
    match=re.search(r'(?:^|[-_.])(Q\d(?:_[A-Z0-9]+)*|IQ\d(?:_[A-Z0-9]+)*|F16|BF16|F32)(?:[-.]|$)',name.upper())
    return match[1] if match else '不明'


def license_id(data):
    value=(data.get('cardData') or {}).get('license')
    if isinstance(value,list):value=', '.join(map(str,value))
    return str(value or next((x[8:] for x in data.get('tags',[]) if x.startswith('license:')),'要確認'))


def files(data):
    result=[]
    for entry in data.get('siblings',[]):
        name=entry.get('rfilename','');lfs=entry.get('lfs') or {}
        if not name.lower().endswith('.gguf'):continue
        result.append({'file':name,'size':int(lfs.get('size') or entry.get('size') or 0),'sha256':lfs.get('sha256',''),'quant':quantization(name),
                       'split':bool(re.search(r'-\d{5}-of-\d{5}\.gguf$',name,re.I))})
    return result


def fit(size,pc,context=8192):
    """Conservative estimate for the shipped CPU runtime; never a benchmark."""
    if not size:return '要確認','容量情報がありません'
    gb=size/1024**3;overhead=1.5+context/8192*1.5;need=gb+overhead
    ram=pc.get('ram',0);free=pc.get('free',0)
    if free and free<gb+.5:return '非推奨','空きディスク容量不足'
    if not ram:return '要確認','空きRAMを取得できません'
    vram=max((g.get('vram_gb',0) for g in pc.get('gpus',[])),default=0)
    detail=f'context {context} / 空きRAM {ram:.1f}GB / 専用VRAM {vram:.1f}GB。KVキャッシュ・ドライバー・速度はモデル依存'
    if vram>0 and ram>=3:
        if gb+overhead-1.5<max(0,vram-1):return '推奨','GPU推論の見込み / '+detail
        if need<ram+max(0,vram-2)-2:return 'CPU併用見込み','一部をVRAMへ転送する見込み / '+detail
    if need>ram:return '非推奨',f'推定RAM {need:.1f}GB / 空き {ram:.1f}GB'
    if need>ram*.8:return 'メモリ余裕少',f'推定RAM {need:.1f}GB'
    label='推奨' if need<ram*.65 and gb<8 else '動作見込み'
    return label,f'CPU推論の見込み / 推定RAM {need:.1f}GB / '+detail


def matches(data,selected,pc):
    text=' '.join([data.get('id',''),*data.get('tags',[])]).lower()
    mapping={'日本語':['japanese','ja','日本語'],'コード生成':['coder','code'],'推論':['reason','thinking'],'Instruct':['instruct','chat'],'長文対応':['long-context','longcontext','128k','32k']}
    for tag,words in mapping.items():
        if tag in selected and not any(w in text.split() if w=='ja' else w in text for w in words):return False
    scale=parameters(data)
    ranges={'〜3B':(0,3.9),'7B前後':(4,10),'14B前後':(10,20),'30B前後':(20,69.9),'70B以上':(70,float('inf'))}
    chosen=[v for k,v in ranges.items() if k in selected]
    if chosen and (scale is None or not any(lo<=scale<=hi for lo,hi in chosen)):return False
    if '軽量' in selected and (scale is None or scale>4):return False
    if '高性能' in selected and (scale is None or scale<14):return False # size proxy, not measured quality
    license=license_id(data).lower()
    if '要確認' in selected and license in PERMISSIVE:return False
    if selected.intersection({'ライセンス確認済','商用利用可','再配布可'}) and license not in PERMISSIVE:return False
    candidates=files(data)
    quants=selected.intersection(QUANTS)
    if quants:candidates=[f for f in candidates if any(f['quant'].startswith(q) for q in quants)]
    caps={'〜4GB':(0,4),'〜8GB':(0,8),'〜12GB':(0,12),'〜16GB':(0,16),'16GB超':(16,float('inf'))}
    limits=[v for k,v in caps.items() if k in selected]
    if limits:candidates=[f for f in candidates if any(lo<f['size']/1024**3<=hi for lo,hi in limits)]
    if 'このPCで推奨' in selected:candidates=[f for f in candidates if fit(f['size'],pc)[0]=='推奨']
    if 'RAM内に収まりやすい' in selected:candidates=[f for f in candidates if f['size']/1024**3+3<pc.get('ram',0)]
    if 'VRAM内に収まりやすい' in selected:
        vram=max((g.get('vram_gb',0) for g in pc.get('gpus',[])),default=0)
        candidates=[f for f in candidates if f['size']/1024**3+3<vram]
    requires_files=bool(quants or limits or selected.intersection(TAGS['PC適合']))
    return bool(candidates) if requires_files else True


class ModelCatalog:
    def search(self,query,selected=frozenset(),pc=None):
        sort='lastModified' if '最近更新' in selected else 'createdAt' if '新着' in selected else 'likes' if '人気' in selected else 'downloads'
        params={'search':query.strip()[:160],'filter':'gguf','sort':sort,'direction':-1,'limit':40,'full':'true','config':'true'}
        rows=fetch_json('https://huggingface.co/api/models?'+urlencode(params))
        from concurrent.futures import ThreadPoolExecutor
        # Hydrate bounded candidates so every displayed result has real file metadata.
        candidates=[item for item in rows if matches(item,set(selected)-set(TAGS['PC適合'])-set(QUANTS),pc or {})]
        def hydrate(item):
            try:return self.detail(item['id'])
            except (OSError,ValueError,KeyError):return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            details=list(pool.map(hydrate,candidates))
        results=[item for item in details if item and files(item) and matches(item,set(selected),pc or {})]
        return results

    def detail(self,repo):return fetch_json(f'https://huggingface.co/api/models/{repo_path(repo)}?blobs=true')

    def resolve(self,selected):
        data=self.detail(selected['repo']);entry=next((x for x in files(data) if x['file']==selected['file']),None)
        if not entry or entry['split']:raise ValueError('単一GGUFファイルを選択してください。分割モデルは未対応です。')
        license=license_id(data)
        if license=='要確認' and not selected.get('license_ack'):raise ValueError('モデルの配布条件を確認してください。')
        sha=data.get('sha','')
        if not re.fullmatch('[0-9a-f]{40,64}',sha):raise ValueError('固定Revisionを確認できません。')
        return {'name':data['id'],'author':data['id'].split('/')[0],'file':entry['file'],'quant':entry['quant'],'parameters':parameters(data),'updated':data.get('lastModified','未記録'),'version':sha,'license':license,'trust':'Verified Candidate','size':entry['size'],'sha256':entry['sha256'],
                'url':f'https://huggingface.co/{repo_path(data["id"])}/resolve/{sha}/{quote(entry["file"],safe="/")}'}
