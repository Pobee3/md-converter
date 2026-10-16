#!/usr/bin/env python3
"""Build a portable, annotated Markdown handoff from any mapped source bundle.

The canonical source bundle remains untouched. This is a derivative reading and
translation input, with machine-readable block provenance in HTML comments.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from export_reading import (NOTE_COLORS, NOTE_TOKEN, decorate_note_refs,
                            display_note_number, note_body, note_label,
                            safe_boundary)


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def j(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def page_label(page: dict | None) -> str:
    verified = [x for x in (page or {}).get('printed_labels', [])
                if x.get('verification') == 'verified']
    return str(verified[0]['label_raw']) if verified else '?'


def source_file(bundle: Path) -> Path | None:
    for name in ('source.pdf', 'source.epub'):
        candidate = bundle / name
        if candidate.is_file():
            return candidate
    return None


def page_mark(page: int, ordinal: int, label: str, original_href: str | None) -> str:
    visible = f'[S.{label}/{page}-{ordinal}]'
    attrs = (f'class="source-mark" data-printed-page="{html.escape(label, quote=True)}" '
             f'data-pdf-page="{page}" data-page-paragraph="{ordinal}" '
             f'style="color:#555555;text-decoration:none;"')
    if original_href:
        return f'<a {attrs} href="{original_href}#page={page}">{visible}</a>'
    return f'<span {attrs}>{visible}</span>'


def render_note_tokens(value: str, notes: list[dict], seen: dict[int, int]) -> str:
    def replace(match):
        index, occurrence, label = int(match.group(1)), int(match.group(2)), match.group(3)
        note = notes[index]
        role = str(note.get('responsibility') or 'unknown')
        color = NOTE_COLORS.get(role, NOTE_COLORS['unknown'])
        seen[index] = max(seen.get(index, 0), occurrence)
        anchor = f'nref-{index}-{occurrence}'
        return (f'<a id="{anchor}" class="note-ref" href="#note-{index}" '
                f'data-note-id="{html.escape(note["note_id"], quote=True)}" '
                f'style="color:#{color};"><sup>{html.escape(label)}</sup></a>')
    return NOTE_TOKEN.sub(replace, value)


def build(bundle: Path, output: Path, title: str, selected_ids: set[str] | None,
          original_override: Path | None = None) -> tuple[str, dict]:
    blocks_data = read(bundle / 'blocks.json')
    maps_data = read(bundle / 'source_map.json')
    notes_data = read(bundle / 'notes.json') if (bundle / 'notes.json').is_file() else {'notes': []}
    pages_data = read(bundle / 'page_map.json') if (bundle / 'page_map.json').is_file() else {'pages': []}
    if len({blocks_data['document_id'], maps_data['document_id'], notes_data['document_id']}) != 1:
        raise ValueError('document_id differs across source files')
    if len({blocks_data['source_revision'], maps_data['source_revision'], notes_data['source_revision']}) != 1:
        raise ValueError('source_revision differs across source files')
    blocks = sorted(blocks_data['blocks'], key=lambda item: item['order'])
    by_map = {item['source_id']: item for item in maps_data['entries']}
    by_page = {item['pdf_page']: item for item in pages_data['pages']}
    source = original_override.resolve() if original_override else source_file(bundle)
    if source and not source.is_file():
        raise ValueError('original file does not exist: ' + str(source))
    relative_source = os.path.relpath(source, output.parent).replace(os.sep, '/') if source else None
    original_href = quote(relative_source, safe='/._-') if source and source.suffix == '.pdf' else None
    counters: dict[int, int] = {}
    ordinals: dict[tuple[str, int], int] = {}
    selected_blocks = []
    for block in blocks:
        fragments = by_map.get(block['block_id'], {}).get('fragments', [])
        if block['kind'] == 'paragraph':
            seen_pages: set[int] = set()
            for fragment_index, fragment in enumerate(fragments):
                if fragment.get('source_type') == 'pdf':
                    page = fragment['pdf_page']
                    if page in seen_pages:
                        continue
                    seen_pages.add(page)
                    counters[page] = counters.get(page, 0) + 1
                    ordinals[(block['block_id'], fragment_index)] = counters[page]
        if selected_ids is None or block['block_id'] in selected_ids:
            selected_blocks.append(block)
    if selected_ids is not None:
        missing = selected_ids - {block['block_id'] for block in selected_blocks}
        if missing:
            raise ValueError('selected source IDs not found: ' + ', '.join(sorted(missing)))
    included = {block['block_id'] for block in selected_blocks}
    notes = [note for note in notes_data['notes']
             if any(reference in included for reference in note.get('references', []))]
    bundle_metadata = read(bundle / 'bundle.json') if (bundle / 'bundle.json').is_file() else {}
    metadata = {
        'format': 'md-converter-translation-handoff-v1',
        'document_id': blocks_data['document_id'],
        'source_revision': blocks_data['source_revision'],
        'quality_state': bundle_metadata.get('quality_state', 'unreported'),
        'canonical_source_md': os.path.relpath(bundle / 'source.md', output.parent).replace(os.sep, '/'),
        'canonical_source_sha256': sha256(bundle / 'source.md'),
        'original_file': relative_source,
        'original_sha256': sha256(source) if source else None,
        'scope': 'selected' if selected_ids is not None else 'bundle',
        'block_count': len(selected_blocks),
    }
    out = ['---']
    for key, value in metadata.items():
        out.append(f'{key}: {j(value)}')
    out += ['---', '', f'# {title}', '']
    seen_note_refs: dict[int, int] = {}
    token_counts: dict[int, int] = {}
    used_pages: set[int] = set()
    for block in selected_blocks:
        source_text = block['text']
        fragments = by_map.get(block['block_id'], {}).get('fragments', [])
        inserts = []
        seen_pages: set[int] = set()
        for fragment_index, fragment in enumerate(fragments):
            if fragment.get('source_type') == 'pdf':
                page = fragment['pdf_page']
                used_pages.add(page)
                if page in seen_pages:
                    continue
                seen_pages.add(page)
                if block['kind'] == 'paragraph':
                    offset = (fragment.get('range') or [0])[0]
                    visible_offset = safe_boundary(source_text, offset)
                    inserts.append((visible_offset, page_mark(page, ordinals[(block['block_id'], fragment_index)],
                                                               page_label(by_page.get(page)), original_href)))
        block_record = {'block_id': block['block_id'], 'source_id': block.get('source_id'),
                        'kind': block['kind']}
        out += [f'<!-- source-block {j(block_record)} -->', f'<a id="{html.escape(block["block_id"], quote=True)}"></a>', '']
        # Page offsets are defined on the canonical text. Notes are linked after page
        # insertion, so note markup cannot shift an original fragment boundary.
        text = source_text
        for offset, marker in sorted(inserts, key=lambda item: item[0], reverse=True):
            text = text[:offset] + marker + ' ' + text[offset:]
        item = [{'id': block['block_id'], 'text': text}]
        decorate_note_refs(item, notes, token_counts)
        text = render_note_tokens(item[0]['text'], notes, seen_note_refs)
        if block['kind'] == 'heading':
            text = re.sub(r'^#{1,6}\s*', '', text)
            out += [f'## {text}', '']
        else:
            out += [text, '']
    if notes:
        out += ['## 原注', '']
        for index, note in enumerate(notes):
            role = str(note.get('responsibility') or 'unknown')
            label = display_note_number(note)
            color = NOTE_COLORS.get(role, NOTE_COLORS['unknown'])
            note_record = {'note_id': note['note_id'], 'responsibility': role}
            out.append(f'<!-- source-note {j(note_record)} -->')
            if index in seen_note_refs:
                number = (f'<a id="note-{index}" href="#nref-{index}-1" '
                          f'style="color:#{color};">{html.escape(label)}</a>')
            else:
                number = f'<span id="note-{index}" style="color:#{color};">{html.escape(label)}</span>'
            out += [f'{number} {note_label(note)}  {note_body(note)}', '']
    report = {'document_id': metadata['document_id'], 'source_revision': metadata['source_revision'],
              'enhanced_md': output.name, 'enhanced_md_sha256': None,
              'canonical_source_md': metadata['canonical_source_md'],
              'source_map': os.path.relpath(bundle / 'source_map.json', output.parent).replace(os.sep, '/'),
              'source_map_sha256': sha256(bundle / 'source_map.json'),
              'blocks_json': os.path.relpath(bundle / 'blocks.json', output.parent).replace(os.sep, '/'),
              'blocks_sha256': sha256(bundle / 'blocks.json'),
              'notes_json': os.path.relpath(bundle / 'notes.json', output.parent).replace(os.sep, '/') if (bundle / 'notes.json').is_file() else None,
              'notes_sha256': sha256(bundle / 'notes.json') if (bundle / 'notes.json').is_file() else None,
              'original_file': relative_source, 'original_sha256': metadata['original_sha256'],
              'translation_md': None, 'alignment_json': None,
              'block_count': len(selected_blocks), 'note_count': len(notes),
              'linked_note_count': len(seen_note_refs), 'pdf_pages': sorted(used_pages),
              'unlinked_note_ids': [note['note_id'] for index, note in enumerate(notes)
                                    if index not in seen_note_refs],
              'quality_state': metadata['quality_state']}
    return '\n'.join(out) + '\n', report


def main() -> None:
    parser = argparse.ArgumentParser(description='Create enhanced Markdown for translation handoff')
    parser.add_argument('source_bundle', type=Path)
    parser.add_argument('output_md', type=Path)
    parser.add_argument('--title', required=True)
    parser.add_argument('--source-id-file', type=Path)
    parser.add_argument('--original', type=Path, help='Original PDF/EPUB when not inside source bundle')
    parser.add_argument('--library-index', type=Path, help='Existing private workbench index to register the handoff')
    args = parser.parse_args()
    bundle = args.source_bundle.resolve()
    output = args.output_md.absolute()
    if output.exists():
        parser.error('output Markdown already exists; choose a new path')
    if not (bundle / 'source.md').is_file():
        parser.error('source bundle lacks source.md')
    selected = set(read(args.source_id_file)['source_ids']) if args.source_id_file else None
    library_path = args.library_index.resolve() if args.library_index else None
    library = read(library_path) if library_path else None
    output.parent.mkdir(parents=True, exist_ok=True)
    content, report = build(bundle, output, args.title, selected, args.original)
    if library is not None and (library.get('document_id'), library.get('source_revision')) != (report['document_id'], report['source_revision']):
        raise ValueError('library index identity differs from source bundle')
    output.write_text(content, encoding='utf-8')
    report['enhanced_md_sha256'] = sha256(output)
    report_path = output.with_name(output.stem + '-交接索引.json')
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if library_path is not None and library is not None:
        library.setdefault('paths', {})['translation_input_markdown'] = os.path.relpath(output, library_path.parent)
        library['paths']['translation_handoff_index'] = os.path.relpath(report_path, library_path.parent)
        temp = library_path.with_suffix(library_path.suffix + '.new')
        temp.write_text(json.dumps(library, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temp.replace(library_path)
    print(json.dumps({'markdown': str(output), 'index': str(report_path), **report}, ensure_ascii=False))


if __name__ == '__main__':
    main()
