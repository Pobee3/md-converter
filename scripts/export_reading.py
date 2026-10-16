#!/usr/bin/env python3
"""Export a mapped source bundle as justified, page-labelled reading copies.

The canonical source.md and source_map.json are never changed. Page annotations
are display-only and are derived from the saved source fragments.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


FORMATS = {'md', 'word', 'pdf', 'epub'}
PAGE_MARKER = re.compile(r'(\[S\.[^\]]+\])')
NOTE_TOKEN = re.compile(r'⟦N(\d+):(\d+):([^⟧]+)⟧')
INLINE_TOKEN = re.compile(r'(\[S\.[^\]]+\]|⟦N\d+:\d+:[^⟧]+⟧|</?(?:sup|sub)>)')
EXPLICIT_NOTE_REF = re.compile(r'\[([^\]]+)\]\(#(n-[^)]+)\)')
SUP_NOTE_REF = re.compile(r'<sup>([^<]+)</sup>')
UNICODE_SUP_NOTE_REF = re.compile(r'[⁰¹²³⁴⁵⁶⁷⁸⁹]+')
SUP_TO_DIGIT = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹', '0123456789')
NOTE_COLORS = {
    'author': '9B6840',
    'original_translator': '9B6840',
    'editor': '3D75B3',
    'critical_apparatus': '9A86B7',
    'other': '686868',
    'unknown': '686868',
}
PAGE_MARKER_COLOR = '555555'
ROLE_LABELS = {
    'author': '作者注',
    'original_translator': '原版译者注',
    'editor': '编者说明',
    'critical_apparatus': '校注',
    'other': '原注',
    'unknown': '责任未明的原注',
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def printed_label(page_row: dict | None) -> str:
    if not page_row:
        return '?'
    labels = page_row.get('printed_labels') or []
    verified = [x for x in labels if x.get('verification') == 'verified']
    return str(verified[0].get('label_raw', '?')) if verified else '?'


def verified_printed_label(page_row: dict | None) -> str | None:
    labels = (page_row or {}).get('printed_labels') or []
    verified = next((row for row in labels if row.get('verification') == 'verified'), None)
    return str(verified['label_raw']) if verified and verified.get('label_raw') else None


def page_token(page: int, pages: dict[int, dict]) -> str:
    return f'{printed_label(pages.get(page))}/{page}'


def marker(page: int, ordinal: int, pages: dict[int, dict]) -> str:
    return f'[S.{page_token(page, pages)}-{ordinal}]'


def safe_boundary(value: str, offset: int) -> int:
    """Move an intra-word page edge to the word's start for readable annotation."""
    offset = max(0, min(len(value), offset))
    if 0 < offset < len(value) and value[offset - 1].isalnum() and value[offset].isalnum():
        while offset > 0 and value[offset - 1].isalnum():
            offset -= 1
    return offset


def source_items(source: Path, selected: set[str] | None = None):
    blocks_data = read_json(source / 'blocks.json')
    maps_data = read_json(source / 'source_map.json')
    pages_file = source / 'page_map.json'
    pages = {p['pdf_page']: p for p in read_json(pages_file)['pages']} if pages_file.exists() else {}
    maps = {x['source_id']: x for x in maps_data['entries']}
    counters: dict[int, int] = {}
    all_items = []
    for block in sorted(blocks_data['blocks'], key=lambda x: x['order']):
        bid = block['block_id']
        fragments = [f for f in maps.get(bid, {}).get('fragments', []) if f.get('source_type') == 'pdf']
        kind = block['kind']
        is_para = kind == 'paragraph'
        unique_fragments = []
        seen_pages = set()
        for fragment in fragments:
            page = fragment['pdf_page']
            if page not in seen_pages:
                unique_fragments.append(fragment)
                seen_pages.add(page)
        page_ordinals = {}
        if is_para:
            for fragment in unique_fragments:
                page = fragment['pdf_page']
                counters[page] = counters.get(page, 0) + 1
                page_ordinals[page] = counters[page]
        if selected is not None and bid not in selected:
            continue
        value = block['text']
        if kind == 'heading':
            value = re.sub(r'^#{1,6}\s*', '', value)
            value = value.replace(r'\<', '<').replace(r'\>', '>')
        positions = []
        if is_para and unique_fragments:
            first = unique_fragments[0]
            positions.append((0, marker(first['pdf_page'], page_ordinals[first['pdf_page']], pages)))
            for fragment in unique_fragments[1:]:
                page = fragment['pdf_page']
                positions.append((safe_boundary(value, fragment['range'][0]), marker(page, page_ordinals[page], pages)))
        # Keep the logical paragraph intact; annotations never become new blocks.
        for pos, tag in sorted(positions, key=lambda x: x[0], reverse=True):
            value = value[:pos] + tag + ' ' + value[pos:]
        all_items.append({'id': bid, 'kind': kind, 'text': value, 'pages': [f['pdf_page'] for f in unique_fragments],
                          'raw_text': block['text']})
    notes = read_json(source / 'notes.json').get('notes', []) if (source / 'notes.json').exists() else []
    if selected is not None:
        notes = [note for note in notes if set(note.get('references', [])) & selected]
    return blocks_data, all_items, pages, notes


