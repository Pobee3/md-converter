"""Native EPUB candidate pipeline. Core outputs plus private task trace evidence.

Shared publication remains gated on contract-owner synchronization. This module
never invents PDF pages for reflowable content.
"""
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from common import ConversionError, Registry, WorkLock, atomic, confined, digest, fingerprint, load, token
from epub_structure import Book, extract, render, text_of
from mapping import validate_correspondence, require
from note_identity import classify

ADAPTER_VERSION='0.2.1-native-dom'


def prepare(source,work_dir,document_id):
    source=Path(source).resolve();require(source.stat().st_size<=128*1024*1024,'EPUB too large')
    data=source.read_bytes();sha=digest(data)
    base=confined(Path(work_dir),'books/'+token(document_id));base.mkdir(parents=True,exist_ok=True)
    return source,data,sha,base


def inspect(source,work_dir,document_id):
    source,data,sha,base=prepare(source,work_dir,document_id)
    with WorkLock(base/'.converter-file.lock'):
        registry=Registry(base/'revisions/converter-id-registry.json',sha,document_id);book=Book(source)
        try:
            model=extract(book,registry)
            return {'private_format':'md-converter-inspection-2','document_id':document_id,'input_sha256':sha,
                    'source_type':'epub','title':book.title,
                    'chapters':[{k:c.get(k) for k in ('chapter_id','title','parent_id','href','element_id','inferred')} for c in model['chapters']],
                    'page_list':book.page_list,'issues':model['issues'],
                    'notice':'Native chapter/element locations; no generated PDF or screen page numbers.'}
        finally:book.close()


def convert(source,work_dir,document_id,task_id,selected=None,stop_after=None,engine_result=None):
    start=time.monotonic();source,data,sha,base=prepare(source,work_dir,document_id)
    with WorkLock(base/'.converter-file.lock'):
        registry=Registry(base/'revisions/converter-id-registry.json',sha,document_id);book=Book(source)
        try:return _convert(book,registry,source,data,sha,base,document_id,task_id,selected,stop_after,start,engine_result)
        finally:book.close()


