#!/usr/bin/env python3
"""Standalone native conversion and source lookup CLI (agent-facing)."""
import argparse
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from common import ConversionError, WorkLock, atomic, confined, digest, load, token
from epub_native import inspect, convert
from epub_structure import Book
from mapping import locate, require, validate_correspondence


def verify_merge_gate(task,staging,trace):
    """Prove the current scope was merged, including after a later interruption."""
    report=load(staging/'conversion-report.json');scope=report['requested_scope']
    bundled=(task/'bundle.json').exists()
    if trace['private_format']=='md-converter-pdf-trace-1':
        requested=scope['pdf_pages'];processed=report['processed_pages']
        require(len(requested)==len(set(requested)) and sorted(requested)==sorted(processed),
                'PDF target pages have not all been merged')
        if scope['coverage_basis']=='whole_document':
            inventory=load(staging/'page_map.json')['pages']
            require(requested==list(range(1,len(inventory)+1)),
                    'Full-book PDF claim does not cover every original file page')
        raw_pages={p['page_idx']+1 for record in trace['engine_runs']
                   for p in load(confined(task,record['path']))['pages']}
        require(raw_pages==set(requested),'PDF engine evidence does not cover exactly the reported scope')
        entries=load(staging/'source_map.json')['entries']
        first_pages=[e['fragments'][0]['pdf_page'] for e in entries]
        require(first_pages==sorted(first_pages),'PDF Markdown/source-map block order differs from original file-page order')
        batch_report=report.get('batch_execution')
        if batch_report:
            require(sorted(batch_report['target_pages'])==sorted(requested)
                    and set(batch_report['context_pages'])==raw_pages,
                    'PDF batch report differs from merged page coverage')
        if bundled:return {'scope_merged':True}
        checkpoint=task/'checkpoint.json'
        if not checkpoint.exists():
            require(scope['coverage_basis']!='whole_document',
                    'Full-book PDF draft requires a verified batch checkpoint')
            return {'scope_merged':True}
        cp=load(checkpoint);plan=load(task/'plan.json')
        require(cp.get('status')==report['status'] and cp['status'] in ('needs_attention','completed')
                and not cp.get('inflight'),'PDF merge is unfinished or a batch is in flight')
        require(cp['input_sha256']==trace['input_sha256']==plan['input_sha256']
                and cp['configuration']['requested_pages']==requested==plan['requested_scope']['pdf_pages'],
                'PDF checkpoint scope or source differs from merged report')
        require(cp['configuration'].get('selected_chapters',[])==plan.get('selection',{}).get('included_chapter_ids',[])
                ==report.get('selected_chapter_ids',[]),'PDF chapter selection changed since merge')
        require(batch_report is not None,'PDF batch merge evidence missing')
        from pdf_pipeline import valid_batch
        covered=set()
        for index,batch in enumerate(plan['batches']):
            require(valid_batch(task,cp['batches'].get(str(index)),trace['input_sha256'],batch['context_pdf_pages']),
                    'PDF batch invalid since merge')
            covered.update(batch['target_pdf_pages'])
        require(covered==set(requested),'PDF batch plan does not cover requested pages')
        return {'scope_merged':True}
    if trace['private_format']=='md-converter-native-trace-1':
        processed=set(report['processed_spine'])
        body_spine={entry['origin']['spine_index'] for entry in trace['entries'] if entry['kind']=='block'}
        require(processed==body_spine and processed,'EPUB spine coverage differs from merged body')
        if scope['coverage_basis']=='whole_document':
            from epub_structure import Book
            snapshot=task/'source.epub' if bundled else task.parent.parent/'originals/source.epub'
            book=Book(snapshot)
            try:require(processed==set(range(len(book.spine))),
                        'Full-book EPUB claim omits original spine content')
            finally:book.close()
        if bundled:return {'scope_merged':True}
        cp=load(task/'checkpoint.json')
        require(cp.get('status')==report['status'] and cp['status'] in ('needs_attention','completed'),
                'EPUB merge is unfinished')
        for index in processed:
            entry=cp['verified_batches'].get(str(index))
            require(entry is not None,'EPUB source spine missing from checkpoint')
            for f in entry['files']:
                require(digest(confined(task,f['path']).read_bytes())==f['sha256'],
                        'EPUB batch changed since merge')
        return {'scope_merged':True}
    raise ConversionError('Unknown source trace format')


