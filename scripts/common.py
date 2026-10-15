"""Private converter primitives. Not an iReader public API."""
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

VERSION = '0.1.0'

class ConversionError(ValueError):
    pass

def digest(data):
    return hashlib.sha256(data).hexdigest()

def fingerprint(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def confined(root, relative):
    root = Path(root).resolve()
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        raise ConversionError('Unsafe output path: ' + str(relative))
    target = root / p
    if not target.resolve().is_relative_to(root):
        raise ConversionError('Symlink escape: ' + str(relative))
    return target

def atomic(path, data):
    path = Path(path)
    if path.is_symlink():
        raise ConversionError('Refusing symlink output: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (dict, list)):
        data = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    elif isinstance(data, str):
        data = data.encode()
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def token(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', value):
        raise ConversionError('Use a non-path document/task identifier')
    return value

class Registry:
    """UUIDs persisted against exact source evidence; never derived from text/page."""
    def __init__(self, path, source_hash, document_id):
        self.path = path
        self.data = load(path) if path.exists() else {
            'private_format': 'md-converter-ids-1', 'document_id': document_id,
            'input_sha256': source_hash, 'ids': {}}
        if self.data['input_sha256'] != source_hash or self.data['document_id'] != document_id:
            raise ConversionError('Source identity changed; reviewed ID migration required')
    def get(self, kind, evidence):
        key = kind + ':' + evidence
        if key not in self.data['ids']:
            self.data['ids'][key] = kind + '-' + uuid.uuid4().hex
            atomic(self.path, self.data)
        return self.data['ids'][key]

class WorkLock:
    """OS lock releases on crash; no stale-directory deletion guesses (macOS/Linux)."""
    def __init__(self,path):self.path=Path(path)
    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.is_symlink():raise ConversionError('Lock symlink refused')
        self.file=open(self.path,'a+')
        try:fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close();raise ConversionError('Another converter holds this document lock')
        return self
    def __exit__(self,*args):self.file.close()
