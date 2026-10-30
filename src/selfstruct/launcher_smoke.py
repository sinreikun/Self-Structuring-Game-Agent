"""Windows executable integration check; explicitly not a human desktop test."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def smoke(binary, report):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
    from .launch_runtime import Launcher,stop_owned_process
    from .resources import Gateway
    def progress(stage):report.write_text(json.dumps({'status':'running','stage':stage}),encoding='utf-8')
    progress('loading verification runtime')
    children=[]
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);source=root/'download';shutil.copyfile(binary,source)
        def envelope(version,sequence):
            payload=json.dumps({'schema':1,'protocol':1,'channel':'Development','sequence':sequence,'version':version,'expires':time.time()+1000,
                'application':{'size':source.stat().st_size,'sha256':Gateway.digest(source)}}).encode()
            return {'payload':base64.b64encode(payload).decode(),'signature':base64.b64encode(key.sign(payload)).decode()}
        def spawn(args,env):
            child=subprocess.Popen(args,env=env);children.append(child);return child
        first=envelope('1.0.0',1)
        original=Gateway.download
        Gateway.download=lambda *args:source
        launcher=Launcher(root,public,'fixture',fetch=lambda u:first,spawn=spawn,timeout=30)
        try:
            progress("native launch")
            assert launcher.run()
            stop_owned_process(children[-1])
            progress('second body')
            shutil.copyfile(binary,source);second=envelope('2.0.0',2);launcher.fetch=lambda u:second
            assert launcher.run()
            stop_owned_process(children[-1])
            progress('invalid body rollback')
            source.write_bytes(b'invalid executable');bad=envelope('3.0.0',3);launcher.fetch=lambda u:bad
            assert launcher.run() and launcher.state['last-known-good'].startswith('2.0.0')
            stop_owned_process(children[-1])
            progress('offline fallback')
            launcher.fetch=lambda u:(_ for _ in ()).throw(OSError('offline'))
            assert launcher.run()
        finally:
            Gateway.download=original
            for child in children:
                if child.poll() is None:stop_owned_process(child)
        report.write_text(json.dumps({'status':'passed','checks':['same launcher: two native body generations','native GUI health acknowledgment','invalid executable rollback','offline native stable body startup'],
            'human_desktop_test':False}),encoding='utf-8')
