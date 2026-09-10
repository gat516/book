"""Model-facing references to exact source slices, not model-copied quotations.

This adapter changes the wire contract only. Publication still consumes validated
quotes/offsets and the authoritative resolution map; a reference is not proof of a claim.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import json
import re
from typing import Callable, Literal

from pipeline.evidence import CJK_RE, digest, passage


class PassagePackingError(ValueError):
    """A request cannot be represented within the caller's model context budget."""


class FixedOverheadTooLarge(PassagePackingError):
    """Instructions/schema/vocabulary/output headroom consume the whole context."""

    def __init__(self, *, context_tokens: int, output_tokens: int,
                 overhead_tokens: int, components: dict[str, int]):
        self.context_tokens = context_tokens
        self.output_tokens = output_tokens
        self.overhead_tokens = overhead_tokens
        self.components = components
        super().__init__(
            "fixed request overhead exceeds model context: "
            f"overhead={overhead_tokens} tokens, context={context_tokens}, "
            f"output_headroom={output_tokens}"
        )


class PassageTooLarge(PassagePackingError):
    """One passage cannot fit even after all fixed overhead is accounted for."""

    def __init__(self, passage_id: str, *, tokens: int, available_tokens: int):
        self.passage_id = passage_id
        self.tokens = tokens
        self.available_tokens = available_tokens
        super().__init__(
            f"passage {passage_id!r} needs {tokens} tokens but only "
            f"{available_tokens} remain after fixed overhead"
        )


SchemaTransport = Literal["native", "prompt", "duplicated"]


def conservative_token_estimate(text: str) -> int:
    """Conservatively estimate model tokens without downloading a tokenizer.

    UTF-8 bytes are used as a deliberately conservative upper bound. This may under-fill
    a context, but it cannot silently lose source text because a provider tokenizer was
    more expensive than expected. A production caller may pass an exact ``tokenizer``
    to :func:`pack_passages`.
    """
    if not text:
        return 0
    return len(text.encode("utf-8"))


