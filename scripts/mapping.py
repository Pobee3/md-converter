"""Source correspondence primitives; ranges are Unicode code points, never MD lines.

Evidence decisions are supplied by the converter/reviewer, not inferred from labels.
Private helpers do not extend the iReader schema.
"""
import copy
import re
from pathlib import Path
from common import ConversionError, load


def require(test, message):
    if not test:
        raise ConversionError(message)


def check_schema(value, definition, schema=None, where='$'):
    """Validate the bundled core schema vocabulary; reject unsupported keywords.

    Deliberately not a general JSON Schema implementation.
    """
    if schema is None:
        schema = load(Path(__file__).parent.parent/'references/core.schema.json')
    rule = schema['$defs'][definition] if isinstance(definition, str) else definition
    supported = {'$ref','type','properties','required','additionalProperties','items',
                 'minItems','maxItems','minLength','minimum','maximum','pattern',
                 'enum','const','oneOf','anyOf','allOf','description','title'}
    require(not set(rule)-supported, 'Unsupported schema vocabulary: '+str(set(rule)-supported))
    if '$ref' in rule:
        require(rule['$ref'].startswith('#/$defs/'), 'Nonlocal schema ref')
        check_schema(value, schema['$defs'][rule['$ref'].split('/')[-1]], schema, where)
    types = {'object': lambda x:isinstance(x,dict), 'array':lambda x:isinstance(x,list),
             'string':lambda x:isinstance(x,str), 'integer':lambda x:type(x) is int,
             'number':lambda x:type(x) in (int,float), 'boolean':lambda x:type(x) is bool,
             'null':lambda x:x is None}
    if 'type' in rule:
        wanted=rule['type'] if isinstance(rule['type'],list) else [rule['type']]
        require(any(types[t](value) for t in wanted), where+': wrong type')
    if 'const' in rule:require(value==rule['const'],where+': wrong constant')
    if 'enum' in rule:require(value in rule['enum'],where+': wrong enumeration')
    for op in ('oneOf','anyOf','allOf'):
        if op not in rule:continue
        successes=0
        for r in rule[op]:
            try:check_schema(value,r,schema,where);successes+=1
            except ConversionError:pass
        require(successes==1 if op=='oneOf' else successes>0 if op=='anyOf' else successes==len(rule[op]), where+': '+op+' failed')
    if isinstance(value,dict):
        require(set(rule.get('required',[]))<=set(value),where+': missing required field')
        props=rule.get('properties',{})
        for k,v in value.items():
            if k in props:check_schema(v,props[k],schema,where+'.'+k)
            elif rule.get('additionalProperties') is False:raise ConversionError(where+': extra field '+k)
            elif isinstance(rule.get('additionalProperties'),dict):check_schema(v,rule['additionalProperties'],schema,where+'.'+k)
    if isinstance(value,list):
        require(len(value)>=rule.get('minItems',0),where+': too few items')
        require(len(value)<=rule.get('maxItems',len(value)),where+': too many items')
        for i,v in enumerate(value):
            if 'items' in rule:check_schema(v,rule['items'],schema,where+'['+str(i)+']')
    if isinstance(value,str):
        require(len(value)>=rule.get('minLength',0),where+': string too short')
        if 'pattern' in rule:require(re.search(rule['pattern'],value),where+': pattern mismatch')
    if type(value) in (int,float):
        require(value>=rule.get('minimum',value) and value<=rule.get('maximum',value),where+': outside bounds')


