"""Publisher-only signing. The private key is a file path, never a CLI secret."""
import argparse
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization

parser = argparse.ArgumentParser()
parser.add_argument("manifest", type=Path)
parser.add_argument("--private-key-file", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
payload = args.manifest.read_bytes()
json.loads(payload)
key = serialization.load_pem_private_key(args.private_key_file.read_bytes(), password=None)
signature = key.sign(payload)
args.output.write_text(json.dumps({"payload":base64.b64encode(payload).decode(), "signature":base64.b64encode(signature).decode()}), encoding="utf-8")
