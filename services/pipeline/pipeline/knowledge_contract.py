"""Request-local graph choices. Grammar and application enforce the same allowlists.

The old fixed-slot 'names'/'name_slots' grammar (name_schema/materialize_names) is
gone with B.6's merged extract pass; PassageContract.schema/.materialize in
passages.py now own the 'extract' stage's names[] list directly.
"""
import json

from pipeline.evidence import IdentityDecisions, Verification, Verdict


def identity_schema(rows, passage_ids):
    """Constrain output to the only datum the identity selector actually owns.

    Evidence is application-selected and the independent verifier owns the semantic
    verdict. Echoing either one enlarged the prompt and completion without adding
    authority (§0.3, §5.4).
    """
    slots = {row['occurrence_ref']: dict(type='string',enum=list(row['choices']))
             for row in rows}
    return dict(type='object', additionalProperties=False, required=list(slots), properties=slots)


def materialize_identity(body, rows, contract):
    if not isinstance(body, dict) or set(body) != {r['occurrence_ref'] for r in rows}:
        raise ValueError('identity response must contain one unique decision per occurrence')
    decisions = []
    for row in rows:
        choice = body[row['occurrence_ref']]
        if not isinstance(choice,str) or choice not in row['choices']:
            raise ValueError('unoffered identity choice')
        subject=row.get('subject') or {}
        quote=subject.get('quote');start=subject.get('context_start',subject.get('char_start'))
        if not isinstance(quote,str) or not isinstance(start,int):
            raise ValueError('identity occurrence lacks application-owned evidence')
        outcome, target, reason = row['choices'][choice]
        decisions.append(dict(occurrence_ref=row['occurrence_ref'], outcome=outcome,
            target_ref=target, reason_code=reason, explanation=reason.replace('_',' '),
            quote=quote,evidence_start=start))
    return IdentityDecisions(decisions=decisions)


def compact_identity_payload(rows, contract):
    """Normalize repeated identity contexts into one request-local subject table.

    Every offered choice remains present, while each quote or candidate description is
    serialized once rather than once per comparison (§0.7).
    """
    subjects={};subject_refs={}

    def compact(context):
        context=dict(context)
        start=context.get('char_start');end=context.get('char_end')
        passage_ids=[]
        if isinstance(start,int):
            passage_ids=[p['id'] for p in contract.passages
                if p['char_start']<=start and (not isinstance(end,int) or end<=p['char_end'])]
        if passage_ids:
            # Passage text already contains the exact quote. `at` preserves narrative
            # order and distinguishes repeated surfaces inside the same passage.
            result={k:context[k] for k in ('surface','kind') if k in context}
            result.update(at=start,passages=passage_ids)
            return result
        return {k:v for k,v in context.items()
                if k not in {'char_start','char_end','context_start','context_end'}}

    def intern(context):
        normalized=compact(context)
        key=json.dumps(normalized,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        ref=subject_refs.get(key)
        if ref is None:
            ref=f's{len(subjects)+1}'
            subject_refs[key]=ref;subjects[ref]=normalized
        return ref

    occurrences={};choice_subjects={}
    for row in rows:
        for choice,context in row.get('choice_context',{}).items():
            if context is not None:
                ref=intern(context)
                existing=choice_subjects.setdefault(choice,ref)
                if existing!=ref:
                    raise ValueError('identity choice token describes multiple subjects')
        # [current subject ref, allowed choices]. Choice descriptions live once in the
        # request-wide table; self/unresolved deliberately have no comparison subject.
        occurrences[row['occurrence_ref']]=[intern(row['subject']),list(row['choices'])]
    return dict(identity=dict(subjects=subjects,choice_subjects=choice_subjects,
                              occurrences=occurrences),
                passages=[dict(id=p['id'],text=p['text']) for p in contract.passages])


def verification_schema(item_refs):
    """Inline exact verdict slots to keep llama.cpp's grammar small and deterministic."""
    verdict = dict(type='object', additionalProperties=False,
        required=['supported', 'reason'], properties={
            'supported': dict(type='boolean'),
            'reason': dict(type='string', maxLength=200)})
    properties = {ref: dict(verdict) for ref in item_refs}
    return dict(type='object', additionalProperties=False,
                required=list(properties), properties=properties)


def materialize_verification(body, item_refs):
    expected = set(item_refs)
    if not isinstance(body, dict) or set(body) != expected:
        raise ValueError('verification response must contain one unique verdict per item')
    verdicts = []
    for ref in item_refs:
        slot = body[ref]
        if not isinstance(slot, dict) or set(slot) != {'supported', 'reason'}:
            raise ValueError('invalid verification slot fields')
        verdicts.append(Verdict(id=ref, supported=slot['supported'], reason=slot['reason']))
    return Verification(verdicts=verdicts)


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: '+key)
        result[key] = value
    return result
