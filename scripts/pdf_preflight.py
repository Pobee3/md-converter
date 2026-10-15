#!/usr/bin/env python3
"""pypdf-only metadata/page inventory and non-executing MinerU 4 plan."""
import argparse
import importlib.metadata
import json
import re
from pathlib import Path
from common import ConversionError, Registry, atomic, confined, digest, token

def _normalized(value):
    return ''.join(c for c in str(value).casefold() if c.isalnum())

def outline_chapters(reader, registry=None):
    """Bookmark candidates with original-page evidence, not a fabricated printed TOC."""
    chapters=[]
    def visit(items,parent=None,depth=0):
        previous=None
        for item in items:
            if isinstance(item,list):
                visit(item,previous or parent,depth+1)
                continue
            index=len(chapters)
            try:zero=reader.get_destination_page_number(item)
            except (KeyError,TypeError,ValueError):zero=None
            page=zero+1 if type(zero) is int and 0<=zero<len(reader.pages) else None
            title=str(item.title).strip()
            key='outline:'+str(index)+':'+str(zero)
            cid=registry.get('ch',key) if registry else key
            top=item.get('/Top') if hasattr(item,'get') else None
            height=float(reader.pages[zero].mediabox.height) if page else None
            y=round(1-float(top)/height,6) if height and isinstance(top,(int,float)) else None
            if y is not None and not 0<=y<=1:y=None
            page_text=reader.pages[zero].extract_text() or '' if page else ''
            title_hit=bool(title and _normalized(title) in _normalized(page_text))
            chapters.append({'chapter_id':cid,'parent_id':parent,'title':title,'pdf_page':page,
                'start_y':y,'depth':depth,'title_text_hit':title_hit,
                'evidence':{'bookmark_index':index,'bookmark_title':title,'destination_pdf_page':page,
                    'destination_top_pdf':float(top) if isinstance(top,(int,float)) else None,
                    'title_in_pdf_text_layer':title_hit},
                'boundary_state':'candidate' if page and y is not None and title_hit else 'needs_review'})
            previous=cid
    visit(reader.outline)
    return chapters

def reviewed_catalog(reader, registry, review):
    """Assistant-reviewed original TOC for PDFs without trustworthy bookmarks."""
    decisions=[d for d in review.get('decisions',[]) if d.get('kind')=='toc_catalog']
    if not decisions:return None
    if len(decisions)!=1 or registry is None:raise ConversionError('One reviewed TOC catalog requires a persistent work directory')
    d=decisions[0]
    if d.get('reviewed') is not True or not d.get('evidence') or not d.get('chapters'):
        raise ConversionError('Original TOC catalog needs reviewed source evidence')
    result=[];keys={};previous=(0,-1.0)
    for index,row in enumerate(d['chapters']):
        key=row.get('key');page=row.get('pdf_page');y=row.get('start_y');parent=row.get('parent_key')
        if not isinstance(key,str) or not key or key in keys or not isinstance(row.get('title'),str) or not row['title'].strip():
            raise ConversionError('Duplicate or empty original TOC chapter')
        if type(page) is not int or not 1<=page<=len(reader.pages) or y is not None and (type(y) not in (int,float) or not 0<=y<=1):
            raise ConversionError('Original TOC chapter source position invalid')
        if parent is not None and parent not in keys:raise ConversionError('Original TOC parent must precede child')
        if (page,y if y is not None else -1)<previous:raise ConversionError('Original TOC chapter positions out of order')
        if not row.get('evidence'):raise ConversionError('Original TOC chapter missing page evidence')
        cid=registry.get('ch','catalog:'+key);title=row['title'].strip();hit=_normalized(title) in _normalized(reader.pages[page-1].extract_text() or '')
        result.append({'chapter_id':cid,'parent_id':keys.get(parent),'title':title,'pdf_page':page,
            'start_y':y,'depth':result[[c['chapter_id'] for c in result].index(keys[parent])]['depth']+1 if parent is not None else 0,
            'title_text_hit':hit,'boundary_state':'reviewed' if y is not None else 'needs_review',
            'evidence':{'original_toc_review':row['evidence'],'title_in_pdf_text_layer':hit,
                        'catalog_key':key,'catalog_review':d['evidence']}})
        keys[key]=cid;previous=(page,y if y is not None else -1)
    return result

