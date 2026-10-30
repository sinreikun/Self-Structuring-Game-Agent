"""Bounded, allowlisted, redacted application diagnostics; never upload automatically."""
from dataclasses import asdict, is_dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import tempfile
import zipfile

from . import __version__

MAX_ZIP_INPUT=24*1024**2


def redact(value):
    if isinstance(value,dict):
        return {k:('[REDACTED]' if re.search(r'password|secret|token|credential|api.?key|authorization|catalog_key',k,re.I) else redact(v)) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [redact(v) for v in value]
    if not isinstance(value,str): return value
    value=re.sub(r'(?i)(Bearer\s+)[^\s"\']+',r'\1[REDACTED]',value)
    value=re.sub(r'(?i)((?:password|api[_-]?key|access[_-]?token|secret|authorization)\s*[=:]\s*)[^\s,;]+',r'\1[REDACTED]',value)
    value=re.sub(r'(?i)([A-Z]:[\\/]Users[\\/])[^\\/\s"\']+',r'\1[user]',value)
    value=re.sub(r'(/(?:home|Users)/)[^/\s"\']+',r'\1[user]',value)
    value=re.sub(r'https?://[^\s"\']+',lambda m: m[0].split('?')[0] if '@' not in m[0] else '[private URL]',value)
    value=re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})\b','[REDACTED]',value)
    return value


