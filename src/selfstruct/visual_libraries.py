"""Host-only acquisition of signed, declarative visual libraries.

Only pipeline JSON is supported. Native DLLs, Python, arbitrary WASM imports,
archives, and unsigned runtime extensions cannot enter the Agent process.
"""
import json

from .release import UPDATE_KEY
from .resources import Gateway, verify_catalog


def acquire(store, request, analyzers):
    catalog_path = store.account_root/'host'/'visual-library-catalog.json'
    if not catalog_path.is_file():
        return {"state": "awaiting_verified_catalog", "request": request,
                "reason": "No signed catalog entry is installed; the request is retained"}
    if catalog_path.stat().st_size > 2*1024**2:
        raise ValueError("Visual library catalog too large")
    from cryptography.exceptions import InvalidSignature
    try:
        catalog = verify_catalog(json.loads(catalog_path.read_text(encoding='utf-8')), UPDATE_KEY)
    except InvalidSignature as error:
        raise ValueError('Visual library catalog signature is invalid') from error
    items = catalog.get('libraries', [])
    if not isinstance(items, list) or len(items) > 256:
        raise ValueError("Invalid library catalog")
    item = next((entry for entry in items if entry.get('name') == request['name']), None)
    if item is None:
        return {"state": "awaiting_verified_catalog", "request": request,
                "reason": "The requested library is absent from the signed catalog"}
    if item.get('runtime') != 'selfstruct-visual-pipeline-v1' or not 0 < item.get('size', 0) <= 1024**2:
        raise ValueError("Only bounded declarative visual libraries are supported")
    # Signature validates the expected hash/source/license/runtime. Gateway checks
    # TLS source, trust, disk budget, download length and artifact hash again.
    artifact = Gateway(store.account_root, store.quota).download(item, 'library')
    value = json.loads(artifact.read_text(encoding='utf-8'))
    definitions = value.get('analyzers')
    if value.get('runtime') != item['runtime'] or not isinstance(definitions, list) or not 1 <= len(definitions) <= 16:
        raise ValueError("Invalid declarative visual library")
    identifiers = []
    for definition in definitions:
        definition = {**definition, 'source': {'provenance': 'external', 'library': request['name'],
                                               'sha256': item['sha256'], 'license': item['license']}}
        identifiers.append(analyzers.create(definition))
    return {'state': 'acquired_unverified_analyzers', 'analyzers': identifiers, 'license': item['license'],
            'sha256': item['sha256'], 'runtime': item['runtime'], 'signature': 'verified_catalog',
            'permissions': 'bounded image operations only; independent evidence evaluation required'}