def selected_chapter_pages(chapters,selected,page_count):
    if not chapters:raise ConversionError('PDF has no selectable chapter candidates; review original TOC first')
    by_id={c['chapter_id']:i for i,c in enumerate(chapters)}
    if not selected or any(cid not in by_id for cid in selected):raise ConversionError('Unknown or empty PDF chapter selection')
    pages=set();included=set()
    for cid in selected:
        start=by_id[cid];chapter=chapters[start]
        if chapter['pdf_page'] is None:raise ConversionError('Selected chapter has no valid PDF destination')
        for neighbor in chapters:
            if neighbor['pdf_page']==chapter['pdf_page'] and neighbor['chapter_id']!=cid and (neighbor['start_y'] is None or chapter['start_y'] is None):
                raise ConversionError('Same-page chapter boundary needs an original-page position review')
        finish=next((j for j in range(start+1,len(chapters)) if chapters[j]['depth']<=chapter['depth']),len(chapters))
        stop=chapters[finish]['pdf_page'] if finish<len(chapters) else page_count
        if stop is None or stop<chapter['pdf_page']:raise ConversionError('Chapter destinations are out of order')
        pages.update(range(chapter['pdf_page'],min(stop,page_count)+1))
        included.update(c['chapter_id'] for c in chapters[start:finish])
    return sorted(pages),sorted(included)

