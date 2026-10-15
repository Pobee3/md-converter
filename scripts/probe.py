#!/usr/bin/env python3
"""Read-only probe; no model loads, downloads, services or config writes."""
import argparse
import datetime
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from common import atomic

def run(argv):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
        return {'argv': argv, 'returncode': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'argv': argv, 'error': str(e)}

def probe(python):
    metadata_code = """import sys,json,importlib.metadata as m
names=['mineru','mineru-vl-utils','mineru-llama-cpp','torch','transformers','docvortex','pypdf','pypdfium2','lxml','onnxruntime','PyYAML','jsonschema']
r={'python':sys.version,'executable':sys.executable,'packages':{}}
for n in names:
 try:r['packages'][n]=m.version(n)
 except m.PackageNotFoundError:r['packages'][n]=None
print(json.dumps(r))"""
    result = {'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scope': 'Read-only metadata and --help; no inference or model load',
              'os': platform.platform(), 'architecture': platform.machine(),
              'disk': shutil.disk_usage('.')._asdict(),
              'path_entries': {n: shutil.which(n) for n in ['python3','uv','mineru','mineru-kit','pdfinfo','pdftoppm']},
              'runtime': run([str(python), '-c', metadata_code]), 'entries': {}, 'models': []}
    if sys.platform == 'darwin':
        result['memory_query'] = run(['/usr/sbin/sysctl', '-n', 'hw.memsize'])
    for name in ['mineru','mineru-kit']:
        p = python.parent / name
        if p.exists():
            first = p.read_text().splitlines()[0]
            result['entries'][name] = {'path': str(p), 'shebang': first,
                'shebang_target_exists': Path(first[2:]).exists() if first.startswith('#!') else None}
    kit = python.parent / 'mineru-kit'
    if kit.exists():
        result['kit_version'] = run([str(python),str(kit),'--version'])
        result['kit_parse_help'] = run([str(python),str(kit),'parse','--help'])
    for p in sorted((Path.home()/'.cache/huggingface/hub').glob('models--*')):
        if not any(x in p.name.lower() for x in ['mineru','pdf-extract']): continue
        files = [f for f in (p/'snapshots').rglob('*') if f.is_file()]
        partials = [f for f in (p/'blobs').glob('*.incomplete') if f.is_file()]
        result['models'].append({'path': str(p), 'snapshot_files': len(files),
            'snapshot_bytes': sum(f.stat().st_size for f in files),
            'incomplete_blobs': len(partials), 'incomplete_bytes': sum(f.stat().st_size for f in partials)})
    cfg = Path.home()/'mineru.json'
    if cfg.exists():
        data=json.loads(cfg.read_text())
        result['legacy_config'] = {'path': str(cfg), 'keys': list(data),
            'model_paths': {k: {'path':v,'exists':bool(v) and Path(v).exists()} for k,v in data.get('models-dir',{}).items()}}
    result['capability_status'] = {'cli_help': 'see return codes', 'standard_inference': 'not_run',
        'advanced_inference': 'not_run', 'mineru_epub_flash': 'not_run', 'model_integrity': 'not_verified'}
    return result

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--python',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();atomic(a.output,probe(a.python.absolute()))
