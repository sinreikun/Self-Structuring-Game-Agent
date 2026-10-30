"""Release maintainer signs verified body metadata offline. Never commit the key."""
import argparse
import base64
import json
from pathlib import Path
import time

from cryptography.hazmat.primitives import serialization


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--private-key-file',type=Path,required=True)
    p.add_argument('--binary',type=Path,required=True)
    p.add_argument('--tag',required=True)
    p.add_argument('--version',required=True)
    p.add_argument('--output',type=Path,default=Path('updates'))
    args=p.parse_args()
    from selfstruct.resources import Gateway
    from selfstruct.release import UPDATE_KEY
    key=serialization.load_pem_private_key(args.private_key_file.read_bytes(),password=None)
    public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()
    if public!=UPDATE_KEY:raise ValueError('Signing key differs from launcher trust anchor')
    for channel in ('Stable','Development'):
        manifest={'schema':1,'protocol':1,'sequence':int(time.time()),'expires':time.time()+365*86400,
            'version':args.version,'channel':channel,'application':{'url':f'https://github.com/sinreikun/Self-Structuring-Game-Agent/releases/download/{args.tag}/SelfStruct.exe',
            'sha256':Gateway.digest(args.binary),'size':args.binary.stat().st_size,'trust':'Trusted','license':'Repository-owner-authorized distribution; no OSS license specified'}}
        payload=json.dumps(manifest,separators=(',',':')).encode()
        envelope={'payload':base64.b64encode(payload).decode(),'signature':base64.b64encode(key.sign(payload)).decode()}
        args.output.mkdir(parents=True,exist_ok=True)
        (args.output/(channel+'.json')).write_text(json.dumps(envelope),encoding='utf-8')


if __name__=='__main__':main()