def note_label(note: dict) -> str:
    return ROLE_LABELS.get(note.get('responsibility'), ROLE_LABELS['unknown'])


def note_text(note: dict) -> str:
    value = str(note.get('text') or '')
    label = str(note.get('original_label') or '')
    if not label:
        return value
    plain = html.unescape(re.sub(r'<[^>]+>', '', value)).lstrip()
    leading = plain.translate(str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹', '0123456789'))
    return value if leading.startswith(label) else label + ' ' + value


def note_body(note: dict) -> str:
    value = str(note.get('text') or '').strip()
    label = re.escape(str(note.get('original_label') or ''))
    if label:
        value = re.sub(rf'^(?:<sup>\s*{label}\s*</sup>|{label})(?:\s|\u00a0)*', '', value, count=1)
        superscript_label = str(note.get('original_label') or '').translate(str.maketrans('0123456789', '⁰¹²³⁴⁵⁶⁷⁸⁹'))
        if value.startswith(superscript_label):
            value = value[len(superscript_label):].lstrip()
    return value.strip()


def display_note_number(note: dict, original: str | None = None) -> str:
    label = (original or str(note.get('original_label') or '')).replace('\\*', '*').strip()
    if note.get('responsibility') == 'editor' and label and not label.startswith('E'):
        return 'E' + label
    return label or '?'


def note_anchor(index: int) -> str:
    return f'note_{index}'


def ref_anchor(index: int, occurrence: int) -> str:
    return f'note_ref_{index}_{occurrence}'


def decorate_note_refs(items: list[dict], notes: list[dict], counts: dict[int, int] | None = None) -> None:
    """Bind original callouts to note records without altering canonical source blocks."""
    by_id = {note['note_id']: i for i, note in enumerate(notes)}
    if counts is None:
        counts = {}
    for item in items:
        related = [(i, note) for i, note in enumerate(notes) if item['id'] in note.get('references', [])]
        if not related:
            continue

        def token(index: int, label: str) -> str:
            counts[index] = counts.get(index, 0) + 1
            return f'⟦N{index}:{counts[index]}:{display_note_number(notes[index], label)}⟧'

        def explicit(match):
            index = by_id.get(match.group(2))
            return token(index, match.group(1)) if index is not None else match.group(0)

        value = EXPLICIT_NOTE_REF.sub(explicit, item['text'])

        def superscript(match):
            label = html.unescape(match.group(1)).strip()
            candidates = [(i, note) for i, note in related
                          if label == str(note.get('original_label') or '').strip()
                          or (note.get('responsibility') == 'critical_apparatus'
                              and label == str(note.get('original_label') or '').strip() + '′')]
            return token(candidates[0][0], label) if len(candidates) == 1 else match.group(0)

        value = SUP_NOTE_REF.sub(superscript, value)
        superscript_matches = list(UNICODE_SUP_NOTE_REF.finditer(value))
        counts_by_label: dict[str, int] = {}
        for match in superscript_matches:
            label = match.group(0).translate(SUP_TO_DIGIT)
            counts_by_label[label] = counts_by_label.get(label, 0) + 1

        def unicode_superscript(match):
            label = match.group(0).translate(SUP_TO_DIGIT)
            if match.start() and value[match.start() - 1].isdigit():
                return match.group(0)
            candidates = [(i, note) for i, note in related
                          if label == str(note.get('original_label') or '').strip()]
            if len(candidates) == 1 and counts_by_label[label] == 1:
                return token(candidates[0][0], label)
            return match.group(0)

        item['text'] = UNICODE_SUP_NOTE_REF.sub(unicode_superscript, value)


def display_rows(items: list[dict], pages: dict[int, dict]):
    seen = set()
    rows = []
    for item in items:
        for page in item['pages'][:1]:
            if page not in seen:
                rows.append(('page', f'印刷页码/PDF页码：{printed_label(pages.get(page))}/{page}', None))
                seen.add(page)
        kind = 'raw' if re.search(r'!\[[^]]*\]\(', item['raw_text']) else item['kind']
        rows.append((kind, item['text'], item['id']))
    return rows


def toc_entries(source: Path, blocks_data: dict, items: list[dict], pages: dict[int, dict]):
    """Resolve each included chapter to a real block in this reading copy."""
    toc_path = source / 'toc.json'
    if not toc_path.exists():
        return []
    chapters = read_json(toc_path).get('chapters', [])
    block_order = {block['block_id']: block['order'] for block in blocks_data['blocks']}
    selected = [item for item in items if item['id'] in block_order]
    selected.sort(key=lambda item: block_order[item['id']])
    by_chapter = {chapter['chapter_id']: chapter for chapter in chapters}
    result = []
    for chapter in sorted(chapters, key=lambda row: row['order']):
        first = block_order.get(chapter['first_block_id'])
        last = block_order.get(chapter['last_block_id'])
        if first is None or last is None:
            continue
        target = next((item for item in selected if first <= block_order[item['id']] <= last), None)
        if target is None:
            continue
        depth = 0
        parent = chapter.get('parent_id')
        visited = set()
        while parent and parent in by_chapter and parent not in visited:
            visited.add(parent)
            depth += 1
            parent = by_chapter[parent].get('parent_id')
        first_page = target['pages'][0] if target['pages'] else None
        label = verified_printed_label(pages.get(first_page)) if first_page is not None else None
        if label is None:
            label = f'PDF {first_page}' if first_page is not None else ''
        result.append({'chapter_id': chapter['chapter_id'], 'parent_id': chapter.get('parent_id'),
                       'title': chapter['title'], 'target_id': target['id'],
                       'bookmark': f'toc_{len(result) + 1}', 'depth': depth,
                       'source_page_label': label})
    return result


def epub_toc_list(chapters: list[dict], href_prefix: str) -> str:
    by_id = {chapter['chapter_id']: chapter for chapter in chapters}
    children: dict[str | None, list[dict]] = {}
    for chapter in chapters:
        parent = chapter['parent_id'] if chapter['parent_id'] in by_id else None
        children.setdefault(parent, []).append(chapter)

    def render(parent_id: str | None) -> str:
        parts = ['<ol>']
        for chapter in children.get(parent_id, []):
            href = href_prefix + '#' + chapter['bookmark']
            page = (f'<span class="toc-page"> · {html.escape(chapter["source_page_label"])}</span>'
                    if chapter['source_page_label'] else '')
            parts.append(f'<li><a href="{html.escape(href, quote=True)}">{html.escape(chapter["title"])}{page}</a>')
            if chapter['chapter_id'] in children:
                parts.append(render(chapter['chapter_id']))
            parts.append('</li>')
        parts.append('</ol>')
        return ''.join(parts)

    return render(None)


def inline_html(value: str, notes: list[dict] | None = None) -> str:
    escaped = html.escape(value)
    for tag in ('sup', 'sub'):
        escaped = escaped.replace(f'&lt;{tag}&gt;', f'<{tag}>')
        escaped = escaped.replace(f'&lt;/{tag}&gt;', f'</{tag}>')
    escaped = PAGE_MARKER.sub(lambda m: f'<span class="source-mark" style="font-size:0.73em;vertical-align:baseline;color:#{PAGE_MARKER_COLOR};">{html.escape(m.group(1))}</span>', escaped)
    if notes is not None:
        def note_link(match):
            index, occurrence, label = int(match.group(1)), int(match.group(2)), match.group(3)
            role = notes[index].get('responsibility', 'unknown')
            return (f'<a id="{ref_anchor(index, occurrence)}" href="#{note_anchor(index)}" '
                    f'class="note-ref note-{html.escape(role)}" '
                    f'style="color:#{NOTE_COLORS.get(role, NOTE_COLORS["unknown"])};">'
                    f'<sup>{html.escape(label)}</sup></a>')
        escaped = NOTE_TOKEN.sub(note_link, escaped)
    escaped = escaped.replace('\n', '<br/>')
    return escaped


def write_md(path: Path, title: str, rows: list[tuple[str, str, str | None]], notes: list[dict]):
    font_css = "font-family:'Songti SC','Noto Serif CJK SC',serif;"
    parts = [f'<h1 style="{font_css}">{html.escape(title)}</h1>', '']
    for kind, value, _ in rows:
        if kind == 'page':
            parts += [f'<p class="source-page" style="{font_css}">{html.escape(value)}</p>', '']
        elif kind == 'heading':
            parts += [f'<h2 style="{font_css}">{html.escape(value)}</h2>', '']
        elif kind != 'paragraph':
            parts += [value, '']
        else:
            parts += [f'<p style="text-align: justify; text-justify: inter-word; {font_css}">{inline_html(value, notes)}</p>', '']
    if notes:
        parts += ['## 原注', '']
        for index, note in enumerate(notes):
            number = display_note_number(note)
            color = NOTE_COLORS.get(note.get('responsibility'), NOTE_COLORS['unknown'])
            label = (f'<a id="{note_anchor(index)}" href="#{ref_anchor(index, 1)}" '
                     f'style="color:#{color};font-size:0.86em;">{html.escape(number)}</a>')
            parts += [f'<p style="text-align: justify; font-size:0.86em; {font_css}">{label} '
                      f'{html.escape(note_label(note))} {inline_html(note_body(note))}</p>', '']
    path.write_text('\n'.join(parts), encoding='utf-8')


def write_pdf_direct(path: Path, title: str, rows: list[tuple[str, str, str | None]],
                     notes: list[dict], chapters: list[dict], toc_numbers: dict[str, int] | None = None):
    """Render the PDF with an embedded CJK font and real internal links."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph

    configured = os.environ.get('MD_CONVERTER_PDF_FONT')
    candidates = ([(Path(configured), int(os.environ.get('MD_CONVERTER_PDF_FONT_SUBFONT', '0')))]
                  if configured else [
                      (Path('/System/Library/Fonts/Supplemental/Songti.ttc'), 6),
                      (Path('/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc'), 0),
                      (Path('/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc'), 0),
                      (Path('C:/Windows/Fonts/simsun.ttc'), 0),
                  ])
    available = next(((path, index) for path, index in candidates if path.is_file()), None)
    if available is None:
        raise RuntimeError('PDF export needs a CJK serif font; set MD_CONVERTER_PDF_FONT to a local TTF/TTC font')
    font_path, subfont = available
    pdfmetrics.registerFont(TTFont('ReadingSongti', str(font_path), subfontIndex=subfont))
    font = 'ReadingSongti'
    width, height = A4
    left, right, top, bottom = 65, 65, height - 65, 52
    body_width = width - left - right
    body = ParagraphStyle('body', fontName=font, fontSize=12, leading=15.2,
                          alignment=TA_JUSTIFY, textColor=colors.HexColor('#243445'))
    small = ParagraphStyle('small', parent=body, fontSize=8, leading=11,
                           textColor=colors.HexColor('#555555'))
    note_style = ParagraphStyle('note', parent=body, fontSize=10.5, leading=13.2)
    heading_style = ParagraphStyle('heading', parent=body, alignment=TA_LEFT)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setTitle(title)
    page = 0
    y = top
    targets = {}
    chapter_targets = {}
    for chapter in chapters:
        chapter_targets.setdefault(chapter['target_id'], []).append(chapter['bookmark'])

    def new_page():
        nonlocal page, y
        if page:
            pdf.showPage()
        page += 1
        y = top
        pdf.setFont(font, 8)
        pdf.setFillColor(colors.HexColor('#555555'))
        pdf.drawRightString(width - right, 27, str(page))

    def markup(value: str) -> str:
        result = []
        for part in INLINE_TOKEN.split(value):
            if not part:
                continue
            match = NOTE_TOKEN.fullmatch(part)
            if match:
                index, occurrence, label = int(match.group(1)), int(match.group(2)), match.group(3)
                role = notes[index].get('responsibility', 'unknown')
                color = NOTE_COLORS.get(role, NOTE_COLORS['unknown'])
                result.append(f'<link href="#note_{index}" color="#{color}"><super>{html.escape(label)}</super></link>')
            elif PAGE_MARKER.fullmatch(part):
                result.append(f'<font size="8" color="#{PAGE_MARKER_COLOR}">{html.escape(part)}</font>')
            elif part in ('<sup>', '</sup>', '<sub>', '</sub>'):
                result.append(part.replace('sup', 'super').replace('sub', 'sub'))
            else:
                result.append(html.escape(part).replace('\n', '<br/>'))
        return ''.join(result)

    def draw_text(value: str, style: ParagraphStyle, *, before: float = 0,
                  after: float = 8, bookmarks: list[str] | None = None):
        nonlocal y
        y -= before
        remaining = Paragraph(value, style)
        first = True
        while remaining:
            if y - bottom < style.leading * 2:
                new_page()
            if first and bookmarks:
                for bookmark in bookmarks:
                    pdf.bookmarkPage(bookmark)
                    targets[bookmark] = page
            parts = remaining.split(body_width, y - bottom)
            if not parts:
                new_page()
                continue
            section = parts[0]
            _, h = section.wrap(body_width, y - bottom)
            section.drawOn(pdf, left, y - h)
            y -= h
            remaining = parts[1] if len(parts) > 1 else None
            first = False
        y -= after

    new_page()
    draw_text('目录', body, after=15)
    for chapter in chapters:
        if y < bottom + 35:
            new_page()
        label = html.escape(chapter['title'])
        number = (toc_numbers or {}).get(chapter['bookmark'])
        indent = chapter['depth'] * 12
        line = Paragraph(label, heading_style)
        _, h = line.wrap(body_width - 40 - indent, 5000)
        line.drawOn(pdf, left + indent, y - h)
        if number is not None:
            pdf.setFillColor(colors.HexColor('#243445'))
            pdf.setFont(font, 10.5)
            pdf.drawRightString(width - right, y - 11, str(number))
        pdf.linkRect('', chapter['bookmark'], (left, y - h, width - right, y),
                     relative=0, thickness=0)
        y -= h + 10
    new_page()
    for kind, value, block_id in rows:
        bookmarks = list(chapter_targets.get(block_id, []))
        for match in NOTE_TOKEN.finditer(value):
            bookmarks.append(ref_anchor(int(match.group(1)), int(match.group(2))))
        if kind == 'page':
            draw_text(html.escape(value), small, before=5, after=4)
        elif kind == 'heading':
            draw_text(html.escape(value), heading_style, before=10, after=9, bookmarks=bookmarks)
        else:
            draw_text(markup(value), body, after=8, bookmarks=bookmarks)
    if notes:
        draw_text('原注', body, before=12, after=10)
        for index, note in enumerate(notes):
            color = NOTE_COLORS.get(note.get('responsibility'), NOTE_COLORS['unknown'])
            label = html.escape(display_note_number(note))
            prefix = (f'<link href="#{ref_anchor(index, 1)}" color="#{color}">{label}</link>'
                      if ref_anchor(index, 1) in targets else label)
            value = prefix + '  ' + html.escape(note_label(note)) + '  ' + markup(note_body(note))
            draw_text(value, note_style, after=9, bookmarks=[note_anchor(index)])
    pdf.save()
    return {'reading_page_count': page, 'toc_targets': {c['bookmark']: targets.get(c['bookmark']) for c in chapters}}


def write_docx(path: Path, title: str, rows: list[tuple[str, str, str | None]], notes: list[dict], chapters: list[dict]):
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
        from docx.shared import Cm, Pt
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
    except ImportError as exc:
        raise RuntimeError('Word/PDF export requires python-docx in the selected Python runtime') from exc
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2.2)
    section.left_margin = section.right_margin = Cm(2.3)
    normal = doc.styles['Normal']
    normal.font.name, normal.font.size = 'Songti SC', Pt(12)
    normal._element.rPr.rFonts.set(qn('w:eastAsia'), 'Songti SC')
    normal.paragraph_format.line_spacing = 1.22
    normal.paragraph_format.space_after = Pt(7)
    heading = doc.add_paragraph(title)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    heading.runs[0].bold = True
    heading.runs[0].font.size = Pt(15)
    heading.runs[0].font.name = 'Songti SC'
    heading.runs[0]._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Songti SC')
    def add_hyperlink(paragraph, label, bookmark):
        link = OxmlElement('w:hyperlink')
        link.set(qn('w:anchor'), bookmark)
        run = OxmlElement('w:r')
        props = OxmlElement('w:rPr')
        style = OxmlElement('w:rStyle')
        style.set(qn('w:val'), 'Hyperlink')
        props.append(style)
        run.append(props)
        text = OxmlElement('w:t')
        text.text = label
        run.append(text)
        link.append(run)
        paragraph._p.append(link)

    def add_page_reference(paragraph, bookmark):
        link = OxmlElement('w:hyperlink')
        link.set(qn('w:anchor'), bookmark)
        field = OxmlElement('w:fldSimple')
        field.set(qn('w:instr'), f'PAGEREF {bookmark} \\h')
        run = OxmlElement('w:r')
        text = OxmlElement('w:t')
        text.text = '1'
        run.append(text)
        field.append(run)
        link.append(field)
        paragraph._p.append(link)

    bookmark_counter = len(chapters) + 100
    linked_refs: set[int] = set()

    def add_note_link(paragraph, label, target, role, *, own_anchor=None, superscript=False):
        nonlocal bookmark_counter
        if own_anchor:
            bookmark_counter += 1
            start = OxmlElement('w:bookmarkStart')
            start.set(qn('w:id'), str(bookmark_counter))
            start.set(qn('w:name'), own_anchor)
            paragraph._p.append(start)
        link = OxmlElement('w:hyperlink')
        link.set(qn('w:anchor'), target)
        run = OxmlElement('w:r')
        props = OxmlElement('w:rPr')
        fonts = OxmlElement('w:rFonts')
        for axis in ('ascii', 'hAnsi', 'eastAsia'):
            fonts.set(qn('w:' + axis), 'Songti SC')
        props.append(fonts)
        color = OxmlElement('w:color')
        color.set(qn('w:val'), NOTE_COLORS.get(role, NOTE_COLORS['unknown']))
        props.append(color)
        size = OxmlElement('w:sz')
        size.set(qn('w:val'), '18' if superscript else '21')
        props.append(size)
        if superscript:
            vertical = OxmlElement('w:vertAlign')
            vertical.set(qn('w:val'), 'superscript')
            props.append(vertical)
        run.append(props)
        text = OxmlElement('w:t')
        text.text = label
        run.append(text)
        link.append(run)
        paragraph._p.append(link)
        if own_anchor:
            end = OxmlElement('w:bookmarkEnd')
            end.set(qn('w:id'), str(bookmark_counter))
            paragraph._p.append(end)

    def add_rich_runs(paragraph, value, *, size=12):
        script = None
        for segment in INLINE_TOKEN.split(value):
            if not segment:
                continue
            if segment in ('<sup>', '<sub>'):
                script = segment[1:-1]
                continue
            if segment in ('</sup>', '</sub>'):
                script = None
                continue
            note_match = NOTE_TOKEN.fullmatch(segment)
            if note_match:
                index, occurrence, label = int(note_match.group(1)), int(note_match.group(2)), note_match.group(3)
                linked_refs.add(index)
                add_note_link(paragraph, label, note_anchor(index), notes[index].get('responsibility'),
                              own_anchor=ref_anchor(index, occurrence), superscript=True)
                continue
            run = paragraph.add_run(segment)
            run.font.name = 'Songti SC'
            run._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Songti SC')
            run.font.size = Pt(size)
            if PAGE_MARKER.fullmatch(segment):
                run.font.size = Pt(8)
                from docx.shared import RGBColor
                run.font.color.rgb = RGBColor.from_string(PAGE_MARKER_COLOR)
            if script == 'sup':
                run.font.superscript = True
            elif script == 'sub':
                run.font.subscript = True

    targets: dict[str, list[tuple[int, str]]] = {}
    if chapters:
        toc_title = doc.add_paragraph('目录')
        toc_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        toc_title.runs[0].bold = True
        toc_title.runs[0].font.name = 'Songti SC'
        for number, chapter in enumerate(chapters, 1):
            entry = doc.add_paragraph()
            entry.paragraph_format.left_indent = Cm(min(chapter['depth'], 4) * 0.55)
            entry.paragraph_format.tab_stops.add_tab_stop(Cm(16.4), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
            add_hyperlink(entry, chapter['title'], chapter['bookmark'])
            entry.add_run('\t')
            add_page_reference(entry, chapter['bookmark'])
            targets.setdefault(chapter['target_id'], []).append((number, chapter['bookmark']))
        doc.add_page_break()
        update = OxmlElement('w:updateFields')
        update.set(qn('w:val'), 'true')
        doc.settings.element.append(update)
    for kind, value, block_id in rows:
        paragraph = doc.add_paragraph()
        if kind == 'page':
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            paragraph.paragraph_format.space_before = Pt(8)
            paragraph.paragraph_format.space_after = Pt(3)
            run = paragraph.add_run(value)
            run.font.size = Pt(8)
        elif kind == 'heading':
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            paragraph.paragraph_format.space_before = Pt(12)
            run = paragraph.add_run(value)
            run.bold = True
            run.font.name = 'Songti SC'
            run._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Songti SC')
        else:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            add_rich_runs(paragraph, value)
        for number, bookmark in targets.get(block_id, []):
            start = OxmlElement('w:bookmarkStart')
            start.set(qn('w:id'), str(number))
            start.set(qn('w:name'), bookmark)
            end = OxmlElement('w:bookmarkEnd')
            end.set(qn('w:id'), str(number))
            paragraph._p.insert(1 if paragraph._p.pPr is not None else 0, start)
            paragraph._p.append(end)
    if notes:
        note_heading = doc.add_paragraph('原注')
        note_heading.runs[0].bold = True
        note_heading.runs[0].font.name = 'Songti SC'
        for index, note in enumerate(notes):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            if index in linked_refs:
                add_note_link(p, display_note_number(note), ref_anchor(index, 1), note.get('responsibility'),
                              own_anchor=note_anchor(index))
            else:
                number = p.add_run(display_note_number(note))
                number.font.size = Pt(10.5)
            role = p.add_run('  ' + note_label(note) + '  ')
            role.font.name = 'Songti SC'
            role.font.size = Pt(10.5)
            add_rich_runs(p, note_body(note), size=10.5)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), 'PAGE')
    footer._p.append(field)
    doc.save(path)


def epub_xhtml(title: str, rows: list[tuple[str, str, str | None]], notes: list[dict], chapters: list[dict]) -> str:
    body = [f'<h1>{html.escape(title)}</h1>']
    linked_indices = {int(match.group(1)) for _, value, _ in rows for match in NOTE_TOKEN.finditer(value)}
    anchors: dict[str, list[str]] = {}
    if chapters:
        body.append('<nav class="reading-toc" aria-label="目录"><h2>目录</h2>')
        body.append(epub_toc_list(chapters, ''))
        for chapter in chapters:
            anchors.setdefault(chapter['target_id'], []).append(chapter['bookmark'])
        body.append('</nav>')
    for kind, value, block_id in rows:
        anchor = ''.join(f'<span id="{html.escape(name)}"></span>' for name in anchors.get(block_id, []))
        if kind == 'page':
            body.append(f'<p class="source-page">{html.escape(value)}</p>')
        elif kind == 'heading':
            body.append(f'<h2>{anchor}{html.escape(value)}</h2>')
        else:
            body.append(f'<p class="source-text">{anchor}{inline_html(value, notes)}</p>')
    if notes:
        body.append('<h2>原注</h2>')
        for index, note in enumerate(notes):
            number = html.escape(display_note_number(note))
            role = str(note.get('responsibility') or 'unknown')
            color = NOTE_COLORS.get(role, NOTE_COLORS['unknown'])
            if index in linked_indices:
                number_html = (f'<a id="{note_anchor(index)}" href="#{ref_anchor(index, 1)}" '
                               f'class="note-number" style="color:#{color};">{number}</a>')
            else:
                number_html = f'<span class="note-number" style="color:#{color};">{number}</span>'
            body.append(f'<p class="source-text original-note">{number_html} '
                        f'{html.escape(note_label(note))} {inline_html(note_body(note))}</p>')
    return '<?xml version="1.0" encoding="utf-8"?>\n' + \
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>' + html.escape(title) + \
        '</title><link rel="stylesheet" href="style.css" type="text/css"/></head><body>' + ''.join(body) + '</body></html>'


def write_epub(path: Path, title: str, document_id: str, rows: list[tuple[str, str, str | None]], notes: list[dict], chapters: list[dict]):
    css = "body{font-family:'Songti SC','Noto Serif CJK SC',serif;font-size:12pt}.source-text{text-align:justify;text-justify:inter-word;line-height:1.5;margin:0 0 1em}.source-page{font-size:.75em;margin-top:1em}.source-mark{font-size:.73em;vertical-align:baseline;color:#555555}.original-note{font-size:.875em}.note-ref,.note-number{font-family:inherit;text-decoration:none}.note-ref sup{font-size:.82em}.reading-toc ol{list-style:none;padding-left:1.2em}.reading-toc li{margin:.35em 0}.reading-toc a{display:flex;justify-content:space-between;gap:1em}.toc-page{white-space:nowrap}"
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">{html.escape(document_id)}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>de</dc:language></metadata>
<manifest><item id="content" href="content.xhtml" media-type="application/xhtml+xml"/><item id="css" href="style.css" media-type="text/css"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest>
<spine><itemref idref="content"/></spine></package>'''
    if chapters:
        nav_items = epub_toc_list(chapters, 'content.xhtml')
    else:
        nav_items = f'<ol><li><a href="content.xhtml">{html.escape(title)}</a></li></ol>'
    nav = f'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>目录</title></head><body><nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc">{nav_items}</nav></body></html>'''
    container = '''<?xml version="1.0" encoding="utf-8"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml', container)
        z.writestr('OEBPS/package.opf', opf)
        z.writestr('OEBPS/nav.xhtml', nav)
        z.writestr('OEBPS/style.css', css)
        z.writestr('OEBPS/content.xhtml', epub_xhtml(title, rows, notes, chapters))


def convert_pdf(docx: Path, output: Path, soffice: str):
    with tempfile.TemporaryDirectory(prefix='md-converter-reading-') as temp:
        cmd = [soffice, '--headless', '--convert-to', 'pdf', '--outdir', temp, str(docx)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode:
            raise RuntimeError('PDF conversion failed: ' + (proc.stderr or proc.stdout).strip())
        generated = Path(temp) / (docx.stem + '.pdf')
        if not generated.is_file():
            raise RuntimeError('PDF conversion did not produce a PDF')
        shutil.copy2(generated, output)


def choose_preview_pages(page_texts: list[str], has_toc: bool, has_notes: bool) -> dict:
    """Choose three distinct reading pages by purpose, without changing the book."""
    total = len(page_texts)
    if not total:
        raise ValueError('rendered reading PDF has no pages')
    # This exporter writes the TOC first and original notes last. Use that
    # structure: some Word renderers omit Chinese glyphs from extracted text.
    toc = 1
    note = total if has_notes and total > toc else None
    reserved = {toc} | ({note} if note else set())
    body = [page for page in range(toc + 1, note or total + 1) if page not in reserved]
    if not body:
        body = [page for page in range(1, total + 1) if page not in reserved]
    middle_pool = body[len(body) // 3:max(len(body) // 3 + 1, (2 * len(body) + 2) // 3)] if body else []
    middle = random.SystemRandom().choice(middle_pool) if middle_pool else None
    selected = [('toc' if has_toc else 'no_toc_fallback', toc)]
    if middle is not None:
        selected.append(('middle_body', middle))
    if note is not None:
        selected.append(('original_note', note))
    if len(selected) < min(3, total):
        remaining = [page for page in range(1, total + 1) if page not in {number for _, number in selected}]
        fallback = remaining[-1] if remaining else None
        if fallback is not None:
            selected.append(('no_note_fallback' if has_notes else 'no_notes_in_source', fallback))
    return {'selection': [{'role': role, 'reading_page': page} for role, page in selected],
            'note_page_verified': note is not None}


def preview_three(pdf: Path, destination: Path, has_toc: bool, has_notes: bool):
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError('Word/PDF preview requires pypdfium2') from exc
    document = pdfium.PdfDocument(str(pdf))
    n = len(document)
    selection = choose_preview_pages([''] * n, has_toc, has_notes)
    destination.mkdir(parents=True, exist_ok=True)
    outputs = []
    for entry in selection['selection']:
        page_no = entry['reading_page']
        page = document[page_no - 1]
        bitmap = page.render(scale=1.5)
        target = destination / f'reading-page-{page_no}.png'
        bitmap.to_pil().save(target)
        outputs.append(str(target))
        page.close()
    document.close()
    return {'reading_page_count': n,
            'sampled_reading_pages': [row['reading_page'] for row in selection['selection']],
            'selection': selection['selection'], 'note_page_verified': selection['note_page_verified'],
            'preview_images': outputs}


def main():
    ap = argparse.ArgumentParser(description='Export a justified, annotated original reading copy')
    ap.add_argument('source_bundle', type=Path)
    ap.add_argument('output_dir', type=Path)
    ap.add_argument('--name', required=True)
    ap.add_argument('--formats', default='md', help='Comma-separated: md,word,pdf,epub; default md')
    ap.add_argument('--source-id-file', type=Path, help='JSON with source_ids for a selected sample')
    ap.add_argument('--soffice', help='LibreOffice executable for PDF and Word previews')
    args = ap.parse_args()
    formats = {x.strip().lower() for x in args.formats.split(',') if x.strip()}
    if not formats or formats - FORMATS:
        ap.error('formats must be md,word,pdf,epub')
    source = args.source_bundle.resolve()
    out = args.output_dir.absolute()
    if out.exists() and any(out.iterdir()):
        ap.error('output directory must be new or empty; existing results are not overwritten')
    selected = None
    if args.source_id_file:
        selected = set(read_json(args.source_id_file)['source_ids'])
    blocks_data, items, pages, notes = source_items(source, selected)
    if selected and selected - {i['id'] for i in items}:
        ap.error('some selected source IDs were not found in the bundle')
    unsupported = [i['id'] for i in items if i['kind'] not in {'paragraph', 'heading'} or re.search(r'!\[[^]]*\]\(', i['raw_text'])]
    if unsupported and formats & {'word', 'pdf', 'epub'}:
        ap.error('visual export needs manual figure/table handling for source IDs: ' + ', '.join(unsupported))
    decorate_note_refs(items, notes)
    rows = display_rows(items, pages)
    chapters = toc_entries(source, blocks_data, items, pages)
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    if 'md' in formats:
        if (source / 'assets').is_dir():
            shutil.copytree(source / 'assets', out / 'assets')
        path = out / (args.name + '.md')
        write_md(path, args.name, rows, notes)
        paths['md'] = str(path)
    previews = {}
    if 'word' in formats:
        word = out / (args.name + '.docx')
        write_docx(word, args.name, rows, notes, chapters)
        paths['word'] = str(word)
        soffice = args.soffice or shutil.which('soffice')
        if not soffice:
            raise RuntimeError('LibreOffice is required to preview Word output')
        word_preview_pdf = out / (args.name + '-word-preview.pdf')
        convert_pdf(word, word_preview_pdf, soffice)
        previews['word'] = preview_three(word_preview_pdf, out / 'Word抽样预览', bool(chapters), bool(notes))
        word_preview_pdf.unlink()
    if 'pdf' in formats:
        pdf = out / (args.name + '.pdf')
        first_pass = out / (args.name + '-pass1.pdf')
        first = write_pdf_direct(first_pass, args.name, rows, notes, chapters)
        write_pdf_direct(pdf, args.name, rows, notes, chapters, first['toc_targets'])
        first_pass.unlink()
        paths['pdf'] = str(pdf)
        previews['pdf'] = preview_three(pdf, out / '抽样预览', bool(chapters), bool(notes))
    preview = previews.get('pdf') or previews.get('word')
    if 'epub' in formats:
        epub = out / (args.name + '.epub')
        write_epub(epub, args.name, blocks_data['document_id'], rows, notes, chapters)
        paths['epub'] = str(epub)
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'source_bundle': str(source),
              'document_id': blocks_data['document_id'], 'source_revision': blocks_data['source_revision'],
              'formats': sorted(formats), 'outputs': paths, 'sampled_preview': preview,
              'format_previews': previews,
              'paragraph_alignment': 'justified', 'canonical_source_unchanged': True,
              'linked_toc_entries': len(chapters)}
    (out / '导出记录.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