def validate_task(task):
    task=Path(task);staging=task if (task/'bundle.json').exists() else task/'staging';trace=load(task/'trace.json')
    if (task/'bundle.json').exists():
        for f in load(task/'bundle.json')['files']:
            require(digest(confined(task,f['path']).read_bytes())==f['sha256'],'Bundle file changed: '+f['path'])
    if trace['private_format']=='md-converter-pdf-trace-1':
        from pdf_adapter import validate_pdf_task
        return dict(validate_pdf_task(task,staging,trace),**verify_merge_gate(task,staging,trace))
    docs={k:load(staging/(k+'.json')) for k in ('blocks','toc','source_map','notes')}
    checks=validate_correspondence(docs['blocks'],docs['source_map'],docs['toc'],docs['notes'])
    snapshot=task/'source.epub' if (task/'bundle.json').exists() else task.parent.parent/'originals/source.epub'
    require(digest(snapshot.read_bytes())==trace['input_sha256'],'Source snapshot changed')
    book=Book(snapshot)
    try:
        by_id={e['id']:e for e in trace['entries']}
        for e in trace['entries']:
            o=e['origin'];root=book.xml(o['href']);node=root
            require(o['node_path'].split('/')[0]=='0','Invalid trace root')
            for part in o['node_path'].split('/')[1:]:node=node[int(part)]
            require(node.get('id')==o['element_id'],'Native element locator mismatch')
            require(digest(ET.tostring(node,encoding='utf-8'))==o['markup_sha256'],'Original element changed')
        for b in docs['blocks']['blocks']:require(by_id[b['block_id']]['text']==b['text'],'Block differs from source trace')
        for n in docs['notes']['notes']:require(by_id[n['note_id']]['text']==n['text'],'Note differs from source trace')
        expected='\n\n'.join('<a id="'+e['id']+'"></a>\n\n'+e['text'] for e in trace['entries'])+'\n'
        require((staging/'source.md').read_text()==expected,'Markdown changed relative to mapped blocks/notes')
        for a in trace['assets']:
            p=confined(staging,a['path']);require(digest(p.read_bytes())==a['sha256'],'Asset changed')
            require(digest(book.zip.read(a['original_href']))==a['sha256'],'Asset differs from original EPUB')
        for ref in trace['references']:
            require(ref['from_id'] in by_id,'Reference owner missing');txt=by_id[ref['from_id']]['text'];a,z=ref['range']
            require(0<=a<z<=len(txt),'Reference range outside original block')
            if ref['state']=='resolved':require(ref['target_id'] in by_id,'Resolved link target missing')
        return dict(checks,native_nodes_verified=len(trace['entries']),assets_verified=len(trace['assets']),
                    source_sha256=trace['input_sha256'],academic_review='not_performed',
                    **verify_merge_gate(task,staging,trace))
    finally:book.close()


def lookup(task,identifier,start=0,end=None):
    task=Path(task);validate_task(task);trace=load(task/'trace.json')
    st=task if (task/'bundle.json').exists() else task/'staging'
    if trace['private_format']=='md-converter-pdf-trace-1':
        if identifier.startswith('pdf-page:'):
            try:page_number=int(identifier.split(':',1)[1])
            except ValueError as exc:raise ConversionError('Use pdf-page:<1-based file page>') from exc
            page=next((p for p in load(st/'page_map.json')['pages'] if p['pdf_page']==page_number),None)
            require(page is not None,'Unknown PDF file page')
            entries=load(st/'source_map.json')['entries']
            return {'page':page,'source_blocks':[{'source_id':e['source_id'],
                    'fragments':[f for f in e['fragments'] if f['pdf_page']==page_number]}
                    for e in entries if any(f['pdf_page']==page_number for f in e['fragments'])]}
        chapter=next((c for c in load(st/'toc.json')['chapters'] if c['chapter_id']==identifier),None)
        if chapter:
            locator=next((c for c in load(st/'chapter_locator.json')['chapters'] if c['chapter_id']==identifier),None)
            require(locator is not None,'Chapter source locator missing')
            return {'chapter':chapter,'source_location':locator,
                    'first_block_fragments':locate(load(st/'blocks.json'),load(st/'source_map.json'),chapter['first_block_id'])}
    row=next((e for e in trace['entries'] if e['id']==identifier),None);require(row is not None,'Unknown source/note ID')
    fragments=locate(load(st/'blocks.json'),load(st/'source_map.json'),identifier,start,end) if row['kind']=='block' else []
    if trace['private_format']=='md-converter-pdf-trace-1':
        snapshot=task/'source.pdf' if (task/'bundle.json').exists() else task.parent.parent/'originals/source.pdf'
        return {'id':identifier,'kind':row['kind'],'source_snapshot':str(snapshot.resolve()),'input_sha256':trace['input_sha256'],
                'origins':row['origins'],'fragments':fragments,'mapped_markdown':row['text'],
                'note_occurrences':[c for c in trace.get('note_occurrences',[]) if c['note_id']==identifier] if row['kind']=='note' else [],
                'precision':'Original PDF page and paragraph region; no character geometry inferred'}
    snapshot=task/'source.epub' if (task/'bundle.json').exists() else task.parent.parent/'originals/source.epub';book=Book(snapshot)
    try:
        node=book.xml(row['origin']['href'])
        for part in row['origin']['node_path'].split('/')[1:]:node=node[int(part)]
        return {'id':identifier,'kind':row['kind'],'source_snapshot':str(snapshot.resolve()),
                'input_sha256':trace['input_sha256'],'origin':row['origin'],'fragments':fragments,
                'mapped_markdown':row['text'],'original_markup':ET.tostring(node,encoding='unicode'),
                'precision':'native DOM element; no character geometry inferred'}
    finally:book.close()


