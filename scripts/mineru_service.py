"""Resumable local MinerU 4 jobs. Executed with the existing MinerU interpreter.

SDK private source/result helpers are version-gated to the tested 4.0 API.
Submission and polling use documented local HTTP routes, never remote fallback.
"""
import argparse
import importlib.metadata
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
from common import ConversionError, WorkLock, atomic, digest, fingerprint, load
from mapping import require

TERMINAL={'completed','partial','failed','canceled'}


def local_url(url):
    u=urlsplit(url)
    require(u.scheme=='http' and u.hostname in ('127.0.0.1','localhost','::1') and not u.username and not u.password
            and not u.query and not u.fragment and u.path in ('','/'),'Only a loopback MinerU service root is permitted')
    return url.rstrip('/')


def job_step(path,identity,payload,request):
    """One submission or one poll. Ambiguous submissions are never repeated.

    request(method,path,payload) is injected for protocol fault tests.
    A saved job handle is always polled, even if a previous observation timed out.
    """
    path=Path(path);key=fingerprint(identity)
    state=load(path) if path.exists() else {'private_format':'md-converter-local-job-1','identity':identity,'identity_hash':key,'phase':'new'}
    require(state['identity_hash']==key,'Job source/configuration differs')
    if state.get('job_id'):
        job=request('GET','/v1/parse/jobs/'+quote(state['job_id'],safe=''),None)
    else:
        require(state['phase']=='new','Submission outcome unknown; reconcile server jobs before any retry')
        state['phase']='submitting';state['submitted_at']=time.time();atomic(path,state)
        # No automatic HTTP POST retry: a lost response could still mean accepted.
        job=request('POST','/v1/parse/jobs',payload)
    require(isinstance(job.get('job_id'),str) and job['job_id'],'Service response has no job identifier')
    if state.get('job_id'):require(state['job_id']==job['job_id'],'Service returned another job')
    require(job.get('status') in {'queued','running',*TERMINAL},'Unknown job status')
    state.update(job_id=job['job_id'],phase=job['status'],last_observed_at=time.time(),last_response=job)
    atomic(path,state)
    return job


def run(source,output,service,tier,pages=None,timeout=600,stop_after_submit=False):
    import httpx
    from mineru.parser import MinerUApiParser
    source=Path(source).resolve();output=Path(output).absolute();service=local_url(service)
    version=importlib.metadata.version('mineru');require(version.startswith('4.0.'),'Unverified MinerU API version')
    require(tier in ('standard','advanced'),'Local service PDF tier must be Standard or Advanced')
    require(source.suffix.lower()=='.pdf','Service job adapter currently handles PDF')
    selected=sorted(set(pages)) if pages else None
    sha=digest(source.read_bytes());identity={'input_sha256':sha,'service':service,'tier':tier,'pages':selected,'mineru_version':version}
    output.parent.mkdir(parents=True,exist_ok=True);state_path=output.with_suffix('.job.json')
    with WorkLock(output.with_suffix('.job.lock')):
        parser=MinerUApiParser(api_url=service,api_key='',tier=tier,include_images=True)
        state=load(state_path) if state_path.exists() else None
        if state is not None:require(state['identity_hash']==fingerprint(identity),'Job source/configuration differs')
        # Upload/source preparation is unnecessary when resuming a known job.
        payload=None
        if state is None:
            payload=parser._build_payload(parser._build_source(source),','.join(map(str,selected)) if selected else '')
        started=time.monotonic()
        with httpx.Client(timeout=30,trust_env=False,follow_redirects=False) as client:
            def request(method,path,body):
                response=client.request(method,service+path,json=body if method=='POST' else None)
                if response.status_code==404:raise ConversionError('Saved job is missing on this service; do not assume its output was published')
                response.raise_for_status();return response.json()
            while True:
                job=job_step(state_path,identity,payload,request)
                if stop_after_submit and job['status'] not in TERMINAL:
                    return {'status':'interrupted','job_id':job['job_id'],'reason':'requested stop after saved server handle'}
                if job['status'] in TERMINAL:break
                if time.monotonic()-started>=timeout:
                    return {'status':'interrupted','job_id':job['job_id'],'reason':'observation budget expired; resume the same handle'}
                time.sleep(1)
        require(job['status']=='completed','MinerU job terminal status: '+job['status'])
        # Downloads are tied to the completed server handle; they can be retried independently.
        result=parser._build_result(job,source.name);raw=result.to_json()
        require(digest(source.read_bytes())==sha,'Source changed during parsing')
        atomic(output,raw)
        return {'status':'completed','job_id':job['job_id'],'actual_tier':tier,'elapsed_seconds':round(time.monotonic()-started,3)}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--service',required=True);p.add_argument('--tier',choices=['standard','advanced'],required=True)
    p.add_argument('--pages');p.add_argument('--timeout',type=int,default=600);p.add_argument('--stop-after-submit',action='store_true')
    a=p.parse_args()
    try:
        result=run(a.source,a.output,a.service,a.tier,[int(n) for n in a.pages.split(',')] if a.pages else None,a.timeout,a.stop_after_submit)
        atomic(a.output.with_suffix('.worker.json'),result)
        if result['status']!='completed':p.exit(3,result['reason']+'\n')
    except Exception as exc:p.exit(2,str(exc)+'\n')
