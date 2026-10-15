"""PDF Standard batching with verified cache, persistent in-flight handles and merge gates."""
import importlib.metadata
import subprocess
from pathlib import Path
from common import ConversionError, WorkLock, atomic, confined, digest, fingerprint, load, token
from mapping import require
from pdf_preflight import preflight
from pdf_adapter import adapt
from mineru_runner import parse_local

PIPELINE_VERSION='pdf-pipeline-1'

class LocalEngine:
    def __init__(self,runtime,service):
        self.runtime=Path(runtime).absolute();self.service=service
        proc=subprocess.run([str(self.runtime),'-c','import importlib.metadata;print(importlib.metadata.version("mineru"))'],capture_output=True,text=True)
        require(proc.returncode==0,'MinerU interpreter unavailable')
        self.identity={'runtime':str(self.runtime),'mineru_version':proc.stdout.strip(),'service':service}
    def parse(self,source,output,pages):return parse_local(source,self.runtime,output,'standard',pages,self.service)


def valid_batch(task,entry,source_sha,pages):
    if not entry:return False
    try:
        rawpath=confined(task,entry['output']);runpath=rawpath.with_suffix('.run.json')
        raw=load(rawpath);run=load(runpath)
        return (digest(rawpath.read_bytes())==entry['output_sha256']==run['output_sha256']
            and digest(runpath.read_bytes())==entry['run_sha256']
            and run['status']=='completed' and run['input_sha256']==source_sha
            and run.get('actual_tier')=='standard'
            and raw.get('schema')=='docvortex.middle' and raw.get('schema_version')=='2.0'
            and raw.get('extensions',{}).get('mineru',{}).get('tier')=='standard'
            and sorted(p['page_idx']+1 for p in raw['pages'])==sorted(pages))
    except (OSError,ValueError,KeyError,TypeError):return False


