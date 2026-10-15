"""Private ownership-aware lifecycle for a dedicated local MinerU API server."""
import os
import secrets
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler
from urllib.error import URLError

from common import ConversionError, WorkLock, atomic, load

RECORD = '.md-converter-owned-service.json'


def _psutil():
    try:import psutil
    except ImportError as exc:raise ConversionError('Service ownership needs psutil in the selected MinerU runtime') from exc
    return psutil


def _request(url):
    import json
    # Loopback calls must not follow the host's HTTP proxy environment.
    with build_opener(ProxyHandler({})).open(url, timeout=3) as response:
        return json.load(response)


def _record(work_dir):
    path=Path(work_dir).absolute()/RECORD
    return path,load(path) if path.exists() else None


def _process(record):
    psutil=_psutil()
    try:
        proc=psutil.Process(record['pid'])
        if abs(proc.create_time()-record['create_time'])>0.01:return None
        if proc.cmdline()!=record['argv']:return None
        if proc.environ().get('MD_CONVERTER_OWNER_TOKEN')!=record['token']:return None
        return proc
    except (psutil.NoSuchProcess,psutil.AccessDenied,KeyError):return None


def _cli_probe(runtime):
    if not runtime:return {'available':None,'reason':'runtime_not_specified'}
    runtime=Path(runtime).absolute()
    if not runtime.is_file():return {'available':False,'runtime':str(runtime),'reason':'runtime_missing'}
    try:
        check=subprocess.run([str(runtime),'-c',
            'import importlib.metadata; print(importlib.metadata.version("mineru"))'],
            text=True,capture_output=True,timeout=8,check=False)
        version=check.stdout.strip()
        return {'available':check.returncode==0 and version.startswith('4.0.'),
                'runtime':str(runtime),'mineru_version':version if check.returncode==0 else None}
    except (OSError,subprocess.TimeoutExpired):
        return {'available':False,'runtime':str(runtime),'reason':'probe_failed'}


def status(work_dir,shared_service=None,runtime=None):
    path,record=_record(work_dir)
    owned=_process(record) if record else None
    result={'mode':'owned_service' if owned else 'unowned_or_no_service',
            'owned_service':{'url':record['url'],'pid':record['pid'],'running':bool(owned)} if record else None}
    if shared_service:
        from mineru_service import local_url
        url=local_url(shared_service)
        try:result['shared_service']={'url':url,'healthy':_request(url+'/v1/health').get('status')=='ok',
                                      'ownership':'external_iread_or_other'}
        except (OSError,ValueError):result['shared_service']={'url':url,'healthy':False,'ownership':'external_iread_or_other'}
    result['local_cli']=_cli_probe(runtime or (record.get('runtime') if record else None))
    return result


def start(work_dir,runtime,port=None):
    """Start a new loopback service; never adopt a pre-existing listener."""
    work_dir=Path(work_dir).absolute();runtime=Path(runtime).absolute()
    with WorkLock(work_dir/'.md-converter-service.lock'):
        path,old=_record(work_dir)
        if old:raise ConversionError('Service ownership record already exists; inspect it before starting another server')
        if port is None:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        if not 1<=int(port)<=65535:raise ConversionError('Invalid loopback port')
        upload=work_dir/'.md-converter-uploads';upload.mkdir(parents=True,exist_ok=True)
        argv=[str(runtime),'-m','mineru.kit.main','api-server','--host','127.0.0.1',
              '--port',str(port),'--upload-dir',str(upload),'--tier','standard',
              '--allow-local-source','--concurrency','1','--log-level','warning']
        owner=secrets.token_hex(24);env=os.environ.copy();env['MD_CONVERTER_OWNER_TOKEN']=owner
        logfile=work_dir/'.md-converter-service.log'
        with open(logfile,'ab') as log:
            child=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=env,start_new_session=True)
        psutil=_psutil();proc=psutil.Process(child.pid)
        record={'private_format':'md-converter-owned-service-1','pid':child.pid,'create_time':proc.create_time(),
                'argv':argv,'token':owner,'url':f'http://127.0.0.1:{port}','runtime':str(runtime)}
        atomic(path,record)
        try:
            deadline=time.monotonic()+45
            while time.monotonic()<deadline:
                if child.poll() is not None:raise ConversionError('Dedicated MinerU service exited; inspect private service log')
                try:
                    if _request(record['url']+'/v1/health').get('status')=='ok':
                        # A raced external listener must not be mistaken for our child.
                        if not any(c.status==psutil.CONN_LISTEN and c.laddr.port==port for c in proc.net_connections(kind='tcp')):
                            raise ConversionError('Port belongs to another process; dedicated server ownership unverified')
                        return {'mode':'owned_service','url':record['url'],'pid':child.pid,'ownership_record':str(path)}
                except (OSError,URLError):pass
                time.sleep(0.2)
            raise ConversionError('Dedicated MinerU service did not become healthy; inspect private service log')
        except BaseException as exc:
            # Only signal the exact process started above. Keep evidence if cleanup fails.
            if child.poll() is None and _process(record):
                child.terminate()
                try:child.wait(timeout=15)
                except subprocess.TimeoutExpired:pass
            if child.poll() is not None:path.unlink(missing_ok=True)
            else:
                record['startup_error']=str(exc);atomic(path,record)
            raise


def _active_jobs(url):
    # Check every page; absence of a listing is never proof that the service is idle.
    from urllib.parse import quote
    after=None;seen=set();active=[]
    while True:
        suffix='?limit=100'+('&after='+quote(after,safe='') if after else '')
        data=_request(url+'/v1/parse/jobs'+suffix)
        if data.get('object')!='list' or not isinstance(data.get('data'),list):
            raise ConversionError('Cannot prove owned service is idle')
        active.extend(j['job_id'] for j in data['data'] if j['status'] in ('queued','running'))
        if not data.get('has_more'):return active
        after=data.get('last_id')
        if not after or after in seen:raise ConversionError('Cannot complete owned service job inventory')
        seen.add(after)


def stop(work_dir):
    work_dir=Path(work_dir).absolute()
    with WorkLock(work_dir/'.md-converter-service.lock'):
        path,record=_record(work_dir)
        if not record:raise ConversionError('No dedicated service ownership record; shared service remains untouched')
        proc=_process(record)
        if not proc:raise ConversionError('Dedicated service identity is absent or changed; refusing to signal a PID')
        active=_active_jobs(record['url'])
        if active:raise ConversionError('Dedicated service still has active jobs: '+','.join(active))
        proc.send_signal(signal.SIGTERM)
        try:proc.wait(timeout=15)
        except _psutil().TimeoutExpired:raise ConversionError('Graceful shutdown timed out; service record retained for review')
        path.unlink()
        return {'mode':'no_service','stopped_owned_pid':record['pid'],'shared_service_untouched':True}


def exit_task(task=None,work_dir=None,stop_owned=False):
    """Checkpoint-safe user exit. Conversion lock prevents competing checkpoint writes."""
    result={'task':'none','checkpoint':'unchanged','shared_service_untouched':True}
    if task:
        task=Path(task).absolute();base=task.parent.parent
        with WorkLock(base/'.converter-file.lock'):
            path=task/'checkpoint.json'
            if path.exists():
                cp=load(path)
                if cp.get('status') in ('running','validating'):
                    cp['status']='interrupted';atomic(path,cp);result['checkpoint']='interrupted'
                else:result['checkpoint']=cp.get('status','unknown')
            result['task']=str(task)
    if stop_owned:
        if not work_dir:raise ConversionError('Stopping owned service requires its work directory')
        result['service']=stop(work_dir)
    else:result['service']='left_running_or_external'
    return result