def preflight(source, document_id, runtime=None, pages=None, batch_size=8, work_dir=None, chapter_ids=None, review=None):
    from pypdf import PdfReader
    if batch_size < 1: raise ConversionError('batch_size must be positive')
    data=Path(source).read_bytes();r=PdfReader(Path(source))
    if r.is_encrypted: raise ConversionError('Encrypted PDF; no automatic password guessing')
    n=len(r.pages)
    if chapter_ids and pages is not None:raise ConversionError('Choose chapter IDs or file pages, not both')
    registry=None
    if work_dir is not None:
        base=confined(Path(work_dir),'books/'+token(document_id))
        registry=Registry(base/'revisions/converter-id-registry.json',digest(data),document_id)
    if review is not None and review.get('input_sha256')!=digest(data):raise ConversionError('Review belongs to another PDF')
    chapters=reviewed_catalog(r,registry,review) if review is not None else None
    toc_basis='reviewed_original_catalog' if chapters is not None else 'pdf_bookmarks'
    if chapters is None:chapters=outline_chapters(r,registry)
    selected_chapters=[]
    if chapter_ids:selected,selected_chapters=selected_chapter_pages(chapters,chapter_ids,n)
    else:selected=list(range(1,n+1)) if pages is None else sorted(set(pages))
    if not selected or any(i<1 or i>n for i in selected):raise ConversionError('Invalid physical page range')
    labels=r.page_labels if '/PageLabels' in r.trailer['/Root'] else [None]*n
    inventory=[];original_toc_page_candidates=[]
    for i,page in enumerate(r.pages):
        rotation=int(page.get('/Rotate',0))%360
        if rotation not in [0,90,180,270]:raise ConversionError('Unsupported rotation')
        text=page.extract_text() or ''
        lines=[line.strip() for line in text.splitlines() if line.strip()]
        edge=lines[:6]+lines[-6:]
        if i<40 and any(re.fullmatch(r'(?i)contents|table of contents|inhalt|目录|目次',line) for line in lines[:25]):
            original_toc_page_candidates.append({'pdf_page':i+1,'evidence':'PDF text-layer heading; inspect original page image',
                                                 'verification':'unverified'})
        candidates=list(dict.fromkeys(line for line in edge if re.fullmatch(r'(?i)[ivxlcdm]{1,12}|[0-9]{1,5}',line)))
        inventory.append({'pdf_page':i+1,'viewer_label':labels[i], 'label_state':'unknown',
            'printed_labels':[],'printed_label_candidates':[{'label_raw':v,'evidence':'PDF text-layer edge line; verify original page image','verification':'unverified'} for v in candidates],
            'edition_markers':[],'logical_order':i+1,'rotation':rotation})
    reviewed_evidence={}
    if review is not None:
        if review.get('input_sha256')!=digest(data):raise ConversionError('Page review belongs to another PDF')
        reviewed=set()
        for decision in review.get('decisions',[]):
            if decision.get('kind')=='toc_confirmation':
                rows=decision.get('chapters',[])
                if decision.get('reviewed') is not True or not decision.get('evidence') or len(rows)!=len(chapters):
                    raise ConversionError('TOC confirmation must cover all source chapter candidates with evidence')
                by_id={c['chapter_id']:c for c in chapters}
                if {c.get('chapter_id') for c in rows}!=set(by_id):raise ConversionError('TOC confirmation differs from source chapter IDs')
                for row in rows:
                    chapter=by_id[row['chapter_id']]
                    if row.get('pdf_page')!=chapter['pdf_page'] or row.get('title')!=chapter['title'] or not row.get('evidence'):
                        raise ConversionError('TOC confirmation differs from original-page candidate')
                    chapter['boundary_state']='reviewed'
                    chapter['evidence']['original_page_review']=row['evidence']
                continue
            if decision.get('kind')!='page_labels':continue
            page=decision.get('pdf_page')
            if type(page) is not int or not 1<=page<=n or page in reviewed or decision.get('reviewed') is not True or not decision.get('evidence'):
                raise ConversionError('Invalid or duplicate reviewed page label decision')
            reviewed.add(page)
            reviewed_evidence[page]=decision['evidence']
            if decision.get('label_state') not in ('known','absent') or bool(decision.get('printed_labels'))!=(decision['label_state']=='known'):
                raise ConversionError('Reviewed printed-page state is inconsistent')
            inventory[page-1]['label_state']=decision['label_state']
            inventory[page-1]['printed_labels']=decision['printed_labels']
            inventory[page-1]['edition_markers']=decision.get('edition_markers',[])
    for chapter in chapters:
        if chapter['pdf_page'] is not None:
            page=inventory[chapter['pdf_page']-1]
            chapter['viewer_label']=page['viewer_label']
            chapter['printed_label_state']=page['label_state']
            chapter['visible_printed_labels']=[label['label_raw'] for label in page['printed_labels']]
            chapter['printed_page_evidence']=[label['evidence'] for label in page['printed_labels']]
            if page['label_state']=='absent':chapter['printed_page_evidence']=[reviewed_evidence[chapter['pdf_page']]]
    batches=[]
    for start in range(0,len(selected),batch_size):
        batch=selected[start:start+batch_size]
        # Context overlap only when physically adjacent; never renumber extracted pages.
        context=([selected[start-1]] if start and selected[start-1]+1==batch[0] else [])+batch
        args=[str(runtime),'-m','mineru.kit.main','parse',str(Path(source).absolute()),
            '--tier','standard','--pages',','.join(map(str,context)),'--format','middle_json',
            '--output',f'batch-{len(batches)+1}.middle.json'] if runtime else None
        batches.append({'target_pdf_pages':batch,'context_pdf_pages':context,'argv_proposal':args})
    return {'private_format':'md-converter-pdf-plan-1','document_id':document_id,
        'input_sha256':digest(data),'source_type':'pdf','page_count':n,'requested_scope':{'pdf_pages':selected},
        'selection':{'chapter_ids':chapter_ids or [],'included_chapter_ids':selected_chapters},
        'chapters':chapters,'toc_basis':toc_basis,'original_toc_page_candidates':original_toc_page_candidates,
        'tool_versions':{'pypdf':importlib.metadata.version('pypdf')},'pages':inventory,'batches':batches,
        'status':'needs_attention','parse_executed':False,'unresolved':['Visible printed labels, edition markers, blank pages, text/scan classification and TOC require source review.',
        'Preflight does not execute MinerU or establish fidelity; validate the selected runtime and actual candidate output separately.'],
        'review_policy':'Keep Standard and Advanced candidates separately; select against original page evidence.'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('--document-id',required=True)
    p.add_argument('--mineru-python',type=Path);p.add_argument('--pages',help='1-based comma separated physical pages')
    p.add_argument('--batch-size',type=int,default=8);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    try:
        result=preflight(a.source,a.document_id,a.mineru_python,[int(x) for x in a.pages.split(',')] if a.pages else None,a.batch_size)
        atomic(a.output,result)
    except (ConversionError,ValueError) as e:p.exit(2,str(e)+'\n')
