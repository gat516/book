"""Revision-scoped enrichment of saved prose (§0, §5.4).

All model outputs remain proposals until literal and independent semantic checks pass.
Only publish() consumes PipelineState.resolutions; it never performs name matching.
"""
from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
import functools
import json
import math
import time
import sys
from types import SimpleNamespace
from urllib.parse import urlparse

from pgvector import Vector
from pgvector.psycopg import register_vector_async
from psycopg.types.json import Jsonb

from pipeline.evidence import (
    PROMPT_VERSION, LEGACY_PROMPT_VERSION, STAGE_PROMPT_VERSIONS, Proposals,
    IdentityDecisions, ExtractProposals,
    Decision, Claim, Alignments, Verification, Verdict, digest, stable_id,
    source_mentions, validate_proposals, approved, aligned_mentions, passage,
)
from pipeline.failures import failure_category
from pipeline.llm.ollama import OllamaProvider
from pipeline.llm.provider import AdmissionRejected, Class
from pipeline.config import graph_runtime
from pipeline.passages import PassageContract, source_passages, source_windows
from pipeline.vocabulary import load_visible, vocabulary_prompt, normalize_name, valid_name, resolve as resolve_vocabulary, record_candidate
from pipeline.knowledge_contract import (
    compact_identity_payload, identity_schema, materialize_identity, materialize_verification,
    unique_json_object, verification_schema,
)


