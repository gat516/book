"""Request-local graph choices. Grammar and application enforce the same allowlists."""
from pipeline.evidence import IdentityDecisions, Names, Verification, Verdict


NAME_SLOT_COUNT = 8


def name_schema(passage_ids, kinds):
    """Fixed nullable slots bound generation without verbose unused placeholders."""
    slots = {}
    for index in range(1, NAME_SLOT_COUNT + 1):
        item=dict(type='object', additionalProperties=False,
            required=['surface', 'kind', 'passage_id'], properties={
                'surface': dict(type='string', minLength=1, maxLength=80),
                'kind': dict(type='string', enum=kinds),
                'passage_id': dict(type='string', enum=passage_ids)})
        slots[f'n{index}'] = dict(anyOf=[item,dict(type='null')])
    return dict(type='object', additionalProperties=False,
                required=list(slots), properties=slots)


def materialize_names(body, contract, ontology):
    expected={f'n{index}' for index in range(1, NAME_SLOT_COUNT + 1)}
    if not isinstance(body, dict) or set(body) != expected:
        raise ValueError('name response must fill every request-local slot')
    names=[]
    rejected=[]
    kinds=set(ontology['kinds'])
    for ref in sorted(expected):
        item=body[ref]
        if item is None:
            continue
        if not isinstance(item, dict) or set(item) != {'surface','kind','passage_id'}:
            raise ValueError('invalid name slot')
        surface=item['surface']
        if (not isinstance(surface,str) or not surface.strip() or len(surface)>80
                or item['kind'] not in kinds):
            raise ValueError('invalid named surface or kind')
        evidence=contract.resolve(item['passage_id'])
        if not evidence or surface not in evidence['quote']:
            rejected.append(dict(**item,rejection='unknown passage or surface absent from cited passage'))
            continue
        names.append(dict(surface=surface,kind=item['kind'],named=True,**evidence))
    return Names(names=names,reviewed_kinds=list(ontology['kinds']),rejected=rejected)


def identity_schema(rows, passage_ids):
    slots = {}
    for row in rows:
        slots[row['occurrence_ref']] = dict(type='object', additionalProperties=False,
            required=['choice', 'passage_id', 'explanation'], properties={
                'choice': dict(type='string', enum=list(row['choices'])),
                'passage_id': dict(type='string', enum=passage_ids),
                'explanation': dict(type='string', maxLength=200)})
    return dict(type='object', additionalProperties=False, required=list(slots), properties=slots)


def materialize_identity(body, rows, contract):
    if not isinstance(body, dict) or set(body) != {r['occurrence_ref'] for r in rows}:
        raise ValueError('identity response must contain one unique decision per occurrence')
    decisions = []
    for row in rows:
        slot = body[row['occurrence_ref']]
        if not isinstance(slot, dict) or set(slot) != {'choice', 'passage_id', 'explanation'}:
            raise ValueError('invalid identity slot fields')
        choice = slot['choice']
        if choice not in row['choices']:
            raise ValueError('unoffered identity choice')
        evidence = contract.resolve(slot['passage_id'])
        if evidence is None:
            raise ValueError('unoffered identity passage')
        outcome, target, reason = row['choices'][choice]
        decisions.append(dict(occurrence_ref=row['occurrence_ref'], outcome=outcome,
            target_ref=target, reason_code=reason, explanation=slot['explanation'], **evidence))
    return IdentityDecisions(decisions=decisions)


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
