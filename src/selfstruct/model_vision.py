"""Pair only an exact, immutable HF model revision with its generic projector.

No model-name capability guesses: the running engine still decides whether image
input is supported. Failure preserves text input and an explicit diagnostic.
"""
import re
import time
from urllib.parse import quote, urlparse

from .resources import Gateway, fetch_json


def resolve_projector(info):
    url=urlparse(info.get('url',''))
    match=re.fullmatch(r'/([\w.-]+/[\w.-]+)/resolve/([0-9a-f]{40,64})/(.+\.gguf)',url.path)
    if url.scheme!='https' or url.netloc!='huggingface.co' or not match:return None
    repo, revision, filename=match.groups()
    if revision!=info.get('version') or not re.fullmatch('[0-9a-f]{64}',info.get('sha256','')):return None
    data=fetch_json(f'https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true')
    if data.get('sha')!=revision:raise ValueError('視覚資産の固定Revisionが一致しません。')
    entries={f['rfilename']:f for f in data.get('siblings',[])}
    weight=entries.get(filename,{})
    if weight.get('lfs',{}).get('sha256')!=info['sha256']:raise ValueError('視覚資産の対応モデルが一致しません。')
    # Multiple unrelated projectors in one repository are ambiguous. Only the
    # conventional single-model root assets are accepted, never filename guessing.
    choices=('mmproj-F16.gguf','mmproj-BF16.gguf','mmproj-F32.gguf')
    if any('/' not in name and name.lower().startswith('mmproj') and name.lower().endswith('.gguf') and name not in choices for name in entries):return None
    for name in choices:
        entry=entries.get(name)
        if not entry:continue
        meta=entry.get('lfs',{})
        if not re.fullmatch('[0-9a-f]{64}',meta.get('sha256','')) or type(meta.get('size'))!=int or meta['size']<=0:
            raise ValueError('視覚資産の完全性情報がありません。')
        return {'name':repo+' / '+name,'version':revision,'role':'vision_projector',
                'paired_model_sha256':info['sha256'],'license':info.get('license','要確認'),
                'trust':info.get('trust','Verified Candidate'),'size':meta['size'],'sha256':meta['sha256'],
                'url':f'https://huggingface.co/{repo}/resolve/{revision}/{quote(name)}'}
    return None


def prepare_projector(manager,state,progress=lambda done,total:None,cancel=None):
    """One resolution per immutable model; no network on ordinary inference."""
    cached=state.get('vision_resolution',{})
    if state.get('projector') or (cached.get('model_sha256')==state['model_sha256'] and cached.get('status') in ('paired','not_available','runtime_failed')):return state
    info=state.get('model_info') or {}
    if not info.get('url'):return state
    state=dict(state)
    resolution={'model_sha256':state['model_sha256'],'checked_at':time.time()}
    try:
        if info.get('sha256')!=state['model_sha256']:raise ValueError('モデルの完全性情報が一致しません。')
        item=resolve_projector(info)
        if item:
            path=Gateway(manager.root,manager.quota).download(item,'library',progress,cancel)
            state.update(projector=str(path),projector_sha256=item['sha256'],projector_info=item)
        resolution['status']='paired' if item else 'not_available'
    except InterruptedError:raise
    except (OSError,ValueError,KeyError) as exc:
        resolution.update(status='unavailable',error=type(exc).__name__+': '+str(exc)[:500])
    state['vision_resolution']=resolution
    return state


def text_fallback(state,error):
    state={k:v for k,v in state.items() if k not in ('projector','projector_sha256','projector_info')}
    state['vision_resolution']={'model_sha256':state['model_sha256'],'status':'runtime_failed',
                                'error':type(error).__name__+': '+str(error)[:500],'checked_at':time.time()}
    return state