PROMPT_HARD_BYTES = 42 * 1024
CANDIDATE_LIMIT = 8
# B.4 provisional cost-target ceilings ("experimental ceilings, not approved
# production settings" -- qualification is Phase F's job, not this phase's). Every
# *_HOSTED constant below is the hosted-path counterpart selected in __init__.
EXTRACT_WINDOW_CHARS = 1800
EXTRACT_WINDOW_OVERLAP = 200
EXTRACT_WINDOW_CHARS_HOSTED = 24_000
MIN_EXTRACT_WINDOW_CHARS = 400  # B.5 bisection floor, shared with the paragraph split below
EXTRACT_PASSAGE_CHARS = 400  # citation granularity inside one extract window (adjacency)
NAME_LIMIT, ATTR_LIMIT, REL_LIMIT, OCCUR_LIMIT = 14, 8, 6, 5
NAME_LIMIT_HOSTED, ATTR_LIMIT_HOSTED, REL_LIMIT_HOSTED, OCCUR_LIMIT_HOSTED = 64, 48, 24, 32
IDENTITY_BATCH_SIZE = 24
IDENTITY_BATCH_SIZE_HOSTED = 48
IDENTITY_PROMPT_SOFT_BYTES = 24 * 1024
IDENTITY_PROMPT_SOFT_BYTES_HOSTED = 64 * 1024
IDENTITY_PROMPT_HARD_BYTES = 32 * 1024
IDENTITY_PROMPT_HARD_BYTES_HOSTED = 96 * 1024
ALIGN_WINDOW_CHARS = 6000
ALIGN_WINDOW_CHARS_HOSTED = 24_000
# Mirrors Alignments.alignments' Field(max_length=...) upper bound in evidence.py.
ALIGNMENT_LIMIT = 32
ALIGNMENT_LIMIT_HOSTED = 96
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
        revision_prompt_version = revision.get('prompt_version', PROMPT_VERSION)
        if revision_prompt_version == PROMPT_VERSION:
            self.prompt_drift = None
        elif revision_prompt_version == LEGACY_PROMPT_VERSION:
            # Revisions made before per-stage markers existed may resume.  The cache
            # remains content addressed, while this provenance is recorded on the
            # revision at the first async touchpoint.  Preview/activation still checks
            # the exact graph_revision.prompt_version (see graph_rebuild.py).
            self.prompt_drift = dict(
                from_version=revision_prompt_version,
                to_version=PROMPT_VERSION,
                compatible=True,
                stage_versions=dict(STAGE_PROMPT_VERSIONS),
            )
            print(json.dumps(dict(event='prompt_drift',revision=revision.get('id'),
                                  **self.prompt_drift)),file=sys.stderr,flush=True)
        else:
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
        self.prompt_hard_bytes = 256 * 1024 if self.hosted else PROMPT_HARD_BYTES
        # B.4: local vs hosted ceilings, selected once per engine. All experimental --
        # see the module docstring at the top of this constants block.
        self.extract_window_chars = EXTRACT_WINDOW_CHARS_HOSTED if self.hosted else EXTRACT_WINDOW_CHARS
        self.extract_limits = dict(
            names=NAME_LIMIT_HOSTED if self.hosted else NAME_LIMIT,
            attributes=ATTR_LIMIT_HOSTED if self.hosted else ATTR_LIMIT,
            relations=REL_LIMIT_HOSTED if self.hosted else REL_LIMIT,
            occurrences=OCCUR_LIMIT_HOSTED if self.hosted else OCCUR_LIMIT)
        self.identity_batch_size = IDENTITY_BATCH_SIZE_HOSTED if self.hosted else IDENTITY_BATCH_SIZE
        self.identity_soft_bytes = IDENTITY_PROMPT_SOFT_BYTES_HOSTED if self.hosted else IDENTITY_PROMPT_SOFT_BYTES
        self.identity_hard_bytes = IDENTITY_PROMPT_HARD_BYTES_HOSTED if self.hosted else IDENTITY_PROMPT_HARD_BYTES
        self.align_window_chars = ALIGN_WINDOW_CHARS_HOSTED if self.hosted else ALIGN_WINDOW_CHARS
        self.alignment_limit = ALIGNMENT_LIMIT_HOSTED if self.hosted else ALIGNMENT_LIMIT
        self.embedder = OllamaProvider(host=cfg.ollama_host,model=cfg.embed_model,
            timeout=limits['idle_timeout_seconds'],
            total_timeout=limits['total_timeout_seconds'] or EMBED_FALLBACK_TIMEOUT_SECONDS)
        # Scheduling only (see Config.graph_max_concurrent_calls): deliberately absent
        # from `runtime['identity']` and so from the completion cache key.
        self.max_concurrent_calls = max(1,getattr(cfg,'graph_max_concurrent_calls',1))
        self._call_slots = asyncio.Semaphore(self.max_concurrent_calls)
        # psycopg's AsyncConnection is one connection shared by every in-flight call, and
        # it is not safe for concurrent cursors. execute() buffers the whole result for a
        # client-side cursor, so holding this only across execute() is sufficient.
        self._db_lock = asyncio.Lock()

    async def _resolve_novel_id(self) -> str:
        """Resolve and memoize the novel owning this revision.

        ``__init__`` is synchronous, but both cache reads and candidate retrieval need
        the tenant boundary.  Keeping this in one async helper makes it impossible for
        the cache path to accidentally fall back to a global key (§0, §5.4).
        """
        novel_id = getattr(self, '_novel_id', None) or self.revision.get('novel_id')
        if novel_id:
            self._novel_id = str(novel_id)
            return self._novel_id
        row = await self._exec(
            'SELECT novel_id::text FROM graph_revision WHERE id=%s',
            (self.revision['id'],),
        )
        found = await row.fetchone()
        if not found:
            raise ValueError('revision has no owning novel')
        self._novel_id = str(found[0])
        return self._novel_id

    def _expected_served_identity(self) -> tuple[str, str]:
        """Return the concrete served pair allowed by this revision.

        Hosted providers may return a concrete model different from a requested alias.
        Such a response is reusable only when the revision records that concrete pin;
        otherwise a failover would be cached under the wrong identity (§5.4, §14.3).
        """
        model = self.revision.get('model', {})
        # Only the explicit pin fields are part of the revision model contract.
        # Do not infer a pin from undocumented nested provider metadata.
        provider = model.get('served_provider') or self.provider_id
        name = model.get('served_model') or self.model
        return str(provider), str(name)

    async def _record_cache_run(self, *, cache_key: str, served_provider: str,
                                served_model: str, stage: str, batch_id: str,
                                requested_provider: str, requested_model: str,
                                stage_prompt_version: str, prompt_digest: str,
                                schema_digest: str) -> None:
        """Record attribution without making a valid model result un-reusable.

        This table intentionally has no FK to the cache primary key.  A reporting row
        is useful provenance, but it is bookkeeping and must not turn a completed model
        call into a failed chapter when a concurrent cleanup races it.
        """
        if not self.run_id:
            return
        try:
            await self._optional_exec('''INSERT INTO completion_cache_run
                (revision_id,cache_key,served_provider,served_model,run_id,chapter_index,
                 stage,batch_id,requested_provider,requested_model,stage_prompt_version,
                 prompt_digest,schema_digest)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING''',
                (self.revision['id'],cache_key,served_provider,served_model,self.run_id,
                 self.current_chapter,stage,batch_id,requested_provider,requested_model,
                 stage_prompt_version,prompt_digest,schema_digest))
        except Exception as exc:
            print(json.dumps(dict(event='completion_cache_attribution_failed',
                                  revision=self.revision.get('id'),stage=stage,
                                  cache_key=cache_key,error_type=type(exc).__name__,
                                  error=str(exc))),file=sys.stderr,flush=True)

    async def _exec(self, sql, params=None):
        """Serialized access to the shared connection. Returns a fully buffered cursor.

        Tests build engines with object.__new__, so a missing lock means nothing
        concurrent was ever started and the plain connection is already safe.
        """
        lock=getattr(self,'_db_lock',None)
        if lock is None:
            return await (self.db.execute(sql,params) if params is not None
                          else self.db.execute(sql))
        async with lock:
            return await (self.db.execute(sql,params) if params is not None
                          else self.db.execute(sql))

    async def _optional_exec(self, sql, params=None):
        """Run bookkeeping in a savepoint so a failure cannot poison extraction.

        Cache touches, attribution, and prompt-drift diagnostics are useful metadata but
        never outrank the chapter result. A nested ``transaction()`` is a savepoint when
        the worker already owns a transaction; on a standalone connection it commits the
        optional statement normally. Tests using lightweight DB doubles fall back to the
        serialized executor.
        """
        transaction = getattr(self.db, 'transaction', None)
        if transaction is None:
            return await self._exec(sql, params)

        async def execute():
            return await (self.db.execute(sql,params) if params is not None
                          else self.db.execute(sql))
        lock=getattr(self,'_db_lock',None)
        if lock is None:
            async with self.db.transaction():
                return await execute()
        async with lock:
            async with self.db.transaction():
                return await execute()

    async def _fan_out(self, factories: list):
        """Run independent calls with bounded concurrency, preserving input order.

        `factories` are zero-argument coroutine functions so the serial path never builds
        a coroutine it does not await. A TaskGroup would wrap failures in an
        ExceptionGroup and break the precise `except ValueError` and failure_category
        handling every caller here depends on, so settle every sibling first and then
        re-raise the first failure in input order -- exactly how the serial loop failed.
        """
        if max(1,getattr(self,'max_concurrent_calls',1)) == 1:
            return [await make() for make in factories]
        results = await asyncio.gather(*(make() for make in factories),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result,BaseException):
                raise result
        return list(results)

    async def _ensure_run(self, novel: str, chapter: int, source: str, display: str) -> None:
        self._novel_id = novel
        self.current_chapter = chapter
        if getattr(self, 'prompt_drift', None) and not getattr(self, '_prompt_drift_recorded', False):
            # Keep the exact provenance on the revision without overwriting an existing
            # evaluation report.  Activation remains blocked by the prompt-version gate;
            # this marker only explains why a staging run was allowed to resume.
            try:
                # Merge the diagnostic into the existing report, but do not replace an
                # evaluation (or an already-recorded drift diagnostic) produced by a
                # previous attempt.  The prompt gate still prevents this staging run
                # from being adopted; this is provenance only (§0, §5.4).
                await self._optional_exec(
                    """UPDATE graph_revision
                          SET evaluation=CASE
                              WHEN COALESCE(evaluation,'{}'::jsonb) ? 'prompt_drift'
                                THEN COALESCE(evaluation,'{}'::jsonb)
                              ELSE COALESCE(evaluation,'{}'::jsonb) || %s
                            END
                        WHERE id=%s""",
                    (Jsonb(dict(prompt_drift=self.prompt_drift)), self.revision['id']),
                )
                self._prompt_drift_recorded = True
            except Exception as exc:
                print(json.dumps(dict(event='prompt_drift_record_failed',
                                      revision=self.revision.get('id'),
                                      error_type=type(exc).__name__,error=str(exc))),
                      file=sys.stderr,flush=True)
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
        await self._exec('''INSERT INTO chapter_knowledge_activity
            (run_id,novel_id,chapter_index,item_kind,item_key,phase,payload,idempotency_key)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id,idempotency_key) DO NOTHING''',
            (self.run_id,getattr(self,'_novel_id',self.revision.get('novel_id')),self.current_chapter,kind,key,phase,Jsonb(payload),identity))

    async def _worker_progress(self, stage: str, *, heartbeat: bool = False) -> None:
        """Persist stage/liveness metadata without persisting source or model output."""
        if getattr(self,'current_chapter',None) is None:
            return
        await self._exec('''UPDATE graph_job SET
            stage_started_at=CASE WHEN current_stage IS DISTINCT FROM %s THEN now()
                                  ELSE stage_started_at END,
            current_stage=%s,last_progress_at=now(),
            updated_at=CASE WHEN %s THEN updated_at ELSE now() END
          WHERE revision_id=%s AND chapter_index=%s''',
            (stage,stage,heartbeat,self.revision['id'],self.current_chapter))

    async def _proposal_activity(self, stage: str, parsed) -> None:
        if stage != 'extract':
            return
        body=parsed  # already a plain dict; passages.materialize's 'extract' branch
        for item in body.get('names',[]):
            await self._activity('term',str(stable_id(stage,item)),'proposed',item)
        for key in ('attributes','relations','occurrences'):
            for item in body.get(key,[]):
                await self._activity('fact',str(stable_id(stage,key,item)),'proposed',item)

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
            # B.3: names the durability test directly, and explicitly permits empty
            # attribute/relation/occurrence lists -- the old 'claims' instruction
            # implicitly demanded output from every window, which is root cause #2's
            # upstream defect (everything defaults to a catch-all 'description').
            'extract': (
                'Extract from the offered source passages only. Inventory explicitly named '
                'subjects into names[] (people, places, organizations, factions, schools, '
                'techniques and other named story-specific things) -- exact source spelling, '
                'most appropriate ontology kind, and the passage that contains them. A name '
                'mentioned once still counts; exclude generic titles, pronouns and unnamed '
                'categories. An empty names[] is correct when a window contains no explicit name.\n'
                'Record a property in attributes[] only when it passes this test: it remains '
                'true after this scene ends. A property that stops being true the moment the '
                'character leaves the room -- current position, current action, the weather -- '
                'is an occurrence, not an attribute. Do not record counts of unnamed creatures. '
                'AN EMPTY attributes[] LIST IS THE CORRECT ANSWER FOR AN ACTION SCENE with no '
                'durable properties revealed.\n'
                'Record relations[] only for an explicitly stated relationship between two named '
                'subjects, with sentiment -1/0/1 only when the text supports it.\n'
                'Record occurrences[] for a scene beat: what happened, who was involved, using '
                'anchored occurrence references (name_index + passage + occurrence ordinal), '
                'never a name string. Do not translate values; do not require a value to be a '
                'literal substring -- structural evidence citation, not verbatim copying, is '
                'what proves support. Cite one or two adjacent offered passages when both an '
                'antecedent and an assertion are needed; the assertion itself must occur in a '
                'cited passage.'
            ),
            'identity_slots': IDENTITY_SLOT_INSTRUCTIONS,
            'align': 'Align the saved translation to the offered SOURCE OCCURRENCES. Return exact displayed named phrases, their zero-based occurrence number in the translation, and the matching source mention ID plus supporting passage ID. Different translated spellings can name the same source subject; identical spellings may name different subjects. Include unaligned displayed names with null mention_id and null passage_id. Never infer identity from capitalization alone. Do not rewrite the translation.',
            # B.6: the one verifier kept for every binding, fast-path proposals included.
            'verify': 'Independently check EACH offered occurrence identity decision and fill every required item_ref JSON slot exactly once. supported=true only when the evidence explicitly establishes the identity and kind. Same spelling, vector proximity, plausibility and prior mistaken labels are not identity evidence. A wrong merge fuses two separate people or places into one history -- when uncertain, prefer unsupported. Keep each reason at most 200 characters.',
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
        vocabulary = None
        if stage == 'extract' and getattr(self, 'current_chapter', None) is not None:
            try:
                rows = await load_visible(self.db, await self._resolve_novel_id(), self.current_chapter)
            except AttributeError:
                # Lightweight call doubles used by the pure contract tests predate the
                # vocabulary table and expose only fetchone(). Production DB errors are
                # not swallowed here.
                rows = []
            # Unlike the old 'claims' stage, extraction proposes attributes/relations
            # before any occurrence's kind is known (names and attributes are proposed
            # in the SAME call), so the admitted vocabulary cannot yet be filtered by
            # participant kind; the application-side kind check happens later in
            # validate_proposals once mentions are anchored.
            rows = [r for r in rows if r['status']=='admitted']
            vocabulary = vocabulary_prompt(rows)
            payload['vocabulary'] = vocabulary
            instructions['extract'] += (' The admitted vocabulary is supplied as vocabulary.attributes and '
                'vocabulary.relations with load-bearing glosses (each gloss states its durability test). Use an '
                'admitted name when it fits; a new snake_case name is allowed only when the gloss instruction '
                'supports a durable assertion.')
        wire_schema=contract.schema(stage,schema,ontology,vocabulary,payload.get('_limits'))
        guidance=' Cite offered passage references instead of generating quote text or offsets. A selected passage is evidence to verify, never proof by itself. Null references leave identities unsupported.' if stage in {'extract','align'} else ''
        if stage == 'identity_slots':
            wire_schema=identity_schema(payload['identity_occurrences'],list(contract.by_id))
        elif stage == 'verify':
            wire_schema=verification_schema([i['item_ref'] for i in payload['items']])
        shape = ('\nOUTPUT JSON SCHEMA:\n'+json.dumps(wire_schema,ensure_ascii=False)
                 if stage in {'identity_slots','verify','extract'} else '')
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
        hard_bytes=(self.identity_hard_bytes if stage=='identity_slots'
                    else self.prompt_hard_bytes)
        if len(prompt.encode()) > hard_bytes:
            raise ValueError('graph context exceeds hard model budget; bounded caller contract regressed')
        stage_prompt_version = STAGE_PROMPT_VERSIONS[stage]
        prompt_hash = digest(prompt)
        schema_hash = digest(wire_schema)
        # Revision IDs deliberately do not participate in this key.  The request
        # material, stage marker, and requested runtime identity fully describe the
        # output-affecting work; revision_id is retained only in completion_cache_run
        # for provenance (§0, §5.4, §14.3).
        requested_model_id=f'{self.provider_id}:{self.model}'
        key = digest([requested_model_id,self.runtime['identity'],stage_prompt_version,stage,prompt,wire_schema])
        novel_id = await self._resolve_novel_id()
        expected_provider, expected_model = self._expected_served_identity()
        row = await (await self._exec(
            '''SELECT response,runtime_metrics,served_provider,served_model
               FROM completion_cache
              WHERE novel_id=%s AND cache_key=%s
                AND requested_provider=%s AND requested_model=%s
                AND served_provider=%s AND served_model=%s
                AND stage=%s AND stage_prompt_version=%s
                AND prompt_digest=%s AND schema_digest=%s''',
            (novel_id,key,self.provider_id,self.model,expected_provider,expected_model,
             stage,stage_prompt_version,prompt_hash,schema_hash))).fetchone()
        def materialize(body):
            return (materialize_identity(body,payload['identity_occurrences'],contract)
                    if stage=='identity_slots' else
                    materialize_verification(body,[i['item_ref'] for i in payload['items']])
                    if stage=='verify' else
                    contract.materialize(stage,schema,body,ontology))

        if row:
            served_provider, served_model = row[2], row[3]
            try:
                cached_body=row[0]
                if isinstance(cached_body,str):
                    cached_body=json.loads(cached_body,object_pairs_hook=unique_json_object)
                parsed=materialize(cached_body)
            except Exception as exc:
                # A cache row is disposable bookkeeping. If its raw wire response no
                # longer satisfies the current request contract, delete it and perform a
                # fresh call rather than replaying an invalid result forever.
                print(json.dumps(dict(event='completion_cache_rejected',
                                      revision=self.revision.get('id'),stage=stage,
                                      cache_key=key,error_type=type(exc).__name__,
                                      error=str(exc))),file=sys.stderr,flush=True)
                try:
                    await self._optional_exec(
                        '''DELETE FROM completion_cache
                           WHERE novel_id=%s AND cache_key=%s
                             AND served_provider=%s AND served_model=%s''',
                        (novel_id,key,served_provider,served_model))
                except Exception as delete_exc:
                    print(json.dumps(dict(event='completion_cache_delete_failed',
                                          revision=self.revision.get('id'),stage=stage,
                                          cache_key=key,error_type=type(delete_exc).__name__,
                                          error=str(delete_exc))),file=sys.stderr,flush=True)
            else:
                hits=getattr(self,'_stage_cache_hits',{})
                hits[stage]=hits.get(stage,0)+1
                self._stage_cache_hits=hits
                try:
                    await self._optional_exec(
                        'UPDATE completion_cache SET last_used_at=now() WHERE novel_id=%s AND cache_key=%s AND served_provider=%s AND served_model=%s',
                        (novel_id,key,served_provider,served_model),
                    )
                except Exception as exc:
                    print(json.dumps(dict(event='completion_cache_touch_failed',
                                          revision=self.revision.get('id'),stage=stage,
                                          cache_key=key,error_type=type(exc).__name__,
                                          error=str(exc))),file=sys.stderr,flush=True)
                await self._record_cache_run(
                    cache_key=key,served_provider=served_provider,served_model=served_model,
                    stage=stage,batch_id=payload.get('_batch_id',key[:12]),
                    requested_provider=self.provider_id,requested_model=self.model,
                    stage_prompt_version=stage_prompt_version,prompt_digest=prompt_hash,
                    schema_digest=schema_hash)
                if stage=='extract':
                    # B.5: saturation is tracked per list, including on cache hits, so a
                    # cache-hit response must not silently skip bisection.
                    counts=(row[1] or {}).get('proposed_counts')
                    if counts is None:
                        counts=dict(names=len(parsed['names']),attributes=len(parsed['attributes']),
                            relations=len(parsed['relations']),occurrences=len(parsed['occurrences']))
                    payload['_proposed_counts']=counts
                elif stage=='align':
                    proposed=(row[1] or {}).get('proposed_count')
                    if proposed is None:
                        proposed=payload.get('_proposed_count',len(parsed.alignments))
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
        # `progress_sink` is a single mutable slot on the provider, so two in-flight
        # calls would overwrite each other's sink and the first `finally` would clear the
        # survivor's. Stream progress only when this engine is serial; the per-stage
        # _worker_progress writes around every call keep the job visibly alive either way.
        streaming = (max(1,getattr(self,'max_concurrent_calls',1)) == 1
                     and hasattr(self.provider,'progress_sink'))
        if streaming:
            self.provider.progress_sink = progress
        try:
            async with (getattr(self,'_call_slots',None) or contextlib.nullcontext()):
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
            if streaming:
                self.provider.progress_sink = None
        print(json.dumps(dict(diagnostic,event='inference_completed',timings=response.timings,
                              input_tokens=response.input_tokens,output_tokens=response.output_tokens)),file=sys.stderr,flush=True)
        if (response.served_provider,response.served_model) != (expected_provider,expected_model):
            raise RuntimeError(
                'serving identity changed; revision must pin the concrete served provider/model '
                f'(expected {expected_provider}:{expected_model}, got '
                f'{response.served_provider}:{response.served_model})')
        try:
            body=json.loads(response.text,object_pairs_hook=unique_json_object)
            wire_body=deepcopy(body)
            if stage == 'extract' and isinstance(body,dict):
                # B.5: saturation is a property of what the model emitted, tracked per
                # list, before materialize's structural rejections can shrink any of them.
                payload['_proposed_counts']=dict(
                    names=len(body.get('names') or []),attributes=len(body.get('attributes') or []),
                    relations=len(body.get('relations') or []),occurrences=len(body.get('occurrences') or []))
            elif stage == 'align' and isinstance(body,dict) and isinstance(body.get('alignments'),list):
                payload['_proposed_count']=len(body['alignments'])
            parsed = materialize(body)
        except Exception as exc:
            print(json.dumps(dict(diagnostic,event='contract_rejected',
                                  error_type=type(exc).__name__,error=str(exc))),
                  file=sys.stderr,flush=True)
            raise
        runtime_metrics = dict(response.timings,input_tokens=response.input_tokens,
                               output_tokens=response.output_tokens,stage=stage,
                               batch_id=diagnostic['batch_id'],prompt_metrics=prompt_metrics,
                               stall_retries=attempt,
                               **({'proposed_count':payload['_proposed_count']}
                                  if '_proposed_count' in payload else {}),
                               **({'proposed_counts':payload['_proposed_counts']}
                                  if '_proposed_counts' in payload else {}))
        try:
            await self._optional_exec('''INSERT INTO completion_cache
                (novel_id,cache_key,served_provider,served_model,response,elapsed_seconds,
                 runtime_metrics,stage,chapter_index,requested_provider,requested_model,
                 stage_prompt_version,prompt_digest,schema_digest)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(novel_id,cache_key,served_provider,served_model)
                DO UPDATE SET response=EXCLUDED.response,
                              elapsed_seconds=EXCLUDED.elapsed_seconds,
                              runtime_metrics=EXCLUDED.runtime_metrics,
                              stage=EXCLUDED.stage,
                              chapter_index=EXCLUDED.chapter_index,
                              requested_provider=EXCLUDED.requested_provider,
                              requested_model=EXCLUDED.requested_model,
                              stage_prompt_version=EXCLUDED.stage_prompt_version,
                              prompt_digest=EXCLUDED.prompt_digest,
                              schema_digest=EXCLUDED.schema_digest,
                              last_used_at=now()''',
                (novel_id,key,response.served_provider,response.served_model,
                 Jsonb(wire_body),time.monotonic()-started,Jsonb(runtime_metrics),
                 stage,self.current_chapter,self.provider_id,self.model,
                 stage_prompt_version,prompt_hash,schema_hash))
        except Exception as exc:
            print(json.dumps(dict(event='completion_cache_write_failed',
                                  revision=self.revision.get('id'),stage=stage,
                                  cache_key=key,error_type=type(exc).__name__,
                                  error=str(exc))),file=sys.stderr,flush=True)
        await self._record_cache_run(
            cache_key=key,served_provider=response.served_provider,
            served_model=response.served_model,stage=stage,
            batch_id=diagnostic['batch_id'],requested_provider=self.provider_id,
            requested_model=self.model,stage_prompt_version=stage_prompt_version,
            prompt_digest=prompt_hash,schema_digest=schema_hash)
        await self._proposal_activity(stage,parsed)
        await self._worker_progress(stage)
        return parsed

    @staticmethod
    def _passages_in_range(source: str, lo: int, hi: int, *,
                           max_chars: int = EXTRACT_PASSAGE_CHARS) -> list[dict]:
        """Paragraph-sized citable units inside one extract window (adjacency granularity).

        B.5: EXTRACT_WINDOW_CHARS bounds how much context one call sees; this bounds
        what it may cite, exactly as the old claims stage split focus batches from
        antecedent context -- see _extract_window.
        """
        return [p for p in source_passages(source,max_chars=max_chars,overlap=0)
                if lo<=p['char_start'] and p['char_end']<=hi]

    @staticmethod
    def _mention_passages(source: str, mentions: list[dict]) -> list[str]:
        result=[]
        for p in PassageContract(source).passages:
            if any(p['char_start']<=m.get('char_start',-1)<p['char_end'] for m in mentions):
                result.append(p['id'])
        return result

    async def _resolve_incremental(self, source, mentions, candidates, ontology):
        """Carry only verified representatives across bounded requests (§0)."""
        verified=[];rejected=[];count=0
        by_mid={m['id']:m for m in mentions}
        entity_context={c['id']:c for cs in candidates.values() for c in cs}

        # B.6 two-tier identity proposals: a mention whose retrieval returned exactly
        # one same-kind candidate may skip the identity_slots model call, but the
        # resulting decision is still an untrusted proposal -- it goes through the
        # identical validate_proposals + semantic verifier path as every
        # model-proposed decision below. Judgment call: candidates_for's merged
        # exact+vector output has no separate "exact alias" marker, so "sole
        # same-kind retrieved candidate" is this engine's approximation of "sole
        # exact-alias candidate"; worth tightening if candidates_for starts
        # reporting hit provenance (see report).
        fast_ids=set()
        if mentions:
            fast_proposals=[]
            for m in mentions:
                same_kind=[c for c in candidates.get(m['id'],[]) if c['kind']==m['kind']]
                if len(same_kind)==1:
                    fast_ids.add(m['id'])
                    fast_proposals.append(Decision(mention_id=m['id'],outcome='existing',
                        target_id=same_kind[0]['id'],quote=m['quote'],
                        evidence_start=m.get('context_start',m['char_start']),
                        reason='sole retrieved candidate of matching kind'))
            if fast_proposals:
                good,no=validate_proposals(source,mentions,candidates,
                    Proposals(decisions=fast_proposals,claims=[]),ontology)
                rejected.extend(no)
                for i,item in enumerate(good): item['id']=f'identity:fast:{i}'
                verdicts=await self._verify_items(source,good,'identity',mentions=mentions,
                    candidates=candidates,batch_identity='identity-fast-path')
                good,no=approved(good,verdicts)
                rejected.extend(no)
                verified.extend(good);count+=len(fast_proposals)
        mentions=[m for m in mentions if m['id'] not in fast_ids]

        start=0;batch_limit=self.identity_batch_size
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
                """Build one candidate request. Pure: `added` is applied only if sent."""
                batch=mentions[start:start+size]
                added={}
                def visible(mid):
                    return candidates[mid]+added.get(mid,[])
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
                        # A prior "existing" target must also become a retrieval candidate
                        # or validate_proposals would reject the very choice being offered.
                        # Only the chosen size may apply this; a sizing probe that mutated
                        # `candidates` would leak into batches it never sent.
                        if d['outcome']=='existing' and not any(
                                c['id']==d['target_id'] for c in visible(m['id'])):
                            added.setdefault(m['id'],[]).append(entity_context[d['target_id']])
                    for c in visible(m['id']):
                        if c['kind']!=m['kind']: continue
                        token=target_ref('existing',c['id'])
                        key='existing:'+token
                        choices[key]=('existing',token,'existing_evidence')
                        descriptions[key]={k:v for k,v in entity_context[c['id']].items() if k!='id'}
                    rows.append(dict(occurrence_ref=refs[m['id']],subject=self._subject_context(m),
                        choices=choices,choice_context=descriptions))
                payload=dict(source=source,ontology=ontology,identity_occurrences=rows,
                    _passage_ids=self._mention_passages(source,list(offered_mentions.values())))
                return batch,refs,targets,rows,payload,added

            # Adding an occurrence adds both its own row and a choice on every same-kind
            # row already present, so serialized size is monotone in `size`. That makes the
            # largest fitting batch a binary search instead of a decrement-and-rebuild scan.
            requested=min(batch_limit,len(mentions)-start)
            def fits(prepared):
                return (self._identity_request_size(source,prepared[4])['request_material_bytes']
                        <=self.identity_soft_bytes)
            chosen=prepare(requested);size=requested
            if requested>1 and not fits(chosen):
                # `low` is the largest size known to fit (1 is the floor and always sent,
                # since a single occurrence cannot be divided further); `high` never fits.
                low,high=1,requested;chosen=prepare(1)
                while low+1<high:
                    middle=(low+high)//2
                    probe=prepare(middle)
                    if fits(probe): low,chosen=middle,probe
                    else: high=middle
                size=low
            # Keep the established metric: how many occurrences the packer had to drop.
            self._identity_context_splits+=requested-size
            batch,refs,targets,rows,request,added=chosen
            for mid,extra in added.items():
                candidates[mid].extend(extra)
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
        # B.6 kept this verifier for identity only; 'claims'/'alignment' contracts and
        # the old 'name eligibility'/name_verify stage are gone.
        by_mid={m['id']:m for m in mentions or []}
        entities={c['id']:c for rows in (candidates or {}).values() for c in rows}
        batch_size=12
        # Verdicts are per-item and no batch reads another's result, so the batches fan
        # out and their verdicts are concatenated in batch order.
        async def check(start):
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
            checked=await self.call('verify',Verification,dict(source=source,items=wire,
                contract=contract_name,display_contexts=[],_passage_ids=passage_ids,
                _batch_id=f'verify-{batch_identity or contract_name}-{start//batch_size+1}'))
            by_ref={v.id:v for v in checked.verdicts}
            if len(checked.verdicts)!=len(batch) or set(by_ref)!=set(refs.values()):
                raise ValueError('verification must contain one unique verdict per offered item')
            return [Verdict(id=item['id'],supported=by_ref[refs[item['id']]].supported,
                            reason=by_ref[refs[item['id']]].reason) for item in batch]
        verdicts=await self._fan_out([functools.partial(check,start)
                                      for start in range(0,len(items),batch_size)])
        return Verification(verdicts=[v for batch_verdicts in verdicts for v in batch_verdicts])

    async def _align_window(self, novel: str, chapter: int, source: str, display: str,
                            mentions: list[dict], lo: int, hi: int, display_identity: str
                            ) -> list[dict]:
        """Align one translated window, splitting it if the model saturates the alignment limit.

        A name that recurs often in one window (a recurring protagonist, say) forces the
        model to emit one alignment object per occurrence with nothing capping the count
        -- unbounded, that reliably exhausted num_predict and truncated the whole response
        into unparseable partial JSON, failing the chapter over a call that was never
        stuck, just asked to do too much at once (§0: never publish a partial extraction).
        Detect the model returned exactly the capped amount, halve the window, and
        recurse for the remainder (B.5's saturation-and-bisection net, kept for align
        per B.6: "keep the call itself; its verification is dropped").
        """
        window=display[lo:hi]
        if not window.strip() or not mentions:
            return []
        request=dict(source=source,translation=window,mentions=mentions,
            _passage_ids=self._mention_passages(source,mentions),_batch_id=f'align-{lo}-{hi}',
            _limits=dict(alignments=self.alignment_limit))
        alignment=await self.call('align',Alignments,request)
        proposed=request.get('_proposed_count',len(alignment.alignments))
        if proposed>=self.alignment_limit and len(mentions)>1 and (hi-lo)>MIN_ALIGNMENT_CHARS:
            self._alignment_subdivisions=getattr(self,'_alignment_subdivisions',0)+1
            mid_display=lo+(hi-lo)//2
            mid_mentions=len(mentions)//2
            left=await self._align_window(novel,chapter,source,display,
                mentions[:mid_mentions],lo,mid_display,display_identity)
            right=await self._align_window(novel,chapter,source,display,
                mentions[mid_mentions:],mid_display,hi,display_identity)
            return left+right
        if proposed>=self.alignment_limit:
            # Smallest window reached and it's still saturated: keep what verified rather
            # than failing the chapter, the same fallback extraction saturation uses.
            await self._activity('run','run','rejected',dict(
                reason='alignment coverage failure: saturated window could not be subdivided further',
                window_chars=hi-lo,mentions=len(mentions)))
        return aligned_mentions(novel,chapter,source,window,mentions,alignment,
            display_offset=lo,display_identity=display_identity)

    @staticmethod
    def _merge_extract(a: dict, b: dict) -> dict:
        return dict(names=a['names']+b['names'],attributes=a['attributes']+b['attributes'],
                    relations=a['relations']+b['relations'],occurrences=a['occurrences']+b['occurrences'])

    async def _extract_window(self, source: str, ontology: dict, lo: int, hi: int
                              ) -> tuple[dict,list[dict]]:
        """One 'extract' call over ``[lo,hi)``, the B.1/B.4/B.5 merged proposal pass.

        Bisects on a prompt-budget error the same way identity/align do, and again
        whenever ANY of the four lists saturates its cap (B.5: tracked per list,
        never just the busiest one). The floor is MIN_EXTRACT_WINDOW_CHARS; a still-
        saturated response at the floor is kept rather than discarded (a coverage
        failure, logged, not silent loss -- same discipline as _align_window).
        """
        empty=dict(names=[],attributes=[],relations=[],occurrences=[])
        passages=self._passages_in_range(source,lo,hi)
        if not passages:
            return empty,[]
        window_id=f'extract-{lo}-{hi}'
        request=dict(source=source,ontology=ontology,_passage_ids=[p['id'] for p in passages],
            _passage_max_chars=EXTRACT_PASSAGE_CHARS,_passage_overlap=0,_batch_id=window_id,
            _limits=self.extract_limits)

        async def split():
            self._extract_context_splits=getattr(self,'_extract_context_splits',0)+1
            mid=lo+(hi-lo)//2
            (got1,no1),(got2,no2)=await self._fan_out([
                functools.partial(self._extract_window,source,ontology,lo,mid),
                functools.partial(self._extract_window,source,ontology,mid,hi)])
            return self._merge_extract(got1,got2),no1+no2

        try:
            response=await self.call('extract',ExtractProposals,request)
        except ValueError as exc:
            if not _is_prompt_budget_error(exc) or (hi-lo)<=MIN_EXTRACT_WINDOW_CHARS:
                raise
            return await split()
        counts=request.get('_proposed_counts',dict(
            names=len(response['names']),attributes=len(response['attributes']),
            relations=len(response['relations']),occurrences=len(response['occurrences'])))
        saturation=getattr(self,'_extract_saturation',
            dict(names=0,attributes=0,relations=0,occurrences=0))
        saturated=[key for key,limit in self.extract_limits.items() if counts.get(key,0)>=limit]
        for key in saturated:
            saturation[key]+=1
        self._extract_saturation=saturation
        if not saturated:
            return response,[]
        if (hi-lo)<=MIN_EXTRACT_WINDOW_CHARS or len(passages)<=1:
            await self._activity('run','run','rejected',dict(
                reason='extract coverage failure: smallest window still saturated',
                window_chars=hi-lo,saturated=saturated))
            return response,[]
        self._extract_subdivisions=getattr(self,'_extract_subdivisions',0)+1
        child,no=await split()
        return self._merge_extract(response,child),no

    async def _merged_extract(self, source: str, ontology: dict) -> tuple[dict,list[dict]]:
        """Split the chapter into overlapping windows and merge/dedupe their proposals.

        Windows overlap by EXTRACT_WINDOW_OVERLAP (B.4) so a boundary-straddling
        assertion is not lost; the same anchored occurrence proposed by two
        overlapping windows collapses via the dedup keys below, since materialize()
        already normalized each response's refs to durable absolute source offsets
        before this function ever combines two responses (B.5).
        """
        empty=dict(names=[],attributes=[],relations=[],occurrences=[])
        if not source.strip():
            return empty,[]
        windows=source_windows(source,max_chars=self.extract_window_chars,overlap=EXTRACT_WINDOW_OVERLAP)
        results=await self._fan_out([
            functools.partial(self._extract_window,source,ontology,w['char_start'],w['char_end'])
            for w in windows])
        merged=dict(empty);rejected=[]
        for got,no in results:
            for key in merged: merged[key]=merged[key]+got[key]
            rejected.extend(no)
        unique_names={}
        for n in merged['names']:
            key=(n['surface'],n['kind'])
            if key not in unique_names or n['evidence_start']<unique_names[key]['evidence_start']:
                unique_names[key]=n
        merged['names']=list(unique_names.values())
        anchor=lambda ref: (ref['char_start'],ref['char_end']) if ref else None
        unique_attrs={}
        for a in merged['attributes']:
            unique_attrs[(anchor(a['subject_ref']),a['attribute'],a['value'])]=a
        merged['attributes']=list(unique_attrs.values())
        unique_rels={}
        for r in merged['relations']:
            unique_rels[(anchor(r['src_ref']),anchor(r['dst_ref']),r['relation'])]=r
        merged['relations']=list(unique_rels.values())
        unique_occ={}
        for o in merged['occurrences']:
            unique_occ[(tuple(anchor(p) for p in o['participant_refs']),o['summary'])]=o
        merged['occurrences']=list(unique_occ.values())
        return merged,rejected

    async def _canonicalize_vocabulary_claims(self, claims: list[Claim], mentions: list[dict]):
        """Resolve admitted aliases and record syntactically valid unknown terms.

        Validation remains deterministic and DB-free in ``evidence.py``; this bounded
        async step owns the novel-scoped lookup and candidate ledger side effects.
        """
        if self.current_chapter is None:
            return claims, {}, []
        novel = await self._resolve_novel_id()
        mention_by_id = {mention['id']: mention for mention in mentions}
        rows = {}
        result=[]
        unknown=[]
        for claim in claims:
            # C.9: occurrences are deliberately not a vocabulary type -- event.summary
            # stays free text, so this whole lookup is skipped for claim.type=='event'.
            if claim.type == 'event':
                result.append(claim)
                continue
            term_type = 'relation' if claim.type == 'relationship' else 'attribute'
            required = 2 if claim.type == 'relationship' else 1
            if len(claim.mention_ids) != required or any(mid not in mention_by_id for mid in claim.mention_ids):
                result.append(claim)
                continue
            resolved = await resolve_vocabulary(self.db, novel, term_type, claim.attribute, self.current_chapter)
            if resolved is None:
                result.append(claim)
                continue
            canonical = resolved['name']
            if resolved['status'] == 'unknown':
                kind = mention_by_id[claim.mention_ids[0]]['kind']
                resolved = dict(resolved, kinds=[kind],
                                dst_kinds=([mention_by_id[claim.mention_ids[1]]['kind']]
                                           if claim.type == 'relationship' and len(claim.mention_ids) > 1
                                           else []))
                unknown.append((claim, term_type, canonical))
            rows[(term_type, canonical)] = resolved
            if canonical != claim.attribute:
                claim = claim.model_copy(update={'attribute': canonical})
            result.append(claim)
        return result, rows, unknown

    async def _record_accepted_vocabulary_candidates(self, accepted, unknown, mentions):
        mention_by_id={m['id']:m for m in mentions}
        for claim, term_type, name in unknown:
            match=next((item for item in accepted
                        if item.get('type') == claim.type and
                           item.get('attribute') == name and
                           item.get('mention_ids') == claim.mention_ids and
                           item.get('value') == claim.value and
                           item.get('quote') == claim.quote and
                           item.get('evidence_start') == claim.evidence_start), None)
            if not match or any(mid not in mention_by_id for mid in claim.mention_ids):
                continue
            await record_candidate(self.db, await self._resolve_novel_id(), term_type, name,
                                   mention_by_id[claim.mention_ids[0]]['kind'], self.current_chapter,
                                   revision_id=self.revision['id'], evidence=dict(value=claim.value),
                                   dst_kind=(mention_by_id[claim.mention_ids[1]]['kind']
                                             if claim.type == 'relationship' else None))

    @staticmethod
    def _filter_description_claims(claim_items: list[dict]) -> tuple[list[dict], list[dict], bool]:
        """Reject same-window description duplicates and report the run warning."""
        norm=lambda value: ' '.join(str(value).strip().split()).casefold()
        specific = {(tuple(item['mention_ids']), norm(item['value']), item.get('evidence_start'))
                    for item in claim_items if item['type']=='fact' and item['attribute']!='description'}
        filtered=[]; rejected=[]
        for item in claim_items:
            key=(tuple(item['mention_ids']),norm(item['value']),item.get('evidence_start'))
            if item['type']=='fact' and item['attribute']=='description' and any(
                    ids==key[0] and value==key[1] and abs((start or 0)-(key[2] or 0))<1200
                    for ids,value,start in specific):
                rejected.append(dict(item,rejection='description duplicates a specific attribute claim'))
            else:
                filtered.append(item)
        descriptions=sum(item['type']=='fact' and item['attribute']=='description' for item in filtered)
        total_facts=sum(item['type']=='fact' for item in filtered)
        return filtered,rejected,bool(total_facts and descriptions*2>total_facts)

    async def candidates_for(self, chapter: int, mentions: list[dict]) -> tuple[dict[str,list[dict]],dict[str,list[float]]]:
        """Retrieve an occurrence-local, revision/kind/chapter filtered allowlist."""
        if not mentions:
            return {},{}
        await register_vector_async(self.db)
        vectors=await self.embedder.embed([m['quote'] for m in mentions])
        if len(vectors)!=len(mentions) or any(len(v)!=self.cfg.embed_dim for v in vectors):
            raise ValueError('unexpected entity embedding count or dimension')
        result={}
        novel_id = await self._resolve_novel_id()
        source_lang = (await (await self.db.execute(
            'SELECT source_lang FROM novel WHERE id=%s',(novel_id,)
        )).fetchone())[0]
        # One statement per retrieval step instead of per occurrence. The old shape cost
        # two queries per mention plus one per candidate -- roughly 400 round trips for a
        # 40-occurrence chapter -- to compute a result that is one set-returning join.
        # Ordering is made explicit (entity_id, then distance) rather than inherited from
        # LATERAL row order, which Postgres does not guarantee.
        rid=self.revision['id']
        rows=[(index,m['kind'],m['surface']) for index,m in enumerate(mentions)]
        placeholders=','.join(['(%s::int,%s::text,%s::text)']*len(rows))
        exact_hits={}
        for index,eid,kind,canonical in await (await self.db.execute(f'''
            SELECT q.idx,c.entity_id,c.kind,c.canonical
            FROM (VALUES {placeholders}) AS q(idx,kind,surface)
            CROSS JOIN LATERAL (
                SELECT DISTINCT e.id::text AS entity_id,e.kind,e.canonical
                FROM entity e JOIN alias a ON a.entity_id=e.id AND a.revision_id=e.revision_id
                WHERE e.revision_id=%s AND e.kind=q.kind AND e.first_seen_chapter<%s
                AND a.surface=q.surface AND a.lang=%s AND a.first_seen_chapter<%s
                ORDER BY entity_id LIMIT %s) c''',
            [value for row in rows for value in row]
            +[rid,chapter,source_lang,chapter,CANDIDATE_LIMIT])).fetchall():
            exact_hits.setdefault(index,[]).append((eid,kind,canonical))
        for hits in exact_hits.values():
            hits.sort(key=lambda hit:hit[0])

        vector_rows=[(index,m['kind'],Vector(v))
                     for index,(m,v) in enumerate(zip(mentions,vectors))]
        placeholders=','.join(['(%s::int,%s::text,%s::vector)']*len(vector_rows))
        dense_hits={}
        for index,eid,kind,canonical,distance in await (await self.db.execute(f'''
            SELECT q.idx,c.entity_id,c.kind,c.canonical,c.distance
            FROM (VALUES {placeholders}) AS q(idx,kind,vec)
            CROSS JOIN LATERAL (
                SELECT e.id::text AS entity_id,e.kind,e.canonical,e.embedding <=> q.vec AS distance
                FROM entity e WHERE e.revision_id=%s AND e.kind=q.kind
                AND e.first_seen_chapter<%s AND e.embedding IS NOT NULL
                ORDER BY e.embedding <=> q.vec LIMIT %s) c''',
            [value for row in vector_rows for value in row]
            +[rid,chapter,CANDIDATE_LIMIT])).fetchall():
            dense_hits.setdefault(index,[]).append((distance,eid,kind,canonical))
        for hits in dense_hits.values():
            # Stable, so ties keep the order the index returned them in.
            hits.sort(key=lambda hit:hit[0])

        for index,mention in enumerate(mentions):
            ordered=[];seen=set()
            for eid,kind,canonical in (exact_hits.get(index,[])
                                       +[hit[1:] for hit in dense_hits.get(index,[])]):
                if eid not in seen and len(ordered)<CANDIDATE_LIMIT:
                    ordered.append(dict(id=eid,kind=kind,canonical=canonical));seen.add(eid)
            result[mention['id']]=ordered

        # Prior source context depends only on the entity, so ask once per distinct
        # candidate rather than once per (occurrence, candidate) pair.
        entity_ids=sorted({c['id'] for rows_ in result.values() for c in rows_})
        contexts={}
        if entity_ids:
            placeholders=','.join(['(%s::uuid)']*len(entity_ids))
            for eid,chapter_index,quote in await (await self.db.execute(f'''
                SELECT q.eid::text,v.chapter_index,v.quote
                FROM (VALUES {placeholders}) AS q(eid)
                CROSS JOIN LATERAL (
                    SELECT v2.chapter_index,v2.quote
                    FROM alias a JOIN graph_evidence v2 ON v2.id=a.evidence_id
                    WHERE a.entity_id=q.eid AND a.revision_id=%s AND v2.revision_id=%s
                    AND a.first_seen_chapter<%s AND v2.chapter_index<%s
                    ORDER BY v2.chapter_index DESC LIMIT 2) v''',
                [*entity_ids,rid,rid,chapter,chapter])).fetchall():
                contexts.setdefault(eid,[]).append((chapter_index,quote))
        for rows_ in result.values():
            for candidate in rows_:
                # A fresh list per candidate: two occurrences can share an entity, and
                # _resolve_incremental hands these dicts on to per-occurrence choices.
                candidate['source_context']=[dict(chapter=c,quote=q) for c,q in
                    sorted(contexts.get(candidate['id'],[]),key=lambda row:-row[0])]
        return result,{m['id']:v for m,v in zip(mentions,vectors)}

    @staticmethod
    def _evidence_for_citation(source: str, passage_ids: list[str]) -> dict | None:
        """Union-span evidence for a validated extract citation (A.6 discipline).

        passages.materialize's 'extract' branch validates and normalizes citations
        (adjacency, offered-ness) but does not itself attach quote/evidence_start to
        attribute/relation/occurrence items -- unlike names[], which cites a single
        passage resolved via PassageContract.resolve. This recomputes the same
        deterministic paragraph split (EXTRACT_PASSAGE_CHARS/no overlap) used to
        build the citations in the first place, so passage IDs decode back to exact
        offsets without re-running a live PassageContract for a call already returned.
        """
        by_id={p['id']:p for p in source_passages(source,max_chars=EXTRACT_PASSAGE_CHARS,overlap=0)}
        rows=[by_id[pid] for pid in passage_ids if pid in by_id]
        if len(rows)!=len(passage_ids) or not rows:
            return None
        lo=min(r['char_start'] for r in rows);hi=max(r['char_end'] for r in rows)
        return dict(quote=source[lo:hi],evidence_start=lo)

    def _anchor_extract_claims(self, source: str, proposals: dict, mentions: list[dict],
                               verified_ids: set[str]) -> tuple[list[Claim],list[dict]]:
        """B.2's anchored-subject rule: an occurrence reference only becomes a claim

        subject when it resolves to one of THIS chapter's verified mentions.
        ``mentions``/``verified_ids`` must already be restricted to independently
        verified occurrences (identity resolution runs before this is called, per
        B.6's extract -> identity -> identity-verify -> align order) -- an anchor
        landing on an unresolved or rejected occurrence is simply absent from
        ``verified_ids`` below.
        """
        by_span={(m['char_start'],m['char_end']):m['id'] for m in mentions}
        def resolve(ref):
            return by_span.get((ref['char_start'],ref['char_end'])) if ref else None
        claims=[];rejected=[]
        for item in proposals['attributes']:
            mid=resolve(item['subject_ref'])
            ev=self._evidence_for_citation(source,item['passage_ids'])
            if mid is None or mid not in verified_ids:
                rejected.append(dict(item,rejection='attribute subject is not a verified occurrence in this chapter'))
                continue
            if ev is None:
                rejected.append(dict(item,rejection='attribute citations could not be resolved to evidence'))
                continue
            claims.append(Claim(type='fact',mention_ids=[mid],attribute=item['attribute'],
                value=item['value'],value_en=item.get('value_en') or '',**ev))
        for item in proposals['relations']:
            src=resolve(item['src_ref']);dst=resolve(item['dst_ref'])
            ev=self._evidence_for_citation(source,item['passage_ids'])
            if src is None or dst is None or src not in verified_ids or dst not in verified_ids:
                rejected.append(dict(item,rejection='relationship subject is not a verified occurrence in this chapter'))
                continue
            if ev is None:
                rejected.append(dict(item,rejection='relationship citations could not be resolved to evidence'))
                continue
            claims.append(Claim(type='relationship',mention_ids=[src,dst],attribute=item['relation'],
                value='',sentiment=item.get('sentiment'),**ev))
        for item in proposals['occurrences']:
            ev=self._evidence_for_citation(source,item['passage_ids'])
            if ev is None:
                rejected.append(dict(item,rejection='occurrence citations could not be resolved to evidence'))
                continue
            # An occurrence may legitimately name no verified participant (B.6:
            # "the sect's inner disciples number three hundred" demotes here with no
            # anchored subject); drop only participants that failed verification.
            participants=[mid for mid in (resolve(ref) for ref in item['participant_refs'])
                         if mid is not None and mid in verified_ids]
            claims.append(Claim(type='event',mention_ids=participants,attribute='',
                value=item['summary'],value_en=item.get('summary_en') or '',**ev))
        return claims,rejected

    async def discover_names(self, novel: str, chapter: int, source: str):
        """B.6: names[] now comes from the merged 'extract' pass, not a separate stage.

        There is no more model eligibility check (name_verify is dropped -- B.1: "names[]
        is a proposal inventory... must be reviewed/validated" is now Phase D's human
        review, not a second model call). ``source_mentions`` still does its own regex
        scan of every literal occurrence across the whole chapter to build durable,
        stable-ID anchors -- the same discipline as before B.6, just fed a differently
        sourced names[] list.
        """
        ontology=self.revision['ontology']
        proposals,rejected=await self._merged_extract(source,ontology)
        self._extract_proposals=proposals
        # Duck-typed in place of pydantic Names/Name (evidence.py): extract's names[]
        # citations are whole EXTRACT_PASSAGE_CHARS-sized passages, not the 400-char
        # legacy cap those pydantic models used to enforce.
        names_ns=SimpleNamespace(names=[SimpleNamespace(
            named=True,surface=n['surface'],kind=n['kind'],quote=n['quote'],
            evidence_start=n['evidence_start']) for n in proposals['names']])
        mentions=source_mentions(novel,chapter,source,names_ns,ontology)
        coverage={kind:dict(proposed_surfaces=len({n['surface'] for n in proposals['names'] if n['kind']==kind}),
                            source_occurrences=sum(m['kind']==kind for m in mentions)) for kind in ontology['kinds']}
        return mentions,coverage,rejected

    def _runtime_diagnostics(self, source: str, display: str) -> dict:
        """Metrics needed to compare chunk policy across chapter sizes (§5.4)."""
        hosted=getattr(self,'hosted',False)
        requests=dict(getattr(self,'_stage_requests',{}))
        cache_hits=dict(getattr(self,'_stage_cache_hits',{}))
        stages=set(requests)|set(cache_hits)
        return dict(
            input_size=dict(source_chars=len(source),source_bytes=len(source.encode()),
                            display_chars=len(display),display_bytes=len(display.encode())),
            # B.4: the four-call target's shape -- one merged 'extract' pass per window,
            # bounded identity proposals, one semantic verifier, one align pass.
            chunk_policy=dict(extract_window_chars=self.extract_window_chars,
                extract_window_overlap=EXTRACT_WINDOW_OVERLAP,
                extract_passage_chars=EXTRACT_PASSAGE_CHARS,
                extract_min_window_chars=MIN_EXTRACT_WINDOW_CHARS,
                extract_limits=dict(self.extract_limits),
                identity_occurrences_per_batch=self.identity_batch_size,
                identity_verification_items_per_batch=12,
                alignment_display_chars=self.align_window_chars,
                alignments_per_response=self.alignment_limit,alignment_min_chars=MIN_ALIGNMENT_CHARS,
                prompt_hard_bytes=getattr(self,'prompt_hard_bytes',PROMPT_HARD_BYTES)),
            extract_chunking=dict(subdivisions=getattr(self,'_extract_subdivisions',0),
                context_splits=getattr(self,'_extract_context_splits',0),
                saturation=dict(getattr(self,'_extract_saturation',
                    dict(names=0,attributes=0,relations=0,occurrences=0)))),
            alignment_subdivisions=getattr(self,'_alignment_subdivisions',0),
            identity_chunking=dict(batch_sizes=list(getattr(self,'_identity_batch_sizes',[])),
                                   context_splits=getattr(self,'_identity_context_splits',0),
                                   soft_prompt_bytes=self.identity_soft_bytes,
                                   hard_prompt_bytes=self.identity_hard_bytes),
            prompt_metrics=list(getattr(self,'_prompt_metrics',[])),
            stage_requests=requests,stage_cache_hits=cache_hits,
            stage_fresh_calls={stage:requests.get(stage,0)-cache_hits.get(stage,0)
                               for stage in sorted(stages)})

    async def extract(self, novel: str, chapter: int, source: str, display: str, target: str,
                      *, include_terms: bool = True, include_facts: bool = True) -> dict:
        """B.4/B.6 call sequence for an unsplit chapter: extract, identity, identity-
        verify, align -- four calls, "only when each fits" (bisection is a net, not
        the normal path). ``target`` is accepted for interface compatibility with the
        old render pass; value_en now comes directly off the extract schema (B.6),
        so no separate target-language call happens here.
        """
        await self._ensure_run(novel,chapter,source,display)
        self._stage_requests={};self._stage_cache_hits={};self._prompt_metrics=[]
        self._extract_subdivisions=0;self._extract_context_splits=0
        self._alignment_subdivisions=0
        ontology = self.revision['ontology']
        mentions,coverage,name_rejected = await self.discover_names(novel,chapter,source)
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
                name_coverage=coverage,rejected=name_rejected+[dict(rejection='No source-valid named mentions; no identity or fact publication attempted.')],
                source_hash=digest(source),display_hash=digest(display))
        candidates,mention_vectors = await self.candidates_for(chapter,mentions)
        verified_identities,rejected,proposed_identity_count=await self._resolve_incremental(
            source,mentions,candidates,ontology)
        rejected=name_rejected+rejected

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

        # B.2's anchored-subject rule: an attribute/relation/occurrence's subject
        # only becomes a claim when its anchor resolves to a verified occurrence.
        # This prevents a failed identity batch from poisoning or erasing otherwise
        # valid claim evidence -- same discipline the old per-focus claims stage had
        # by only offering verified_mentions, now applied post-hoc since B.6 extracts
        # attributes/relations/occurrences in the SAME call as names, before identity
        # resolution runs.
        verified_occurrence_ids={item['mention_id'] for item in verified_identities}
        all_claims=[];proposed_claim_count=0
        if include_facts:
            proposals=getattr(self,'_extract_proposals',
                dict(names=[],attributes=[],relations=[],occurrences=[]))
            proposed_claim_count=len(proposals['attributes'])+len(proposals['relations'])+len(proposals['occurrences'])
            all_claims,anchor_rejected=self._anchor_extract_claims(
                source,proposals,mentions,verified_occurrence_ids)
            rejected.extend(anchor_rejected)
        # Exact application-owned evidence offsets make duplicate elimination
        # deterministic without using names as keys (overlapping extract windows can
        # propose the identical anchored assertion twice).
        unique_claims={}
        for claim in all_claims:
            key=(claim.type,tuple(claim.mention_ids),claim.attribute,claim.value,claim.evidence_start,claim.quote)
            unique_claims[key]=claim
        all_claims=list(unique_claims.values())
        all_claims, vocabulary_rows, unknown_vocabulary = await self._canonicalize_vocabulary_claims(
            all_claims, mentions)
        claim_items,claim_rejected=validate_proposals(
            source,mentions,candidates,Proposals(decisions=[],claims=all_claims),ontology,
            vocabulary=vocabulary_rows)
        await self._record_accepted_vocabulary_candidates(claim_items, unknown_vocabulary, mentions)
        # ``description`` remains admitted, but a same-window duplicate of a more
        # specific assertion is noise. Keep the complete proposal in the activity log.
        claim_items, description_rejected, description_warning = self._filter_description_claims(claim_items)
        for item in description_rejected:
            rejected.append(item)
            await self._activity('fact',item['id'],'rejected',item)
        rejected.extend(claim_rejected)
        if description_warning:
            descriptions=sum(item['type']=='fact' and item['attribute']=='description' for item in claim_items)
            total_facts=sum(item['type']=='fact' for item in claim_items)
            await self._activity('run','run','rejected',dict(
                warning='description exceeds 50% of facts',severity='warning',
                descriptions=descriptions,total_facts=total_facts))
        # B.6 drops fact_verify/fact_review/render/render_verify entirely: structural
        # validation above plus human review (Phase D) replace the model verifier as
        # the semantic approval boundary. Every accepted claim is already a durable,
        # anchored, evidence-bound proposal; value_en came off the extract schema.
        verified_claims=claim_items if include_facts else []
        spans=[]
        # Bound both translated prose and source occurrences. Windows deliberately
        # fail closed at boundaries; they never manufacture a global offset.
        window_count=max(1,math.ceil(len(display)/self.align_window_chars),
                         math.ceil(len(mentions)/max(1,self.alignment_limit*2))) if include_terms else 0
        display_identity=digest(display)
        def window(i):
            lo=len(display)*i//window_count;hi=len(display)*(i+1)//window_count
            mlo=len(mentions)*i//window_count;mhi=len(mentions)*(i+1)//window_count
            return functools.partial(self._align_window,novel,chapter,source,display,
                mentions[mlo:mhi],lo,hi,display_identity)
        # Windows cover disjoint display ranges and disjoint occurrences, so they fan out;
        # extending in window order keeps the by_span last-writer-wins below deterministic.
        for window_spans in await self._fan_out([window(i) for i in range(window_count)]):
            spans.extend(window_spans)
        by_span={}
        for span in spans:
            key=(span['char_start'],span['char_end'])
            if key in by_span and by_span[key]['mention_id']!=span['mention_id']:
                span['mention_id']=span['quote']=None
            by_span[key]=span
        spans=sorted(by_span.values(),key=lambda s:s['char_start'])
        # B.6: "keep the call itself; its verification is dropped." aligned_mentions()
        # already requires literal evidence (evidence.passage) before it ever assigns
        # a mention_id, so an unsupported or ambiguous alignment already surfaces as
        # an unlinked, still-clickable card -- the semantic double-check this used to
        # add is what's gone, not the literal citation discipline.
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
                # B.2.5: the assertion-evidence ledger. novel_assertion_evidence's PK
                # includes chapter_index, so a re-run of this chapter cannot add a
                # second vote for the same (entity, attribute, assertion) -- load-
                # bearing for corroboration never self-corroborating (§0.7). This is
                # write-only here; consuming it for bulk-eligibility is Phase D.
                assertion_signature = digest(' '.join(str(item['value']).strip().split()).casefold())
                try:
                    await self._optional_exec('''INSERT INTO novel_assertion_evidence
                        (novel_id,revision_id,entity_id,attribute,assertion_signature,chapter_index,
                         valid_from_chapter,evidence_id,source_hash,value,review_flag)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                        (novel,revision,ids[0],item['attribute'],assertion_signature,chapter,
                         chapter,ev,digest(source),item['value'],
                         ','.join(item.get('review_flags') or []) or None))
                except Exception as exc:
                    print(json.dumps(dict(event='assertion_evidence_write_failed',
                                          revision=revision,chapter=chapter,
                                          error_type=type(exc).__name__,error=str(exc))),
                          file=sys.stderr,flush=True)
            elif item['type']=='relationship':
                await self.db.execute('''INSERT INTO edge(novel_id,src_id,dst_id,rel_type,sentiment,
                    valid_from_chapter,source_chapter,revision_id,evidence_id,claim_key)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (novel,*ids,item['attribute'],item.get('sentiment'),chapter,chapter,revision,ev,key))
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
