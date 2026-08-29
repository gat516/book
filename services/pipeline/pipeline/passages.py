"""Model-facing references to exact source slices, not model-copied quotations.

This adapter changes the wire contract only. Publication still consumes validated
quotes/offsets and the authoritative resolution map; a reference is not proof of a claim.
"""
from __future__ import annotations

from copy import deepcopy
import re

from pipeline.evidence import Names, digest, passage


def source_passages(source: str) -> list[dict]:
    """Stable chapter-content-scoped IDs, retaining whitespace and punctuation.

    Paragraphs are capped at 400 code points. Long paragraphs overlap by 80, so a
    maximum-length named surface cannot disappear at a hard split. No text is rewritten.
    """
    result=[]
    source_hash=digest(source)
    for paragraph in re.finditer(r'[^\r\n]+',source):
        start,end=paragraph.span()
        if not paragraph.group().strip():
            continue
        while start < end:
            stop=min(start+400,end)
            result.append(dict(id=f'p{start:x}_{source_hash[:12]}',source_hash=source_hash,
                               char_start=start,char_end=stop,text=source[start:stop]))
            if stop==end:
                break
            start=stop-80
    return result


class PassageContract:
    def __init__(self, source: str, passage_ids: set[str] | None = None):
        self.source=source
        all_passages=source_passages(source)
        self.passages=[p for p in all_passages if passage_ids is None or p['id'] in passage_ids]
        # Validation must only accept references that were actually offered.
        self.by_id={p['id']:p for p in self.passages}

    def prompt_payload(self, payload: dict) -> dict:
        result={k:v for k,v in payload.items() if k not in {'source','_passage_ids'}}
        result['passages']=[dict(id=p['id'],text=p['text']) for p in self.passages]
        # IDs are attached only to current-chapter occurrences. Reconciliation's
        # earlier mentions have their own context, not current-source offsets.
        if 'mentions' in result:
            result['mentions']=[dict(m,passage_ids=[p['id'] for p in self.passages
                if p['char_start']<=m['char_start'] and m['char_end']<=p['char_end']])
                if 'char_start' in m else m for m in result['mentions']]
        return result

    def schema(self, stage: str, internal_schema, ontology: dict) -> dict:
        ids=list(self.by_id)
        reference={'anyOf':[dict(type='string',enum=ids),dict(type='null')]}
        if stage=='names':
            item=dict(type='object',additionalProperties=False,required=['surface','passage_id'],properties={
                'surface':dict(type='string',minLength=1,maxLength=80),
                'kind':dict(type='string',enum=ontology['kinds']),
                'passage_id':dict(type='string',enum=ids)})
            item['required'].append('kind')
            kinds=ontology['kinds']
            return dict(type='object',additionalProperties=False,required=['reviewed','names'],properties={
                'reviewed':dict(type='object',additionalProperties=False,required=kinds,
                                properties={k:dict(type='boolean',const=True) for k in kinds}),
                'names':dict(type='array',maxItems=64,items=item)})
        result=deepcopy(internal_schema.model_json_schema())
        for definition in result.get('$defs',{}).values():
            fields=definition.get('properties',{})
            if 'quote' not in fields:
                continue
            fields.pop('quote');fields.pop('evidence_start',None)
            fields['passage_id']=reference
            definition['required']=[k for k in definition.get('required',[]) if k not in {'quote','evidence_start'}]+['passage_id']
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

    def materialize(self, stage: str, internal_schema, body: dict, ontology: dict):
        if not isinstance(body,dict):
            raise ValueError('expected an object containing passage references')
        if stage=='names':
            kinds=ontology['kinds']
            reviewed=body.get('reviewed');items=body.get('names')
            if (set(body)!= {'reviewed','names'} or not isinstance(reviewed,dict)
                or set(reviewed)!=set(kinds) or not all(value is True for value in reviewed.values())):
                raise ValueError('name discovery must explicitly review every ontology kind')
            if not isinstance(items,list) or len(items)>64:
                raise ValueError('invalid names list')
            names=[];rejected=[]
            for item in items:
                if not isinstance(item,dict) or set(item)!= {'surface','kind','passage_id'}:
                    raise ValueError('name proposals must reference passages, never supply quotations')
                kind=item['kind'];evidence=self.resolve(item['passage_id']);surface=item['surface']
                if kind not in kinds or not isinstance(surface,str) or not surface.strip() or len(surface)>80:
                    raise ValueError('invalid named surface or kind')
                if not evidence or surface not in evidence['quote']:
                    rejected.append(dict(**item,rejection='unknown passage or surface absent from cited passage'))
                    continue
                names.append(dict(surface=surface,kind=kind,named=True,**evidence))
            return Names(names=names,reviewed_kinds=list(kinds),rejected=rejected)
        if stage not in {'propose','align'}:
            return internal_schema.model_validate(body)
        result=deepcopy(body)
        for key in (['decisions','claims'] if stage=='propose' else ['alignments']):
            for item in result.get(key,[]):
                if 'quote' in item or 'evidence_start' in item or 'passage_id' not in item:
                    raise ValueError('model must cite offered passages, not generate evidence text or offsets')
                ev=self.resolve(item.pop('passage_id'))
                # Invalid/unlinked references retain empty evidence. Existing literal
                # validation rejects claims/links; unaligned cards stay clickable.
                item.update(ev or dict(quote='',evidence_start=None))
        return internal_schema.model_validate(result)