def validate_correspondence(blocks, source_map, toc, notes=None, page_map=None):
    for value,name in [(blocks,'blocks'),(source_map,'source_map'),(toc,'toc'),(notes,'notes'),(page_map,'page_map')]:
        if value is not None:
            check_schema(value,name)
            for field in ('document_id','source_revision'):
                require(value[field]==blocks[field],'Mismatched '+field)
    if page_map:require(page_map['map_revision']==source_map['map_revision'],'Mismatched map revision')
    bs=blocks['blocks']; ids=[b['block_id'] for b in bs]; by_id={b['block_id']:b for b in bs}
    require(len(ids)==len(set(ids)),'Duplicate block ID')
    require([b['order'] for b in bs]==list(range(1,len(bs)+1)),'Nonsequential block order')
    chs={c['chapter_id']:c for c in toc['chapters']}
    require(len(chs)==len(toc['chapters']),'Duplicate chapter ID')
    for c in chs.values():
        require(c['parent_id'] is None or c['parent_id'] in chs,'Missing chapter parent')
        seen={c['chapter_id']};parent=c['parent_id']
        while parent:
            require(parent not in seen,'Chapter cycle');seen.add(parent);parent=chs[parent]['parent_id']
        require(c['first_block_id'] in by_id and c['last_block_id'] in by_id,'Chapter endpoint missing')
        require(by_id[c['first_block_id']]['order']<=by_id[c['last_block_id']]['order'],'Reversed chapter range')
    entries=source_map['entries'];mids=[m['source_id'] for m in entries]
    require(len(mids)==len(set(mids)) and set(mids)==set(ids),'Missing or duplicate block mapping')
    fragments=set();pages={p['pdf_page']:p for p in page_map['pages']} if page_map else {}
    if page_map:
        require(len(pages)==len(page_map['pages']) and sorted(pages)==list(range(1,len(pages)+1)), 'Physical page inventory incomplete')
        for p in pages.values():
            require(bool(p['printed_labels'])==(p['label_state']=='known'),'Printed-label state mismatch')
            for marker in p['edition_markers']:
                require(marker['source_id'] in by_id,'Marginal marker target missing')
                require(marker['offset']<=len(by_id[marker['source_id']]['text']),'Marker outside block')
    for b in bs:
        require(b['chapter_id'] in chs,'Block chapter missing')
        require(b['source_id']==b['block_id'] if b['kind']=='paragraph' else b['source_id'] is None,'Wrong paragraph identity')
    for m in entries:
        b=by_id[m['source_id']];require(m['chapter_id']==b['chapter_id'],'Mapping chapter mismatch')
        cursor=0
        for f in m['fragments']:
            require(f['fragment_id'] not in fragments,'Duplicate fragment ID');fragments.add(f['fragment_id'])
            a,z=f['range'];require(a==cursor and z>a and z<=len(b['text']),'Gap, overlap or invalid fragment range');cursor=z
            if f['source_type']=='pdf':
                require(page_map is not None and f['pdf_page'] in pages,'PDF fragment page missing')
                box=f['bbox']
                if box is not None:require(box[0]<box[2] and box[1]<box[3],'Invalid bbox geometry')
            else:require(not any(k in f for k in ('pdf_page','bbox')),'EPUB has fabricated PDF position')
        require(cursor==len(b['text']),'Unmapped end of block')
    if notes:
        ns=notes['notes'];require(len({n['note_id'] for n in ns})==len(ns),'Duplicate note ID')
        for n in ns:
            require(n['origin']=='original_document','Added note in original source')
            require(set(n['references'])<=set(ids),'Missing note reference block')
    return {'blocks':len(bs),'fragments':len(fragments),'chapters':len(chs),'physical_pages':len(pages)}


def locate(blocks, source_map, block_id, start=0, end=None):
    """Return every intersecting physical fragment. Never interpolate a character bbox."""
    b=next((b for b in blocks['blocks'] if b['block_id']==block_id),None)
    require(b is not None,'Unknown block ID')
    end=len(b['text']) if end is None else end
    require(0<=start<end<=len(b['text']),'Invalid Unicode range')
    m=next((m for m in source_map['entries'] if m['source_id']==block_id),None)
    require(m is not None,'Unmapped block')
    return [dict(f,selected_range=[max(start,f['range'][0]),min(end,f['range'][1])])
            for f in m['fragments'] if f['range'][0]<end and start<f['range'][1]]


def find_printed_page(page_map, edition_id, sequence_id, label_raw):
    """Return all exact matches; duplicates must stay visible."""
    return [p['pdf_page'] for p in page_map['pages'] if any(
        l['edition_id']==edition_id and l['sequence_id']==sequence_id and l['label_raw']==label_raw
        for l in p['printed_labels'])]


def merge_paragraphs(left, left_map, right, right_map, evidence, separator=' '):
    """Explicit reviewed join only. ID migration is required before publishing old IDs."""
    require(bool(evidence.strip()),'Cross-page join needs source evidence')
    require(left['kind']==right['kind']=='paragraph','Join requires paragraphs')
    require(left['chapter_id']==right['chapter_id'],'Cannot join across chapters')
    require(separator in ('',' ','\n'),'Unsupported join separator')
    b=copy.deepcopy(left);m=copy.deepcopy(left_map);shift=len(left['text'])+len(separator)
    b['text']+=separator+right['text']
    # Normalization whitespace belongs to the left physical fragment, explicitly recorded.
    m['fragments'][-1]['range'][1]+=len(separator)
    m['fragments'][-1]['evidence']+='; join normalization '+repr(separator)+'; '+evidence
    for f in copy.deepcopy(right_map['fragments']):
        f['range']=[x+shift for x in f['range']];m['fragments'].append(f)
    return b,m,{'old_ids':[left['block_id'],right['block_id']],'new_ids':[left['block_id']],
                'reason':evidence,'review_state':'reviewed'}
