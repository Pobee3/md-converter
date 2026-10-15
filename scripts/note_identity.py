"""Conservative original-note attribution: explicit labels only, never placement."""
import re

# An explicit note heading/signature is evidence; mentions inside prose are not.
LABELS={
    'author':r"(?:author(?:['’]s)? note|作者注|原作者注)",
    'original_translator':r"(?:translator(?:['’]s)? note|译者注|譯者註|原译者注|原譯者註)",
    'editor':r"(?:editor(?:['’]s)? note|编者注|編者註|编注|編註)",
    'critical_apparatus':r'(?:critical apparatus|校勘注|校勘記|校勘记)'
}

def classify(text):
    prefix=r'^\s*(?:[0-9０-９]+[.)、]?\s*|[*†‡]+\s*)?[\[【（(]?'
    suffix=r'[\]】）)]?\s*[:：.。—-]'
    matches=[]
    for role,label in LABELS.items():
        m=re.search(prefix+'('+label+')'+suffix,text,re.I)
        if m:matches.append((role,m.group(0)))
    # Explicit terminal editorial signature observed in German critical editions.
    # A discussion of this abbreviation in note prose does not establish identity.
    signature=re.search(r'[—–-]\s*Anm\.\s*d\.\s*Hrsg\.\s*$',text,re.I)
    if signature:matches.append(('editor',signature.group(0)))
    if len(matches)==1:
        role,label=matches[0]
        return {'responsibility':role,'agent_role':role,'apparatus_kind':'critical' if role=='critical_apparatus' else None,
                'evidence':'Explicit original note heading/signature '+repr(label)+'; no personal name inferred','verification':'unverified'}
    return {'responsibility':'unknown','agent_role':'unknown','apparatus_kind':None,
            'evidence':'No unambiguous explicit responsibility heading; placement is not identity','verification':'unverified'}
