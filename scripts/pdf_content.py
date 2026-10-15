"""Faithful rendering of MinerU inline text and nested printed indexes.

Index rows retain their supplied order and printed destination hints. They do
not establish chapter targets or a verified TOC hierarchy by themselves.
"""
import re
from urllib.parse import urlsplit
from mapping import require
from epub_structure import escape

STYLE_TAGS={'bold':'strong','italic':'em','underline':'u','emphasis':'em',
            'strikethrough':'s','superscript':'sup','subscript':'sub'}


def plain_spans(block):
    output=[]
    for span in block.get('content',[]):
        if span.get('type')=='hyperlink':
            output.append(plain_spans(span));continue
        require(span.get('type') in ('text','equation_inline') and isinstance(span.get('content'),str),
                'Unsupported MinerU inline content')
        content=span['content'];marker=re.fullmatch(r'\^\{([0-9]+)\}',content) if span['type']=='equation_inline' else None
        output.append(marker.group(1) if marker else content)
    return ''.join(output)


def render_spans(block):
    spans=block.get('content',[])
    require(isinstance(spans,list),'Unknown MinerU span representation')
    output=[]
    for span in spans:
        if span.get('type')=='hyperlink':
            target=span.get('url');parts=urlsplit(target) if isinstance(target,str) else None
            require(parts and parts.scheme in ('http','https','mailto') and not any(c in target for c in '<>\r\n'),
                    'Unsupported or unsafe source hyperlink')
            output.append('['+render_spans(span)+'](<'+target+'>)');continue
        require(span.get('type') in ('text','equation_inline') and isinstance(span.get('content'),str),
                'Non-text spans require dedicated faithful adapter')
        if span['type']=='equation_inline':
            marker=re.fullmatch(r'\^\{([0-9]+)\}',span['content'])
            output.append('<sup>'+marker.group(1)+'</sup>' if marker else '$'+span['content']+'$')
            continue
        styles=span.get('styles',[])
        require(isinstance(styles,list) and all(s in STYLE_TAGS for s in styles),'Unknown inline style')
        # HTML wrappers preserve tiny note numbers and adjacent styled runs;
        # unstyled text uses the same Markdown escaping as the native adapter.
        text=escape(span['content'])
        for style in reversed(list(dict.fromkeys(styles))):
            tag=STYLE_TAGS[style];text='<'+tag+'>'+text+'</'+tag+'>'
        output.append(text)
    return ''.join(output)


def render_index(block,depth=0):
    require(depth<32,'Printed index nesting exceeds limit')
    rows=block.get('content',[]);require(isinstance(rows,list),'Unknown index representation')
    output=[]
    for row in rows:
        typ=row.get('type')
        if typ in ('index','list'):text=render_index(row,depth+1)
        else:
            require(typ in ('text','ref_text','paragraph_title','doc_title'),'Unknown printed index row')
            text=render_spans(row)
        if text:output.append(text)
    return '\n\n'.join(output)


def reviewed_edits(text,edits):
    """Apply explicit Unicode ranges, never broad replacements of numeric text."""
    previous=0
    for edit in edits:
        start,end=edit['range']
        require(type(start) is int and type(end) is int and previous<=start<end<=len(text),'Invalid or overlapping correction range')
        require(text[start:end]==edit['before'],'Correction no longer matches original candidate')
        require(isinstance(edit['after'],str) and bool(edit.get('evidence','').strip()),'Correction needs replacement and source evidence')
        previous=end
    for edit in reversed(edits):
        start,end=edit['range'];text=text[:start]+edit['after']+text[end:]
    return text
