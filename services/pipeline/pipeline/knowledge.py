"""Revision-scoped enrichment of saved prose (§0, §5.4).

All model outputs remain proposals until literal and independent semantic checks pass.
Only publish() consumes PipelineState.resolutions; it never performs name matching.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import sys
from copy import deepcopy
from urllib.parse import urlparse

from pgvector import Vector
from pgvector.psycopg import register_vector_async
from psycopg.types.json import Jsonb

from pipeline.evidence import (
    PROMPT_VERSION, Names, Proposals, IdentityDecisions, ClaimProposals, HostedClaimProposals,
    Decision, Claim, Alignments, Verification, Verdict, EvidenceReading,
    FactComponentVerification, HostedFactReviews, FactRenderings, digest, stable_id,
    source_mentions, validate_proposals, approved, aligned_mentions, passage,
)
from pipeline.failures import failure_category
from pipeline.llm.ollama import OllamaProvider
from pipeline.llm.provider import AdmissionRejected, Class
from pipeline.config import graph_runtime
from pipeline.passages import PassageContract, source_passages
from pipeline.knowledge_contract import (
    compact_identity_payload, identity_schema, materialize_identity, materialize_names, materialize_verification,
    name_schema, NAME_SLOT_COUNT,
    unique_json_object, verification_schema,
)


PROMPT_HARD_BYTES = 42 * 1024
CANDIDATE_LIMIT = 8
IDENTITY_BATCH_SIZE = 12
IDENTITY_PROMPT_SOFT_BYTES = 16 * 1024
IDENTITY_PROMPT_HARD_BYTES = 24 * 1024
CLAIM_LIMIT = 12
CLAIM_PASSAGE_CHARS = 1200
MIN_CLAIM_PASSAGE_CHARS = 100
# Mirrors Alignments.alignments' Field(max_length=...) in evidence.py.
ALIGNMENT_LIMIT = 24
MIN_ALIGNMENT_CHARS = 800
# graph_ollama_total_timeout_seconds defaults to None (no cap) because the streaming
# provider's own idle/first-token budgets already catch a stuck call. embed() has no
# such per-chunk detector -- it is one flat request -- so it always needs a real ceiling
# even when the operator has left the streaming cap disabled.
EMBED_FALLBACK_TIMEOUT_SECONDS = 300
# A genuine stall (no bytes for the idle/first-token budget) gets retried in place this
# many extra times before it is allowed to fail the whole chapter attempt. graph_completion
# caches every OTHER call in the chapter, so this is cheap: only the stuck request repeats.
STALL_RETRY_ATTEMPTS = 2
STALL_RETRY_DELAY_SECONDS = 5

IDENTITY_SLOT_INSTRUCTIONS = (
    'Resolve every occurrence by selecting exactly one offered choice. The identity table '
    'contains deduplicated subjects, choice_subjects, and occurrences. Each occurrence is '
    '[current_subject_ref, allowed_choices]; choice_subjects maps comparison choices to '
    'subject refs. new:self '
    'means a clearly named first appearance. Choose new:o*, prior:o*, or existing:t* only '
    'when narrative context establishes the same subject; same spelling alone cannot. '
    'unresolved means uncertain. Prior representatives are verified. Never merge homonyms. '
    'Return one choice string for every occurrence key as a JSON object.'
)


def _is_prompt_budget_error(exc: ValueError) -> bool:
    """True only for the caller-contract failure bounded stages may subdivide."""
    return 'graph context exceeds hard model budget' in str(exc)


def _outside(ranges: list, focus: dict | list[dict]) -> list[tuple[int,int]]:
    """Antecedent context minus the focus span.

    A synthetic context range that contains the focus text lets the model cite its
    assertion without citing a focus passage, which is exactly what the focus check
    rejects. Clipping keeps the antecedent — the only reason these ranges exist — while
    leaving the focus citable solely through its own ID.
    """
    focuses=focus if isinstance(focus,list) else [focus]
    start=min(row['char_start'] for row in focuses)
    end=max(row['char_end'] for row in focuses)
    result=[]
    for lo,hi in ranges:
        if hi<=start or lo>=end:
            result.append((lo,hi));continue
        if lo<start:
            result.append((lo,start))
        if end<hi:
            result.append((end,hi))
    return result


class KnowledgeEngine:
    def __init__(self, db, cfg, revision: dict, *, run_id: str | None = None,
                 provider=None):
        self.db, self.cfg, self.revision = db, cfg, revision
        self.run_id, self.current_chapter = run_id, None
        if revision.get('prompt_version',PROMPT_VERSION)!=PROMPT_VERSION:
            raise ValueError('extraction prompt changed; create a new revision')
        self.model = revision['model']['name']
        self.provider_id = revision['model']['provider']
        self.hosted = revision['model'].get('strategy') == 'api_two_pass'
        self.runtime = (dict(identity={},limits=dict(
            idle_timeout_seconds=cfg.graph_ollama_timeout_seconds or 120,
            total_timeout_seconds=cfg.graph_ollama_total_timeout_seconds,
            first_token_timeout_seconds=cfg.graph_ollama_first_token_seconds or 120))
            if self.hosted else graph_runtime(cfg))
        # A revision pins output-affecting options. Deadline tuning remains live config:
        # it changes whether a call gets to finish, not what a finished call contains.
        identity = revision['model'].get('identity', self.runtime['identity'])
        limits = self.runtime['limits']
        if self.provider_id == 'ollama':
            if urlparse(cfg.ollama_host).hostname not in {'localhost','127.0.0.1','::1'}:
                raise ValueError('graph repair requires a loopback Ollama endpoint')
            self.provider = provider or OllamaProvider(host=cfg.ollama_host, model=self.model,
                timeout=limits['idle_timeout_seconds'], total_timeout=limits['total_timeout_seconds'],
                first_token_timeout=limits['first_token_timeout_seconds'],
                num_ctx=identity.get('num_ctx'), num_predict=identity['num_predict'], stream=True,
                think=identity.get('think'))
        elif provider is None:
            raise ValueError('hosted graph repair requires its pinned provider')
        else:
            self.provider = provider
        self.claim_limit = 48 if self.hosted else CLAIM_LIMIT
        self.prompt_hard_bytes = 256 * 1024 if self.hosted else PROMPT_HARD_BYTES
        self.embedder = OllamaProvider(host=cfg.ollama_host,model=cfg.embed_model,
            timeout=limits['idle_timeout_seconds'],
            total_timeout=limits['total_timeout_seconds'] or EMBED_FALLBACK_TIMEOUT_SECONDS)

    async def _ensure_run(self, novel: str, chapter: int, source: str, display: str) -> None:
        self._novel_id = novel
        self.current_chapter = chapter
        if self.run_id:
            await self.db.execute("UPDATE chapter_knowledge_run SET state='processing',updated_at=now() WHERE id=%s",(self.run_id,))
            return
        row=await(await self.db.execute('''INSERT INTO chapter_knowledge_run
            (novel_id,chapter_index,revision_id,mode,state,input_hash,display_hash,model_identity,
             graph_generation,graph_version)
            SELECT %s,%s,%s,'ordinary','processing',%s,%s,%s,%s,%s
              FROM chapter c WHERE c.novel_id=%s AND c.chapter_index=%s
            ON CONFLICT(revision_id,chapter_index,mode) WHERE mode='ordinary'
            DO UPDATE SET state=CASE WHEN chapter_knowledge_run.state='published' THEN 'published' ELSE 'processing' END,
                          updated_at=now()
            RETURNING id::text''',(novel,chapter,self.revision['id'],digest(source),digest(display),
                digest(self.revision['model']),self.revision.get('generation',1),self.revision.get('version',1),novel,chapter))).fetchone()
        self.run_id=row[0] if row else None
        await self._activity('run','run','detected',dict(chapter=chapter))

    async def _activity(self, kind: str, key: str, phase: str, payload: dict) -> None:
        if not getattr(self,'run_id',None) or getattr(self,'current_chapter',None) is None:
            return
        identity=digest([kind,key,phase,payload])
        await self.db.execute('''INSERT INTO chapter_knowledge_activity
            (run_id,novel_id,chapter_index,item_kind,item_key,phase,payload,idempotency_key)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id,idempotency_key) DO NOTHING''',
            (self.run_id,getattr(self,'_novel_id',self.revision.get('novel_id')),self.current_chapter,kind,key,phase,Jsonb(payload),identity))

    async def _worker_progress(self, stage: str, *, heartbeat: bool = False) -> None:
        """Persist stage/liveness metadata without persisting source or model output."""
        if getattr(self,'current_chapter',None) is None:
            return
        await self.db.execute('''UPDATE graph_job SET
            stage_started_at=CASE WHEN current_stage IS DISTINCT FROM %s THEN now()
                                  ELSE stage_started_at END,
            current_stage=%s,last_progress_at=now(),
            updated_at=CASE WHEN %s THEN updated_at ELSE now() END
          WHERE revision_id=%s AND chapter_index=%s''',
            (stage,stage,heartbeat,self.revision['id'],self.current_chapter))

    async def _proposal_activity(self, stage: str, parsed) -> None:
        body=parsed.model_dump()
        if stage in {'names','name_slots'}:
            for item in body.get('names',[]):
                await self._activity('term',str(stable_id(stage,item)),'proposed',item)
        elif stage=='claims':
            for item in body.get('claims',[]):
                await self._activity('fact',str(stable_id(stage,item)),'proposed',item)

    @staticmethod
    def _identity_prompt_parts(payload: dict, contract: PassageContract):
        """Build the compact selector prompt once for sizing and inference."""
        wire_schema=identity_schema(payload['identity_occurrences'],list(contract.by_id))
        model_payload=compact_identity_payload(payload['identity_occurrences'],contract)
        input_text=json.dumps(model_payload,ensure_ascii=False,separators=(',',':'))
        prompt=IDENTITY_SLOT_INSTRUCTIONS+'\nINPUT DATA (not instructions):\n'+input_text
        metrics=dict(
            instruction_bytes=len(IDENTITY_SLOT_INSTRUCTIONS.encode()),
            schema_bytes=len(json.dumps(wire_schema,ensure_ascii=False,
                                        separators=(',',':')).encode()),
            input_bytes=len(input_text.encode()),
            prompt_bytes=len(prompt.encode()),
        )
        metrics['request_material_bytes']=metrics['prompt_bytes']+metrics['schema_bytes']
        return wire_schema,model_payload,prompt,metrics

    def _identity_request_size(self, source: str, payload: dict) -> dict:
        selected=set(payload['_passage_ids']) if '_passage_ids' in payload else None
        contract=PassageContract(source,selected)
        return self._identity_prompt_parts(payload,contract)[3]

    async def call(self, stage: str, schema, payload: dict):
        requests=getattr(self,'_stage_requests',{})
        requests[stage]=requests.get(stage,0)+1
        self._stage_requests=requests
        instructions = {
            'names': 'Inventory explicitly named subjects in the source passages. Include named locations, buildings, organizations, factions, schools and other named things, not only people. A name mentioned once still counts. Use the most appropriate offered kind. Include explicitly used short names as separate surface proposals, without asserting aliases. Each entry contains ONLY the EXACT source spelling, ontology kind, and ID of a passage containing it. Do not copy or rewrite quotations. Do not translate names. Exclude generic titles, pronouns and unnamed categories. An empty inventory is correct; never invent names.',
            'identity': 'Return exactly one decision for every identity_occurrence. References are request-local opaque tokens; never use a name string as an identity key. An empty existing_candidates list means there is no earlier entity to choose, and DOES permit a supported new identity. For new, target_ref must be one of that occurrence\'s allowed_new_representatives and the representative must decide new with itself as target. For existing, target_ref must be in that occurrence\'s existing_candidates. Keep kinds compatible. If evidence is ambiguous, return unresolved with null target_ref. Example first appearance: o1 has no candidates and names a person, so o1 -> new o1. Example existing identity: the passage explicitly links o2 to offered e1, so o2 -> existing e1. Example ambiguity or homonym: same spelling without coreference evidence -> unresolved. Cite one offered passage and use a reason_code plus at most 200 explanation characters.',
            'claims': 'Extract only atomic, explicitly supported source-language claims from the focus passages. Neighboring passages provide antecedent context, including pronoun-only continuations. Use only verified_occurrences request-local refs; never use name strings as identity keys. A claim may cite one to three offered passage_ids when both an antecedent and assertion are needed. The assertion itself must occur in a focus passage. Preserve attribution, uncertainty, quantities, location, and scope inside value; do not turn an observation about some members into a universal property. Return at most 12 claims, with values at most 400 characters. Candidate facts are context, not evidence. Do not translate values and do not require a value to be a literal substring; semantic verification will independently check support.',
            'evidence': 'Read only the offered source passages, without guessing what a proposed fact might be. Inventory their explicit atomic statements. Copy the subject and assertion as exact source substrings. Copy every attribution, uncertainty, quantity, location, subset, or temporal qualifier as a separate exact source substring. Resolve pronouns only when the offered text establishes the antecedent, but still copy the named subject phrase. Do not translate, paraphrase, add background knowledge, or infer uses. Return an empty list when the passages establish no durable statement.',
            'fact_verify': 'Check the single proposed claim against the original offered source and the claim-independent evidence reading. The original source is authoritative; the reading is only an index. Judge four components separately: the cited subject owns or performs the assertion; the complete assertion is stated; all attribution, uncertainty, quantities, subsets, locations, and time limits are preserved; and the cited evidence is sufficient. Any uncertainty is false. A real quote is not proof. Reject these known error patterns: attaching beasts\' traits to a pond, claiming a plant emits mist when beasts emit it toward the plant, inventing uses not stated in the passage, and turning a report about a few inner-area specimens into a universal property.',
            'fact_review': 'Independently review every proposed claim against only the offered source passages. Judge subject ownership, the complete assertion, every qualifier, and evidence sufficiency separately. Unsupported or uncertain means false. For each supported claim, render its value concisely in target_language without adding, strengthening, weakening, or omitting meaning. Fill every item_ref exactly once. This is the second and final model pass for hosted fact extraction; be conservative.',
            'render': 'Translate each already verified source-language fact value into concise English. Preserve attribution, uncertainty, quantities, subsets, locations, time limits, and who owns or performs the assertion. Do not add explanation or background knowledge. Fill every offered item_ref exactly once; use an empty string if a faithful concise rendering is not possible.',
            'render_verify': 'Check whether each English rendering preserves the complete meaning of its verified source claim: subject ownership, assertion, attribution, uncertainty, quantities, subsets, locations, and time limits. The English may be concise but may add nothing. Unsupported, stronger, weaker, or uncertain means false.',
            'propose': 'Extract only the requested bounded proposals and cite offered passages.',
            'align': 'Align the saved translation to the offered SOURCE OCCURRENCES. Return exact displayed named phrases, their zero-based occurrence number in the translation, and the matching source mention ID plus supporting passage ID. Different translated spellings can name the same source subject; identical spellings may name different subjects. Include unaligned displayed names with null mention_id and null passage_id. Never infer identity from capitalization alone. Do not rewrite the translation.',
            'name_verify': 'Check whether EACH offered surface is a proper name or explicitly named story-specific person, place, group, creature, object, technique, realm, or other ontology concept of the offered kind. Reject generic objects, actions, quantities, colors, body parts, directions, titles, pronouns, and descriptive fragments. Fill every required item_ref exactly once. Unsupported or uncertain means false. Keep each reason at most 200 characters.',
            'verify': 'Independently check EACH offered item and fill every required item_ref JSON slot exactly once. supported=true only when the evidence explicitly establishes the identity, assertion, or source-to-translation alignment. Same spelling, vector proximity, plausibility and prior mistaken labels are not identity evidence. Check occurrence identity and kind. Unsupported or uncertain means false. A real quotation does not prove a false assertion attached to it. Keep each reason at most 200 characters.',
        }
        selected=set(payload['_passage_ids']) if '_passage_ids' in payload else None
        contract=PassageContract(payload['source'],selected,
            max_chars=payload.get('_passage_max_chars',400),
            overlap=payload.get('_passage_overlap',80))
        # Two IDs naming the same slice is an ambiguity the model cannot resolve: it reads
        # an assertion out of the focus paragraph and cites whichever of the duplicates it
        # saw first. Offer each source range exactly once.
        offered_ranges={(p['char_start'],p['char_end']) for p in contract.passages}
        for lo,hi in payload.get('_context_ranges',[]):
            if not 0 <= lo < hi <= len(payload['source']):
                raise ValueError('invalid source context range')
            if (lo,hi) in offered_ranges:
                continue
            p=dict(id=f'c{lo}_{hi}',source_hash=digest(payload['source']),
                   char_start=lo,char_end=hi,text=payload['source'][lo:hi])
            contract.passages.append(p);contract.by_id[p['id']]=p
            offered_ranges.add((lo,hi))
        ontology=payload.get('ontology',self.revision.get('ontology',{}))
        wire_schema=contract.schema(stage,schema,ontology)
        def item_schema(property_name: str) -> dict:
            item=wire_schema['properties'][property_name]['items']
            if '$ref' in item:
                return wire_schema['$defs'][item['$ref'].rsplit('/',1)[-1]]
            return item
        guidance=' Cite offered passage references instead of generating quote text or offsets. A selected passage is evidence to verify, never proof by itself. Null references leave identities unsupported.' if stage in {'propose','identity','claims','align'} else ''
        if stage == 'name_slots':
            instructions[stage] = (f'Inventory distinct names in every offered source passage. Fill all {NAME_SLOT_COUNT} request-local slots. '
                'For every actual proper name or explicitly named story-specific concept use one slot containing its exact source spelling, most appropriate kind, and passage ID. '
                'Set every unused slot to null. Do not translate, infer aliases, or list generic titles or pronouns. If all slots '
                'are used, report the clearest explicit names; the application will recursively divide the source window for coverage. '
                'Generic objects, actions, quantities, colors, body parts, directions, and descriptive fragments are not names.')
            wire_schema=name_schema(list(contract.by_id),ontology['kinds'])
        elif stage == 'identity_slots':
            instructions[stage] = IDENTITY_SLOT_INSTRUCTIONS
            wire_schema=identity_schema(payload['identity_occurrences'],list(contract.by_id))
        elif stage == 'identity':
            decisions=wire_schema['properties']['decisions']
            count=len(payload['identity_occurrences'])
            decisions.update(minItems=count,maxItems=count)
            base=item_schema('decisions')
            variants=[]
            for outcome,target_schema,reasons in (
                ('existing',dict(type='string'),['existing_evidence']),
                ('new',dict(type='string'),['new_first_appearance','new_coreference']),
                ('unresolved',dict(type='null'),['ambiguous','insufficient_evidence'])):
                variant=deepcopy(base)
                variant['properties']['occurrence_ref']['enum']=[o['occurrence_ref'] for o in payload['identity_occurrences']]
                variant['properties']['outcome']=dict(type='string',const=outcome)
                variant['properties']['target_ref']=target_schema
                variant['properties']['reason_code']=dict(type='string',enum=reasons)
                variants.append(variant)
            decisions['items']=dict(oneOf=variants)
        elif stage == 'claims':
            if self.hosted:
                instructions[stage]=instructions[stage].replace(
                    'Return at most 12 claims','Return at most 48 claims')
            claims=wire_schema['properties']['claims']
            claims['maxItems']=self.claim_limit
            item_schema('claims')['properties']['occurrence_refs']['items']['enum']=[o['occurrence_ref'] for o in payload['verified_occurrences']]
        elif stage in {'verify','name_verify'}:
            wire_schema=verification_schema([i['item_ref'] for i in payload['items']])
        elif stage == 'fact_verify':
            refs=[i['item_ref'] for i in payload['items']]
            fields={
                'subject_supported':dict(type='boolean'),
                'assertion_supported':dict(type='boolean'),
                'qualifiers_supported':dict(type='boolean'),
                'evidence_sufficient':dict(type='boolean'),
                'reason':dict(type='string',maxLength=200)}
            verdict_item=dict(type='object',additionalProperties=False,
                required=['id',*fields],properties=dict(id=dict(type='string',enum=refs),**fields))
            wire_schema=dict(type='object',additionalProperties=False,required=['verdicts'],properties={
                'verdicts':dict(type='array',minItems=len(refs),maxItems=len(refs),items=verdict_item)})
        elif stage == 'fact_review':
            refs=[i['item_ref'] for i in payload['items']]
            fields={
                'subject_supported':dict(type='boolean'),
                'assertion_supported':dict(type='boolean'),
                'qualifiers_supported':dict(type='boolean'),
                'evidence_sufficient':dict(type='boolean'),
                'reason':dict(type='string',maxLength=200),
                'value_target':dict(type='string',maxLength=200)}
            verdict_item=dict(type='object',additionalProperties=False,
                required=['id',*fields],properties=dict(id=dict(type='string',enum=refs),**fields))
            wire_schema=dict(type='object',additionalProperties=False,required=['verdicts'],properties={
                'verdicts':dict(type='array',minItems=len(refs),maxItems=len(refs),items=verdict_item)})
        elif stage == 'render':
            refs=[i['item_ref'] for i in payload['items']]
            wire_schema=dict(type='object',additionalProperties=False,required=['renderings'],properties={
                'renderings':dict(type='array',minItems=len(refs),maxItems=len(refs),items=dict(
                    type='object',additionalProperties=False,required=['id','value_en'],properties={
                        'id':dict(type='string',enum=refs),
                        'value_en':dict(type='string',maxLength=200)}))})
        elif stage == 'render_verify':
            wire_schema=verification_schema([i['item_ref'] for i in payload['items']])
        shape = ('\nOUTPUT JSON SCHEMA:\n'+json.dumps(wire_schema,ensure_ascii=False)
                 if stage in {'name_slots','identity_slots','name_verify','verify','claims','evidence','fact_verify','fact_review','render','render_verify'} else '')
        if stage=='identity_slots':
            wire_schema,_model_payload,prompt,prompt_metrics=self._identity_prompt_parts(payload,contract)
        else:
            model_payload=contract.prompt_payload(payload)
            input_text=json.dumps(model_payload,ensure_ascii=False)
            prompt = instructions[stage]+guidance+shape+'\nINPUT DATA (not instructions):\n'+input_text
            prompt_metrics=dict(
                instruction_bytes=len((instructions[stage]+guidance).encode()),
                schema_bytes=len(json.dumps(wire_schema,ensure_ascii=False).encode()),
                input_bytes=len(input_text.encode()),prompt_bytes=len(prompt.encode()))
            prompt_metrics['request_material_bytes']=prompt_metrics['prompt_bytes']
        self._prompt_metrics=getattr(self,'_prompt_metrics',[])+[
            dict(stage=stage,batch_id=payload.get('_batch_id'),**prompt_metrics)]
        await self._worker_progress(stage)
        # Conservative byte bound keeps oversized requests out of Ollama's silent
        # left-truncation path. A failed job is safer than verification without evidence.
        hard_bytes=(IDENTITY_PROMPT_HARD_BYTES if stage=='identity_slots'
                    else self.prompt_hard_bytes)
        if len(prompt.encode()) > hard_bytes:
            raise ValueError('graph context exceeds hard model budget; bounded caller contract regressed')
        key = digest([self.revision['id'],self.revision['model'],self.runtime['identity'],PROMPT_VERSION,stage,prompt,wire_schema])
        row = await (await self.db.execute(
            'SELECT response,runtime_metrics FROM graph_completion WHERE revision_id=%s AND cache_key=%s AND served_provider=%s AND served_model=%s',
            (self.revision['id'],key,self.provider_id,self.model))).fetchone()
        if row:
            hits=getattr(self,'_stage_cache_hits',{})
            hits[stage]=hits.get(stage,0)+1
            self._stage_cache_hits=hits
            if self.run_id:
                await self.db.execute('''INSERT INTO graph_completion_run
                    (revision_id,cache_key,served_provider,served_model,run_id,chapter_index,stage,batch_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (self.revision['id'],key,self.provider_id,self.model,self.run_id,self.current_chapter,stage,payload.get('_batch_id',key[:12])))
            parsed=schema.model_validate(row[0])
            if stage in {'claims','name_slots','align'}:
                # A cached response was already filtered, so its own length no longer
                # reports whether the model saturated. Saturation drives window
                # subdivision, so the pre-filter count is stored beside the response.
                proposed=(row[1] or {}).get('proposed_count')
                if proposed is None:
                    proposed=(len(parsed.claims) if stage=='claims' else
                              len(parsed.names) if stage=='name_slots' else len(parsed.alignments))
                payload['_proposed_count']=proposed
            await self._proposal_activity(stage,parsed)
            await self._worker_progress(stage)
            return parsed
        started=time.monotonic()
        diagnostic = dict(revision=self.revision['id'],model=self.model,stage=stage,
                          batch_id=payload.get('_batch_id',key[:12]),request_id=key[:12],
                          prompt_metrics=prompt_metrics)
        print(json.dumps(dict(diagnostic,event='inference_started')),file=sys.stderr,flush=True)
        last_heartbeat=started
        async def progress(update):
            nonlocal last_heartbeat
            print(json.dumps(dict(diagnostic,**update)),file=sys.stderr,flush=True)
            now=time.monotonic()
            if now-last_heartbeat>=10:
                await self._worker_progress(stage,heartbeat=True)
                last_heartbeat=now
        if hasattr(self.provider,'progress_sink'):
            self.provider.progress_sink = progress
        try:
            for attempt in range(STALL_RETRY_ATTEMPTS + 1):
                try:
                    response = await self.provider.complete(prompt, json_schema=wire_schema,
                                                              cls=Class.BATCH, model=self.model,
                                                              pin_model=not self.hosted)
                    break
                except TimeoutError as exc:
                    # A stall, not a bad chapter: the idle/first-token budget fired
                    # because no bytes arrived, which is exactly what retrying THIS one
                    # request (not the whole chapter) is for. Every other call this
                    # chapter has already made is in graph_completion, so re-entering
                    # extract() from a chapter-level retry would skip straight back to
                    # here anyway -- doing it in place just skips the wait.
                    if attempt >= STALL_RETRY_ATTEMPTS:
                        raise
                    print(json.dumps(dict(diagnostic,event='inference_stalled_retrying',
                                          attempt=attempt+1,elapsed_seconds=time.monotonic()-started,
                                          error=str(exc))),file=sys.stderr,flush=True)
                    await asyncio.sleep(STALL_RETRY_DELAY_SECONDS)
        except Exception as exc:
            print(json.dumps(dict(diagnostic,event='inference_failed',elapsed_seconds=time.monotonic()-started,
                                  error_type=type(exc).__name__,error=str(exc),
                                  stream=getattr(self.provider,'last_stream_diagnostics',{}))),file=sys.stderr,flush=True)
            raise
        finally:
            if hasattr(self.provider,'progress_sink'):
                self.provider.progress_sink = None
        print(json.dumps(dict(diagnostic,event='inference_completed',timings=response.timings,
                              input_tokens=response.input_tokens,output_tokens=response.output_tokens)),file=sys.stderr,flush=True)
        if response.served_provider != self.provider_id or response.served_model != self.model:
            raise RuntimeError('serving identity changed; new benchmark/revision required')
        try:
            body=json.loads(response.text,object_pairs_hook=unique_json_object)
            if stage == 'claims':
                if not isinstance(body,dict) or not isinstance(body.get('claims'),list):
                    raise ValueError('claim response must contain a claims list')
                focus=set(payload['focus_passage_ids'])
                # An assertion sourced from context alone is unsupported for this window,
                # but it is one bad proposal, not a broken contract. Drop it the way every
                # other unusable proposal is dropped (§0) instead of failing the run, and
                # record why so the rejection is visible outside a stderr line.
                kept=[]
                for item in body['claims']:
                    # A non-object claim is a grammar violation, not a bad proposal: keep it
                    # so materialization still rejects the whole malformed response.
                    if not isinstance(item,dict) or focus.intersection(item.get('passage_ids',[])):
                        kept.append(item);continue
                    await self._activity('fact',str(stable_id(stage,item)),'rejected',
                        dict(item,rejection='claim cites no focus passage containing its assertion'))
                payload['_proposed_count']=len(body['claims'])
                body=dict(body,claims=kept)
            elif stage == 'name_slots' and isinstance(body,dict):
                payload['_proposed_count']=sum(item is not None for item in body.values())
            elif stage == 'align' and isinstance(body,dict) and isinstance(body.get('alignments'),list):
                payload['_proposed_count']=len(body['alignments'])
            parsed = (materialize_names(body,contract,ontology)
                      if stage=='name_slots' else
                      materialize_identity(body,payload['identity_occurrences'],contract)
                      if stage=='identity_slots' else
                      materialize_verification(body,[i['item_ref'] for i in payload['items']])
                      if stage in {'name_verify','verify','render_verify'} else contract.materialize(stage,schema,body,ontology))
        except Exception as exc:
            print(json.dumps(dict(diagnostic,event='contract_rejected',
                                  error_type=type(exc).__name__,error=str(exc))),
                  file=sys.stderr,flush=True)
            raise
        await self.db.execute('''INSERT INTO graph_completion(revision_id,cache_key,served_provider,served_model,response,elapsed_seconds,runtime_metrics,
            chapter_index,stage,batch_id,run_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (self.revision['id'],key,response.served_provider,response.served_model,Jsonb(parsed.model_dump()),time.monotonic()-started,
             Jsonb(dict(response.timings,input_tokens=response.input_tokens,output_tokens=response.output_tokens,
                        stage=stage,batch_id=diagnostic['batch_id'],
                        prompt_metrics=prompt_metrics,
                        stall_retries=attempt,
                        **({'proposed_count':payload['_proposed_count']} if '_proposed_count' in payload else {}))),
             self.current_chapter,stage,diagnostic['batch_id'],self.run_id))
        if self.run_id:
            await self.db.execute('''INSERT INTO graph_completion_run
                (revision_id,cache_key,served_provider,served_model,run_id,chapter_index,stage,batch_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (self.revision['id'],key,response.served_provider,response.served_model,self.run_id,
                 self.current_chapter,stage,diagnostic['batch_id']))
        await self._proposal_activity(stage,parsed)
        await self._worker_progress(stage)
        return parsed

    @staticmethod
    def _passage_batches(source: str, *, budget: int = 8192,
                         max_passages: int = 64, max_chars: int = 400) -> list[list[str]]:
        """Amortize name-inventory prefill over a bounded adjacent context.

        Compact nullable output slots keep generation bounded. A full response is a
        saturation signal; discover_names recursively divides only that source window so
        batching cannot silently lower recall. Aggregation remains application-owned
        (§0, §5.4), so a surface never becomes an identity binding.
        """
        batches=[];current=[];size=0;chars=0
        for p in PassageContract(source).passages:
            cost=len(json.dumps(dict(id=p['id'],text=p['text']),ensure_ascii=False).encode())+2
            if current and (size+cost>budget or chars+len(p['text'])>max_chars
                            or len(current)>=max_passages):
                batches.append(current);current=[];size=0;chars=0
            current.append(p['id']);size+=cost;chars+=len(p['text'])
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _claim_focus_batches(source: str, *, max_chars: int = CLAIM_PASSAGE_CHARS
                             ) -> list[list[dict]]:
        """Pack short paragraphs into bounded focus requests without coarsening evidence."""
        groups=[];current=[];chars=0
        for row in source_passages(source,max_chars=max_chars,overlap=0):
            size=len(row['text'])
            if current and chars+size>max_chars:
                groups.append(current);current=[];chars=0
            current.append(row);chars+=size
        if current:
            groups.append(current)
        return groups

    def _claim_batches(self, source: str) -> list[list[dict]]:
        """Make ordinary hosted chapters one extraction request, splitting large ones."""
        return self._claim_focus_batches(
            source, max_chars=24_000 if getattr(self,'hosted',False) else CLAIM_PASSAGE_CHARS)

    @staticmethod
    def _mention_passages(source: str, mentions: list[dict]) -> list[str]:
        result=[]
        for p in PassageContract(source).passages:
            if any(p['char_start']<=m.get('char_start',-1)<p['char_end'] for m in mentions):
                result.append(p['id'])
        return result

    async def _identity_decisions(self, source: str, mentions: list[dict],
                                  candidates: dict[str,list[dict]], ontology: dict) -> tuple[list[Decision],int]:
        """Resolve opaque request refs back to application-owned occurrence/entity IDs."""
        decisions=[];proposed_count=0
        for start in range(0,len(mentions),IDENTITY_BATCH_SIZE):
            batch=mentions[start:start+IDENTITY_BATCH_SIZE]
            occurrence_refs={m['id']:f'o{i+1}' for i,m in enumerate(batch)}
            entity_refs={}
            for m in batch:
                for candidate in candidates[m['id']]:
                    entity_refs.setdefault(candidate['id'],f'e{len(entity_refs)+1}')
            rows=[]
            for m in batch:
                same_kind=[r for r in batch if r['kind']==m['kind']]
                rows.append(dict(
                    occurrence_ref=occurrence_refs[m['id']],surface=m['surface'],kind=m['kind'],
                    context=m['quote'],passage_ids=self._mention_passages(source,[m]),
                    existing_candidates=[dict(ref=entity_refs[c['id']],kind=c['kind'],canonical=c['canonical'])
                                         for c in candidates[m['id']]],
                    allowed_new_representatives=[dict(ref=occurrence_refs[r['id']],kind=r['kind'],surface=r['surface'])
                                                 for r in same_kind]))
            batch_id=f'identity-{start//IDENTITY_BATCH_SIZE+1}'
            response=await self.call('identity',IdentityDecisions,dict(
                source=source,identity_occurrences=rows,ontology=ontology,
                _passage_ids=self._mention_passages(source,batch),_batch_id=batch_id))
            proposed_count += len(response.decisions)
            by_ref={d.occurrence_ref:d for d in response.decisions}
            expected=set(occurrence_refs.values())
            if len(response.decisions)!=len(batch) or set(by_ref)!=expected:
                raise ValueError('identity response must contain one unique decision per occurrence')
            reverse_occurrence={ref:mid for mid,ref in occurrence_refs.items()}
            reverse_entity={ref:eid for eid,ref in entity_refs.items()}
            for m in batch:
                d=by_ref[occurrence_refs[m['id']]]
                if d.outcome=='unresolved':
                    if d.target_ref is not None:
                        raise ValueError('unresolved identity must have a null target')
                    target=None
                elif d.outcome=='existing':
                    target=reverse_entity.get(d.target_ref)
                else:
                    target=reverse_occurrence.get(d.target_ref)
                decisions.append(Decision(mention_id=m['id'],outcome=d.outcome,target_id=target,
                    quote=d.quote,evidence_start=d.evidence_start,reason=d.explanation))
        return decisions,proposed_count

    async def _resolve_incremental(self, source, mentions, candidates, ontology):
        """Carry only verified representatives across bounded requests (§0)."""
        verified=[];rejected=[];count=0
        by_mid={m['id']:m for m in mentions}
        entity_context={c['id']:c for cs in candidates.values() for c in cs}
        start=0;batch_limit=IDENTITY_BATCH_SIZE
        self._identity_batch_sizes=[];self._identity_context_splits=0
        while start<len(mentions):
            # Recent verified occurrences carry both their source context and binding.
            # Selection never creates a binding; the semantic verifier owns that step.
            recent={}
            for d in reversed(verified):
                recent.setdefault((d['outcome'],d['target_id']),d)
                if len(recent)>=8: break
            prior=list(recent.values())

            def prepare(size):
                batch=mentions[start:start+size]
                offered_mentions={m['id']:m for m in batch}
                offered_mentions.update({d['mention_id']:by_mid[d['mention_id']] for d in prior})
                refs={mid:f'o{i+1}' for i,mid in enumerate(offered_mentions)}
                targets={};target_refs={};rows=[]
                def target_ref(outcome,target_id):
                    key=(outcome,target_id)
                    token=target_refs.get(key)
                    if token is None:
                        token=f't{len(target_refs)+1}'
                        target_refs[key]=token;targets[token]=key
                    return token
                for m in batch:
                    choices={'new:self':('new',refs[m['id']],'new_first_appearance'),
                             'unresolved':('unresolved',None,'ambiguous')}
                    descriptions={}
                    for other in batch:
                        if other['id']==m['id'] or other['kind']!=m['kind']: continue
                        key='new:'+refs[other['id']]
                        choices[key]=('new',refs[other['id']],'new_coreference')
                        descriptions[key]=self._subject_context(other)
                    for d in reversed(prior):
                        other=by_mid[d['mention_id']]
                        if other['kind']!=m['kind']: continue
                        key='prior:'+refs[other['id']]
                        # Application maps a previous occurrence to its verified root.
                        token=target_ref(d['outcome'],d['target_id'])
                        choices[key]=(d['outcome'],token,
                            'new_coreference' if d['outcome']=='new' else 'existing_evidence')
                        descriptions[key]=self._subject_context(other)
                        if d['outcome']=='existing' and not any(
                                c['id']==d['target_id'] for c in candidates[m['id']]):
                            candidates[m['id']].append(entity_context[d['target_id']])
                    for c in candidates[m['id']]:
                        if c['kind']!=m['kind']: continue
                        token=target_ref('existing',c['id'])
                        key='existing:'+token
                        choices[key]=('existing',token,'existing_evidence')
                        descriptions[key]={k:v for k,v in entity_context[c['id']].items() if k!='id'}
                    rows.append(dict(occurrence_ref=refs[m['id']],subject=self._subject_context(m),
                        choices=choices,choice_context=descriptions))
                payload=dict(source=source,ontology=ontology,identity_occurrences=rows,
                    _passage_ids=self._mention_passages(source,list(offered_mentions.values())))
                return batch,refs,targets,rows,payload

            size=min(batch_limit,len(mentions)-start)
            while True:
                batch,refs,targets,rows,request=prepare(size)
                measured=self._identity_request_size(source,request)
                if measured['request_material_bytes']<=IDENTITY_PROMPT_SOFT_BYTES or size==1:
                    break
                size-=1
                self._identity_context_splits+=1
            identity_batch=f'identity-{start+1}-{start+len(batch)}'
            request['_batch_id']=identity_batch
            try:
                response=await self.call('identity_slots',IdentityDecisions,request)
            except ValueError as exc:
                if _is_prompt_budget_error(exc) and len(batch)>1:
                    batch_limit=max(1,len(batch)//2)
                    self._identity_context_splits+=1
                    continue
                category=failure_category(exc)
                rejected.extend(dict(id='identity-skip:'+m['id'],mention_id=m['id'],
                    surface=m['surface'],kind=m['kind'],batch_id=identity_batch,
                    failure_category=category,
                    rejection='identity batch skipped after model call failure') for m in batch)
                start+=len(batch)
                continue
            except AdmissionRejected:
                # Background work must still yield to a reader-critical chapter. This is
                # scheduling, not a failed extraction batch, so preserve the resume path.
                raise
            except Exception as exc:  # provider/contract failure: degrade this batch only
                category=failure_category(exc)
                rejected.extend(dict(id='identity-skip:'+m['id'],mention_id=m['id'],
                    surface=m['surface'],kind=m['kind'],batch_id=identity_batch,
                    failure_category=category,
                    rejection='identity batch skipped after model call failure') for m in batch)
                start+=len(batch)
                continue
            self._identity_batch_sizes.append(len(batch))
            count+=len(response.decisions)
            reverse={ref:mid for mid,ref in refs.items()}
            decisions=[]
            for d in response.decisions:
                outcome,target=targets.get(d.target_ref,(d.outcome,reverse.get(d.target_ref)))
                decisions.append(Decision(mention_id=reverse[d.occurrence_ref],outcome=outcome,
                    target_id=target,quote=d.quote,evidence_start=d.evidence_start,reason=d.explanation))
            good,no=validate_proposals(source,mentions,candidates,Proposals(decisions=decisions,claims=[]),ontology)
            rejected.extend(no)
            for i,item in enumerate(good): item['id']=f'identity:{start+i}'
            verdicts=await self._verify_items(source,good,'identity',mentions=mentions,
                candidates=candidates,batch_identity=identity_batch)
            good,no=approved(good,verdicts);rejected.extend(no)
            roots={d['mention_id'] for d in verified+good
                   if d['outcome']=='new' and d['target_id']==d['mention_id']}
            for d in good:
                if d['outcome']=='new' and d['target_id'] not in roots:
                    rejected.append(dict(d,rejection='new representative lacks a verified self-root decision'))
                else: verified.append(d)
            start+=len(batch)
        return verified,rejected,count

    @staticmethod
    def _subject_context(mention):
        return {k:mention[k] for k in (
            'surface','kind','char_start','char_end','context_start','context_end','quote')
            if k in mention}

    async def _verify_items(self, source: str, items: list[dict], contract_name: str, *,
                            display_contexts: list[str] | None = None,
                            mentions: list[dict] | None = None, candidates: dict | None = None,
                            batch_identity: str | None = None) -> Verification:
        """Require a complete, unique verdict set and hide durable IDs from the wire."""
        result=Verification(verdicts=[])
        by_mid={m['id']:m for m in mentions or []}
        entities={c['id']:c for rows in (candidates or {}).values() for c in rows}
        batch_size=16 if contract_name=='name eligibility' else 12
        for start in range(0,len(items),batch_size):
            batch=items[start:start+batch_size]
            refs={item['id']:f'v{i+1}' for i,item in enumerate(batch)}
            wire=[]
            occurrence_refs={}
            target_refs={}
            for item in batch:
                view={k:v for k,v in item.items() if k not in {'id','mention_id','mention_ids','target_id','value_en'}}
                view['item_ref']=refs[item['id']]
                if 'mention_id' in item:
                    view['occurrence_ref']=occurrence_refs.setdefault(item['mention_id'],f'o{len(occurrence_refs)+1}')
                    view['subject']=self._subject_context(by_mid[item['mention_id']])
                if 'mention_ids' in item:
                    view['occurrence_refs']=[occurrence_refs.setdefault(mid,f'o{len(occurrence_refs)+1}') for mid in item['mention_ids']]
                    view['subjects']=[dict(ref=ref,**self._subject_context(by_mid[mid]))
                        for ref,mid in zip(view['occurrence_refs'],item['mention_ids'])]
                if item.get('target_id') is not None:
                    view['target_ref']=target_refs.setdefault(item['target_id'],f't{len(target_refs)+1}')
                    target=(by_mid[item['target_id']] if item.get('outcome')=='new'
                            else entities[item['target_id']])
                    view['target']={k:v for k,v in target.items() if k!='id'}
                    view['self_root']=item['target_id']==item.get('mention_id')
                wire.append(view)
            if display_contexts is not None:
                for view,context in zip(wire,display_contexts[start:start+len(batch)],strict=True):
                    view['display_context']=context
            ranges=[(i['evidence_start'],i['evidence_start']+len(i.get('quote','')))
                    for i in batch if i.get('evidence_start') is not None]
            passage_ids=[p['id'] for p in PassageContract(source).passages
                         if any(p['char_start']<end and start<p['char_end'] for start,end in ranges)]
            stage='name_verify' if contract_name=='name eligibility' else 'verify'
            checked=await self.call(stage,Verification,dict(source=source,items=wire,
                contract=contract_name,display_contexts=[],_passage_ids=passage_ids,
                _batch_id=f'verify-{batch_identity or contract_name}-{start//batch_size+1}'))
            by_ref={v.id:v for v in checked.verdicts}
            if len(checked.verdicts)!=len(batch) or set(by_ref)!=set(refs.values()):
                raise ValueError('verification must contain one unique verdict per offered item')
            for item in batch:
                verdict=by_ref[refs[item['id']]]
                result.verdicts.append(Verdict(id=item['id'],supported=verdict.supported,reason=verdict.reason))
        return result

    @staticmethod
    def _evidence_passage_ids(source: str, items: list[dict]) -> list[str]:
        ranges=[(i['evidence_start'],i['evidence_start']+len(i.get('quote','')))
                for i in items if i.get('evidence_start') is not None]
        return [p['id'] for p in PassageContract(source).passages
                if any(p['char_start']<end and start<p['char_end'] for start,end in ranges)]

    async def _verify_facts(self, source: str, items: list[dict], mentions: list[dict]) -> Verification:
        """Verify complete atomic facts against a claim-independent source reading (§0.1).

        Each claim gets its own verdict call so another proposal cannot prime the judge to
        repeat an invented assertion. The source-only reading is cached by its exact offered
        passages and never sees the proposal it will later help check.
        """
        result=Verification(verdicts=[])
        by_mid={m['id']:m for m in mentions}
        readings={}
        for item in items:
            if not passage(source,item.get('quote',''),start=item.get('evidence_start')):
                result.verdicts.append(Verdict(id=item['id'],supported=False,
                                                reason='literal evidence is absent'))
                continue
            passage_ids=self._evidence_passage_ids(source,[item])
            if not passage_ids:
                result.verdicts.append(Verdict(id=item['id'],supported=False,
                                                reason='evidence has no offered source passage'))
                continue
            reading_key=tuple(passage_ids)
            if reading_key not in readings:
                readings[reading_key]=await self.call('evidence',EvidenceReading,dict(
                    source=source,_passage_ids=passage_ids,
                    _batch_id='evidence-'+digest(reading_key)[:12]))
            contract=PassageContract(source,set(passage_ids))
            offered_text='\n'.join(p['text'] for p in contract.passages)
            grounded=[statement for statement in readings[reading_key].statements
                      if statement.subject in offered_text and statement.assertion in offered_text
                      and all(q in offered_text for q in statement.qualifiers)]
            subjects=([self._subject_context(by_mid[mid]) for mid in item['mention_ids']]
                      if item.get('mention_ids') else [dict(surface=item['subject'])])
            wire=dict(item_ref='v1',type=item['type'],subjects=subjects,
                      attribute=item['attribute'],value=item['value'],
                      evidence_reading=[s.model_dump() for s in grounded])
            checked=await self.call('fact_verify',FactComponentVerification,dict(
                source=source,items=[wire],_passage_ids=passage_ids,
                _batch_id='fact-verify-'+digest(item['id'])[:12]))
            if len(checked.verdicts)!=1 or checked.verdicts[0].id!='v1':
                raise ValueError('fact verification must return exactly the offered item')
            verdict=checked.verdicts[0]
            components={name:getattr(verdict,name) for name in (
                'subject_supported','assertion_supported','qualifiers_supported','evidence_sufficient')}
            failed=[name for name,value in components.items() if not value]
            reason=verdict.reason if not failed else ', '.join(failed)+': '+verdict.reason
            await self._activity('fact',item['id'],'verified',dict(
                supported=not failed,components=components,reason=verdict.reason))
            result.verdicts.append(Verdict(id=item['id'],supported=not failed,reason=reason[:200]))
        return result

    async def _review_hosted_claims(self, source: str, items: list[dict], mentions: list[dict],
                                    target: str) -> tuple[list[dict],list[dict]]:
        """Verify and render a hosted chapter's claims in one independent second pass."""
        if not items:
            return [],[]
        by_mid={m['id']:m for m in mentions}
        accepted=[];rejected=[]
        for start in range(0,len(items),48):
            batch=items[start:start+48]
            refs={item['id']:f'v{i+1}' for i,item in enumerate(batch)}
            wire=[dict(item_ref=refs[item['id']],type=item['type'],
                       subjects=[self._subject_context(by_mid[mid]) for mid in item['mention_ids']],
                       attribute=item['attribute'],value=item['value']) for item in batch]
            checked=await self.call('fact_review',HostedFactReviews,dict(
                source=source,items=wire,target_language=target,
                _passage_ids=self._evidence_passage_ids(source,batch),
                _batch_id=f'fact-review-{start//48+1}'))
            verdicts={v.id:v for v in checked.verdicts}
            if len(checked.verdicts)!=len(batch) or set(verdicts)!=set(refs.values()):
                raise ValueError('hosted fact review must contain exactly one verdict per claim')
            for item in batch:
                verdict=verdicts[refs[item['id']]]
                components={name:getattr(verdict,name) for name in (
                    'subject_supported','assertion_supported','qualifiers_supported','evidence_sufficient')}
                supported=all(components.values()) and bool(verdict.value_target.strip())
                await self._activity('fact',item['id'],'verified',dict(
                    supported=supported,components=components,reason=verdict.reason))
                if supported:
                    accepted.append(dict(item,value_en=verdict.value_target))
                else:
                    failed=[name for name,value in components.items() if not value]
                    reason=(', '.join(failed)+': ' if failed else '')+verdict.reason
                    rejected.append(dict(item,rejection=reason[:200] or 'empty target-language rendering'))
        return accepted,rejected

    async def _render_facts(self, source: str, items: list[dict], mentions: list[dict]) -> list[dict]:
        """Create and independently validate display English only after source approval."""
        if not items:
            return []
        by_mid={m['id']:m for m in mentions}
        result=[]
        for start in range(0,len(items),12):
            batch=items[start:start+12]
            refs={item['id']:f'r{i+1}' for i,item in enumerate(batch)}
            wire=[dict(item_ref=refs[item['id']],
                       subject=self._subject_context(by_mid[item['mention_ids'][0]]),
                       attribute=item['attribute'],value=item['value']) for item in batch]
            passage_ids=self._evidence_passage_ids(source,batch)
            rendered=await self.call('render',FactRenderings,dict(source=source,items=wire,
                _passage_ids=passage_ids,_batch_id=f'render-facts-{start//12+1}'))
            by_ref={row.id:row.value_en for row in rendered.renderings}
            if len(rendered.renderings)!=len(batch) or set(by_ref)!=set(refs.values()):
                raise ValueError('fact rendering must contain one unique rendering per offered item')
            checks=[dict(item_ref=refs[item['id']],source_value=item['value'],
                         value_en=by_ref[refs[item['id']]]) for item in batch]
            verified=await self.call('render_verify',Verification,dict(source=source,items=checks,
                _passage_ids=passage_ids,_batch_id=f'verify-render-facts-{start//12+1}'))
            verdicts={v.id:v for v in verified.verdicts}
            if len(verified.verdicts)!=len(batch) or set(verdicts)!=set(refs.values()):
                raise ValueError('render verification must contain one unique verdict per offered item')
            for item in batch:
                ref=refs[item['id']]
                supported=verdicts[ref].supported and bool(by_ref[ref].strip())
                if not supported:
                    self._render_rejections=getattr(self,'_render_rejections',0)+1
                    await self._activity('fact',item['id'],'rejected',dict(
                        value_en=by_ref[ref],reason=verdicts[ref].reason))
                result.append(dict(item,value_en=by_ref[ref] if supported else ''))
        return result

    async def _align_window(self, novel: str, chapter: int, source: str, display: str,
                            mentions: list[dict], lo: int, hi: int, display_identity: str
                            ) -> list[dict]:
        """Align one translated window, splitting it if the model saturates ALIGNMENT_LIMIT.

        A name that recurs often in one window (a recurring protagonist, say) forces the
        model to emit one alignment object per occurrence with nothing capping the count
        -- unbounded, that reliably exhausted num_predict and truncated the whole response
        into unparseable partial JSON, failing the chapter over a call that was never
        stuck, just asked to do too much at once (§0: never publish a partial extraction).
        Same fix as claim saturation in _claims_for_focus: detect the model returned
        exactly the capped amount, halve the window, and recurse for the remainder.
        """
        window=display[lo:hi]
        if not window.strip() or not mentions:
            return []
        request=dict(source=source,translation=window,mentions=mentions,
            _passage_ids=self._mention_passages(source,mentions),_batch_id=f'align-{lo}-{hi}')
        alignment=await self.call('align',Alignments,request)
        proposed=request.get('_proposed_count',len(alignment.alignments))
        if proposed>=ALIGNMENT_LIMIT and len(mentions)>1 and (hi-lo)>MIN_ALIGNMENT_CHARS:
            self._alignment_subdivisions=getattr(self,'_alignment_subdivisions',0)+1
            mid_display=lo+(hi-lo)//2
            mid_mentions=len(mentions)//2
            left=await self._align_window(novel,chapter,source,display,
                mentions[:mid_mentions],lo,mid_display,display_identity)
            right=await self._align_window(novel,chapter,source,display,
                mentions[mid_mentions:],mid_display,hi,display_identity)
            return left+right
        if proposed>=ALIGNMENT_LIMIT:
            # Smallest window reached and it's still saturated: keep what verified rather
            # than failing the chapter, the same fallback claim saturation uses.
            await self._activity('run','run','rejected',dict(
                reason='alignment coverage failure: saturated window could not be subdivided further',
                window_chars=hi-lo,mentions=len(mentions)))
        return aligned_mentions(novel,chapter,source,window,mentions,alignment,
            display_offset=lo,display_identity=display_identity)

    async def _claims_for_focus(self, source: str, verified_mentions: list[dict], ontology: dict,
                                focus: dict | list[dict], *, passage_chars: int = CLAIM_PASSAGE_CHARS,
                                context_ranges: list | None = None
                                ) -> tuple[list[Claim],list[dict],int]:
        passages=source_passages(source,max_chars=passage_chars,overlap=0)
        focuses=focus if isinstance(focus,list) else [focus]
        focus_ids={row['id'] for row in focuses}
        try:
            indices=[i for i,p in enumerate(passages) if p['id'] in focus_ids]
            if len(indices)!=len(focuses):
                raise StopIteration
        except StopIteration:
            return [],[dict(rejection='claim coverage window could not be reconstructed',
                            batch_id=','.join(sorted(focus_ids)))],0
        first,last=min(indices),max(indices)
        context=passages[max(0,first-1):min(len(passages),last+2)]
        # Only a subdivided call needs synthetic ranges: it re-slices the source at a
        # smaller size, so the parent's antecedent context no longer matches any offered
        # passage. At the top level those ranges ARE the offered passages, and sending
        # them would hand the model a second ID for the focus text it must cite by its
        # focus ID. Source offsets remain application-owned (§0).
        forwarded=[] if context_ranges is None else _outside(context_ranges,focus)
        if context_ranges is None:
            context_ranges=[(p['char_start'],p['char_end']) for p in context]
        selected_ids=[p['id'] for p in context]
        in_context=[m for m in verified_mentions if any(
            lo<=m['char_start'] and m['char_end']<=hi for lo,hi in context_ranges)]
        if not in_context:
            return [],[],0
        occurrence_refs={m['id']:f'o{i+1}' for i,m in enumerate(in_context)}
        wire_mentions=[dict(occurrence_ref=occurrence_refs[m['id']],surface=m['surface'],
                            kind=m['kind'],context=m['quote']) for m in in_context]
        batch_id=(f'claims-{focuses[0]["char_start"]}-'
                  f'{focuses[-1]["char_end"]}-{passage_chars}')
        request=dict(source=source,verified_occurrences=wire_mentions,ontology=ontology,
            focus_passage_ids=[row['id'] for row in focuses],_passage_ids=selected_ids,
            _passage_max_chars=passage_chars,_passage_overlap=0,_batch_id=batch_id,
            _context_ranges=forwarded)
        try:
            response=await self.call(
                'claims', HostedClaimProposals if getattr(self,'hosted',False) else ClaimProposals, request)
        except ValueError as exc:
            if not _is_prompt_budget_error(exc) or passage_chars<=MIN_CLAIM_PASSAGE_CHARS:
                raise
            self._claim_context_splits=getattr(self,'_claim_context_splits',0)+1
            if len(focuses)>1:
                pivot=max(1,len(focuses)//2)
                child_groups=[focuses[:pivot],focuses[pivot:]]
                smaller=passage_chars
            else:
                smaller=max(MIN_CLAIM_PASSAGE_CHARS,passage_chars//2)
                child_groups=[[p] for p in source_passages(source,max_chars=smaller,overlap=0)
                              if focuses[0]['char_start']<=p['char_start']
                              and p['char_end']<=focuses[0]['char_end']]
            if not child_groups:
                raise
            claims=[];rejected=[];proposed=0
            for child in child_groups:
                got,no,count=await self._claims_for_focus(source,verified_mentions,ontology,child,
                                                          passage_chars=smaller,context_ranges=context_ranges)
                claims.extend(got);rejected.extend(no);proposed+=count
            return claims,rejected,proposed
        # Saturation is a property of what the model emitted, not of what survived the
        # focus filter, so prefer the pre-filter count `call` reports back.
        proposed=request.get('_proposed_count',len(response.claims))
        claim_limit=getattr(self,'claim_limit',CLAIM_LIMIT)
        if proposed==claim_limit:
            self._claim_subdivisions=getattr(self,'_claim_subdivisions',0)+1
            if len(focuses)==1 and passage_chars<=MIN_CLAIM_PASSAGE_CHARS:
                return [],[dict(rejection='claim coverage failure: smallest source window reached claim limit',
                                batch_id=batch_id,claim_limit=claim_limit)],proposed
            if len(focuses)>1:
                pivot=max(1,len(focuses)//2)
                child_groups=[focuses[:pivot],focuses[pivot:]]
                smaller=passage_chars
            else:
                smaller=max(MIN_CLAIM_PASSAGE_CHARS,passage_chars//2)
                child_groups=[[p] for p in source_passages(source,max_chars=smaller,overlap=0)
                              if focuses[0]['char_start']<=p['char_start']
                              and p['char_end']<=focuses[0]['char_end']]
            claims=[];rejected=[]
            for child in child_groups:
                got,no,count=await self._claims_for_focus(source,verified_mentions,ontology,child,
                                                          passage_chars=smaller,context_ranges=context_ranges)
                claims.extend(got);rejected.extend(no);proposed+=count
            if not child_groups:
                rejected.append(dict(rejection='claim coverage failure: saturated window could not be subdivided',
                                     batch_id=batch_id,claim_limit=claim_limit))
            return claims,rejected,proposed
        reverse={ref:mid for mid,ref in occurrence_refs.items()}
        claims=[]
        for proposal in response.claims:
            mids=[reverse.get(ref) for ref in proposal.occurrence_refs]
            if any(mid is None for mid in mids):
                raise ValueError('claim references an unoffered occurrence')
            claims.append(Claim(type=proposal.type,mention_ids=mids,attribute=proposal.attribute,
                value=proposal.value,quote=proposal.quote,
                evidence_start=proposal.evidence_start))
        return claims,[],proposed

    async def candidates_for(self, chapter: int, mentions: list[dict]) -> tuple[dict[str,list[dict]],dict[str,list[float]]]:
        """Retrieve an occurrence-local, revision/kind/chapter filtered allowlist."""
        if not mentions:
            return {},{}
        await register_vector_async(self.db)
        vectors=await self.embedder.embed([m['quote'] for m in mentions])
        if len(vectors)!=len(mentions) or any(len(v)!=self.cfg.embed_dim for v in vectors):
            raise ValueError('unexpected entity embedding count or dimension')
        result={}
        novel_id = self.revision.get('novel_id')
        if not novel_id:
            novel_id = (await (await self.db.execute(
                'SELECT novel_id::text FROM graph_revision WHERE id=%s',(self.revision['id'],)
            )).fetchone())[0]
        source_lang = (await (await self.db.execute(
            'SELECT source_lang FROM novel WHERE id=%s',(novel_id,)
        )).fetchone())[0]
        for mention,vector in zip(mentions,vectors):
            exact=await (await self.db.execute('''SELECT DISTINCT e.id::text AS entity_id,e.kind,e.canonical
                FROM entity e JOIN alias a ON a.entity_id=e.id AND a.revision_id=e.revision_id
                WHERE e.revision_id=%s AND e.kind=%s AND e.first_seen_chapter<%s
                AND a.surface=%s AND a.lang=%s AND a.first_seen_chapter<%s
                ORDER BY entity_id LIMIT %s''',(self.revision['id'],mention['kind'],chapter,
                    mention['surface'],source_lang,chapter,CANDIDATE_LIMIT))).fetchall()
            dense=await (await self.db.execute('''SELECT e.id::text,e.kind,e.canonical
                FROM entity e WHERE e.revision_id=%s AND e.kind=%s AND e.first_seen_chapter<%s
                AND e.embedding IS NOT NULL ORDER BY e.embedding <=> %s LIMIT %s''',
                (self.revision['id'],mention['kind'],chapter,Vector(vector),CANDIDATE_LIMIT))).fetchall()
            ordered=[];seen=set()
            for eid,kind,canonical in [*exact,*dense]:
                if eid not in seen and len(ordered)<CANDIDATE_LIMIT:
                    ordered.append(dict(id=eid,kind=kind,canonical=canonical));seen.add(eid)
            for candidate in ordered:
                rows=await(await self.db.execute('''SELECT v.chapter_index,v.quote
                    FROM alias a JOIN graph_evidence v ON v.id=a.evidence_id
                    WHERE a.entity_id=%s AND a.revision_id=%s AND v.revision_id=%s
                    AND a.first_seen_chapter<%s AND v.chapter_index<%s
                    ORDER BY v.chapter_index DESC LIMIT 2''',
                    (candidate['id'],self.revision['id'],self.revision['id'],chapter,chapter))).fetchall()
                candidate['source_context']=[dict(chapter=c,quote=q) for c,q in rows]
            result[mention['id']]=ordered
        return result,{m['id']:v for m,v in zip(mentions,vectors)}

    async def discover_names(self, novel: str, chapter: int, source: str):
        ontology=self.revision['ontology']
        batches=self._passage_batches(source) if source.strip() else []
        self._name_metrics=dict(passages=sum(len(batch) for batch in batches),
                                top_level_batches=len(batches),saturation_splits=0,
                                incomplete_windows=0)
        if source.strip():
            collected=[];rejected=[]
            async def inventory(passage_ids: list[str], batch_id: str):
                request=dict(source=source,ontology=ontology,_passage_ids=passage_ids,
                             _batch_id=batch_id)
                found=await self.call('name_slots',Names,request)
                collected.extend(found.names);rejected.extend(found.rejected)
                if request.get('_proposed_count',len(found.names))<NAME_SLOT_COUNT:
                    return
                if len(passage_ids)<=1:
                    self._name_metrics['incomplete_windows']+=1
                    rejected.append(dict(rejection='name coverage failure: smallest source passage reached slot limit',
                                         batch_id=batch_id,name_limit=NAME_SLOT_COUNT))
                    return
                self._name_metrics['saturation_splits']+=1
                # Balance by offered source characters rather than paragraph count so a
                # single long paragraph cannot leave one child near the original size.
                sizes={p['id']:len(p['text']) for p in PassageContract(source).passages}
                total=sum(sizes[pid] for pid in passage_ids);running=0;pivot=1
                for index,pid in enumerate(passage_ids[:-1],1):
                    running+=sizes[pid]
                    pivot=index
                    if running>=total/2:
                        break
                await inventory(passage_ids[:pivot],batch_id+'.1')
                await inventory(passage_ids[pivot:],batch_id+'.2')

            for batch_number,passage_ids in enumerate(batches,1):
                await inventory(passage_ids,f'names-{batch_number}')
            unique={}
            for name in collected:
                key=(name.surface,name.kind)
                if key not in unique or name.evidence_start<unique[key].evidence_start:
                    unique[key]=name
            inventory=list(unique.values())
            eligibility_items=[dict(id='name:'+digest([name.surface,name.kind]),type='name',
                surface=name.surface,kind=name.kind,quote=name.quote,
                evidence_start=name.evidence_start) for name in inventory]
            verdicts=await self._verify_items(source,eligibility_items,'name eligibility')
            accepted,no=approved(eligibility_items,verdicts);rejected.extend(no)
            accepted_ids={item['id'] for item in accepted}
            names=Names(names=[name for name,item in zip(inventory,eligibility_items,strict=True)
                               if item['id'] in accepted_ids],
                        reviewed_kinds=ontology['kinds'],rejected=rejected)
        else:
            names=Names(names=[],reviewed_kinds=ontology['kinds'])
        mentions=source_mentions(novel,chapter,source,names,ontology)
        coverage={kind:dict(proposed_surfaces=len({n.surface for n in names.names if n.kind==kind}),
                            source_occurrences=sum(m['kind']==kind for m in mentions)) for kind in ontology['kinds']}
        return names,mentions,coverage

    def _runtime_diagnostics(self, source: str, display: str) -> dict:
        """Metrics needed to compare chunk policy across chapter sizes (§5.4)."""
        hosted=getattr(self,'hosted',False)
        requests=dict(getattr(self,'_stage_requests',{}))
        cache_hits=dict(getattr(self,'_stage_cache_hits',{}))
        stages=set(requests)|set(cache_hits)
        return dict(
            input_size=dict(source_chars=len(source),source_bytes=len(source.encode()),
                            display_chars=len(display),display_bytes=len(display.encode())),
            chunk_policy=dict(name_passages_per_batch=64,name_focus_chars=400,
                name_slots=NAME_SLOT_COUNT,
                identity_occurrences_per_batch=IDENTITY_BATCH_SIZE,
                claim_focus_chars=24_000 if hosted else CLAIM_PASSAGE_CHARS,
                claim_min_chars=MIN_CLAIM_PASSAGE_CHARS,
                claims_per_response=getattr(self,'claim_limit',CLAIM_LIMIT),
                fact_verification_items_per_call=48 if hosted else 1,
                fact_strategy='api_two_pass' if hosted else 'local_staged',
                name_verification_items_per_batch=16,
                other_verification_items_per_batch=12,fact_render_items_per_batch=12,
                alignment_display_chars=12000,alignment_mentions=48,
                alignments_per_response=ALIGNMENT_LIMIT,alignment_min_chars=MIN_ALIGNMENT_CHARS,
                prompt_hard_bytes=getattr(self,'prompt_hard_bytes',PROMPT_HARD_BYTES)),
            name_chunking=getattr(self,'_name_metrics',dict(
                passages=0,top_level_batches=0,saturation_splits=0,incomplete_windows=0)),
            claim_subdivisions=getattr(self,'_claim_subdivisions',0),
            claim_context_splits=getattr(self,'_claim_context_splits',0),
            alignment_subdivisions=getattr(self,'_alignment_subdivisions',0),
            render_rejections=getattr(self,'_render_rejections',0),
            identity_chunking=dict(batch_sizes=list(getattr(self,'_identity_batch_sizes',[])),
                                   context_splits=getattr(self,'_identity_context_splits',0),
                                   soft_prompt_bytes=IDENTITY_PROMPT_SOFT_BYTES,
                                   hard_prompt_bytes=IDENTITY_PROMPT_HARD_BYTES),
            prompt_metrics=list(getattr(self,'_prompt_metrics',[])),
            stage_requests=requests,stage_cache_hits=cache_hits,
            stage_fresh_calls={stage:requests.get(stage,0)-cache_hits.get(stage,0)
                               for stage in sorted(stages)})

    async def extract(self, novel: str, chapter: int, source: str, display: str, target: str,
                      *, include_terms: bool = True, include_facts: bool = True) -> dict:
        await self._ensure_run(novel,chapter,source,display)
        self._stage_requests={};self._stage_cache_hits={};self._prompt_metrics=[]
        self._claim_subdivisions=0;self._claim_context_splits=0;self._render_rejections=0
        self._alignment_subdivisions=0
        ontology = self.revision['ontology']
        names,mentions,coverage = await self.discover_names(novel,chapter,source)
        if include_terms:
            for mention in mentions:
                await self._activity('term',mention['id'],'detected',dict(surface=mention['surface'],kind=mention['kind']))
        if not mentions:
            return dict(mentions=[],items=[],spans=[],embeddings={},
                candidate_ids={},
                diagnostics=dict(discovered_occurrences=0,proposed_identities=0,
                    verified_identities=0,proposed_claims=0,verified_claims=0,
                    published_facts=0,rejection_reasons={
                        'No source-valid named mentions; no identity or fact publication attempted.':1},
                    **self._runtime_diagnostics(source,display)),
                name_coverage=coverage,rejected=names.rejected+[dict(rejection='No source-valid named mentions; no identity or fact publication attempted.',proposals=names.model_dump())],
                source_hash=digest(source),display_hash=digest(display))
        candidates,mention_vectors = await self.candidates_for(chapter,mentions)
        verified_identities,rejected,proposed_identity_count=await self._resolve_incremental(
            source,mentions,candidates,ontology)
        rejected=names.rejected+rejected

        # A new representative is usable only when its own independently verified
        # decision is a self-root. This is the structural authority boundary (§0).
        verified_by_mention={item['mention_id']:item for item in verified_identities}
        rooted=[]
        for item in verified_identities:
            if item['outcome']!='new':
                rooted.append(item);continue
            root=verified_by_mention.get(item['target_id'])
            if root and root['outcome']=='new' and root['target_id']==root['mention_id']:
                rooted.append(item)
            else:
                rejected.append(dict(item,rejection='new representative lacks a verified self-root decision'))
        verified_identities=rooted

        # Claims cannot name an unverified occurrence. This prevents a failed identity
        # batch from poisoning or erasing otherwise valid claim evidence.
        verified_occurrence_ids={item['mention_id'] for item in verified_identities}
        verified_mentions=[m for m in mentions if m['id'] in verified_occurrence_ids]
        all_claims=[];claim_coverage_rejected=[];proposed_claim_count=0
        if include_facts:
            claim_passage_chars=24_000 if self.hosted else CLAIM_PASSAGE_CHARS
            for focus in self._claim_batches(source):
                found,no,count=await self._claims_for_focus(
                    source,verified_mentions,ontology,focus,passage_chars=claim_passage_chars)
                all_claims.extend(found);claim_coverage_rejected.extend(no);proposed_claim_count+=count
        # Neighboring context deliberately overlaps. Exact application-owned evidence
        # offsets make duplicate elimination deterministic without using names as keys.
        unique_claims={}
        for claim in all_claims:
            key=(claim.type,tuple(claim.mention_ids),claim.attribute,claim.value,claim.evidence_start,claim.quote)
            unique_claims[key]=claim
        all_claims=list(unique_claims.values())
        claim_items,claim_rejected=validate_proposals(
            source,mentions,candidates,Proposals(decisions=[],claims=all_claims),ontology)
        rejected.extend(claim_coverage_rejected+claim_rejected)
        if include_facts:
            if self.hosted:
                verified_claims,no=await self._review_hosted_claims(
                    source,claim_items,mentions,target)
                rejected.extend(no)
            else:
                facts=[item for item in claim_items if item['type']=='fact']
                other_claims=[item for item in claim_items if item['type']!='fact']
                fact_verdicts=await self._verify_facts(source,facts,mentions)
                other_verdicts=await self._verify_items(source,other_claims,'claims',mentions=mentions)
                fact_claims,no=approved(facts,fact_verdicts)
                rejected.extend(no)
                verified_other,no=approved(other_claims,other_verdicts)
                rejected.extend(no)
                # Display text is downstream of source acceptance. A bad translation now
                # falls back to the source value instead of becoming an unchecked fact.
                verified_claims=await self._render_facts(source,fact_claims,mentions)+verified_other
        else:
            verified_claims=[]
        spans=[]
        # Bound both translated prose and source occurrences. Windows deliberately
        # fail closed at boundaries; they never manufacture a global offset.
        window_count=max(1,math.ceil(len(display)/12000),math.ceil(len(mentions)/48)) if include_terms else 0
        display_identity=digest(display)
        for i in range(window_count):
            lo=len(display)*i//window_count;hi=len(display)*(i+1)//window_count
            mlo=len(mentions)*i//window_count;mhi=len(mentions)*(i+1)//window_count
            spans.extend(await self._align_window(novel,chapter,source,display,
                mentions[mlo:mhi],lo,hi,display_identity))
        by_span={}
        for span in spans:
            key=(span['char_start'],span['char_end'])
            if key in by_span and by_span[key]['mention_id']!=span['mention_id']:
                span['mention_id']=span['quote']=None
            by_span[key]=span
        spans=sorted(by_span.values(),key=lambda s:s['char_start'])
        alignment_items = [dict(s,id='alignment:'+s['id'],type='alignment') for s in spans if s['mention_id']]
        alignment_verdicts=Verification(verdicts=[])
        for start in range(0,len(alignment_items),12):
            batch=alignment_items[start:start+12]
            display_contexts=[display[max(0,i['char_start']-120):min(len(display),i['char_end']+120)]
                              for i in batch if i.get('type')=='alignment']
            # Alignment retains its established display-context contract. Its verifier
            # also gets request-local item refs and exact-one validation.
            checked=await self._verify_items(source,batch,'alignment',display_contexts=display_contexts,mentions=mentions)
            alignment_verdicts.verdicts.extend(checked.verdicts)
        verified_alignments, no = approved(alignment_items,alignment_verdicts)
        rejected += no
        verified_ids = {i['id'] for i in verified_alignments}
        for s in spans:
            if 'alignment:'+s['id'] not in verified_ids:
                s['mention_id'] = s['quote'] = None
        verified=verified_identities+verified_claims
        if include_terms:
            for item in verified_identities:
                await self._activity('term',item['mention_id'],'verified',item)
        for item in verified_claims:
            await self._activity('fact',item['id'],'verified',item)
        for item in rejected:
            item_id=item.get('id') or item.get('item',{}).get('id') or digest(item)
            kind='fact' if str(item_id).startswith('claim:') else 'term'
            if (kind=='fact' and include_facts) or (kind=='term' and include_terms):
                await self._activity(kind,str(item_id),'rejected',item)
        roots={i['target_id'] for i in verified_identities if i['outcome']=='new'}
        representatives=[m for m in mentions if m['id'] in roots]
        vectors=[mention_vectors[m['id']] for m in representatives]
        rejection_counts={}
        for row in rejected:
            reason=row.get('rejection','unknown')
            rejection_counts[reason]=rejection_counts.get(reason,0)+1
        diagnostics=dict(discovered_occurrences=len(mentions),
            proposed_identities=proposed_identity_count,verified_identities=len(verified_identities),
            proposed_claims=proposed_claim_count,verified_claims=len(verified_claims),
            published_facts=0,rejection_reasons=rejection_counts,
            **self._runtime_diagnostics(source,display))
        print(json.dumps(dict(revision=self.revision['id'],chapter=chapter,stage='knowledge',
                              event='stage_summary',**diagnostics)),file=sys.stderr,flush=True)
        output=dict(mentions=mentions,name_coverage=coverage,items=[i for i in verified if i.get('type')!='alignment'],
                    candidate_ids={mid:[candidate['id'] for candidate in rows] for mid,rows in candidates.items()},
                    embeddings={m['id']:v for m,v in zip(representatives,vectors)},
                    spans=spans if include_terms else [],rejected=rejected,diagnostics=diagnostics,
                    source_hash=digest(source),display_hash=digest(display))
        # Earlier empty cards can be resolved by a later explicit reveal, but that link
        # is append-only and known_from=current chapter, never the old occurrence date.
        earlier=await (await self.db.execute('''SELECT m.id::text,m.surface,m.kind,v.quote
            FROM source_mention m JOIN graph_evidence v ON v.id=m.evidence_id
            WHERE m.revision_id=%s AND m.chapter_index<%s AND NOT EXISTS
            (SELECT 1 FROM mention_binding b WHERE b.revision_id=m.revision_id AND b.mention_id=m.id)
            ORDER BY m.chapter_index,m.id LIMIT 48''',(self.revision['id'],chapter))).fetchall()
        if earlier:
            output['rejected'].append(dict(rejection='Earlier unresolved occurrences retained: reconciliation requires bounded current-chapter evidence.',count=len(earlier)))
        return output

    async def publish(self, state, output: dict, source: str, *, publish_terms: bool = True,
                      publish_facts: bool = True):
        """Caller owns revision/job lock and generation fence; one atomic publication."""
        revision, novel, chapter = self.revision['id'],state.envelope.novel_id,state.envelope.chapter_index
        mentions = {m['id']:m for m in output['mentions']}

        async def evidence(quote, anchor=None, start=None):
            ev = passage(source,quote,anchor=anchor,start=start)
            if not ev:
                raise ValueError('evidence changed before publication')
            eid = stable_id(revision,chapter,digest(source),ev['char_start'],ev['char_end'])
            await self.db.execute('''INSERT INTO graph_evidence
                (id,revision_id,novel_id,chapter_index,source_hash,char_start,char_end,quote)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (eid,revision,novel,chapter,digest(source),ev['char_start'],ev['char_end'],quote))
            return eid

        for m in mentions.values():
            ev = await evidence(m['quote'],m['char_start'],m.get('context_start'))
            await self.db.execute('INSERT INTO source_mention VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (m['id'],revision,novel,chapter,m['surface'],m['kind'],ev))
        decisions = {i['mention_id']:i for i in output['items'] if i['id'].startswith('identity:')}
        # A root must itself be independently verified as a new subject, not an
        # invented shared token. No global English-name or source-string map exists.
        for mid, d in decisions.items():
            if d['outcome'] == 'new':
                root = decisions.get(d['target_id'])
                if not root or root['outcome']!='new' or root['target_id']!=root['mention_id']:
                    continue
                entity_id = stable_id(revision,'entity',root['mention_id'])
                m = mentions[root['mention_id']]
                canonical = m['surface']
                if m['kind'].lower() == 'character':
                    approved = await (await self.db.execute('''SELECT target_term FROM glossary
                        WHERE novel_id=%s AND source_term=%s AND constraint_class='character_name'
                        AND NOT deleted''',(novel,m['surface']))).fetchone()
                    if not approved:
                        output['rejected'].append(dict(item=d,rejection='character name lacks an approved source-anchored spelling'))
                        continue
                    canonical = approved[0]
                await self.db.execute('''INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter,revision_id,embedding)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (entity_id,novel,m['kind'],canonical,chapter,revision,output.get('embeddings',{}).get(root['mention_id'])))
            else:
                entity_id = d['target_id']
            state.resolutions[mid] = entity_id
            m = mentions[mid]
            ev = await evidence(d['quote'],m['char_start'],d.get('evidence_start'))
            await self.db.execute('INSERT INTO mention_binding VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                                  (revision,mid,chapter,entity_id,ev))
            await self.db.execute('''INSERT INTO alias(entity_id,surface,lang,first_seen_chapter,revision_id,evidence_id)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (entity_id,m['surface'],state.envelope.source_lang,chapter,revision,ev))
        for d in output.get('reconciliations',[]):
            ev=await evidence(d['quote'],start=d.get('evidence_start'))
            await self.db.execute('INSERT INTO mention_binding VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (revision,d['mention_id'],chapter,d['target_id'],ev))
        published_facts=0
        for item in output['items']:
            if not item['id'].startswith('claim:'):
                continue
            if not publish_facts:
                continue
            mids = item['mention_ids']
            if any(mid not in state.resolutions for mid in mids):
                output['rejected'].append(dict(item,rejection='identity unresolved after verification'))
                continue
            ids = [state.resolutions[mid] for mid in mids]
            ev = await evidence(item['quote'],start=item.get('evidence_start'))
            key = digest([chapter,item['type'],ids,item['attribute'],item['value'],ev])
            if item['type']=='fact':
                # fact.value is stored in the SOURCE language. Extraction runs on the raw
                # chapter and the bounded claims contract explicitly requires source values.
                # Storage stays this way on purpose: facts are append-only and anchored to
                # source evidence, and enforcing a target language at write time would fail
                # ingestion whenever no glossary term exists yet. Rendering belongs at
                # DISPLAY time, which is what value_en is (migration 0051): a gloss from
                # this same call, never evidence, nullable, and COALESCEd over by
                # reader-api (store.go) and askai (retrieval.py) so pre-0051 facts still
                # render. It is absent from claim_key above on purpose -- keying identity
                # on a display string would make a re-extraction with a differently worded
                # gloss insert a duplicate of a fact the graph already holds.
                await self.db.execute('''INSERT INTO fact(novel_id,entity_id,attribute,value,value_en,valid_from_chapter,
                    source_chapter,revision_id,evidence_id,claim_key,kind,supersedes) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (novel,ids[0],item['attribute'],item['value'],item.get('value_en') or None,
                     chapter,chapter,revision,ev,key,item.get('kind','assertion'),item.get('supersedes')))
                published_facts += 1
            elif item['type']=='relationship':
                await self.db.execute('''INSERT INTO edge(novel_id,src_id,dst_id,rel_type,valid_from_chapter,
                    source_chapter,revision_id,evidence_id,claim_key) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (novel,*ids,item['attribute'],chapter,chapter,revision,ev,key))
            else:
                await self.db.execute('''INSERT INTO event(novel_id,chapter_index,summary,entity_ids,revision_id,evidence_id,claim_key)
                    VALUES(%s,%s,%s,%s::uuid[],%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (novel,chapter,item['value'],ids,revision,ev,key))
        output.setdefault('diagnostics',{})['published_facts']=published_facts
        print(json.dumps(dict(revision=revision,chapter=chapter,stage='publish',event='stage_summary',
                              published_facts=published_facts,
                              published_claims=sum(i['id'].startswith('claim:') for i in output['items']))),
              file=sys.stderr,flush=True)
        if publish_terms:
          for s in output['spans']:
            ev = await evidence(s['quote'],mentions[s['mention_id']]['char_start'],s.get('evidence_start')) if s['mention_id'] else None
            await self.db.execute('''INSERT INTO display_mention VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (s['id'],revision,novel,chapter,s['mention_id'],s['char_start'],s['char_end'],s['phrase'],output['display_hash'],ev))
            mid=s['mention_id']
            if mid and mid in mentions:
                await self.db.execute('''INSERT INTO term_rendering_occurrence
                    (novel_id,chapter_index,char_start,char_end,source_term,display_term,method)
                    VALUES(%s,%s,%s,%s,%s,%s,'aligned')
                    ON CONFLICT(novel_id,chapter_index,char_start,char_end) DO UPDATE
                    SET source_term=EXCLUDED.source_term,display_term=EXCLUDED.display_term,method='aligned' ''',
                    (novel,chapter,s['char_start'],s['char_end'],mentions[mid]['surface'],s['phrase']))
            if mid in state.resolutions:
                # A verified alignment may supply target-language display metadata. It
                # never participates in resolution, which remains source anchored (§0).
                await self.db.execute('''UPDATE source_mention
                    SET surface_en=coalesce(surface_en,%s)
                    WHERE revision_id=%s AND id=%s''',(s['phrase'],revision,mid))
                await self.db.execute('''UPDATE entity SET canonical_en=coalesce(canonical_en,%s)
                    WHERE revision_id=%s AND id=%s''',(s['phrase'],revision,state.resolutions[mid]))
                # This is an independently verified alignment proposal, not an alias
                # cache hit. Retries cannot add a second vote for the same chapter.
                await self.db.execute('''INSERT INTO glossary_proposal_chapter VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING''',(revision,mentions[mid]['surface'],s['phrase'],chapter,ev))
                # Preserve wording, deletions and audit history. Only bind an existing
                # approved phrase when this exact occurrence independently supports it.
                await self.db.execute('''INSERT INTO glossary_binding(novel_id,source_term,revision_id,entity_id,known_from_chapter)
                    SELECT novel_id,source_term,%s,%s,%s FROM glossary WHERE novel_id=%s AND source_term=%s
                    AND target_term=%s AND NOT deleted ON CONFLICT DO NOTHING''',
                    (revision,state.resolutions[mid],chapter,novel,mentions[mid]['surface'],s['phrase']))
        for item in output['items']:
            if publish_facts and item['id'].startswith('claim:'):
                await self._activity('fact',item['id'],'published',item)
        await self._activity('run','run','published',dict(published_facts=published_facts))
        if getattr(self,'run_id',None):
            await self.db.execute("UPDATE chapter_knowledge_run SET state='published',updated_at=now() WHERE id=%s",(self.run_id,))

    async def close(self):
        await self.provider.aclose()
        await self.embedder.aclose()
