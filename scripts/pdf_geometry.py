"""Original text-layer geometry for conservative review proposals, never OCR truth.

Numeric tokens are eligible only inside a source-reviewed margin band, with
matching neighboring text. A similar number in the body is not removed.
"""
import re
from pathlib import Path
from common import digest
from mapping import require


def native_page(source,page_number):
    import pypdfium2 as pdfium
    document=pdfium.PdfDocument(str(source));page=None;textpage=None
    try:
        page=document[page_number-1];require(page.get_rotation()==0,'Native geometry rotation is not verified')
        width,height=page.get_size();textpage=page.get_textpage();text=textpage.get_text_range()
        require(len(text)==textpage.count_chars(),'Native character indexing is ambiguous; use visual review')
        tokens=[]
        for match in re.finditer(r'(?<!\d)\d{1,3}(?!\d)',text):
            boxes=[textpage.get_charbox(i) for i in range(match.start(),match.end())]
            left=min(b[0] for b in boxes);bottom=min(b[1] for b in boxes)
            right=max(b[2] for b in boxes);top=max(b[3] for b in boxes)
            tokens.append({'text':match.group(),'range':[match.start(),match.end()],
                           'bbox':[left/width,1-top/height,right/width,1-bottom/height]})
        return {'input_sha256':digest(Path(source).read_bytes()),'pdf_page':page_number,'text':text,'text_sha256':digest(text.encode()),'number_tokens':tokens,
                'coordinate_system':'unrotated visible page, normalized top-left','width':width,'height':height}
    finally:
        if textpage:textpage.close()
        if page:page.close()
        document.close()


def normalized(text):
    # Only generated inline tags; escaped source angle brackets remain literal.
    hidden=set()
    for match in re.finditer(r'(?<!\\)</?(?:sup|sub|strong|em|u|s)>',text):hidden.update(range(match.start(),match.end()))
    chars=[];positions=[]
    for i,char in enumerate(text):
        if i not in hidden and char.isalnum():chars.append(char.lower());positions.append(i)
    return ''.join(chars),positions


def context(text,start,end,size=16):
    left,_=normalized(text[:start]);right,_=normalized(text[end:])
    return left[-size:],right[:size]


def margin_proposals(text,native,band):
    """Return exact-range proposals and uncertain candidates, not accepted edits.

    band is an original-page-reviewed [left,top,right,bottom] margin region.
    Surrounding normalized text must match exactly on available sides (>=12
    characters in total), and each geometric token may match only once.
    """
    require(len(band)==4 and 0<=band[0]<band[2]<=1 and 0<=band[1]<band[3]<=1,'Invalid margin band')
    source=[]
    for token in native['number_tokens']:
        b=token['bbox']
        if band[0]<=b[0] and b[2]<=band[2] and band[1]<=b[1] and b[3]<=band[3]:source.append(token)
    proposals=[];unresolved=[]
    for match in re.finditer(r'(?<!\d)(?:5|10|15|20|25|30|35|40)(?!\d)',text):
        before,after=context(text,match.start(),match.end());matches=[]
        for token in source:
            if token['text']!=match.group():continue
            a,z=token['range'];left,right=context(native['text'],a,z)
            nleft=min(len(before),len(left));nright=min(len(after),len(right))
            if nleft+nright<12:continue
            if nleft and before[-nleft:]!=left[-nleft:]:continue
            if nright and after[:nright]!=right[:nright]:continue
            matches.append(token)
        item={'range':[match.start(),match.end()],'before':match.group(),'after':'',
              'raw_context':text[max(0,match.start()-35):match.end()+35]}
        if len(matches)==1:
            proposals.append(dict(item,native_token=matches[0],native_text_sha256=native['text_sha256'],pdf_page=native['pdf_page']))
        else:unresolved.append(dict(item,reason='No unique original-margin token with matching neighboring text'))
    counts={}
    for p in proposals:
        key=tuple(p['native_token']['range']);counts[key]=counts.get(key,0)+1
    unique=[]
    for p in proposals:
        if counts[tuple(p['native_token']['range'])]==1:unique.append(p)
        else:unresolved.append(dict(p,reason='Repeated candidate maps to the same original token'))
    return {'proposals':unique,'unresolved':unresolved}


def page_margin_proposals(blocks,native,band):
    """Enforce unique source-token use across all candidate blocks on a page."""
    results={key:margin_proposals(text,native,band) for key,text in blocks.items()};counts={}
    for result in results.values():
        for p in result['proposals']:
            key=tuple(p['native_token']['range']);counts[key]=counts.get(key,0)+1
    for result in results.values():
        accepted=[]
        for p in result['proposals']:
            if counts[tuple(p['native_token']['range'])]==1:accepted.append(p)
            else:result['unresolved'].append(dict(p,reason='Original margin token matches several engine blocks'))
        result['proposals']=accepted
    return results