def save_bundle(task,destination,name):
    """Copy a self-contained standalone draft, never overwrite user files or publish iReader."""
    checks=validate_task(task);task=Path(task).resolve();dest=Path(destination).absolute()
    require(name and name not in ('.','..') and '/' not in name and '\\' not in name,'Filename must be a plain display name')
    if name.endswith('.md'):name=name[:-3]
    require(name and name not in ('.','..'),'Empty filename')
    require(not dest.exists() and not dest.is_symlink(),'Destination already exists; choose a new folder')
    # copytree uses exclusive mkdir; existing user directories are never merged.
    require(not any(p.is_symlink() for p in (task/'staging').rglob('*')),'Staging contains a symlink')
    shutil.copytree(task/'staging',dest,symlinks=False)
    try:
        shutil.copy2(task/'trace.json',dest/'trace.json');
        if (task/'checkpoint.json').exists():shutil.copy2(task/'checkpoint.json',dest/'checkpoint.json')
        source_name='source.pdf' if load(task/'trace.json')['private_format']=='md-converter-pdf-trace-1' else 'source.epub'
        shutil.copy2(task.parent.parent/'originals'/source_name,dest/source_name)
        if (task/'raw').exists():
            require(not any(p.is_symlink() for p in (task/'raw').rglob('*')),'Raw evidence contains a symlink')
            shutil.copytree(task/'raw',dest/'raw')
        if name!='source':shutil.copy2(dest/'source.md',dest/(name+'.md'))
        files=[{'path':p.relative_to(dest).as_posix(),'sha256':digest(p.read_bytes())} for p in sorted(dest.rglob('*')) if p.is_file()]
        atomic(dest/'bundle.json',{'private_format':'md-converter-standalone-draft-1','entry_markdown':name+'.md',
             'source_sha256':checks['source_sha256'],'files':files,'quality_state':'needs_review',
             'compatibility':'Standalone draft; not an iReader published release. CR-converter-20261014-01/02 pending synchronization.'})
        return {'saved':str(dest),'markdown':str(dest/(name+'.md')),'format':'standalone_draft','checks':checks}
    except BaseException:
        # Preserve partial evidence; do not delete a user-selected directory recursively.
        atomic(dest/'SAVE-INCOMPLETE.txt','Save interrupted. This folder is incomplete; use a different destination to retry.\n');raise


