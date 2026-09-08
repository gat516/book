"""Model-facing references to exact source slices, not model-copied quotations.

This adapter changes the wire contract only. Publication still consumes validated
quotes/offsets and the authoritative resolution map; a reference is not proof of a claim.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import re

from pipeline.evidence import CJK_RE, digest, passage


@lru_cache(maxsize=32)
def _split_passages(source: str, max_chars: int, overlap: int) -> tuple[dict,...]:
    """Memoized paragraph split. See `source_passages` for the contract.

    One chapter's source is re-split many times per extraction: inside every `call()`,
    once per fact in `_evidence_passage_ids`, and once per candidate size while packing
    identity batches. The split is a pure function of its arguments, so compute it once.
    """
    result=[]
    source_hash=digest(source)
    for paragraph in re.finditer(r'[^\r\n]+',source):
        start,end=paragraph.span()
        if not paragraph.group().strip():
            continue
        while start < end:
            stop=min(start+max_chars,end)
            result.append(dict(id=f'p{start:x}_{source_hash[:12]}',source_hash=source_hash,
                               char_start=start,char_end=stop,text=source[start:stop]))
            if stop==end:
                break
            start=stop-overlap
    return tuple(result)


def source_passages(source: str, *, max_chars: int = 400, overlap: int = 80) -> list[dict]:
    """Stable chapter-content-scoped IDs, retaining whitespace and punctuation.

    Paragraphs are capped at ``max_chars`` code points. Long paragraphs overlap, so a
    maximum-length named surface cannot disappear at a hard split. No text is rewritten.

    Passage dicts are shared with the memo and must be treated as immutable; the list
    is a fresh copy, so a caller may append a synthetic range to its own result.
    """
    return list(_split_passages(source,max_chars,overlap))


@lru_cache(maxsize=32)
def _split_windows(source: str, max_chars: int, overlap: int) -> tuple[dict,...]:
    """Memoized window split. See `source_windows` for the contract."""
    result = []
    source_hash = digest(source)
    start = 0
    while start < len(source):
        stop = min(start + max_chars, len(source))
        if stop < len(source):
            boundary = source.rfind("\n", start + (max_chars * 3 // 4), stop)
            if boundary > start:
                stop = boundary + 1
        text = source[start:stop]
        if text.strip():
            result.append(dict(id=f'w{start:x}_{source_hash[:12]}', source_hash=source_hash,
                               char_start=start, char_end=stop, text=text))
        if stop == len(source):
            break
        start = max(start + 1, stop - overlap)
    return tuple(result)


def source_windows(source: str, *, max_chars: int = 1600, overlap: int = 200) -> list[dict]:
    """Contiguous evidence windows that retain antecedents across paragraph breaks.

    Event roles often use a name in one paragraph and a pronoun in the next. The generic
    claim extractor keeps paragraph-sized evidence, while chapter events opt into these
    larger exact slices so a cited record can prove both identity and action (§0.2).

    Same immutability contract as `source_passages`.
    """
    if max_chars <= overlap or overlap < 0:
        raise ValueError("passage window must be larger than its overlap")
    return list(_split_windows(source,max_chars,overlap))


class PassageContract:
    def __init__(self, source: str, passage_ids: set[str] | None = None,
                 *, max_chars: int = 400, overlap: int = 80, windows: bool = False):
        self.source=source
        self.max_chars=max_chars
        self.overlap=overlap
        self.windows=windows
        factory = source_windows if windows else source_passages
        all_passages=factory(source, max_chars=max_chars, overlap=overlap)
        self.passages=[p for p in all_passages if passage_ids is None or p['id'] in passage_ids]
        # Validation must only accept references that were actually offered.
        self.by_id={p['id']:p for p in self.passages}

    def prompt_payload(self, payload: dict) -> dict:
        result={k:v for k,v in payload.items() if not k.startswith('_') and k != 'source'}
        result['passages']=[dict(id=p['id'],text=p['text']) for p in self.passages]
        # IDs are attached only to current-chapter occurrences. Reconciliation's
        # earlier mentions have their own context, not current-source offsets.
        if 'mentions' in result:
            result['mentions']=[dict(m,passage_ids=[p['id'] for p in self.passages
                if p['char_start']<=m['char_start'] and m['char_end']<=p['char_end']])
                if 'char_start' in m else m for m in result['mentions']]
        return result

    def schema(self, stage: str, internal_schema, ontology: dict, vocabulary: dict | None = None,
              limits: dict | None = None) -> dict:
        """Build the wire JSON schema for ``stage``.

        ``limits`` overrides per-list ``maxItems`` (B.4: local vs hosted ceilings are
        experimental, not qualified, and must never live as a second copy of the
        internal pydantic Field bound -- the model only sees what's passed here).
        """
        ids=list(self.by_id)
        reference={'anyOf':[dict(type='string',enum=ids),dict(type='null')]}
        result=deepcopy(internal_schema.model_json_schema())
        for definition in result.get('$defs',{}).values():
            fields=definition.get('properties',{})
            if 'quote' not in fields:
                continue
            fields.pop('quote');fields.pop('evidence_start',None)
            fields['passage_id']=reference
            definition['required']=[k for k in definition.get('required',[]) if k not in {'quote','evidence_start'}]+['passage_id']
        if stage == 'align' and limits and 'alignments' in limits:
            result['properties']['alignments']['maxItems']=limits['alignments']
        if stage == 'extract':
            limits = limits or {}
            ref=dict(type='object',additionalProperties=False,
                     required=['name_index','passage_id','occurrence_index'],properties={
                         'name_index':dict(type='integer',minimum=0),
                         'passage_id':dict(type='string',enum=ids),
                         'occurrence_index':dict(type='integer',minimum=0)})
            citations=dict(type='array',minItems=1,maxItems=2,items=dict(type='string',enum=ids))
            admitted=[]
            if vocabulary:
                admitted=sorted({x['name'] if isinstance(x,dict) else x for x in vocabulary.get('attributes',[])} |
                                {x['name'] if isinstance(x,dict) else x for x in vocabulary.get('relations',[])})
            term=dict(anyOf=[dict(type='string',enum=admitted),dict(type='string',pattern=r'^[a-z][a-z0-9_]{1,39}$')]) if admitted else dict(type='string',pattern=r'^[a-z][a-z0-9_]{1,39}$')
            name=dict(type='object',additionalProperties=False,required=['surface','kind','passage_id'],properties={
                'surface':dict(type='string',minLength=1,maxLength=80),'kind':dict(type='string',enum=ontology['kinds']),
                'passage_id':dict(type='string',enum=ids)})
            attr=dict(type='object',additionalProperties=False,required=['subject_ref','attribute','value','value_en','passage_ids'],properties={
                'subject_ref':ref,'attribute':term,'value':dict(type='string',maxLength=400),'value_en':dict(type='string',maxLength=200),'passage_ids':citations})
            rel=dict(type='object',additionalProperties=False,required=['src_ref','dst_ref','relation','sentiment','passage_ids'],properties={
                'src_ref':ref,'dst_ref':ref,'relation':term,'sentiment':dict(anyOf=[dict(type='integer',minimum=-1,maximum=1),dict(type='null')]),'passage_ids':citations})
            occ=dict(type='object',additionalProperties=False,required=['summary','summary_en','participant_refs','passage_ids'],properties={
                'summary':dict(type='string',maxLength=400),'summary_en':dict(type='string',maxLength=200),'participant_refs':dict(type='array',maxItems=8,items=ref),'passage_ids':citations})
            return dict(type='object',additionalProperties=False,required=['names','attributes','relations','occurrences'],properties={
                'names':dict(type='array',maxItems=limits.get('names',14),items=name),
                'attributes':dict(type='array',maxItems=limits.get('attributes',8),items=attr),
                'relations':dict(type='array',maxItems=limits.get('relations',6),items=rel),
                'occurrences':dict(type='array',maxItems=limits.get('occurrences',5),items=occ)})
        return result

    def resolve(self, reference) -> dict | None:
        if not isinstance(reference,str):
            return None
        p=self.by_id.get(reference)
        if not p or p['source_hash']!=digest(self.source):
            return None
        if not passage(self.source,p['text'],start=p['char_start']):
            return None
        return dict(quote=p['text'],evidence_start=p['char_start'])

    def claim_refs_are_adjacent(self, references) -> bool:
        """Return whether cited claim ranges have no uncited passage between them.

        This is intentionally based on the complete source split, rather than the order
        of the IDs in ``self.by_id``. A claims request may offer a filtered prompt and
        model-generated reference order is not source order. Malformed, unknown, duplicate
        or overlapping references remain materialization errors; this helper only owns the
        soft rejection of a valid, non-adjacent pair (§0.2).
        """
        if not isinstance(references,list) or len(references) < 2:
            return True
        cited=[self.by_id.get(reference) for reference in references]
        if any(p is None for p in cited):
            return True
        ranges=sorted((p['char_start'],p['char_end']) for p in cited)
        if any(right_start < left_end
               for (_,left_end),(right_start,_) in zip(ranges,ranges[1:])):
            return True
        factory=source_windows if self.windows else source_passages
        complete=factory(self.source,max_chars=self.max_chars,overlap=self.overlap)
        for (_,left_end),(right_start,_) in zip(ranges,ranges[1:]):
            if any(p['char_start'] >= left_end and p['char_end'] <= right_start
                   for p in complete):
                return False
        return True

    def materialize(self, stage: str, internal_schema, body: dict, ontology: dict):
        if not isinstance(body,dict):
            raise ValueError('expected an object containing passage references')
        if stage == 'extract':
            if set(body) != {'names','attributes','relations','occurrences'}:
                raise ValueError('extract response must contain four proposal lists')
            names=body['names']
            if not all(isinstance(n,dict) and set(n)=={'surface','kind','passage_id'} for n in names):
                raise ValueError('invalid extract name proposal')
            def matches(surface, passage_row):
                # Same CJK discipline as evidence.source_mentions: Python's isalnum()
                # is True for Han characters, so an unguarded boundary check would
                # reject nearly every occurrence in zh source text (no spaces mean a
                # name is always "surrounded" by alnum characters). Both functions
                # must agree, or an anchored reference here finds a different
                # occurrence set than the mention_id source_mentions() produced.
                text=passage_row['text']; out=[]; start=0
                cjk=CJK_RE.search(surface)
                while True:
                    at=text.find(surface,start)
                    if at<0: break
                    before=text[at-1] if at else ''; after=text[at+len(surface)] if at+len(surface)<len(text) else ''
                    if cjk or not ((before.isalnum() and surface[0].isalnum()) or (after.isalnum() and surface[-1].isalnum())):
                        out.append((passage_row['char_start']+at,passage_row['char_start']+at+len(surface)))
                    start=at+1
                return out
            def ref(item, key):
                value=item.get(key)
                if not isinstance(value,dict) or set(value)!={'name_index','passage_id','occurrence_index'}:
                    raise ValueError('invalid anchored occurrence reference')
                ni=value['name_index']; pid=value['passage_id']; oi=value['occurrence_index']
                if not isinstance(ni,int) or not 0<=ni<len(names) or pid not in self.by_id:
                    raise ValueError('occurrence reference is not offered')
                if pid not in item.get('passage_ids',[]):
                    raise ValueError('occurrence citation does not include its passage')
                found=matches(names[ni]['surface'],self.by_id[pid])
                if oi>=len(found): raise ValueError('occurrence ordinal is out of range')
                lo,hi=found[oi]
                return dict(mention_index=ni,passage_id=pid,occurrence_index=oi,char_start=lo,char_end=hi,
                            surface=names[ni]['surface'],kind=names[ni]['kind'])
            def citations(item):
                refs=item.get('passage_ids')
                if not isinstance(refs,list) or not 1<=len(refs)<=2 or len(set(refs))!=len(refs):
                    raise ValueError('extract items must cite one or two distinct passages')
                if any(r not in self.by_id for r in refs): raise ValueError('extract citation is not offered')
                if len(refs)==2 and not self.claim_refs_are_adjacent(refs):
                    raise ValueError('extract citations must be adjacent')
                return refs
            out=dict(names=[] ,attributes=[],relations=[],occurrences=[])
            for n in names:
                ev=self.resolve(n['passage_id'])
                if not ev or n['surface'] not in ev['quote']: raise ValueError('name surface absent from cited passage')
                out['names'].append(dict(surface=n['surface'],kind=n['kind'],**ev))
            for item in body['attributes']:
                citations(item); subject=ref(item,'subject_ref')
                out['attributes'].append(dict(item,subject_ref=subject))
            for item in body['relations']:
                citations(item); src=ref(item,'src_ref'); dst=ref(item,'dst_ref')
                out['relations'].append(dict(item,src_ref=src,dst_ref=dst))
            for item in body['occurrences']:
                citations(item)
                # Participant refs are validated with the same anchor contract.
                participants=[]
                for value in item.get('participant_refs',[]):
                    holder=dict(item,subject_ref=value); participants.append(ref(holder,'subject_ref'))
                out['occurrences'].append(dict(item,participant_refs=participants))
            return out
        if stage not in {'propose','align'}:
            return internal_schema.model_validate(body)
        # 'propose' is the SEPARATE structured-event track's stage (pipeline/events.py,
        # event_rebuild.py's lineage -- CLAUDE.md: "do not merge with the structured-
        # event track"), not part of Phase B's merged extract. It shares this evidence
        # contract with 'align': the model selects an offered passage_id but never
        # supplies a quote or offset (§0.2).
        result=deepcopy(body)
        keys = {'propose':['decisions','claims','events'],'align':['alignments']}[stage]
        for key in keys:
            for item in result.get(key,[]):
                if 'quote' in item or 'evidence_start' in item or 'passage_id' not in item:
                    raise ValueError('model must cite offered passages, not generate evidence text or offsets')
                ref=item.pop('passage_id')
                ev=self.resolve(ref)
                # Invalid/unlinked references retain empty evidence. Existing literal
                # validation rejects links; unaligned cards stay clickable.
                item.update(ev or dict(quote='',evidence_start=None))
        return internal_schema.model_validate(result)
