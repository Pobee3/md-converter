"""Native EPUB DOM extraction and exact archive locators. Never executes book content."""
import html
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit, quote
from common import ConversionError, digest
from mapping import require

EPUB_TYPE='{http://www.idpf.org/2007/ops}type'
def local(e):return e.tag.rsplit('}',1)[-1] if isinstance(e.tag,str) else ''
def text_of(e):return re.sub(r'\s+',' ',''.join(e.itertext())).strip()
def escape(s):return re.sub(r'([\\`*_{}\[\]<>#!|])',r'\\\1',s)
def target(base,href):
    u=urlsplit(href)
    if u.scheme or u.netloc:raise ConversionError('External resource: '+href)
    p=unquote(u.path)
    if '\\' in p or '\x00' in p or p.startswith('/'):raise ConversionError('Unsafe EPUB href')
    p=posixpath.normpath(posixpath.join(posixpath.dirname(base),p)) if p else base
    if p=='..' or p.startswith('../'):raise ConversionError('EPUB path escapes archive')
    return p,unquote(u.fragment)

def destination(base,href):
    p,e=target(base,href);return p+'#'+e

class Book:
    def __init__(self,source):
        self.zip=zipfile.ZipFile(source)
        try:self._read()
        except BaseException:self.zip.close();raise
    def _read(self):
        infos=self.zip.infolist();names=[i.filename for i in infos]
        if len(names)!=len(set(names)):raise ConversionError('Duplicate ZIP member')
        if len(infos)>20000 or sum(i.file_size for i in infos)>256*1024*1024:raise ConversionError('Archive exceeds bounded limits')
        for i in infos:
            p=PurePosixPath(i.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in i.filename or '\x00' in i.filename or i.file_size>32*1024*1024:
                raise ConversionError('Unsafe or oversized ZIP member')
            if i.flag_bits & 1:raise ConversionError('Encrypted ZIP unsupported')
        self.names=set(names)
        if self.zip.read('mimetype')!=b'application/epub+zip':raise ConversionError('Not EPUB')
        if 'META-INF/encryption.xml' in self.names:
            # Font obfuscation does not encrypt prose. Do not guess other encryption schemes.
            for e in self.xml('META-INF/encryption.xml').iter():
                if local(e)=='EncryptionMethod' and e.get('Algorithm') not in ('http://www.idpf.org/2008/embedding','http://ns.adobe.com/pdf/enc#RC'):
                    raise ConversionError('Encrypted EPUB content unsupported')
        roots=[e.get('full-path') for e in self.xml('META-INF/container.xml').iter() if local(e)=='rootfile']
        if len(roots)!=1:raise ConversionError('Select EPUB rendition first')
        self.opf,_=target('',roots[0]);package=self.xml(self.opf)
        self.title=next((text_of(e) for e in package.iter() if local(e)=='title'),'')
        items=[e for e in package.iter() if local(e)=='item']
        self.items={e.get('id'):e for e in items}
        if len(items)!=len(self.items):raise ConversionError('Duplicate manifest ID')
        self.spine=[];self.documents={};self.media={}
        for i in items:
            path,_=target(self.opf,i.get('href',''));self.media[path]=i.get('media-type')
        for e in package.iter():
            if local(e)=='itemref':
                i=self.items[e.get('idref')];path,_=target(self.opf,i.get('href',''))
                if i.get('media-type')!='application/xhtml+xml':raise ConversionError('Non-XHTML spine requires review')
                self.spine.append({'href':path,'linear':e.get('linear','yes')})
        if not self.spine:raise ConversionError('Empty EPUB spine')
        if len({s['href'] for s in self.spine})!=len(self.spine):raise ConversionError('Repeated spine href needs explicit occurrence mapping')
        self.navigation=[];self.page_list=[];self.nav_paths=set()
        for item in items:
            if 'nav' not in item.get('properties','').split():continue
            path,_=target(self.opf,item.get('href'));self.nav_paths.add(path)
            for nav in self.xml(path).iter():
                if local(nav)!='nav':continue
                kinds=nav.get(EPUB_TYPE,'').split()
                rows=self.navigation if 'toc' in kinds else self.page_list if 'page-list' in kinds else None
                if rows is None:continue
                def walk(e,parent=None):
                    if local(e)=='li':
                        a=next((x for x in e if local(x) in ('a','span')),None)
                        if a is not None:
                            h,el=target(path,a.get('href')) if a.get('href') else (None,None)
                            idx=len(rows);rows.append({'title':text_of(a),'href':h,'element_id':el or None,'parent_index':parent})
                            parent=idx
                    for c in e:walk(c,parent)
                walk(nav)
        if not self.navigation:
            for item in items:
                if item.get('media-type')!='application/x-dtbncx+xml':continue
                path,_=target(self.opf,item.get('href'))
                def ncx(e,parent=None):
                    if local(e)=='navPoint':
                        lab=next((x for x in e if local(x)=='navLabel'),None)
                        content=next((x for x in e if local(x)=='content'),None)
                        if content is None:raise ConversionError('NCX target missing')
                        h,el=target(path,content.get('src'));idx=len(self.navigation)
                        self.navigation.append({'title':text_of(lab) if lab is not None else '', 'href':h,'element_id':el or None,'parent_index':parent});parent=idx
                    for c in e:ncx(c,parent)
                ncx(self.xml(path))
    def xml(self,path):
        data=self.zip.read(path)
        declaration=re.search(br'<!DOCTYPE[^>]*',data,re.I)
        if b'<!ENTITY' in data.upper() or (declaration and b'[' in declaration.group(0)):
            raise ConversionError('XML entities/internal DTD unsupported')
        # EPUB2 external DTD declarations are metadata, never fetched by ElementTree.
        return ET.fromstring(data)
    def close(self):self.zip.close()


def extract(book,registry):
    records=[];notes=[];anchors={};issues=[];raw_nodes={}
    def parse_doc(href,spine_index):
        root=book.xml(href);seen=set();serial=0;covered=set()
        def walk(e,path,note_parent=False):
            nonlocal serial
            serial+=1;position=[spine_index if spine_index is not None else len(book.spine),serial]
            eid=e.get('id');loc=href+'#'+(eid if eid else '@'+path)
            if eid:
                if eid in seen:raise ConversionError('Duplicate XHTML ID in '+href)
                seen.add(eid);anchors[href+'#'+eid]={'position':position,'locator':loc}
            raw_nodes[loc]={'href':href,'element_id':eid,'node_path':path,'spine_index':spine_index,
                            'markup_sha256':digest(ET.tostring(e,encoding='utf-8'))}
            tag=local(e);types=e.get(EPUB_TYPE,'').split();role=e.get('role','')
            is_note=any(t in types for t in ('footnote','endnote','rearnote')) or role in ('doc-footnote','doc-endnote')
            kinds={'p':'paragraph','h1':'heading','h2':'heading','h3':'heading','h4':'heading','h5':'heading','h6':'heading',
                   'img':'figure','figure':'figure','svg':'figure','table':'table','math':'equation','ul':'list','ol':'list','pre':'paragraph','blockquote':'paragraph'}
            item=None
            if is_note and not note_parent:
                item={'note_id':registry.get('n',loc),'locator':loc,'element':e,'origin':raw_nodes[loc],
                      'placement':'endnote' if any(t in types for t in ('endnote','rearnote')) or role=='doc-endnote' else 'footnote'}
                notes.append(item)
            elif not note_parent and tag in kinds:
                kind=kinds[tag]
                if kind=='paragraph' and not text_of(e) and not any(local(n) in ('img','svg','math','br') for n in e.iter()):
                    issues.append({'kind':'empty_layout_block_omitted','locator':loc,'origin':raw_nodes[loc]})
                else:
                    item={'block_id':registry.get('p' if kind=='paragraph' else 'b',loc),'kind':kind,
                        'locator':loc,'element':e,'origin':raw_nodes[loc],'position':position,'level':int(tag[1]) if kind=='heading' else None}
                    records.append(item)
            if item is not None:covered.update(id(n) for n in e.iter())
            if tag in ('script','style'):
                if tag=='script':issues.append({'kind':'active_content_omitted','locator':loc})
                return
            # Traverse descendants to register their precise IDs; do not duplicate blocks.
            for i,c in enumerate(e):walk(c,path+'/'+str(i),note_parent or item is not None)
        walk(root,'0')
        own=[r for r in records if r['origin']['href']==href]
        if own:anchors[href+'#']={'position':own[0]['position'],'locator':own[0]['locator']}
        # Non-whitespace text in an unmodeled body is not silently discarded.
        body=next((e for e in root.iter() if local(e)=='body'),None)
        if body is not None:
            for el in body.iter():
                if id(el) not in covered and local(el) not in ('script','style'):
                    if el.text and el.text.strip():issues.append({'kind':'unmodeled_container_text','href':href,'text':el.text})
                    for child in el:
                        if child.tail and child.tail.strip():issues.append({'kind':'unmodeled_container_tail','href':href,'text':child.tail})
        if body is not None and text_of(body) and not own and not any(n['origin']['href']==href for n in notes):
            issues.append({'kind':'unmodeled_document','href':href})
    for i,s in enumerate(book.spine):parse_doc(s['href'],i)
    # Read non-spine XHTML as dependency candidates only, never as extra body chapters.
    for href,media in book.media.items():
        if media=='application/xhtml+xml' and href not in book.nav_paths and href not in {s['href'] for s in book.spine}:
            before=len(records);parse_doc(href,None);del records[before:]
    chapters=[]
    for idx,n in enumerate(book.navigation):
        loc=n['href']+'#'+(n['element_id'] or '') if n['href'] else None
        a=anchors.get(loc);start=a['position'] if a else None
        if not start and n['href']:issues.append({'kind':'unresolved_toc_target','target':loc})
        chapters.append(dict(n,chapter_id=registry.get('ch','navigation:'+str(idx)+':'+str(loc)),start=start))
    # Structural grouping entries without href inherit the first descendant's start.
    for i in reversed(range(len(chapters))):
        if chapters[i]['start'] is None:
            child=next((c for c in chapters if c['parent_index']==i and c['start'] is not None),None)
            if child:chapters[i]['start']=child['start']
    if not chapters:
        issues.append({'kind':'missing_original_toc'})
        for i,s in enumerate(book.spine):
            rs=[r for r in records if r['origin']['spine_index']==i]
            if rs:chapters.append({'chapter_id':registry.get('ch','fallback:'+s['href']),'title':next((text_of(r['element']) for r in rs if r['kind']=='heading'),s['href']),
                'href':s['href'],'element_id':None,'parent_index':None,'start':rs[0]['position'],'inferred':True})
    for c in chapters:
        c['parent_id']=chapters[c['parent_index']]['chapter_id'] if c['parent_index'] is not None else None
    starts=[(c['start'],i,c) for i,c in enumerate(chapters) if c['start'] is not None]
    if any(starts[i][0]>starts[i+1][0] for i in range(len(starts)-1)):
        issues.append({'kind':'toc_order_differs_from_spine'})
    for r in records:
        eligible=[x for x in starts if x[0]<=r['position']]
        if not eligible:
            # Front matter without a TOC entry stays preserved and explicitly flagged.
            issues.append({'kind':'body_before_first_toc','locator':r['locator']});c=chapters[0]
        else:c=max(eligible,key=lambda x:(x[0],x[1]))[2]
        r['chapter_id']=c['chapter_id']
    by_locator={r['locator']:r for r in records}
    targets={}
    for a,info in anchors.items():
        r=by_locator.get(info['locator'])
        if not r:
            # Container target -> first descendant; inline target -> containing block.
            matching=[r for r in records if r['origin']['href']==a.split('#')[0] and
                (r['origin']['node_path'].startswith(raw_nodes[info['locator']]['node_path']+'/') or
                 raw_nodes[info['locator']]['node_path'].startswith(r['origin']['node_path']+'/'))]
            r=matching[0] if matching else None
        if r:targets[a]=r['block_id']
    for n in notes:
        if n['origin']['element_id']:targets[n['origin']['href']+'#'+n['origin']['element_id']]=n['note_id']
    return {'records':records,'notes':notes,'chapters':chapters,'targets':targets,'issues':issues,'raw_nodes':raw_nodes}


def render(element,href,targets,copy_asset,owner_id,available_ids=None):
    """Serialize inline structure directly; repeated reference labels are never replaced globally."""
    refs=[];assets=[]
    def rec(e,path,base):
        tag=local(e)
        if tag in ('script','style'):return ''
        if tag=='img':
            p,_=target(href,e.get('src',''));asset=copy_asset(p);assets.append(asset)
            return '!['+escape(e.get('alt',''))+']('+asset['path']+')'
        if tag=='svg':
            elements=list(e.iter());images=[node for node in elements if local(node)=='image']
            require(len(images)==1 and all(local(node) in ('svg','image') for node in elements),
                    'Complex inline SVG needs original-page review')
            image=images[0];source=next((value for key,value in image.attrib.items() if key.rsplit('}',1)[-1]=='href'),None)
            require(source and not any(key.lower().startswith('on') for node in elements for key in node.attrib),
                    'Active or missing inline SVG image source')
            p,_=target(href,source);asset=copy_asset(p);assets.append(asset)
            return '![]('+asset['path']+')'
        if tag=='br':return '<br />'
        if tag=='a' and e.get('href'):
            raw=e.get('href');u=urlsplit(raw);dest=None
            if not u.scheme and not u.netloc:dest=targets.get(destination(href,raw))
            if dest and available_ids is not None and dest not in available_ids:
                label=children(e,path,base)
                refs.append({'from_id':owner_id,'node_path':path,'range':[base,base+len(label)],'original_label':text_of(e),
                             'original_target':raw,'target_id':dest,'state':'outside_selected_scope'})
                return label
            url='#'+dest if dest else raw
            if u.scheme and u.scheme.lower() not in ('http','https','mailto'):
                # Preserve label and original target in evidence; do not emit active script URLs.
                label=children(e,path,base)
                refs.append({'from_id':owner_id,'node_path':path,'range':[base,base+len(label)],'original_target':raw,'target_id':None,'state':'unsafe_scheme'})
                return label
            label=children(e,path,base+1)
            result='['+label+']('+quote(url,safe='/#:?=&%+@,;~-._')+')'
            refs.append({'from_id':owner_id,'node_path':path,'range':[base,base+len(result)],'label_range':[base+1,base+1+len(label)],
                'original_label':text_of(e),'original_target':raw,'target_id':dest,'state':'resolved' if dest else 'external' if u.scheme else 'unresolved',
                'noteref':'noteref' in e.get(EPUB_TYPE,'').split() or e.get('role')=='doc-noteref'})
            return result
        wrappers={'em':('*','*'),'i':('*','*'),'strong':('**','**'),'b':('**','**'),
                  'sup':('<sup>','</sup>'),'sub':('<sub>','</sub>'),'code':('<code>','</code>')}
        if tag in wrappers:
            a,z=wrappers[tag];return a+children(e,path,base+len(a))+z
        if tag=='table':
            # Native HTML retains rowspan/colspan, units, signs and cell notes.
            return table(e,path,base)
        if tag=='math':return ET.tostring(e,encoding='unicode').strip()
        if tag in ('ul','ol'):
            out=''
            for i,c in enumerate(e):
                pre=('- ' if tag=='ul' else str(i+1)+'. ')
                out+=pre+children(c,path+'/'+str(i),base+len(out)+len(pre))+'\n'
            return out.rstrip('\n')
        if tag=='figcaption':return children(e,path,base)
        return children(e,path,base)
    def table(e,path,base):
        tag=local(e)
        if tag not in ('table','thead','tbody','tfoot','tr','th','td','caption','colgroup','col'):
            return rec(e,path,base)
        attrs=''.join(' '+k+'="'+html.escape(v,quote=True)+'"' for k,v in e.attrib.items() if k in ('rowspan','colspan','scope'))
        out='<'+tag+attrs+'>'+html.escape(e.text or '')
        for i,c in enumerate(e):out+=table(c,path+'/'+str(i),base+len(out))+html.escape(c.tail or '')
        return out+'</'+tag+'>'
    def children(e,path,base):
        out=escape(re.sub(r'\s+',' ',e.text or ''))
        for i,c in enumerate(e):
            out+=rec(c,path+'/'+str(i),base+len(out));out+=escape(re.sub(r'\s+',' ',c.tail or ''))
        return out
    result=rec(element,'0',0)
    trim=len(result)-len(result.lstrip());result=result.strip()
    for ref in refs:
        for field in ('range','label_range'):
            if field in ref:ref[field]=[x-trim for x in ref[field]]
    return result,refs,assets