def create_diagnostics(root: Path, store=None, config=None, activity=None, gui=None, destination=None) -> Path:
    root=Path(root).resolve()
    folder=root/'exports'/'diagnostics';folder.mkdir(parents=True,exist_ok=True)
    destination=Path(destination) if destination else folder/f'SelfStruct-{__version__}-{datetime.now():%Y%m%d-%H%M%S-%f}.zip'
    from .presentation import system_info,gpu_info
    from importlib.metadata import version,PackageNotFoundError
    runtime={}
    for package in ('wasmtime','Pillow','cryptography'):
        try:runtime[package]=version(package)
        except PackageNotFoundError:runtime[package]='unknown'
    system=system_info()
    system.update(os=platform.platform(),gpu=gpu_info())
    snapshot=asdict(activity) if is_dataclass(activity) else activity
    info={'version':__version__,'launcher_version':os.environ.get('SELFSTRUCT_LAUNCHER_VERSION','未使用'),
          'system':system,'runtime':runtime,'config':asdict(config) if is_dataclass(config) else config,
          'activity':snapshot,'gui':gui,'state':'diagnostic snapshot', 'limitations':['画像にはゲーム内の表示内容が含まれます。']}
    if store:
        info.update(storage=store.storage_snapshot(),breakdown=store.storage_breakdown(),counts=store.counts())
    budget=MAX_ZIP_INPUT
    if store:
        budget=min(budget,max(65536,store.quota-store.usage(force=True)-65536))
    tmp=destination.with_suffix('.pending')
    try:
        with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as z:
            def put(name,data):
                nonlocal budget
                if isinstance(data,str): data=data.encode('utf-8')
                if len(data)>budget:return False
                z.writestr(name,data);budget-=len(data);return True
            put('diagnosis.json',json.dumps(redact(info),ensure_ascii=False,indent=2))
            # Curated SQLite, not a raw dump of arbitrary Agent files/prompts/secrets.
            if store:
                with store.lock, tempfile.TemporaryDirectory() as temp:
                    db=sqlite3.connect(Path(temp)/'diagnostics.sqlite3')
                    try:
                        for table,order,limit in [('events','id',512),('knowledge','id',256),('reports','id',100),('gc_history','id',200),('compacted','last',256),('jobs','updated',40)]:
                            columns=[r[1] for r in store.db.execute(f'PRAGMA table_info({table})')]
                            # All values are bound; never execute user content.
                            db.execute(f'CREATE TABLE {table}('+','.join('"'+c+'"' for c in columns)+')')
                            for row in store.db.execute(f'SELECT * FROM {table} ORDER BY {order} DESC LIMIT ?',(limit,)):
                                values=[]
                                for value in row:
                                    if table=='jobs' and columns[len(values)]=='payload':value='[prompt/source omitted]'
                                    if isinstance(value,str):
                                        try: value=json.dumps(redact(json.loads(value)),ensure_ascii=False)
                                        except (ValueError,TypeError): value=redact(value)
                                        value=value[:16000]
                                    values.append(value)
                                db.execute(f'INSERT INTO {table} VALUES('+','.join('?' for _ in columns)+')',values)
                        db.commit();db.close()
                        put('diagnostics.sqlite3',(Path(temp)/'diagnostics.sqlite3').read_bytes())
                    finally:
                        db.close()
                put('研究ノート.md','\n'.join(redact(r['text']) for r in store.note_rows('現在',64)))
                hashes=[]
                for row in store.rows(12):
                    hashes.extend([row['before_hash'],row['after_hash']])
                for digest in dict.fromkeys(hashes):
                    if not re.fullmatch('[a-f0-9]{64}',digest or ''): continue
                    path=store.root/'evidence'/(digest+'.png')
                    if path.is_file() and path.stat().st_size<=2*1024**2:
                        put('evidence/'+path.name,path.read_bytes())
                for filename in ('checkpoint.json','activity.json'):
                    path=store.root/'host'/filename
                    if path.is_file() and path.stat().st_size<65536:
                        try:put('experiment/'+filename,json.dumps(redact(json.loads(path.read_text(encoding='utf-8'))),ensure_ascii=False))
                        except (OSError,ValueError):pass
                path=store.root/'agents'/'revisions'/'state.json'
                if path.is_file() and path.stat().st_size<65536:
                    try:put('experiment/revisions.json',json.dumps(redact(json.loads(path.read_text(encoding='utf-8'))),ensure_ascii=False))
                    except (OSError,ValueError):pass
            # Metadata only: never include the downloaded artifacts themselves.
            for kind in ('model','engine'):
                receipts=sorted((root/'assets'/kind).glob('*/receipt.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:2]
                for index,path in enumerate(receipts):
                    if path.is_symlink() or path.stat().st_size>16384:continue
                    try:
                        receipt=json.loads(path.read_text(encoding='utf-8'))
                        safe={k:receipt.get(k) for k in ('name','version','license','trust','size','sha256')}
                        put(f'assets-metadata/{kind}-{index}.json',json.dumps(redact(safe),ensure_ascii=False))
                    except (OSError,ValueError):pass
            # Explicit allowlist: no model/runtime, environment dump, arbitrary Agent files.
            for name in ('host.log','host.log.1','llm.log','llm.json'):
                path=root/'host'/name
                if path.is_file() and not path.is_symlink():
                    with path.open('rb') as f:
                        f.seek(max(0,path.stat().st_size-256*1024));data=f.read(256*1024).decode('utf-8',errors='replace')
                    if path.suffix=='.json':
                        try: data=json.dumps(redact(json.loads(data)),ensure_ascii=False)
                        except ValueError: data='[invalid metadata]'
                    put('host/'+name,redact(data))
            put('README.txt','診断専用の件数制限付きDBです。全経験の復元用ではありません。モデル・推論エンジン・認証情報は収集しません。画像にはゲーム内の個人情報が表示される場合があります。送信前に内容をご確認ください。')
        os.replace(tmp,destination)
    finally:
        tmp.unlink(missing_ok=True)
    return destination


def copy_file(path: Path, owner: int = 0):
    """CF_HDROP (wide DROPFILES), ownership transfers only on successful SetClipboardData."""
    if os.name!='nt': raise OSError('ファイルのコピーはWindowsで利用できます。')
    import ctypes as c
    from ctypes import wintypes as w
    import struct
    kernel=c.WinDLL('kernel32',use_last_error=True);user=c.WinDLL('user32',use_last_error=True)
    kernel.GlobalAlloc.argtypes=[w.UINT,c.c_size_t];kernel.GlobalAlloc.restype=w.HGLOBAL
    kernel.GlobalLock.argtypes=[w.HGLOBAL];kernel.GlobalLock.restype=c.c_void_p
    kernel.GlobalUnlock.argtypes=[w.HGLOBAL];kernel.GlobalFree.argtypes=[w.HGLOBAL]
    user.OpenClipboard.argtypes=[w.HWND];user.SetClipboardData.argtypes=[w.UINT,w.HANDLE];user.SetClipboardData.restype=w.HANDLE
    payload=struct.pack('<IiiII',20,0,0,0,1)+(str(path.resolve())+'\0\0').encode('utf-16le')
    handle=kernel.GlobalAlloc(0x42,len(payload))
    if not handle:raise OSError('Clipboard allocation failed')
    transferred=False
    try:
        pointer=kernel.GlobalLock(handle)
        if not pointer:raise OSError('Clipboard lock failed')
        c.memmove(pointer,payload,len(payload));kernel.GlobalUnlock(handle)
        if not user.OpenClipboard(owner):raise OSError('Clipboard is busy')
        try:
            if not user.EmptyClipboard() or not user.SetClipboardData(15,handle):raise OSError('Clipboard copy failed')
            transferred=True
        finally:user.CloseClipboard()
    finally:
        if not transferred:kernel.GlobalFree(handle)
