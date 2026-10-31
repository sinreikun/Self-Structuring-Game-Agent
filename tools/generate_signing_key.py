"""Create a publisher key outside the checkout; never print private key material."""
import argparse
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def generate(path):
    path = Path(path).resolve()
    checkout = Path(__file__).resolve().parents[1]
    if path.is_relative_to(checkout):
        raise ValueError('Publisher keys must be stored outside the repository')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = Ed25519PrivateKey.generate()
    data = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    # Exclusive creation prevents accidental replacement of the current signing key.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(generate(args.output))
