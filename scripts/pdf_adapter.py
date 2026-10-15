"""Adapt actual MinerU 4 middle 2.0 evidence to iReader core candidates.

No implicit paragraph joins, note ownership guesses, or printed-page offsets.
Reviewed decisions are agent-authored after comparison with source pages.
"""
import re
from contextlib import nullcontext
from pathlib import Path
from common import Registry, WorkLock, atomic, confined, digest, fingerprint, load, token
from mapping import require, validate_correspondence, merge_paragraphs
from note_identity import classify
from epub_structure import escape
from pdf_preflight import preflight, outline_chapters, reviewed_catalog
from pdf_content import plain_spans, render_spans, render_index, reviewed_edits
from pdf_suspects import review_candidates
from pdf_note_links import pair_candidates


def roman_value(raw):
    if not raw or not re.fullmatch(r'(?i)M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})',raw):return None
    vals={'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000};s=raw.upper()
    return sum(-vals[c] if i+1<len(s) and vals[c]<vals[s[i+1]] else vals[c] for i,c in enumerate(s))


def visible_label(raw,edition,sequence,evidence):
    numeric=int(raw) if re.fullmatch(r'[0-9]+',raw) else roman_value(raw)
    scheme='arabic' if re.fullmatch(r'[0-9]+',raw) else 'roman' if numeric is not None else 'other'
    return {'edition_id':edition,'sequence_id':sequence,'label_raw':raw,'scheme':scheme,'numeric_value':numeric,
            'evidence':evidence,'verification':'unverified'}


def adapt(source,raw_paths,work_dir,document_id,task_id,review=None,_lock_held=False,selected_chapters=None):
    from pypdf import PdfReader
    source=Path(source).resolve();data=source.read_bytes();sha=digest(data)
    base=confined(Path(work_dir),'books/'+token(document_id));base.mkdir(parents=True,exist_ok=True)
    with (nullcontext() if _lock_held else WorkLock(base/'.converter-file.lock')):
        registry=Registry(base/'revisions/converter-id-registry.json',sha,document_id)
        task=confined(base,'tasks/'+token(task_id));task.mkdir(parents=True,exist_ok=True)
        staging=task/'staging';staging.mkdir(exist_ok=True)
        decisions=review or {'input_sha256':sha,'decisions':[]}
        require(decisions['input_sha256']==sha,'Review belongs to another source')
        overlap_reviews={};overlap_observed={};overlap_selected=set()
        for d in decisions['decisions']:
            if d['kind']=='overlap_resolution':
                require(d.get('reviewed') is True and bool(d.get('evidence','').strip()),'Overlap resolution needs original evidence')
                require(d['pdf_page'] not in overlap_reviews,'Duplicate overlap review')
                overlap_reviews[d['pdf_page']]=d;overlap_observed[d['pdf_page']]=set()
        rows=[];seen={};seen_rows={};context_variants=[];raw_records=[];engine_pages=set();issues=[]
        for raw_path in raw_paths:
            raw_path=Path(raw_path);run=load(raw_path.with_suffix('.run.json'));raw=load(raw_path)
            require(run['status']=='completed' and run['input_sha256']==sha and run['output_sha256']==digest(raw_path.read_bytes()),'Engine result not bound to source')
            require(raw.get('schema')=='docvortex.middle' and raw.get('schema_version')=='2.0','Unknown engine schema')
            require(raw.get('extensions',{}).get('mineru',{}).get('tier')=='standard','Base conversion must use Standard; Advanced is separate review evidence')
            record={'path':'raw/'+digest(raw_path.read_bytes())+'.json','sha256':digest(raw_path.read_bytes()),'run':run}
            atomic(confined(task,record['path']),raw_path.read_bytes());raw_records.append(record)
            for p in raw['pages']:
                page=p['page_idx']+1;engine_pages.add(page)
                if page in overlap_reviews:
                    d=overlap_reviews[page];value=fingerprint(p);overlap_observed[page].add(value)
                    require(value in d['candidate_page_fingerprints'],'Overlap review is stale: new page candidate')
                    if record['sha256']!=d['selected_raw_sha256']:continue
                    overlap_selected.add(page)
                for block in p.get('blocks',[]):
                    key=str(page)+':'+str(block['index']);value=fingerprint(block)
                    if key in seen:
                        if seen[key]!=value:
                            previous=seen_rows[key];old=previous['raw']
                            # This hint depends on batch context and is never used to
                            # join paragraphs. Only a boolean/absent hint may differ;
                            # content, bbox, type, order and unknown fields stay strict.
                            valid_hint=all('continues_prev' not in b or type(b['continues_prev']) is bool for b in (old,block))
                            without_hint=lambda b:{k:v for k,v in b.items() if k!='continues_prev'}
                            require(valid_hint and fingerprint(without_hint(old))==fingerprint(without_hint(block)),
                                    'Overlap batch disagrees; original comparison required')
                            context_variants.append({'at':key,'field':'continues_prev',
                                'first_raw_path':previous['raw_path'],'other_raw_path':record['path'],
                                'first_value':old.get('continues_prev'),'other_value':block.get('continues_prev'),
                                'action':'Equivalent content/region retained once; no paragraph join inferred'})
                        # Equal content can come from several batch files. Choose a
                        # stable evidence path so argument order cannot change maps.
                        if record['path']<seen_rows[key]['raw_path']:
                            seen_rows[key].update(raw=block,raw_path=record['path']);seen[key]=value
                        continue
                    seen[key]=value;row={'key':key,'page':page,'raw':block,'raw_path':record['path']}
                    seen_rows[key]=row;rows.append(row)
        require(overlap_selected==set(overlap_reviews),'Selected overlap evidence missing')
        for page,d in overlap_reviews.items():
            require(overlap_observed[page]==set(d['candidate_page_fingerprints']),'Overlap candidate evidence set changed')
        plan=preflight(source,document_id,pages=sorted(engine_pages));pages=plan['pages'];reader=PdfReader(source)
        for page_record in pages:page_record.pop('printed_label_candidates',None)
        issues.extend(review_candidates(rows))
        require(all(1<=p<=len(pages) for p in engine_pages),'Engine physical page out of source bounds')
        catalog_rows=reviewed_catalog(reader,registry,decisions)
        outline_rows=catalog_rows if catalog_rows is not None else outline_chapters(reader,registry)
        chapters=[{'chapter_id':c['chapter_id'],'title':c['title'],'parent_id':c['parent_id'],
                   'start':c['pdf_page'],'start_y':c['start_y'],'boundary_state':c['boundary_state'],
                   'evidence':c['evidence']} for c in outline_rows if c['pdf_page'] is not None]
        if chapters and catalog_rows is None:
            issues.append({'kind':'toc_bookmarks_require_source_review',
                           'reason':'PDF bookmarks are candidates, not evidence of the original printed hierarchy.'})
        if not chapters:
            issues.append({'kind':'missing_original_toc'})
            chapters=[{'chapter_id':registry.get('ch','unconfirmed-document'),'title':source.stem,'parent_id':None,'start':1}]
        excluded={};joins=[];note_joins=[];note_reviews={};page_reviews={};type_reviews={};text_reviews={};note_splits={};paragraph_splits={};toc_review=None;toc_confirmation=None
        for d in decisions['decisions']:
            require(d.get('reviewed') is True and bool(d.get('evidence','').strip()),'Review decision lacks evidence')
            if d['kind']=='exclude':excluded[d['at']]=d
            elif d['kind']=='overlap_resolution':pass
            elif d['kind']=='join':joins.append(d)
            elif d['kind']=='join_notes':note_joins.append(d)
            elif d['kind']=='note':note_reviews[d['at']]=d
            elif d['kind']=='page_labels':page_reviews[d['pdf_page']]=d
            elif d['kind']=='block_type':
                require(d['at'] in seen and d['type'] in ('text','paragraph_title','header','footer','page_footnote'),
                        'Invalid reviewed block classification')
                type_reviews[d['at']]=d
            elif d['kind']=='text_correction':
                require(d['at'] in seen and d['at'] not in text_reviews,'Correction target missing or duplicated')
                text_reviews[d['at']]=d
            elif d['kind']=='split_notes':
                require(d['at'] in seen and d['at'] not in note_splits,'Note split target missing or duplicated')
                note_splits[d['at']]=d
            elif d['kind']=='split_paragraph':
                require(d['at'] in seen and d['at'] not in paragraph_splits,'Paragraph split target missing or duplicated')
                paragraph_splits[d['at']]=d
            elif d['kind']=='toc':
                require(toc_review is None,'Multiple reviewed TOC replacements')
                toc_review=d
            elif d['kind']=='toc_confirmation':
                require(toc_confirmation is None and toc_review is None,'Duplicate or conflicting TOC review')
                toc_confirmation=d
            elif d['kind']=='toc_catalog':pass
            else:raise ValueError('Unsupported review decision')
        require(not (toc_review and (toc_confirmation or catalog_rows is not None)) and not (toc_confirmation and catalog_rows is not None),
                'Choose one original TOC review method')
        if toc_confirmation:
            require(len(toc_confirmation.get('chapters',[]))==len(chapters),
                    'TOC confirmation must cover all source chapter candidates')
            reviewed={c['chapter_id']:c for c in toc_confirmation['chapters']}
            require(set(reviewed)=={c['chapter_id'] for c in chapters},'TOC confirmation chapter IDs changed')
            for c in chapters:
                row=reviewed[c['chapter_id']]
                require(row.get('pdf_page')==c['start'] and row.get('title')==c['title'] and bool(row.get('evidence')),
                        'TOC confirmation differs from source candidate')
                c['boundary_state']='reviewed'
                c['evidence']['original_page_review']=row['evidence']
            issues=[i for i in issues if i['kind']!='toc_bookmarks_require_source_review']
        if toc_review:
            chapters=[];chapter_keys={};previous=(0,-1)
            for c in toc_review['chapters']:
                require(c['key'] not in chapter_keys and c['at'] in seen,'Duplicate chapter key or missing target block')
                position=tuple(map(int,c['at'].split(':')))
                require(position>=previous,'Reviewed TOC must follow source reading order')
                require(c.get('parent_key') is None or c['parent_key'] in chapter_keys,'Reviewed chapter parent missing')
                require(isinstance(c['title'],str) and c['title'].strip(),'Reviewed chapter title missing')
                cid=registry.get('ch','reviewed-toc:'+c['key']);chapter_keys[c['key']]=cid;previous=position
                chapters.append({'chapter_id':cid,'title':c['title'],'parent_id':chapter_keys.get(c.get('parent_key')),
                                 'start':position[0],'start_block':position[1],'printed_page_hint':c.get('printed_page_hint'),
                                 'boundary_state':'reviewed','evidence':toc_review['evidence']})
            require(chapters,'Empty reviewed TOC')
            issues=[i for i in issues if i['kind'] not in ('missing_original_toc','toc_bookmarks_require_source_review')]
        selected_set=set(selected_chapters or [])
        if selected_set:
            require(selected_set.issubset({c['chapter_id'] for c in chapters}),
                    'Selected chapter IDs no longer match the source TOC; re-inspect before conversion')
        blocks=[];maps=[];traces=[];notes_raw=[];loc_ids={};aux=[];assets=[];migrations=[];canonical_by_raw={}
        for row in sorted(rows,key=lambda r:(r['page'],r['raw']['index'])):
            block=row['raw'];typ=block['type'];key=row['key'];page=row['page'];bbox=block.get('bbox')
            if key in type_reviews:typ=type_reviews[key]['type']
            loc={'source_type':'pdf','pdf_page':page,'bbox':bbox,'raw_path':row['raw_path'],'engine_block_index':block['index']}
            if key in excluded:
                aux.append({'origin':loc,'reason':excluded[key],'original_block':block});continue
            if typ in ('header','footer','page_number'):
                aux.append({'origin':loc,'reason':'engine auxiliary classification; retained for review','original_block':block})
                if typ=='page_number':
                    raw=plain_spans(block).strip()
                    if raw:
                        # MinerU classification and PDF PageLabels are clues,
                        # not proof that this glyph is the printed page number.
                        issues.append({'kind':'printed_page_candidate_requires_review','pdf_page':page,
                                       'at':key,'label_candidate':raw,'bbox':bbox,
                                       'reason':'Compare the visible original page; do not infer a printed label from the engine or viewer label.'})
                continue
            if typ=='aside_text':
                # PDF margins may contain line numbers, original-edition page
                # codes, or substantive marginal notes. Never fold them into
                # body text or infer their role from a bare numeric value.
                marginal=render_spans(block)
                aux.append({'origin':loc,'reason':'unclassified original marginal text','original_block':block})
                issues.append({'kind':'marginal_text_requires_classification','page':page,'index':block['index'],
                               'text':marginal,'reason':'Inspect original page; distinguish line number, edition marker and marginal note.'})
                continue
            if typ=='page_footnote':
                if key in note_splits:
                    cursor=0;part_keys=set()
                    for part in note_splits[key]['parts']:
                        start,end=part['span_range'];part_key=part['key']
                        require(type(start) is int and type(end) is int and start==cursor<end<=len(block['content']),
                                'Note split must cover all original spans without overlap')
                        require(isinstance(part_key,str) and part_key and part_key not in part_keys,'Duplicate or empty note part identity')
                        cursor=end;part_keys.add(part_key)
                        subrow=dict(row,key=key+'#'+part_key,raw=dict(block,content=block['content'][start:end]))
                        notes_raw.append((subrow,dict(loc,raw_span_range=[start,end])))
                    require(cursor==len(block['content']),'Note split omitted original content')
                else:notes_raw.append((row,loc))
                continue
            kind={'text':'paragraph','paragraph_title':'heading','doc_title':'heading','ref_text':'paragraph',
                  'index':'list','image':'figure','table':'figure'}.get(typ)
            if not kind:
                issues.append({'kind':'unsupported_engine_block','page':page,'index':block['index'],'type':typ})
                aux.append({'origin':loc,'reason':'unsupported; no content discarded from raw evidence','original_block':block});continue
            if typ in ('image','table'):
                from pdf_assets import crop_image
                asset=crop_image(source,page,bbox,staging);assets.append(asset)
                if typ=='table':
                    text='![原书表格，PDF 文件页 '+str(page)+']('+asset['path']+')'
                    issues.append({'kind':'table_structure_requires_review','page':page,'index':block['index'],
                                   'reason':'The original PDF region is preserved visually; cells and reading order are not transcribed.'})
                else:
                    text='![]('+asset['path']+')'
                    issues.append({'kind':'image_region_requires_review','page':page,'index':block['index']})
                    if any(c.get('type')!='image_body' for c in block.get('content',[])):
                        issues.append({'kind':'image_caption_requires_adapter','page':page,'index':block['index']})
            else:text=render_index(block) if typ=='index' else render_spans(block)
            if key in text_reviews:
                require(typ in ('text','paragraph_title','doc_title','ref_text','index'),'Unsupported correction target type')
                text=reviewed_edits(text,text_reviews[key]['edits'])
            if not text:
                issues.append({'kind':'empty_engine_text','key':key})
                aux.append({'origin':loc,'reason':'empty engine text; original block retained for review','original_block':block})
                continue
            if kind=='heading':text='#'*min(max(int(block.get('level',1)),1),6)+' '+text
            canonical_by_raw[key]={'text':text,'kind':kind}
            def chapter_started(ch):
                if ch['start']<page:return True
                if ch['start']>page:return False
                if 'start_block' in ch:return block['index']>=ch['start_block']
                y=ch.get('start_y');return y is None or bbox is None or bbox[1]>=y-.025
            eligible=[c for c in chapters if chapter_started(c)]
            c=eligible[-1] if eligible else chapters[0]
            if not eligible:issues.append({'kind':'body_before_first_toc','key':key})
            if selected_set and c['chapter_id'] not in selected_set:
                aux.append({'origin':loc,'reason':'outside selected chapter boundary','original_block':block})
                continue
            segments=[(key,text,loc)]
            prior_paragraph_id=registry.data['ids'].get('p:pdf:'+key)
            if key in paragraph_splits:
                d=paragraph_splits[key];require(kind=='paragraph','Only body paragraphs can be split')
                require(d['candidate_sha256']==digest(text.encode()),'Paragraph split is stale after text corrections')
                segments=[];cursor=0;keys=set()
                for part in d['parts']:
                    start,end=part['range'];part_key=part['key']
                    require(type(start) is int and type(end) is int and start==cursor<end<=len(text),'Paragraph split must cover every character without overlap')
                    require(isinstance(part_key,str) and part_key and part_key not in keys,'Invalid paragraph part key')
                    require(bool(text[start:end].strip()),'Paragraph split cannot create an empty logical paragraph')
                    cursor=end;keys.add(part_key)
                    segments.append((key+'#'+part_key,text[start:end],dict(loc,split_key=part_key,normalized_parent_sha256=d['candidate_sha256'],normalized_range=[start,end])))
                require(cursor==len(text),'Paragraph split omitted text')
            new_ids=[]
            for segment_key,segment_text,segment_loc in segments:
                bid=registry.get('p' if kind=='paragraph' else 'b','pdf:'+segment_key);loc_ids[segment_key]=bid;new_ids.append(bid)
                blocks.append({'block_id':bid,'source_id':bid if kind=='paragraph' else None,'chapter_id':c['chapter_id'],
                               'order':len(blocks)+1,'kind':kind,'text':segment_text,'translatable':True})
                f={'fragment_id':registry.get('f','pdf:'+segment_key),'source_type':'pdf','pdf_page':page,'bbox':bbox,'range':[0,len(segment_text)],
                   'verification':'approximate','evidence':'Source SHA256 '+sha+'; '+row['raw_path']+'; page_idx '+str(page-1)+' block '+str(block['index'])}
                maps.append({'source_id':bid,'chapter_id':c['chapter_id'],'fragments':[f]})
                traces.append({'id':bid,'kind':'block','text':segment_text,'origins':[segment_loc]})
            if key in paragraph_splits and prior_paragraph_id:
                migrations.append({'old_ids':[prior_paragraph_id],'new_ids':new_ids,'reason':paragraph_splits[key]['evidence'],
                                   'review_state':'reviewed','kind':'paragraph_split'})
        for decision in joins:
            left=loc_ids.get(decision['left']);right=loc_ids.get(decision['right']);require(left and right,'Join source missing')
            i=next(i for i,b in enumerate(blocks) if b['block_id']==left);j=next(i for i,b in enumerate(blocks) if b['block_id']==right)
            require(j==i+1,'Join requires adjacent logical paragraphs after reviewed exclusions')
            b,m,migration=merge_paragraphs(blocks[i],maps[i],blocks[j],maps[j],decision['evidence'],decision.get('separator',' '))
            blocks[i]=b;maps[i]=m;del blocks[j];del maps[j]
            lt=next(t for t in traces if t['id']==left);rt=next(t for t in traces if t['id']==right)
            lt['text']=b['text'];lt['origins']+=rt['origins'];traces.remove(rt);migrations.append(migration)
            for k,v in loc_ids.items():
                if v==right:loc_ids[k]=left
        for i,b in enumerate(blocks):b['order']=i+1
        note_pairs=pair_candidates(notes_raw,blocks,maps)
        notes=[];render_notes=[];note_ids={};inferred_calls=[]
        for row,loc in notes_raw:
            original_text=plain_spans(row['raw']);text=render_spans(row['raw'])
            if not text:
                issues.append({'kind':'empty_engine_note','key':row['key']})
                aux.append({'origin':loc,'reason':'empty engine note; original block retained for review','original_block':row['raw']})
                continue
            nid=registry.get('n','pdf:'+row['key']);decision=note_reviews.get(row['key'])
            identity=classify(original_text);label_match=re.match(r'^\s*([0-9]+|[*†‡]+)\s+',original_text);label=label_match.group(1) if label_match else None
            note_ids[row['key']]=nid
            refs=[]
            if decision:
                refs=[loc_ids[k] for k in decision['references']];label=decision['original_label']
                if 'responsibility' in decision:identity['responsibility']=decision['responsibility'];identity['agent_role']=decision['responsibility']
            elif row['key'] in note_pairs['pairs']:
                pair=note_pairs['pairs'][row['key']];label=pair['original_label']
                refs=list(dict.fromkeys(c['source_id'] for c in pair['occurrences']))
                inferred_calls.extend(dict(call,raw_note_key=row['key']) for call in pair['occurrences'])
            if refs and label:
                notes.append({'note_id':nid,'origin':'original_document','responsibility':identity['responsibility'],'placement':'footnote',
                    'original_label':label,'agent_name':None,'agent_role':identity['agent_role'],'apparatus_kind':identity['apparatus_kind'],
                    'edition_id':document_id,'text':text,'references':refs,
                    'evidence':identity['evidence']+'; '+(decision['evidence'] if decision else 'MinerU same-page superscript/unique note-label candidate; original page review required'),
                    'verification':'unverified'})
            else:issues.append({'kind':'unresolved_note_reference','note_id':nid,'origin':loc})
            if selected_set and not refs:
                issues.append({'kind':'selected_scope_note_unresolved','note_id':nid,'origin':loc,
                               'reason':'Unlinked note on a shared page cannot be assigned to the selected chapter automatically.'})
                aux.append({'origin':loc,'reason':'unassigned note on selected-page boundary','original_block':row['raw']})
                continue
            traces.append({'id':nid,'kind':'note','text':text,'origins':[dict(loc,text_range=[0,len(text)])]});render_notes.append({'id':nid,'text':text})
        for decision in note_joins:
            left=note_ids.get(decision['left']);right=note_ids.get(decision['right'])
            require(left and right and left!=right,'Continuation note target missing or already merged')
            i=next(i for i,n in enumerate(render_notes) if n['id']==left);j=next(i for i,n in enumerate(render_notes) if n['id']==right)
            require(j==i+1,'Continuation must be adjacent in original note order')
            lt=next(t for t in traces if t['id']==left);rt=next(t for t in traces if t['id']==right)
            require(rt['origins'][0]['pdf_page'] in (lt['origins'][-1]['pdf_page'],lt['origins'][-1]['pdf_page']+1),
                    'Continuation note is not on the same or next physical page')
            sep=decision.get('separator',' ');require(sep in ('',' ','\n'),'Invalid note continuation separator')
            left_note=next((n for n in notes if n['note_id']==left),None);right_note=next((n for n in notes if n['note_id']==right),None)
            if left_note and right_note:
                require(left_note['original_label']==right_note['original_label'] and left_note['responsibility']==right_note['responsibility'],
                        'Conflicting note identities cannot be merged')
                left_note['references']=list(dict.fromkeys(left_note['references']+right_note['references']))
            require(left_note is not None or right_note is None,'Continuation cannot discard a separately identified note')
            shift=len(lt['text'])+len(sep);lt['text']+=sep+rt['text']
            for origin in rt['origins']:
                copied=dict(origin);copied['text_range']=[a+shift for a in origin['text_range']];lt['origins'].append(copied)
            traces.remove(rt);render_notes[i]['text']=lt['text'];del render_notes[j]
            if left_note:left_note['text']=lt['text'];left_note['evidence']+='; '+decision['evidence']
            if right_note:notes.remove(right_note)
            issues=[issue for issue in issues if issue.get('note_id')!=right]
            migrations.append({'old_ids':[left,right],'new_ids':[left],'reason':decision['evidence'],'review_state':'reviewed','kind':'note_continuation'})
            for key,value in note_ids.items():
                if value==right:note_ids[key]=left
        note_occurrences=[]
        for call in inferred_calls:
            item=dict(call);item['note_id']=note_ids[call['raw_note_key']];del item['raw_note_key']
            note_occurrences.append(item)
        for note_id in dict.fromkeys(item['note_id'] for item in note_occurrences):
            calls=[c for c in note_occurrences if c['note_id']==note_id]
            issues.append({'kind':'note_reference_requires_review','note_id':note_id,
                           'pdf_pages':sorted({c['pdf_page'] for c in calls}),
                           'occurrence_count':len(calls),
                           'reason':'Label and source position match; verify original page before accepting note identity or call semantics.'})
        for ambiguous in note_pairs['ambiguous']:
            issues.append({'kind':'ambiguous_original_note_label',**ambiguous})
        for page,d in page_reviews.items():
            require(1<=page<=len(pages),'Reviewed page out of bounds')
            pages[page-1]['printed_labels']=d['printed_labels'];pages[page-1]['label_state']=d['label_state']
            if 'edition_markers' in d:pages[page-1]['edition_markers']=d['edition_markers']
        detected_issues=list(issues);resolved_issues=[];open_issues=[]
        for issue in issues:
            resolved=False
            if issue['kind']=='possibly_combined_notes' and issue['at'] in note_splits:
                resolved=True
            elif issue['kind']=='printed_page_candidate_requires_review' and issue['pdf_page'] in page_reviews:
                resolved=page_reviews[issue['pdf_page']]['label_state'] in ('known','absent')
            elif issue['kind']=='possible_margin_line_numbers':
                # A raw warning stays open unless the corresponding source text
                # was retained and its suspect numbers no longer pollute prose.
                candidates=issue['candidates'];resolved=bool(candidates)
                for candidate in candidates:
                    after=canonical_by_raw.get(candidate['at'])
                    if after is None:resolved=False;break
                    if after['kind']=='paragraph' and re.search(r'(?<!\d)'+re.escape(candidate['candidate'])+r'(?!\d)',after['text']):
                        resolved=False;break
            if resolved:resolved_issues.append(dict(issue,resolution='Applied explicit original-reviewed decisions; raw warning retained for audit'))
            else:open_issues.append(issue)
        issues=open_issues
        common={'schema_version':'0.2.0','document_id':document_id,'source_revision':registry.get('s','task:'+task_id)};maprev=registry.get('map','task:'+task_id)
        public_ch=[];used={b['chapter_id'] for b in blocks};by_ch={c['chapter_id']:c for c in chapters}
        for cid in list(used):
            parent=by_ch[cid]['parent_id']
            while parent:
                used.add(parent);parent=by_ch[parent]['parent_id']
        for c in chapters:
            if c['chapter_id'] not in used:continue
            descendants={c['chapter_id']}
            for candidate in chapters:
                if candidate['parent_id'] in descendants:descendants.add(candidate['chapter_id'])
            own=[b for b in blocks if b['chapter_id'] in descendants]
            public_ch.append({'chapter_id':c['chapter_id'],'parent_id':c['parent_id'] if c['parent_id'] in used else None,'order':len(public_ch)+1,
                'title':c['title'],'first_block_id':own[0]['block_id'],'last_block_id':own[-1]['block_id'],'printed_page_hint':c.get('printed_page_hint')})
        docs={'blocks':dict(common,blocks=blocks),'toc':dict(common,chapters=public_ch),'notes':dict(common,notes=notes),
              'source_map':dict(common,map_revision=maprev,entries=maps),'page_map':dict(common,map_revision=maprev,source_type='pdf',pages=pages)}
        checks=validate_correspondence(docs['blocks'],docs['source_map'],docs['toc'],docs['notes'],docs['page_map'])
        snapshot=base/'originals/source.pdf'
        if snapshot.exists():require(digest(snapshot.read_bytes())==sha,'Source snapshot differs')
        else:atomic(snapshot,data)
        staging=task/'staging';staging.mkdir(exist_ok=True)
        for name,value in docs.items():atomic(staging/(name+'.json'),value)
        atomic(staging/'chapter_locator.json',{'private_format':'md-converter-chapter-locator-1',
            'input_sha256':sha,'chapters':[{'chapter_id':c['chapter_id'],'pdf_page':c['start'],
                'start_y':c.get('start_y'),'start_block':c.get('start_block'),
                'boundary_state':c.get('boundary_state','needs_review'),'evidence':c.get('evidence'),
                'first_block_id':next((b['block_id'] for b in blocks if b['chapter_id']==c['chapter_id']),None)}
                for c in chapters if c['chapter_id'] in used]})
        rendered=[{'id':b['block_id'],'text':b['text']} for b in blocks]+render_notes
        atomic(staging/'source.md','\n\n'.join('<a id="'+b['id']+'"></a>\n\n'+b['text'] for b in rendered)+'\n')
        atomic(task/'trace.json',{'private_format':'md-converter-pdf-trace-1','input_sha256':sha,'entries':traces,'auxiliary':aux,'engine_runs':raw_records,'decisions':decisions,'assets':assets,'overlap_context_variants':context_variants,
                                  'note_occurrences':note_occurrences,'detected_note_calls':note_pairs['all_calls']})
        atomic(task/'join-decisions.json',{'private_format':'md-converter-join-decisions-1','decisions':migrations,'notice':'Candidate IDs only; published ID migration still requires shared protocol.'})
        cross_page=[]
        for left,right in zip(maps,maps[1:]):
            a=left['fragments'][-1]['pdf_page'];b=right['fragments'][0]['pdf_page']
            left_block=next(x for x in blocks if x['block_id']==left['source_id'])
            right_block=next(x for x in blocks if x['block_id']==right['source_id'])
            if b==a+1 and left_block['kind']=='paragraph' and right_block['kind']=='paragraph':
                cross_page.append({'before_source_id':left['source_id'],'after_source_id':right['source_id'],
                                   'before_pdf_page':a,'after_pdf_page':b,'verification':'unverified',
                                   'reason':'Adjacent page-end/page-start paragraphs may be one logical paragraph; compare original pages.'})
        ocr_review=[p for p in sorted(engine_pages) if len((reader.pages[p-1].extract_text() or '').strip())<20]
        report=dict(common,private_format='md-converter-report-2',status='needs_attention',quality_state='needs_review',
            requested_scope={'pdf_pages':sorted(engine_pages),'coverage_basis':'whole_document' if not selected_set and len(engine_pages)==len(pages) else 'selected_scope'},
            selected_chapter_ids=sorted(selected_set),
            processed_pages=sorted(engine_pages),blank_pages=[],missing_pages=[],duplicated_fragments=[],
            unresolved_text=[i for i in issues if i['kind'] in ('unsupported_engine_block','possible_margin_line_numbers')],unresolved_notes=[i for i in issues if 'note' in i['kind']],
            unresolved_refs=[],asset_integrity={'review_required':True},map_verification=checks,corrections=decisions['decisions'],
            issues=issues,detected_issues=detected_issues,resolved_issues=resolved_issues,technical_coverage_complete=not issues,academic_review='not_performed',publication_allowed=False,
            unconfirmed_printed_pages=[p['pdf_page'] for p in pages if p['pdf_page'] in engine_pages and p['label_state']=='unknown'],
            paragraph_boundaries_requiring_review=[b['block_id'] for b in blocks if b['kind']=='paragraph'],
            cross_page_boundaries_requiring_review=cross_page,ocr_review_pages=ocr_review,
            limitation='PDF candidate adapter: complex assets, automatic cross-page review and final publication not yet implemented.')
        atomic(staging/'conversion-report.json',report)
        return {'staging':str(staging),'trace':str(task/'trace.json'),'checks':checks,'published':False,'issues':len(issues)}


def validate_pdf_task(task,staging,trace):
    from pypdf import PdfReader
    task=Path(task);staging=Path(staging)
    snapshot=task/'source.pdf' if (task/'bundle.json').exists() else task.parent.parent/'originals/source.pdf'
    require(digest(snapshot.read_bytes())==trace['input_sha256'],'PDF source snapshot changed')
    docs={k:load(staging/(k+'.json')) for k in ('blocks','toc','source_map','notes','page_map')}
    checks=validate_correspondence(docs['blocks'],docs['source_map'],docs['toc'],docs['notes'],docs['page_map'])
    chapter_locator=load(staging/'chapter_locator.json')
    require(chapter_locator['input_sha256']==trace['input_sha256'] and
            {c['chapter_id'] for c in chapter_locator['chapters']}=={c['chapter_id'] for c in docs['toc']['chapters']},
            'PDF chapter locator does not cover output TOC')
    for c in chapter_locator['chapters']:
        require(type(c['pdf_page']) is int and 1<=c['pdf_page']<=len(docs['page_map']['pages']),
                'PDF chapter locator outside original file')
    require(len(PdfReader(snapshot).pages)==checks['physical_pages'],'PDF page inventory changed')
    raw={}
    for item in trace['engine_runs']:
        p=confined(task,item['path']);require(digest(p.read_bytes())==item['sha256'],'Raw engine evidence changed')
        require(item['run']['input_sha256']==trace['input_sha256'],'Engine evidence belongs to other PDF')
        raw[item['path']]=load(p)
    raw_blocks={(page['page_idx']+1,block['index']) for doc in raw.values() for page in doc['pages'] for block in page.get('blocks',[])}
    accounted={(o['pdf_page'],o['engine_block_index']) for row in trace['entries'] for o in row['origins']}
    accounted|={(a['origin']['pdf_page'],a['origin']['engine_block_index']) for a in trace['auxiliary']}
    require(raw_blocks==accounted,'PDF raw engine block was neither mapped nor retained as review evidence')
    rows={r['id']:r for r in trace['entries']}
    corrections={d['at']:d for d in trace['decisions']['decisions'] if d['kind']=='text_correction'}
    splits={d['at']:d for d in trace['decisions']['decisions'] if d['kind']=='split_paragraph'}
    mapping_by_id={m['source_id']:m for m in docs['source_map']['entries']}
    blocks_by_id={b['block_id']:b for b in docs['blocks']['blocks']}
    notes_by_id={n['note_id']:n for n in docs['notes']['notes']}
    from pdf_note_links import comparison_label
    for call in trace.get('note_occurrences',[]):
        note=notes_by_id.get(call['note_id']);block=blocks_by_id.get(call['source_id'])
        require(note is not None and block is not None and call['source_id'] in note['references'],
                'Note occurrence no longer matches public note or block')
        a,z=call['range'];require(type(a) is int and type(z) is int and 0<=a<z<=len(block['text']),
                                  'Note occurrence range invalid')
        require(block['text'][a:z]==call['surface'] and comparison_label(call['label_raw'])==comparison_label(note['original_label']),
                'Note occurrence text or label differs from source candidate')
        require(any(f['pdf_page']==call['pdf_page'] and f['range'][0]<=a and z<=f['range'][1]
                    for f in mapping_by_id[call['source_id']]['fragments']),
                'Note occurrence does not belong to a mapped physical page')
    for b in docs['blocks']['blocks']:require(rows[b['block_id']]['text']==b['text'],'PDF block text differs from trace')
    for n in docs['notes']['notes']:require(rows[n['note_id']]['text']==n['text'],'PDF note text differs from trace')
    for row in trace['entries']:
        for origin_index,o in enumerate(row['origins']):
            p=next((p for p in raw[o['raw_path']]['pages'] if p['page_idx']+1==o['pdf_page']),None)
            require(p is not None,'Source page not present in raw evidence')
            b=next((b for b in p.get('blocks',[]) if b['index']==o['engine_block_index']),None)
            require(b is not None and b.get('bbox')==o['bbox'],'Source region differs from raw evidence')
            if 'normalized_range' in o:
                key=str(o['pdf_page'])+':'+str(o['engine_block_index']);parent=render_spans(b)
                if key in corrections:parent=reviewed_edits(parent,corrections[key]['edits'])
                require(digest(parent.encode())==o['normalized_parent_sha256'],'Split paragraph parent changed')
                part=next((p for p in splits.get(key,{}).get('parts',[]) if p['key']==o.get('split_key')),None)
                require(part and part['range']==o['normalized_range'],'Split origin differs from reviewed decision')
                start,end=o['normalized_range'];require(0<=start<end<=len(parent),'Split paragraph origin outside candidate')
                fragment=mapping_by_id[row['id']]['fragments'][origin_index];a,z=fragment['range'];expected=parent[start:end]
                require(row['text'][a:z] in (expected,expected+' ',expected+'\n'),'Split paragraph content differs from source candidate')
            if 'raw_span_range' in o:
                start,end=o['raw_span_range'];require(0<=start<end<=len(b['content']),'Note span origin outside raw evidence')
                segment=row['text'][slice(*o['text_range'])] if 'text_range' in o else row['text']
                require(row['kind']=='note' and segment==render_spans(dict(b,content=b['content'][start:end])),
                        'Split note no longer matches its original spans')
            elif row['kind']=='note' and 'text_range' in o:
                start,end=o['text_range'];require(0<=start<end<=len(row['text']),'Note text range outside canonical note')
                require(row['text'][start:end]==render_spans(b),'Continuation note no longer matches raw content')
    expected='\n\n'.join('<a id="'+r['id']+'"></a>\n\n'+r['text'] for r in trace['entries'])+'\n'
    require((staging/'source.md').read_text()==expected,'Markdown differs from PDF trace')
    for asset in trace.get('assets',[]):
        require(digest(confined(staging,asset['path']).read_bytes())==asset['sha256'],'PDF asset changed')
        require(any(o['pdf_page']==asset['pdf_page'] and o['bbox']==asset['bbox'] for r in trace['entries'] for o in r['origins']),
                'PDF asset source region missing')
    return dict(checks,source_sha256=trace['input_sha256'],physical_fragments_verified=sum(len(r['origins']) for r in trace['entries']),
                academic_review='not_performed')
