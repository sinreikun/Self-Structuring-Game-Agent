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
    from .owned_process import start_application
    children=[]
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
    # Windows scanners may briefly retain a terminated executable. Do not let
    # disposable directory cleanup mask the actual update assertions. Jobs are
    # explicitly terminated below, including already-exited bootloader parents.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        root=Path(temp);source=root/'download';shutil.copyfile(binary,source)
        def envelope(version,sequence,signer=key):
            payload=json.dumps({'schema':1,'protocol':1,'channel':'Development','sequence':sequence,'version':version,'expires':time.time()+1000,
                'application':{'size':source.stat().st_size,'sha256':Gateway.digest(source)}}).encode()
            return {'payload':base64.b64encode(payload).decode(),'signature':base64.b64encode(signer.sign(payload)).decode()}
        def spawn(args,env):
            child=start_application(args,env,retain_job=True);children.append(child);return child
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
            stop_owned_process(children[-1])
            progress('signing key migration: cached legacy body')
            data=root/'experiments'/'preserved'/'evidence.txt'
            data.parent.mkdir(parents=True);data.write_text('individual experience must survive key rotation')
            next_key=Ed25519PrivateKey.generate()
            next_public=next_key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
            migrated=Launcher(root,next_public,'fixture',legacy_keys=(public,),
                             fetch=lambda _:(_ for _ in ()).throw(OSError('offline')),spawn=spawn,timeout=30)
            assert migrated.run() and migrated.state['current'].startswith('2.0.0')
            stop_owned_process(children[-1])
            progress('signing key migration: reject legacy network signature')
            shutil.copyfile(binary,source)
            old_network=envelope('4.0.0',4)
            migrated.fetch=lambda _:old_network
            assert migrated.run() and migrated.state['last-known-good'].startswith('2.0.0')
            stop_owned_process(children[-1])
            progress('signing key migration: new signed body')
            shutil.copyfile(binary,source)
            new_network=envelope('4.0.0',4,next_key)
            migrated.fetch=lambda _:new_network
            assert migrated.run() and migrated.state['last-known-good'].startswith('4.0.0')
            assert data.read_text()=='individual experience must survive key rotation'
        finally:
            Gateway.download=original
            for child in children:
                stop_owned_process(child)
        from .release import UPDATE_KEY, LAUNCHER_VERSION
        report.write_text(json.dumps({'status':'passed','launcher_version':LAUNCHER_VERSION,'launcher_public_key':UPDATE_KEY,'key_migration':'passed','checks':['same launcher: two native body generations','native GUI health acknowledgment','invalid executable rollback','offline native stable body startup','key migration: legacy cached fallback','legacy network signatures rejected','new signing key accepted','individual evidence preserved'],
            'human_desktop_test':False}),encoding='utf-8')
