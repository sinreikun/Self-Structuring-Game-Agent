"""Fingerprint the unchanged inference implementation when reusing an earlier check."""
import ast
import hashlib

FILES=('src/selfstruct/resources.py','src/selfstruct/research.py','src/selfstruct/store.py',
       'src/selfstruct/sandbox.py','src/selfstruct/vision.py','src/selfstruct/visual_libraries.py','src/selfstruct/assets/catalog.json','pyproject.toml')


def component_hashes(read):
    result={name:hashlib.sha256(read(name).replace(b'\r\n',b'\n')).hexdigest() for name in FILES}
    tree=ast.parse(read('src/selfstruct/config.py').decode('utf-8'))
    relevant=[node for node in tree.body if isinstance(node,(ast.Import,ast.ImportFrom)) or
              isinstance(node,ast.FunctionDef) and node.name in {'atomic_write','write_json'}]
    result['config_io']=hashlib.sha256(ast.dump(ast.Module(body=relevant,type_ignores=[])).encode()).hexdigest()
    return result
