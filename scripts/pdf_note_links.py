"""Find source-bound PDF note-call candidates without guessing note identity.

The result is private review evidence. A matching label is not proof that an
ambiguous superscript is a footnote call or that OCR read the original right.
"""
import re
import unicodedata
from pdf_content import render_spans
from mapping import require

SUP_DIGITS='⁰¹²³⁴⁵⁶⁷⁸⁹'
LABEL=r'(?:[0-9]+|['+SUP_DIGITS+r']+|[A-Za-z](?:[\'′])?|[*†‡]+)'
HEADING=re.compile(r'^\s*(?:<sup>(?P<raised>'+LABEL+r')</sup>|(?P<plain>'+LABEL+r'))(?=\s|[.):])')
CALL=re.compile(r'(?<!\\)<sup>(?P<html>'+LABEL+r')</sup>|(?P<glyph>['+SUP_DIGITS+r']+)')


def comparison_label(value):
    # NFKC is only for comparing note labels. The original glyph is retained.
    return unicodedata.normalize('NFKC',value).replace('′',"'")


def head_label(block):
    rendered=render_spans(block)
    first=HEADING.match(rendered)
    if not first:return None
    # A second apparent note start within the same engine block can be two
    # notes wrongly joined by OCR. Require a separate split review first.
    if re.search(r'\n\s*(?:<sup>'+LABEL+r'</sup>|'+LABEL+r')\s',rendered[first.end():]):
        return None
    return first.group('raised') or first.group('plain')


def body_calls(blocks, maps):
    """Locate superscript labels inside actual mapped fragments, not separators."""
    require(len(blocks)==len(maps) and all(b['block_id']==m['source_id'] for b,m in zip(blocks,maps)),
            'Note-call scan requires matching body blocks and source maps')
    calls=[]
    for block,mapping in zip(blocks,maps):
        text=block['text']
        # Only generated HTML <sup> or original superscript glyphs. Normal
        # numerals, escaped literal markup, and LaTeX powers are excluded.
        for match in CALL.finditer(text):
            label=match.group('html') or match.group('glyph')
            origin=next((f for f in mapping['fragments'] if f['range'][0]<=match.start()
                         and match.end()<=f['range'][1]),None)
            if origin is None:continue
            calls.append({'source_id':block['block_id'],'pdf_page':origin['pdf_page'],
                          'range':[match.start(),match.end()],'label_raw':label,
                          'surface':match.group(),'kind':'superscript_candidate'})
    return calls


def pair_candidates(notes_raw,blocks,maps):
    """Pair only same-page, unique labels; return all calls for review audit."""
    heads=[]
    for row,loc in notes_raw:
        label=head_label(row['raw'])
        if label:heads.append({'at':row['key'],'pdf_page':loc['pdf_page'],
                               'label_raw':label,'comparison_label':comparison_label(label)})
    calls=body_calls(blocks,maps);paired={};ambiguous=[]
    for head in heads:
        competitors=[h for h in heads if h['pdf_page']==head['pdf_page']
                     and h['comparison_label']==head['comparison_label']]
        hits=[c for c in calls if c['pdf_page']==head['pdf_page']
              and comparison_label(c['label_raw'])==head['comparison_label']]
        if len(competitors)!=1:
            ambiguous.append({'at':head['at'],'pdf_page':head['pdf_page'],
                              'label_raw':head['label_raw'],'reason':'same-page note labels collide'})
        elif hits:paired[head['at']]={'original_label':head['label_raw'],'occurrences':hits}
    return {'pairs':paired,'all_calls':calls,'ambiguous':ambiguous}