def save_split_bundle(task,destination,name):
    """Save a reader copy beside an intact, translation-ready source bundle."""
    dest=Path(destination).absolute()
    require(name and name not in ('.','..') and '/' not in name and '\\' not in name
            and not any(ord(char)<32 for char in name),'Filename must be a plain display name')
    if name.endswith('.md'):name=name[:-3]
    require(name and name not in ('.','..'),'Empty filename')
    require(not dest.exists() and not dest.is_symlink(),'Destination already exists; choose a new folder')
    dest.mkdir(parents=True)
    try:
        source_dir=dest/'来源包'
        saved=save_bundle(task,source_dir,'source')
        reader_dir=dest/'阅读原文';reader_dir.mkdir()
        reader_md=reader_dir/(name+'.md')
        shutil.copy2(source_dir/'source.md',reader_md)
        if (source_dir/'assets').is_dir():shutil.copytree(source_dir/'assets',reader_dir/'assets')
        require(digest(reader_md.read_bytes())==digest((source_dir/'source.md').read_bytes()),
                'Reader copy differs from mapped source Markdown')
        blocks=load(source_dir/'blocks.json');bundle=load(source_dir/'bundle.json')
        source_original=source_dir/'source.pdf' if (source_dir/'source.pdf').is_file() else source_dir/'source.epub'
        require(source_original.is_file(),'Saved source package has no original file')
        original_copy=dest/(name+'-原件'+source_original.suffix)
        shutil.copy2(source_original,original_copy)
        original_sha=digest(original_copy.read_bytes())
        require(original_sha==digest(source_original.read_bytes())==saved['checks']['source_sha256'],
                'Reader-facing original differs from mapped source snapshot')
        paths={'original':original_copy.name,'readable_markdown':'阅读原文/'+reader_md.name,
               'source_bundle':'来源包','source_markdown':'来源包/source.md',
               'blocks':'来源包/blocks.json','toc':'来源包/toc.json',
               'source_map':'来源包/source_map.json','notes':'来源包/notes.json',
               'assets':'来源包/assets','bundle':'来源包/bundle.json'}
        if (source_dir/'page_map.json').is_file():paths['page_map']='来源包/page_map.json'
        atomic(dest/'文献索引.json',{'private_format':'md-converter-library-index-1',
              'document_id':blocks['document_id'],'source_revision':blocks['source_revision'],
              'quality_state':bundle['quality_state'],'original_sha256':original_sha,'paths':paths})
        guide=(
            '# 文件说明\n\n'
            f'- 文献 ID：`{blocks["document_id"]}`；原文版本：`{blocks["source_revision"]}`。\n'
            f'- 当前质量状态：`{bundle["quality_state"]}`。这是可追溯的原文草稿，不代表逐字校读完成。\n\n'
            '`文献索引.json` 记录相对路径和原件指纹，供网页工作台读取；它不是 iReader 的正式发布清单。\n\n'
            '## 原件\n\n'
            f'根目录的 `{original_copy.name}` 供直接查看或网页工作台使用；'
            f'它与 `来源包/{source_original.name}` 字节相同，SHA-256 为 `{original_sha}`。'
            '来源包内的快照用于校验和反查，修改任一副本都不会自动更新原文映射。\n\n'
            '## 阅读原文\n\n'
            f'`阅读原文/{name}.md` 是方便阅读、检索和批注的副本；图片资源在同目录的 `assets/`。'
            '修改这个副本不会同步更新段落 ID、页码和来源映射。\n\n'
            '## 来源包\n\n'
            '`来源包/` 是保持完整的转换成果，含 `source.md`、`blocks.json`、`source_map.json`、'
            '`page_map.json`（PDF）、`toc.json`、`notes.json`、原件快照、资源和 `bundle.json`。'
            '交给 universal-translate 时，提供整个 `来源包/` 文件夹；不要只提供阅读副本，也不要让翻译 Skill 重新为它分配段落 ID。'
            '原文校订须基于原页证据重建来源包，另存新版本，避免旧译文误对齐。\n\n'
            '## 后续翻译\n\n'
            '若选择翻译，试译、全文译文和对照导出另存于本文件夹下的 `翻译/` 与 `对照成品/`，'
            '都绑定上述原文版本。全文翻译仍须先经过翻译 Skill 的中间五页试译确认。\n'
        )
        atomic(dest/'文件说明.md',guide)
        return {'saved':str(dest),'readable_markdown':str(reader_md),'original_file':str(original_copy),
                'translation_source':str(source_dir),'format':'split_standalone_draft',
                'checks':saved['checks']}
    except BaseException:
        atomic(dest/'SAVE-INCOMPLETE.txt','Save interrupted. This folder is incomplete; use a different destination to retry.\n')
        raise


