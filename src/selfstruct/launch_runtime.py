"""Stable bootstrap. Application code is never imported by the launcher."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from .config import InstanceLock, default_root, write_json
from .resources import Gateway, fetch_json, verify_catalog

LAUNCHER_VERSION='1.0.0'


def stop_owned_process(child):
    """PyInstaller onefile has a bootloader parent and a GUI child on Windows."""
    if child.poll() is not None:return
    if os.name=='nt' and isinstance(child,subprocess.Popen):
        command=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32'/'taskkill.exe'
        try:
            result=subprocess.run([str(command),'/PID',str(child.pid),'/T','/F'],capture_output=True,
                                  timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode and child.poll() is None:raise OSError('Process tree did not stop')
            child.wait(timeout=10)
        except Exception as error:
            raise RuntimeError('新しい本体の終了を確認できませんでした。二重起動を避けて起動を中断しました。') from error
    else:
        child.terminate();child.wait(timeout=10)


class Launcher:
    def __init__(self, root, key, url, channel='Development', fetch=fetch_json, spawn=subprocess.Popen, timeout=60):
        self.root=Path(root)/'runtime';self.root.mkdir(parents=True,exist_ok=True)
        self.key=key;self.url=url;self.channel=channel;self.fetch=fetch;self.spawn=spawn;self.timeout=timeout
        self.state_path=self.root/'state.json';self.message=''
        try:self.state=json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.state={}
        if not isinstance(self.state,dict):self.state={}

    def save(self): write_json(self.state_path,self.state)

    def status(self):
        write_json(self.root/'launcher-status.json',{'version':LAUNCHER_VERSION,'message':self.message,'at':time.time()})

    def verified(self, identity):
        if not isinstance(identity,str) or not re.fullmatch(r'[0-9A-Za-z._-]{1,128}',identity):return None
        folder=self.root/'versions'/identity
        try:
            manifest=verify_catalog(json.loads((folder/'manifest.json').read_text(encoding='utf-8')),self.key,allow_expired=True)
            if manifest.get('channel')!=self.channel or manifest.get('protocol')!=1:return None
            item=manifest['application'];binary=folder/'SelfStruct.exe'
            if binary.is_symlink() or binary.stat().st_size!=item['size'] or Gateway.digest(binary)!=item['sha256']:return None
            return binary
        except Exception:return None

    def collect_versions(self):
        keep={self.state.get(k) for k in ('current','last-known-good','previous-known-good','pending')}
        for folder in (self.root/'versions').iterdir():
            if folder.name in keep or folder.is_symlink() or not folder.is_dir():continue
            try:shutil.rmtree(folder)
            except OSError:pass  # A version still running on Windows is retained.

    def install(self, envelope):
        manifest=verify_catalog(envelope,self.key)
        if manifest.get('protocol')!=1 or manifest.get('channel')!=self.channel:raise ValueError('Unknown update protocol or channel')
        version=manifest['version']
        if not isinstance(version,str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:-dev\.\d+)?',version):raise ValueError('Invalid version')
        item=manifest['application'];identity=version+'-'+item['sha256'][:16]
        if identity==self.state.get('failed') or self.verified(identity):return identity
        artifact=Gateway(self.root,2*1024**3).download(item,'update')
        folder=self.root/'versions'/identity;folder.mkdir(parents=True,exist_ok=True)
        binary=folder/'SelfStruct.exe'
        shutil.copyfile(artifact,binary.with_suffix('.pending'))
        if Gateway.digest(binary.with_suffix('.pending'))!=item['sha256']:raise ValueError('Staging digest mismatch')
        os.replace(binary.with_suffix('.pending'),binary)
        write_json(folder/'manifest.json',envelope)
        # Download cache is regenerable. Version executables never overwritten in use.
        artifact.unlink(missing_ok=True)
        return identity

    def launch(self, identity):
        binary=self.verified(identity)
        if not binary:raise ValueError('Local application integrity failure')
        marker=self.root/('health-'+uuid.uuid4().hex)
        env=dict(os.environ,SELFSTRUCT_LAUNCHER_VERSION=LAUNCHER_VERSION)
        child=self.spawn([str(binary),'--health-marker',str(marker)],env=env)
        deadline=time.monotonic()+self.timeout
        healthy=False
        try:
            while time.monotonic()<deadline:
                if child.poll() is not None:return False
                if marker.is_file() and marker.read_text(encoding='utf-8')=='healthy':
                    healthy=True;return True
                time.sleep(.1)
            return False
        finally:
            marker.unlink(missing_ok=True)
            if not healthy and child.poll() is None:
                stop_owned_process(child)

    def run(self):
        with_lock=InstanceLock(self.root/'launcher.lock')
        try:
            if self.state.get('pending'):
                self.state.update(failed=self.state['pending'],pending=None);self.save()
            stable=self.state.get('last-known-good')
            if not self.verified(stable):stable=self.state.get('previous-known-good')
            candidate=None
            try:
                envelope=self.fetch(self.url)
                manifest=verify_catalog(envelope,self.key)
                # Monotonic publication sequence prevents a stale manifest rollback.
                sequence=manifest.get('sequence',0)
                if type(sequence)!=int or sequence<self.state.get('sequence',0):raise ValueError('Stale manifest')
                candidate=self.install(envelope)
            except Exception:
                self.message='更新を確認できませんでした。現在の安定版で起動します。'
            # A body that crashed before health was acknowledged is not retried automatically.
            if candidate and candidate==self.state.get('failed'):candidate=None
            if candidate and candidate!=stable:
                self.state['pending']=candidate;self.save()
                self.status()
                try: healthy=self.launch(candidate)
                except (OSError,TimeoutError): healthy=False
                if healthy:
                    self.state.update(current=candidate,**{'last-known-good':candidate,'previous-known-good':stable},pending=None,sequence=sequence)
                    self.save();self.collect_versions();return True
                self.state.update(failed=candidate,pending=None);self.save()
                self.message='新しい版の起動に失敗したため、前の安定版へ戻しました。'
            if stable and self.verified(stable):
                self.state.update(current=stable,pending=None)
                if candidate==stable:self.state['sequence']=sequence
                self.save();self.collect_versions()
                self.status()
                return self.launch(stable)
            raise RuntimeError('利用できる本体がありません。初回起動には公式配布元への接続が必要です。')
        finally:
            self.status()
            with_lock.close()


def main():
    from .release import UPDATE_KEY, UPDATE_URL
    from .config import Config
    channel=Config.load(default_root()/'host'/'settings.json').channel
    launcher=Launcher(default_root(),UPDATE_KEY,UPDATE_URL.format(channel=channel),channel=channel)
    try:launcher.run()
    except Exception as e:
        import tkinter as tk
        from tkinter import messagebox
        window=tk.Tk();window.withdraw()
        messagebox.showerror('SelfStructの起動',str(e));window.destroy()