def _serialized(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class PassageBatch:
    """One complete request payload and its measured budget components."""

    passages: tuple[dict, ...]
    tokens: int
    source_tokens: int
    overhead_tokens: int
    output_headroom: int
    request_tokens: int
    request_bytes: int


def pack_passages(
    passages: list[dict] | tuple[dict, ...],
    *,
    instructions: str = "",
    system: str = "",
    input_fields: dict | None = None,
    vocabulary=None,
    schema: dict | None = None,
    context_tokens: int,
    output_tokens: int,
    schema_transport: SchemaTransport = "native",
    max_chars: int | None = None,
    tokenizer: Callable[[str], int] | None = None,
) -> list[PassageBatch]:
    """Pack complete existing passages under a context/output token budget.

    ``system``/``instructions``, vocabulary, schema and source are measured separately.
    ``input_fields`` lets callers account for stable non-passage input fields (for
    example a case id or ontology) in the exact serialized request. Native
    schema transport counts the schema once as wire overhead; prompt transport counts
    it in the prompt; ``duplicated`` counts it in both places. ``max_chars`` remains a
    hard per-passage guard, but this function never truncates a passage. If fixed
    overhead leaves no room, it raises :class:`FixedOverheadTooLarge`; if one complete
    passage is too large it raises :class:`PassageTooLarge`. Splitting must happen at a
    higher layer only after a provider explicitly reports a normalized request-size
    failure.
    """
    if context_tokens <= 0 or output_tokens < 0:
        raise ValueError("context_tokens must be positive and output_tokens non-negative")
    if schema_transport not in {"native", "prompt", "duplicated"}:
        raise ValueError(f"unknown schema transport {schema_transport!r}")
    measure = tokenizer or conservative_token_estimate
    rows = tuple(passages)
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("text"), str):
            raise ValueError("passages must contain dict rows with string id and text")
        if max_chars is not None and len(row["text"]) > max_chars:
            raise PassagePackingError(
                f"passage {row['id']!r} exceeds retained char cap {max_chars}; split the source passage first"
            )

    # Match the stable wire shape used by PassageContract, including JSON delimiters.
    instruction_text = system + instructions
    vocabulary_text = _serialized(vocabulary)
    schema_text = _serialized(schema)
    if schema_transport in {"prompt", "duplicated"} and schema_text:
        instruction_text += "\nOUTPUT JSON SCHEMA:\n" + schema_text
    components = {
        "instructions": measure(instruction_text),
        "vocabulary": measure(vocabulary_text),
        "schema": measure(schema_text),
    }
    wire_schema = components["schema"] if schema_transport in {"native", "duplicated"} else 0
    overhead = components["instructions"] + components["vocabulary"] + wire_schema
    available = context_tokens - output_tokens - overhead
    if available <= 0:
        raise FixedOverheadTooLarge(context_tokens=context_tokens, output_tokens=output_tokens,
                                    overhead_tokens=overhead, components=components)

    batches: list[PassageBatch] = []
    current: list[dict] = []
    current_source_tokens = 0

    def finish(rows_for_batch: list[dict], source_tokens: int) -> PassageBatch:
        payload = dict(input_fields or {})
        payload["passages"] = [dict(id=row["id"], text=row["text"]) for row in rows_for_batch]
        source_text = _serialized(payload)
        prompt_tokens = measure(source_text)
        request_tokens = overhead + prompt_tokens
        return PassageBatch(tuple(rows_for_batch), request_tokens, source_tokens, overhead,
                            output_tokens, request_tokens + output_tokens,
                            len((instruction_text + vocabulary_text + source_text +
                                 (schema_text if wire_schema else "")).encode()))

    for row in rows:
        # Measure the actual serialized payload with this candidate appended. This
        # catches escaping, IDs and delimiters instead of treating source chars as tokens.
        candidate = current + [row]
        candidate_source = finish(candidate, 0).tokens - overhead
        if candidate_source > available:
            if not current:
                raise PassageTooLarge(row["id"], tokens=measure(_serialized({"passages": [dict(id=row["id"], text=row["text"])]})),
                                      available_tokens=available)
            batches.append(finish(current, current_source_tokens))
            current = [row]
            current_source_tokens = finish(current, 0).tokens - overhead
            if current_source_tokens > available:
                raise PassageTooLarge(row["id"], tokens=current_source_tokens, available_tokens=available)
        else:
            current = candidate
            current_source_tokens = candidate_source
    if current:
        batches.append(finish(current, current_source_tokens))
    return batches


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
            # Keep vocabulary terms open on the wire.  An admitted term and a new
            # candidate share the same primitive type; combining a closed enum with a
            # regex in ``anyOf`` creates an overlapping union that strict hosted schema
            # adapters must widen (and can silently lose).  ``knowledge.py`` supplies
            # the admitted names and durability glosses as prompt guidance, while
            # vocabulary.valid_name()/validate_proposals enforce the naming rule locally.
            term=dict(type='string')
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
                # Genuine contract violations only (A.6): 0/3+/duplicate/unoffered
                # refs cannot be salvaged and correctly hard-fail the whole response.
                # Non-adjacency is NOT one of these -- it's a soft per-item drop
                # (below), because with the merged pass one bad citation from the
                # model must not destroy a whole window's names/attributes/relations/
                # occurrences together.
                refs=item.get('passage_ids')
                if not isinstance(refs,list) or not 1<=len(refs)<=2 or len(set(refs))!=len(refs):
                    raise ValueError('extract items must cite one or two distinct passages')
                if any(r not in self.by_id for r in refs): raise ValueError('extract citation is not offered')
                return refs
            out=dict(names=[],attributes=[],relations=[],occurrences=[],rejected=[])
            for n in names:
                ev=self.resolve(n['passage_id'])
                if not ev or n['surface'] not in ev['quote']: raise ValueError('name surface absent from cited passage')
                out['names'].append(dict(surface=n['surface'],kind=n['kind'],**ev))
            for item in body['attributes']:
                refs=citations(item)
                if len(refs)==2 and not self.claim_refs_are_adjacent(refs):
                    out['rejected'].append(dict(item,rejection='extract citations must be adjacent'))
                    continue
                subject=ref(item,'subject_ref')
                out['attributes'].append(dict(item,subject_ref=subject))
            for item in body['relations']:
                refs=citations(item)
                if len(refs)==2 and not self.claim_refs_are_adjacent(refs):
                    out['rejected'].append(dict(item,rejection='extract citations must be adjacent'))
                    continue
                src=ref(item,'src_ref'); dst=ref(item,'dst_ref')
                out['relations'].append(dict(item,src_ref=src,dst_ref=dst))
            for item in body['occurrences']:
                refs=citations(item)
                if len(refs)==2 and not self.claim_refs_are_adjacent(refs):
                    out['rejected'].append(dict(item,rejection='extract citations must be adjacent'))
                    continue
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