def _convert(book,registry,source,data,sha,base,document_id,task_id,selected,stop_after,start,engine_result):
    model=extract(book,registry);chapters=model['chapters'];known={c['chapter_id'] for c in chapters}
    chosen=set(selected) if selected else set(known);require(chosen and chosen<=known,'Unknown/empty chapter selection')
    # Selecting a parent includes its children; serial spine files are not chapter IDs.
    changed=True
    while changed:
        old=len(chosen);chosen.update(c['chapter_id'] for c in chapters if c['parent_id'] in chosen);changed=old!=len(chosen)
    rows=[r for r in model['records'] if r['chapter_id'] in chosen]
    require(rows,'Selected scope has no body blocks')
    engine=None
    if engine_result:
        engine_result=Path(engine_result);engine=load(engine_result.with_suffix('.run.json'));raw=load(engine_result)
        require(engine['status']=='completed' and engine['input_sha256']==sha and engine['output_sha256']==digest(engine_result.read_bytes()),'Flash evidence does not match source')
        require(raw.get('extensions',{}).get('mineru',{}).get('tier')=='flash','EPUB engine must be native Flash')
        require(raw.get('metadata',{}).get('document',{}).get('page_count_kind')=='spine','EPUB engine positions are not PDF pages')
        require({p['page_idx'] for p in raw['pages']}==set(range(len(book.spine))),'Flash spine coverage mismatch')
    config={'adapter_version':ADAPTER_VERSION,'input_sha256':sha,'chapter_ids':sorted(chosen),'flash_output_sha256':engine['output_sha256'] if engine else None}
    cfg=fingerprint(config);task=confined(base,'tasks/'+token(task_id));task.mkdir(parents=True,exist_ok=True)
    if engine:
        atomic(task/'raw/flash.json',engine_result.read_bytes());atomic(task/'raw/flash.run.json',engine)
    cp_path=task/'checkpoint.json'
    cp=load(cp_path) if cp_path.exists() else {'private_format':'md-converter-checkpoint-2','input_sha256':sha,'configuration_hash':cfg,
        'source_revision':registry.get('s','task:'+task_id),'map_revision':registry.get('map','task:'+task_id),
        'verified_batches':{},'attempts':0,'resume_log':[]}
    require(cp.get('input_sha256')==sha and cp.get('configuration_hash')==cfg,'Checkpoint source/configuration changed; use a new reviewed task')
    cp['attempts']+=1;cp['status']='running';atomic(cp_path,cp)
    snapshot=base/'originals/source.epub'
    if snapshot.exists():require(digest(snapshot.read_bytes())==sha,'Original snapshot differs')
    else:atomic(snapshot,data)
    staging=task/'staging';staging.mkdir(exist_ok=True)
    common={'schema_version':'0.2.0','document_id':document_id,'source_revision':cp['source_revision']}
    issues=list(model['issues']);trace=[];references=[];assets={};reused=[];rebuilt=[];results=[]
    def copy_asset(href):
        raw=book.zip.read(href);ext=Path(href).suffix.lower()
        require(ext in ('.png','.jpg','.jpeg','.gif','.webp','.svg'), 'Unsupported native asset: '+href)
        if ext=='.svg':
            # Retain original vector evidence, but flag active references rather than emit them.
            root=book.xml(href)
            require(not any(e.tag.rsplit('}',1)[-1]=='script' or any(k.lower().startswith('on') or (k.endswith('href') and (':' in v or v.startswith('//'))) for k,v in e.attrib.items()) for e in root.iter()),'Active SVG requires review')
        name='assets/'+digest(raw)+ext;atomic(confined(staging,name),raw)
        obj={'path':name,'sha256':digest(raw),'original_href':href};assets[name]=obj;return obj
    available_ids={r['block_id'] for r in rows}|{n['note_id'] for n in model['notes']}
    def rendered(item,kind):
        bid=item['block_id'] if kind=='block' else item['note_id']
        body,refs,imgs=render(item['element'],item['origin']['href'],model['targets'],copy_asset,bid,available_ids)
        require(bool(body),'Empty source block requires review: '+item['locator'])
        # Headings use the Markdown marker as part of the canonical mapped text.
        if kind=='block' and item['kind']=='heading':
            prefix='#'*item['level']+' ';body=prefix+body
            for ref in refs:
                for f in ('range','label_range'):
                    if f in ref:ref[f]=[x+len(prefix) for x in ref[f]]
        for ref in refs:ref['reference_id']=registry.get('r',item['locator']+':'+ref['node_path'])
        return {'id':bid,'text':body,'references':refs,'assets':imgs,'origin':item['origin'],'locator':item['locator']}
    # Checkpoint native rendering by spine; reparse DOM for topology, reuse validated output.
    spine_indices=sorted({r['origin']['spine_index'] for r in rows});new_count=0
    try:
        for index in spine_indices:
            key=str(index);batch_path=task/'batches'/('spine-'+key+'.json');entry=cp['verified_batches'].get(key)
            valid=bool(entry)
            if entry:
                for f in entry['files']:
                    p=confined(task,f['path'])
                    if not p.is_file() or digest(p.read_bytes())!=f['sha256']:valid=False
            if valid:
                batch=load(batch_path);reused.append(index)
                for item in batch:
                    for a in item['assets']:assets[a['path']]=a
            else:
                batch=[rendered(r,'block') for r in rows if r['origin']['spine_index']==index]
                atomic(batch_path,batch);files=[{'path':str(batch_path.relative_to(task)),'sha256':digest(batch_path.read_bytes())}]
                for item in batch:
                    for a in item['assets']:files.append({'path':'staging/'+a['path'],'sha256':a['sha256']})
                cp['verified_batches'][key]={'files':files};rebuilt.append(index);new_count+=1;atomic(cp_path,cp)
            results+=batch
            if stop_after and new_count>=stop_after:
                cp['status']='interrupted';cp['resume_log'].append({'reused':reused,'rebuilt':rebuilt});atomic(cp_path,cp)
                return {'status':'interrupted','task_dir':str(task),'published':False}
        cp['status']='validating';atomic(cp_path,cp)
        rendered_by={r['id']:r for r in results};blocks=[];maps=[]
        for item in rows:
            bid=item['block_id'];r=rendered_by[bid];origin=r['origin'];body=r['text']
            blocks.append({'block_id':bid,'source_id':bid if item['kind']=='paragraph' else None,'chapter_id':item['chapter_id'],
                           'order':len(blocks)+1,'kind':item['kind'],'text':body,'translatable':item['kind']!='figure'})
            f={'fragment_id':registry.get('f',item['locator']),'source_type':'epub','spine_index':origin['spine_index'],
               'href':origin['href'],'element_id':origin['element_id'],'cfi':None,'range':[0,len(body)],
               'verification':'verified' if origin['element_id'] else 'approximate',
               'evidence':'Native XML node in source snapshot '+sha+'; path '+origin['node_path']+'; markup SHA256 '+origin['markup_sha256']}
            maps.append({'source_id':bid,'chapter_id':item['chapter_id'],'fragments':[f]})
            trace.append(dict(r,kind='block'));references+=r['references']
        selected_ids={b['block_id'] for b in blocks};note_index={n['note_id']:n for n in model['notes']}
        wanted={r['target_id'] for r in references if r['target_id'] in note_index}
        # Whole-book mode preserves orphan original notes as evidence, never drops them.
        if not selected:wanted.update(note_index)
        notes=[];note_renders=[];processed=set()
        while wanted-processed:
            nid=sorted(wanted-processed)[0];n=note_index[nid];r=rendered(n,'note');processed.add(nid)
            uses=[ref for ref in references if ref['target_id']==nid and ref['from_id'] in selected_ids]
            labels=[ref.get('original_label','') for ref in uses]
            if not labels or not all(labels):issues.append({'kind':'note_without_body_reference_or_label','note_id':nid})
            else:
                identity=classify(text_of(n['element']))
                notes.append({'note_id':nid,'origin':'original_document','responsibility':identity['responsibility'],'placement':n['placement'],
                    'original_label':labels[0],'agent_name':None,'agent_role':identity['agent_role'],'apparatus_kind':identity['apparatus_kind'],'edition_id':document_id,
                    'text':r['text'],'references':[ref['from_id'] for ref in uses],
                    'evidence':'Native semantic note '+n['locator']+'; '+identity['evidence'],
                    'verification':'unverified'})
                if len(set(labels))>1:issues.append({'kind':'note_label_variants','note_id':nid,'labels':labels})
            trace.append(dict(r,kind='note'));note_renders.append(r);references+=r['references']
            wanted.update(ref['target_id'] for ref in r['references'] if ref['target_id'] in note_index)
        present=selected_ids|processed
        for ref in references:
            if ref['state']=='resolved' and ref['target_id'] not in present:ref['state']='outside_selected_scope'
            if ref['state'] in ('unresolved','unsafe_scheme'):issues.append({'kind':'unresolved_reference','reference_id':ref['reference_id']})
        public_ch=[]
        # Retain ancestor TOC groups when selecting a child chapter.
        visible=set(chosen);lookup={c['chapter_id']:c for c in chapters}
        for cid in list(chosen):
            parent=lookup[cid]['parent_id']
            while parent:visible.add(parent);parent=lookup[parent]['parent_id']
        def desc(cid):
            values={cid};again=True
            while again:
                before=len(values);values.update(c['chapter_id'] for c in chapters if c['parent_id'] in values);again=before!=len(values)
            return values
        for c in chapters:
            if c['chapter_id'] not in visible:continue
            bs=[b for b in blocks if b['chapter_id'] in desc(c['chapter_id'])]
            if not bs:issues.append({'kind':'empty_toc_entry','chapter_id':c['chapter_id']});continue
            public_ch.append({'chapter_id':c['chapter_id'],'parent_id':c['parent_id'],'order':len(public_ch)+1,'title':c['title'],
                              'first_block_id':bs[0]['block_id'],'last_block_id':bs[-1]['block_id'],'printed_page_hint':None})
        documents={'blocks':dict(common,blocks=blocks),'toc':dict(common,chapters=public_ch),'notes':dict(common,notes=notes),
                   'source_map':dict(common,map_revision=cp['map_revision'],entries=maps)}
        checks=validate_correspondence(documents['blocks'],documents['source_map'],documents['toc'],documents['notes'])
        # Anchor + original Markdown body only. Review text stays in separate report.
        md='\n\n'.join('<a id="'+r['id']+'"></a>\n\n'+r['text'] for r in results+note_renders)+'\n'
        for name,value in documents.items():atomic(staging/(name+'.json'),value)
        atomic(staging/'source.md',md)
        atomic(task/'trace.json',{'private_format':'md-converter-native-trace-1','input_sha256':sha,'entries':trace,'references':references,
                                 'original_navigation':book.navigation,'original_page_list':book.page_list,'assets':list(assets.values())})
        report=dict(common,private_format='md-converter-report-2',status='needs_attention',quality_state='needs_review',
            requested_scope={'chapter_ids':sorted(chosen),'coverage_basis':'selected_scope' if selected else 'whole_document'},
            input_sha256=sha,configuration_hash=cfg,tool_versions={'adapter':ADAPTER_VERSION,'parser':'Python ZIP/XML native DOM','mineru':engine.get('mineru_version') if engine else None,'tier':'flash' if engine else None},
            elapsed_seconds=round(time.monotonic()-start,3),processed_spine=spine_indices,reused_spine=reused,rebuilt_spine=rebuilt,
            processed_pages=[],blank_pages=[],missing_pages=[],duplicated_fragments=[],unresolved_text=[],
            unresolved_notes=[{'note_id':n['note_id'],'reason':'responsibility unknown'} for n in notes if n['responsibility']=='unknown'],
            unresolved_refs=[r for r in references if r['state']=='unresolved'],asset_integrity={'copied_originals':len(assets)},
            map_verification=checks,corrections=[],issues=issues,technical_coverage_complete=not issues,
            academic_review='not_performed',publication_allowed=False,engine_evidence=engine,
            publication_gate='EPUB page_map product decision accepted; shared contract synchronization and private trace/assets definitions pending')
        atomic(staging/'conversion-report.json',report)
        cp['status']='needs_attention';cp['resume_log'].append({'reused':reused,'rebuilt':rebuilt});atomic(cp_path,cp)
        return {'status':'needs_attention','staging':str(staging),'trace':str(task/'trace.json'),'published':False,
                'checks':checks,'issues':len(issues)}
    except (Exception,KeyboardInterrupt) as e:
        cp['status']='interrupted' if isinstance(e,KeyboardInterrupt) else 'needs_attention';cp['last_error']=str(e);atomic(cp_path,cp);raise