def export_reading(source_bundle, output_dir, name, formats='md', source_id_file=None, soffice=None):
    cmd=[sys.executable,str(Path(__file__).with_name('export_reading.py')),
         str(source_bundle),str(output_dir),'--name',name,'--formats',formats]
    if source_id_file:cmd.extend(['--source-id-file',str(source_id_file)])
    if soffice:cmd.extend(['--soffice',str(soffice)])
    proc=subprocess.run(cmd,capture_output=True,text=True)
    require(proc.returncode==0,'Reading export failed: '+(proc.stderr or proc.stdout).strip())
    return json.loads(proc.stdout)


def export_translation_input(source_bundle, destination, name, original_file):
    output=Path(destination)/'翻译输入'/'原文增强.md'
    cmd=[sys.executable,str(Path(__file__).with_name('export_translation_md.py')),
         str(source_bundle),str(output),'--title',name,
         '--original',str(original_file),
         '--library-index',str(Path(destination)/'文献索引.json')]
    proc=subprocess.run(cmd,capture_output=True,text=True)
    require(proc.returncode==0,'Translation handoff export failed: '+(proc.stderr or proc.stdout).strip())
    return json.loads(proc.stdout)


def main():
    p=argparse.ArgumentParser(description='md-converter: native candidates, evidence and exact source lookup')
    sub=p.add_subparsers(dest='command',required=True)
    for command in ('inspect','convert-epub'):
        c=sub.add_parser(command);c.add_argument('source',type=Path);c.add_argument('--work-dir',type=Path,required=True);c.add_argument('--document-id',required=True)
        if command=='convert-epub':
            c.add_argument('--task-id',required=True);c.add_argument('--chapter-id',action='append');c.add_argument('--stop-after',type=int);c.add_argument('--engine-result',type=Path)
    c=sub.add_parser('convert-pdf');c.add_argument('source',type=Path);c.add_argument('--runtime',type=Path,required=True);c.add_argument('--service',required=True)
    c.add_argument('--work-dir',type=Path,required=True);c.add_argument('--document-id',required=True);c.add_argument('--task-id',required=True)
    c.add_argument('--pages');c.add_argument('--chapter-id',action='append');c.add_argument('--batch-size',type=int,default=8);c.add_argument('--stop-after',type=int);c.add_argument('--review',type=Path)
    c=sub.add_parser('inspect-pdf');c.add_argument('source',type=Path);c.add_argument('--work-dir',type=Path,required=True)
    c.add_argument('--document-id',required=True);c.add_argument('--batch-size',type=int,default=8);c.add_argument('--review',type=Path)
    c=sub.add_parser('parse-local');c.add_argument('source',type=Path);c.add_argument('--runtime',type=Path,required=True)
    c.add_argument('--output',type=Path,required=True);c.add_argument('--tier',choices=['standard','advanced','flash'],default='standard')
    c.add_argument('--pages',help='1-based comma-separated file pages');c.add_argument('--service')
    c=sub.add_parser('adapt-pdf');c.add_argument('source',type=Path);c.add_argument('--raw',type=Path,action='append',required=True)
    c.add_argument('--work-dir',type=Path,required=True);c.add_argument('--document-id',required=True);c.add_argument('--task-id',required=True);c.add_argument('--review',type=Path)
    c=sub.add_parser('validate');c.add_argument('task',type=Path)
    c=sub.add_parser('locate');c.add_argument('task',type=Path);c.add_argument('identifier');c.add_argument('--start',type=int,default=0);c.add_argument('--end',type=int)
    c=sub.add_parser('save');c.add_argument('task',type=Path);c.add_argument('destination',type=Path);c.add_argument('--name',required=True)
    c.add_argument('--layout',choices=['flat','split'],default='flat');c.add_argument('--formats',default='md');c.add_argument('--soffice')
    c=sub.add_parser('export-reading');c.add_argument('source_bundle',type=Path);c.add_argument('output_dir',type=Path)
    c.add_argument('--name',required=True);c.add_argument('--formats',default='md');c.add_argument('--source-id-file',type=Path);c.add_argument('--soffice')
    c=sub.add_parser('service-status');c.add_argument('--work-dir',type=Path,required=True);c.add_argument('--shared-service');c.add_argument('--runtime',type=Path)
    c=sub.add_parser('start-service');c.add_argument('--work-dir',type=Path,required=True);c.add_argument('--runtime',type=Path,required=True);c.add_argument('--port',type=int)
    c=sub.add_parser('stop-service');c.add_argument('--work-dir',type=Path,required=True)
    c=sub.add_parser('exit');c.add_argument('--task',type=Path);c.add_argument('--work-dir',type=Path);c.add_argument('--stop-owned-service',action='store_true')
    a=p.parse_args()
    try:
        if a.command=='inspect':result=inspect(a.source,a.work_dir,a.document_id)
        elif a.command=='convert-epub':result=convert(a.source,a.work_dir,a.document_id,a.task_id,a.chapter_id,a.stop_after,a.engine_result)
        elif a.command=='convert-pdf':
            from pdf_pipeline import convert_pdf
            result=convert_pdf(a.source,a.work_dir,a.document_id,a.task_id,a.runtime,a.service,[int(x) for x in a.pages.split(',')] if a.pages else None,
                               a.batch_size,load(a.review) if a.review else None,a.stop_after,chapter_ids=a.chapter_id)
        elif a.command=='inspect-pdf':
            from pdf_preflight import preflight
            base=confined(a.work_dir,'books/'+token(a.document_id))
            with WorkLock(base/'.converter-file.lock'):
                result=preflight(a.source,a.document_id,batch_size=a.batch_size,work_dir=a.work_dir,
                                 review=load(a.review) if a.review else None)
        elif a.command=='parse-local':
            from mineru_runner import parse_local
            result=parse_local(a.source,a.runtime,a.output,a.tier,[int(x) for x in a.pages.split(',')] if a.pages else None,a.service)
        elif a.command=='adapt-pdf':
            from pdf_adapter import adapt
            result=adapt(a.source,a.raw,a.work_dir,a.document_id,a.task_id,load(a.review) if a.review else None)
        elif a.command=='validate':result=validate_task(a.task)
        elif a.command=='locate':result=lookup(a.task,a.identifier,a.start,a.end)
        elif a.command=='save':
            if a.layout=='split':
                result=save_split_bundle(a.task,a.destination,a.name)
                try:
                    result['translation_input']=export_translation_input(Path(result['saved'])/'来源包',Path(result['saved']),a.name,result['original_file'])
                    result['annotated_reading']=export_reading(Path(result['saved'])/'来源包',Path(result['saved'])/'阅读原文'/'标注版',a.name,a.formats,soffice=a.soffice)
                except BaseException:
                    atomic(Path(result['saved'])/'SAVE-INCOMPLETE.txt','A requested derivative export failed; source package remains saved, but delivery is incomplete.\n')
                    raise
                index_path=Path(result['saved'])/'文献索引.json'
                index=load(index_path)
                index['paths']['annotated_reading']={key:Path(value).relative_to(result['saved']).as_posix()
                                                   for key,value in result['annotated_reading']['outputs'].items()}
                atomic(index_path,index)
                with (Path(result['saved'])/'文件说明.md').open('a',encoding='utf-8') as guide:
                    guide.write('\n## 标注版原文\n\n`阅读原文/标注版/` 是按来源映射生成的阅读版，含页码、段落标记和两端对齐。'
                                'Word/PDF 导出时，`抽样预览/` 保存目录、正文、注释的三页预览和 `导出记录.json`。'
                                '它是派生文件，不改变 `来源包/source.md`。\n')
                    guide.write('\n## 翻译交接\n\n`翻译输入/原文增强.md` 是轻量可读交接稿；'
                                '同目录交接索引指向 `来源包/` 中的精确映射。'
                                '仅在明确选择翻译时才开始试译，未选择时不创建译文文件。\n')
            else:
                require(a.formats=='md','Additional reading formats require --layout split')
                result=save_bundle(a.task,a.destination,a.name)
        elif a.command=='export-reading':result=export_reading(a.source_bundle,a.output_dir,a.name,a.formats,a.source_id_file,a.soffice)
        elif a.command=='service-status':
            from service_lifecycle import status
            result=status(a.work_dir,a.shared_service,a.runtime)
        elif a.command=='start-service':
            from service_lifecycle import start
            result=start(a.work_dir,a.runtime,a.port)
        elif a.command=='stop-service':
            from service_lifecycle import stop
            result=stop(a.work_dir)
        else:
            from service_lifecycle import exit_task
            result=exit_task(a.task,a.work_dir,a.stop_owned_service)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ConversionError,OSError,ValueError,KeyError,IndexError,ET.ParseError) as e:p.exit(2,str(e)+'\n')

if __name__=='__main__':main()
