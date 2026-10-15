"""Review candidates only: detecting a number must never authorize deleting it."""
import re
from collections import defaultdict
from pdf_content import plain_spans


def review_candidates(rows):
    by_page=defaultdict(list);issues=[]
    for row in rows:
        block=row['raw'];typ=block['type']
        if typ not in ('text','ref_text','page_footnote'):continue
        text=plain_spans(block)
        if typ=='page_footnote':
            markers=[s['content'] for s in block.get('content',[]) if s.get('type')=='equation_inline' and re.fullmatch(r'\^\{[0-9]+\}',s['content'])]
            if len(set(markers))>1:
                issues.append({'kind':'possibly_combined_notes','at':row['key'],'page':row['page'],'markers':markers,
                               'reason':'Several note-number forms share a raw footnote block; compare source before splitting.'})
        else:
            for match in re.finditer(r'(?<![\w.])(?:5|10|15|20|25|30|35|40)(?!\w)',text):
                by_page[row['page']].append({'at':row['key'],'raw_text_range':[match.start(),match.end()],
                    'candidate':match.group(),'context':text[max(0,match.start()-30):match.end()+30]})
    for page,matches in by_page.items():
        if len({m['candidate'] for m in matches})>=3:
            issues.append({'kind':'possible_margin_line_numbers','page':page,'candidates':matches,
                           'reason':'Repeated multiples of five may be marginal line numbers. These are unreviewed candidates, not automatic corrections.'})
    return issues
