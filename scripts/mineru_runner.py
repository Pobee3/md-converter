"""Run verified MinerU 4 CLI with explicit local service, preserving source/range evidence."""
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
from common import ConversionError, atomic, digest, load
from mapping import require


def parse_local(source,runtime,output,tier='standard',pages=None,service=None,timeout=600):
    source=Path(source).resolve();output=Path(output).absolute();runtime=Path(runtime).absolute()
    require(tier in ('standard','advanced','flash'),'Unsupported MinerU tier')
    require(source.suffix.lower()!='.pdf' or tier!='flash','PDF must use Standard or Advanced')
    require(source.suffix.lower()!='.epub' or tier=='flash','EPUB must use native Flash')
    if service:
        u=urlsplit(service)
        require(u.scheme=='http' and u.hostname in ('127.0.0.1','localhost','::1') and not u.username and not u.password and not u.query and not u.fragment,
                'Only an explicitly local MinerU service is supported')
    output.parent.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',MINERU_API_KEY='',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',MINERU_MODEL_SOURCE='local')
    # Local-service calls do not start a second model or rewrite existing configuration.
    if not service:env['MINERU_HOME']=str(output.parent/'runtime')
    probe=subprocess.run([str(runtime),'-c','import importlib.metadata; print(importlib.metadata.version("mineru"))'],env=env,capture_output=True,text=True)
    require(probe.returncode==0,'Selected interpreter cannot import MinerU metadata; preserve the virtual-environment executable path')
    ver=probe.stdout.strip()
    require(ver.startswith('4.0.'),'MinerU adapter validated against 4.0 API only; inspect new versions first')
    argv=[str(runtime),'-m','mineru.kit.main','parse',str(source),'--tier',tier,'--format','middle_json','--output',str(output)]
    if service:
        argv=[str(runtime),str(Path(__file__).with_name('mineru_service.py')),str(source),'--tier',tier,'--output',str(output),'--service',service,'--timeout',str(max(1,timeout-10))]
    else:argv+=['--disable-image-analysis']
    if pages:
        require(all(type(p) is int and p>=1 for p in pages),'Invalid physical pages')
        argv+=['--pages',','.join(map(str,sorted(set(pages))))]
    sha=digest(source.read_bytes());started=time.monotonic()
    try:result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        atomic(output.with_suffix('.run.json'),{'status':'interrupted','input_sha256':sha,'argv':argv,'reason':'timeout'});raise ConversionError('MinerU timed out; server job may still run. Inspect before retrying.') from exc
    record={'private_format':'md-converter-engine-run-1','status':'completed' if result.returncode==0 else 'interrupted' if result.returncode==3 else 'failed',
        'input_sha256':sha,'mineru_version':ver,'requested_tier':tier,'requested_pages':pages,
        'local_service':service,'argv':argv,'elapsed_seconds':round(time.monotonic()-started,3),
        'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
    if result.returncode==0:
        raw=load(output);record['output_sha256']=digest(output.read_bytes())
        require(digest(source.read_bytes())==sha,'Source changed during parse')
        require(raw.get('schema')=='docvortex.middle' and raw.get('schema_version')=='2.0','Unknown MinerU middle schema')
        record['actual_tier']=raw.get('extensions',{}).get('mineru',{}).get('tier')
        require(record['actual_tier']==tier,'Engine tier differs from request')
        if service:
            worker=load(output.with_suffix('.worker.json'));record['job_id']=worker['job_id']
        if source.suffix.lower()=='.pdf' and pages:
            actual=[p['page_idx']+1 for p in raw['pages']]
            require(len(actual)==len(set(actual)) and set(actual)==set(pages),'Engine physical page coverage differs')
    atomic(output.with_suffix('.run.json'),record)
    require(result.returncode==0,'MinerU parse failed; see '+str(output.with_suffix('.run.json')))
    return record