def convert_pdf(source,work_dir,document_id,task_id,runtime=None,service=None,pages=None,batch_size=8,
                review=None,stop_after=None,engine=None,chapter_ids=None):
    source=Path(source).resolve();sha=digest(source.read_bytes())
    engine=engine or LocalEngine(runtime,service)
    base=confined(Path(work_dir),'books/'+token(document_id));base.mkdir(parents=True,exist_ok=True)
    task=confined(base,'tasks/'+token(task_id))
    with WorkLock(base/'.converter-file.lock'):
        plan=preflight(source,document_id,pages=pages,batch_size=batch_size,
                       work_dir=work_dir,chapter_ids=chapter_ids,review=review)
        require(sha==plan['input_sha256'],'Source changed during planning')
        task.mkdir(parents=True,exist_ok=True);cp_path=task/'checkpoint.json'
        config={'version':PIPELINE_VERSION,'input_sha256':sha,'engine':engine.identity,
                'requested_pages':plan['requested_scope']['pdf_pages'],'selected_chapters':plan['selection']['included_chapter_ids'],
                'batch_size':batch_size}
        cfg=fingerprint(config)
        cp=load(cp_path) if cp_path.exists() else {'private_format':'md-converter-pdf-checkpoint-1','configuration':config,
             'configuration_hash':cfg,'input_sha256':sha,'batches':{},'attempts':{},'inflight':{},'resume_log':[]}
        require(cp.get('private_format')=='md-converter-pdf-checkpoint-1' and cp['configuration_hash']==cfg,'Source/engine/range/configuration changed; use a new reviewed task')
        snapshot=base/'originals/source.pdf'
        if snapshot.exists():require(digest(snapshot.read_bytes())==sha,'Existing source snapshot differs')
        else:atomic(snapshot,source.read_bytes())
        require(digest(snapshot.read_bytes())==sha,'Source snapshot inconsistent')
        atomic(task/'plan.json',plan);cp['status']='running';atomic(cp_path,cp)
        reused=[];rebuilt=[];raw_paths=[];new_count=0
        try:
            for index,batch in enumerate(plan['batches']):
                key=str(index);scope=batch['context_pdf_pages'];entry=cp['batches'].get(key)
                if valid_batch(task,entry,sha,scope):
                    reused.append(index);raw_paths.append(confined(task,entry['output']));continue
                output=None
                # A saved worker handle must be polled before assigning a new attempt.
                if key in cp['inflight']:
                    output=confined(task,cp['inflight'][key]);jobpath=output.with_suffix('.job.json')
                    runpath=output.with_suffix('.run.json')
                    job=load(jobpath) if jobpath.exists() else None
                    run=load(runpath) if runpath.exists() else None
                    if job and job.get('phase') in ('failed','canceled'):
                        cp['resume_log'].append({'batch':index,'terminal_job':job.get('job_id'),'status':job['phase']});output=None
                    elif run and run.get('status')=='completed':
                        # Crash after engine completion but before checkpoint commit: adopt only verified results.
                        recovered={'output':cp['inflight'][key],'output_sha256':digest(output.read_bytes()),'run_sha256':digest(runpath.read_bytes())}
                        if valid_batch(task,recovered,sha,scope):
                            cp['batches'][key]=recovered;del cp['inflight'][key];atomic(cp_path,cp)
                            reused.append(index);raw_paths.append(output);continue
                        output=None
                    elif not job:
                        require(run is not None and run.get('status')=='failed','Uncertain in-flight attempt without a server handle; inspect before resubmitting')
                        output=None
                if output is None:
                    attempt=cp['attempts'].get(key,0)+1;require(attempt<=3,'Batch retry limit reached; investigate failure before creating a new task')
                    cp['attempts'][key]=attempt
                    output=confined(task,'batches/'+str(index)+'/attempt-'+str(attempt)+'.json')
                    cp['inflight'][key]=str(output.relative_to(task));atomic(cp_path,cp)
                engine.parse(snapshot,output,scope)
                entry={'output':str(output.relative_to(task)),'output_sha256':digest(output.read_bytes()),
                       'run_sha256':digest(output.with_suffix('.run.json').read_bytes()),'target_pages':batch['target_pdf_pages'],'context_pages':scope}
                require(valid_batch(task,entry,sha,scope),'Engine batch failed source/tier/page/hash checks')
                cp['batches'][key]=entry;cp['inflight'].pop(key,None);atomic(cp_path,cp)
                rebuilt.append(index);raw_paths.append(output);new_count+=1
                if stop_after and new_count>=stop_after:
                    cp['status']='interrupted';cp['resume_log'].append({'reused':reused,'rebuilt':rebuilt,'reason':'stop after committed batch'});atomic(cp_path,cp)
                    return {'status':'interrupted','task_dir':str(task),'published':False,'reused_batches':reused,'rebuilt_batches':rebuilt}
            require(digest(snapshot.read_bytes())==sha and digest(source.read_bytes())==sha,'Source changed before merge')
            covered={p for e in cp['batches'].values() for p in e.get('target_pages',[])}
            # Recovered entries derive target coverage from the persisted immutable plan.
            covered=set()
            for i,b in enumerate(plan['batches']):
                require(valid_batch(task,cp['batches'].get(str(i)),sha,b['context_pdf_pages']),'Batch no longer valid before merge')
                covered.update(b['target_pdf_pages'])
            require(covered==set(config['requested_pages']),'Requested pages missing before merge')
            cp['status']='validating';atomic(cp_path,cp)
            result=adapt(snapshot,raw_paths,work_dir,document_id,task_id,review,_lock_held=True,
                         selected_chapters=plan['selection']['included_chapter_ids'])
            reportpath=Path(result['staging'])/'conversion-report.json';report=load(reportpath)
            report['batch_execution']={'pipeline_version':PIPELINE_VERSION,'reused':reused,'rebuilt':rebuilt,'target_pages':sorted(covered),
                                       'context_pages':sorted({p for b in plan['batches'] for p in b['context_pdf_pages']})}
            atomic(reportpath,report)
            cp['status']=report['status'];cp['resume_log'].append({'reused':reused,'rebuilt':rebuilt,'reason':'all target pages verified and merged'})
            atomic(cp_path,cp)
            return dict(result,status=report['status'],reused_batches=reused,rebuilt_batches=rebuilt)
        except (Exception,KeyboardInterrupt) as exc:
            cp['status']='interrupted' if isinstance(exc,KeyboardInterrupt) else 'needs_attention';cp['last_error']=str(exc)
            cp['resume_log'].append({'reused':reused,'rebuilt':rebuilt,'reason':str(exc)});atomic(cp_path,cp);raise
