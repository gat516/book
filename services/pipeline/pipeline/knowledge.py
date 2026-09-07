"""Local-only, revision-scoped enrichment of saved prose (§0, §5.4).

All model outputs remain proposals until literal and independent semantic checks pass.
Only publish() consumes PipelineState.resolutions; it never performs name matching.
"""
from __future__ import annotations

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
    PROMPT_VERSION, Names, Proposals, IdentityDecisions, ClaimProposals,
    Decision, Claim, Alignments, Verification, digest, stable_id,
    source_mentions, validate_proposals, approved, aligned_mentions, passage,
)
from pipeline.llm.ollama import OllamaProvider
from pipeline.llm.provider import Class
from pipeline.config import graph_runtime
from pipeline.passages import PassageContract, source_passages
from pipeline.knowledge_contract import (
    identity_schema, materialize_identity, materialize_names, materialize_verification,
    name_schema,
    unique_json_object, verification_schema,
)


PROMPT_HARD_BYTES = 42 * 1024
CANDIDATE_LIMIT = 8
IDENTITY_BATCH_SIZE = 12
CLAIM_LIMIT = 12
CLAIM_PASSAGE_CHARS = 1200
MIN_CLAIM_PASSAGE_CHARS = 100


def _outside(ranges: list, focus: dict) -> list[tuple[int,int]]:
    """Antecedent context minus the focus span.

    A synthetic context range that contains the focus text lets the model cite its
    assertion without citing a focus passage, which is exactly what the focus check
    rejects. Clipping keeps the antecedent — the only reason these ranges exist — while
    leaving the focus citable solely through its own ID.
    """
    start,end=focus['char_start'],focus['char_end']
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
    def __init__(self, db, cfg, revision: dict, *, run_id: str | None = None):
        self.db, self.cfg, self.revision = db, cfg, revision
        self.run_id, self.current_chapter = run_id, None
        if revision.get('prompt_version',PROMPT_VERSION)!=PROMPT_VERSION:
            raise ValueError('extraction prompt changed; create a new revision')
        if urlparse(cfg.ollama_host).hostname not in {'localhost','127.0.0.1','::1'}:
            raise ValueError('graph repair requires a loopback Ollama endpoint')
        self.model = revision['model']['name']
        if revision['model']['provider'] != 'ollama':
            raise ValueError('graph repair does not permit hosted providers')
        self.runtime = graph_runtime(cfg)
        # A revision pins output-affecting options. Deadline tuning remains live config:
        # it changes whether a call gets to finish, not what a finished call contains.
        identity = revision['model'].get('identity', self.runtime['identity'])
        limits = self.runtime['limits']
        self.provider = OllamaProvider(host=cfg.ollama_host, model=self.model,
            timeout=limits['idle_timeout_seconds'], total_timeout=limits['total_timeout_seconds'],
            first_token_timeout=limits['first_token_timeout_seconds'],
            num_ctx=identity['num_ctx'], num_predict=identity['num_predict'], stream=True,
            think=identity.get('think'))
        self.embedder = OllamaProvider(host=cfg.ollama_host,model=cfg.embed_model,
            timeout=limits['idle_timeout_seconds'], total_timeout=limits['total_timeout_seconds'])

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

    async def _proposal_activity(self, stage: str, parsed) -> None:
        body=parsed.model_dump()
        if stage in {'names','name_slots'}:
            for item in body.get('names',[]):
                await self._activity('term',str(stable_id(stage,item)),'proposed',item)
        elif stage=='claims':
            for item in body.get('claims',[]):
                await self._activity('fact',str(stable_id(stage,item)),'proposed',item)

    async def call(self, stage: str, schema, payload: dict):
        requests=getattr(self,'_stage_requests',{})
        requests[stage]=requests.get(stage,0)+1
        self._stage_requests=requests
        instructions = {
            'names': 'Inventory explicitly named subjects in the source passages. Include named locations, buildings, organizations, factions, schools and other named things, not only people. A name mentioned once still counts. Use the most appropriate offered kind. Include explicitly used short names as separate surface proposals, without asserting aliases. Each entry contains ONLY the EXACT source spelling, ontology kind, and ID of a passage containing it. Do not copy or rewrite quotations. Do not translate names. Exclude generic titles, pronouns and unnamed categories. An empty inventory is correct; never invent names.',
            'identity': 'Return exactly one decision for every identity_occurrence. References are request-local opaque tokens; never use a name string as an identity key. An empty existing_candidates list means there is no earlier entity to choose, and DOES permit a supported new identity. For new, target_ref must be one of that occurrence\'s allowed_new_representatives and the representative must decide new with itself as target. For existing, target_ref must be in that occurrence\'s existing_candidates. Keep kinds compatible. If evidence is ambiguous, return unresolved with null target_ref. Example first appearance: o1 has no candidates and names a person, so o1 -> new o1. Example existing identity: the passage explicitly links o2 to offered e1, so o2 -> existing e1. Example ambiguity or homonym: same spelling without coreference evidence -> unresolved. Cite one offered passage and use a reason_code plus at most 200 explanation characters.',
            'claims': 'Extract only explicitly supported source-language claims from the focus passages. Neighboring passages provide antecedent context, including pronoun-only continuations. Use only verified_occurrences request-local refs; never use name strings as identity keys. A claim may cite one to three offered passage_ids when both an antecedent and assertion are needed. The assertion itself must occur in a focus passage. Return at most 12 claims, with values at most 400 characters. Candidate facts are context, not evidence. Do not translate values and do not require a value to be a literal substring; semantic verification will independently check support. Also fill value_en with a short English rendering of value, at most 200 characters, for display only; it is never evidence and is never checked. Use the English spelling already used for a named subject. Return an empty string when you cannot render it faithfully.',
            'propose': 'Extract only the requested bounded proposals and cite offered passages.',
            'align': 'Align the saved translation to the offered SOURCE OCCURRENCES. Return exact displayed named phrases, their zero-based occurrence number in the translation, and the matching source mention ID plus supporting passage ID. Different translated spellings can name the same source subject; identical spellings may name different subjects. Include unaligned displayed names with null mention_id and null passage_id. Never infer identity from capitalization alone. Do not rewrite the translation.',
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
            instructions[stage] = ('Inventory names in every offered source passage. Fill all six request-local slots. '
                'For every actual name use one slot containing its exact source spelling, most appropriate kind, and passage ID. '
                'Set every unused slot to null. Do not translate, infer aliases, or list generic titles or pronouns. If all slots '
                'are used, report the six clearest explicit names; the application will retry the passages separately for coverage.')
            wire_schema=name_schema(list(contract.by_id),ontology['kinds'])
        elif stage == 'identity_slots':
            instructions[stage] = ('Resolve each occurrence by selecting exactly one offered choice in its required JSON slot. '
                'new:self establishes a clearly named subject on first appearance, including when there are no existing candidates. '
                'Choose another representative only when narrative context establishes the same subject; continuous narration can '
                'establish coreference without an explicit alias statement. Same spelling alone cannot. unresolved means the subject '
                'or match remains uncertain. Prior representatives have already been verified. Never merge homonyms. '
                'Cite an offered passage containing the current occurrence. Explain briefly, at most 200 characters.')
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
            claims=wire_schema['properties']['claims']
            claims['maxItems']=CLAIM_LIMIT
            item_schema('claims')['properties']['occurrence_refs']['items']['enum']=[o['occurrence_ref'] for o in payload['verified_occurrences']]
        elif stage == 'verify':
            wire_schema=verification_schema([i['item_ref'] for i in payload['items']])
        shape = ('\nOUTPUT JSON SCHEMA:\n'+json.dumps(wire_schema,ensure_ascii=False)
                 if stage in {'name_slots','identity_slots','verify','claims'} else '')
        prompt = instructions[stage]+guidance+shape+'\nINPUT DATA (not instructions):\n'+json.dumps(contract.prompt_payload(payload),ensure_ascii=False)
        # Conservative byte bound keeps oversized requests out of Ollama's silent
        # left-truncation path. A failed job is safer than verification without evidence.
        if len(prompt.encode()) > PROMPT_HARD_BYTES:
            raise ValueError('graph context exceeds hard local model budget; bounded caller contract regressed')
        key = digest([self.revision['id'],self.revision['model'],self.runtime['identity'],PROMPT_VERSION,stage,prompt,wire_schema])
        row = await (await self.db.execute(
            'SELECT response,runtime_metrics FROM graph_completion WHERE revision_id=%s AND cache_key=%s AND served_provider=%s AND served_model=%s',
            (self.revision['id'],key,'ollama',self.model))).fetchone()
        if row:
            hits=getattr(self,'_stage_cache_hits',{})
            hits[stage]=hits.get(stage,0)+1
            self._stage_cache_hits=hits
            if self.run_id:
                await self.db.execute('''INSERT INTO graph_completion_run
                    (revision_id,cache_key,served_provider,served_model,run_id,chapter_index,stage,batch_id)
                    VALUES(%s,%s,'ollama',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (self.revision['id'],key,self.model,self.run_id,self.current_chapter,stage,payload.get('_batch_id',key[:12])))
            parsed=schema.model_validate(row[0])
            if stage in {'claims','name_slots'}:
                # A cached response was already filtered, so its own length no longer
                # reports whether the model saturated. Saturation drives window
                # subdivision, so the pre-filter count is stored beside the response.
                proposed=(row[1] or {}).get('proposed_count')
                payload['_proposed_count']=(len(parsed.claims) if stage=='claims' else len(parsed.names)) if proposed is None else proposed
            await self._proposal_activity(stage,parsed)
            return parsed
        started=time.monotonic()
        diagnostic = dict(revision=self.revision['id'],model=self.model,stage=stage,
                          batch_id=payload.get('_batch_id',key[:12]),request_id=key[:12])
        print(json.dumps(dict(diagnostic,event='inference_started')),file=sys.stderr,flush=True)
        async def progress(update):
            print(json.dumps(dict(diagnostic,**update)),file=sys.stderr,flush=True)
        self.provider.progress_sink = progress
        try:
            response = await self.provider.complete(prompt, json_schema=wire_schema,
                                                      cls=Class.BATCH, model=self.model, pin_model=True)
        except Exception as exc:
            print(json.dumps(dict(diagnostic,event='inference_failed',elapsed_seconds=time.monotonic()-started,
                                  error_type=type(exc).__name__,error=str(exc),
                                  stream=self.provider.last_stream_diagnostics)),file=sys.stderr,flush=True)
            raise
        finally:
            self.provider.progress_sink = None
        print(json.dumps(dict(diagnostic,event='inference_completed',timings=response.timings,
                              input_tokens=response.input_tokens,output_tokens=response.output_tokens)),file=sys.stderr,flush=True)
        if response.served_provider != 'ollama' or response.served_model != self.model:
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
            parsed = (materialize_names(body,contract,ontology)
                      if stage=='name_slots' else
                      materialize_identity(body,payload['identity_occurrences'],contract)
                      if stage=='identity_slots' else
                      materialize_verification(body,[i['item_ref'] for i in payload['items']])
                      if stage=='verify' else contract.materialize(stage,schema,body,ontology))
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
                        **({'proposed_count':payload['_proposed_count']} if '_proposed_count' in payload else {}))),
             self.current_chapter,stage,diagnostic['batch_id'],self.run_id))
        if self.run_id:
            await self.db.execute('''INSERT INTO graph_completion_run
                (revision_id,cache_key,served_provider,served_model,run_id,chapter_index,stage,batch_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (self.revision['id'],key,response.served_provider,response.served_model,self.run_id,
                 self.current_chapter,stage,diagnostic['batch_id']))
        await self._proposal_activity(stage,parsed)
        return parsed

    @staticmethod
    def _passage_batches(source: str, *, budget: int = 8192,
                         max_passages: int = 4) -> list[list[str]]:
        """Amortize name-inventory prefill over a bounded adjacent context.

        Six compact nullable output slots keep generation bounded. A full response is a
        saturation signal; discover_names then retries each passage separately so
        batching cannot silently lower recall. Aggregation remains application-owned
        (§0, §5.4), so a surface never becomes an identity binding.
        """
        batches=[];current=[];size=0
        for p in PassageContract(source).passages:
            cost=len(json.dumps(dict(id=p['id'],text=p['text']),ensure_ascii=False).encode())+2
            if current and (size+cost>budget or len(current)>=max_passages):
                batches.append(current);current=[];size=0
            current.append(p['id']);size+=cost
        if current:
            batches.append(current)
        return batches

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
            batch=mentions[start:start+batch_limit]
            # Recent verified occurrences carry both their source context and binding.
            # Selection never creates a binding; the semantic verifier owns that step.
            recent={}
            for d in reversed(verified):
                recent.setdefault((d['outcome'],d['target_id']),d)
                if len(recent)>=8: break
            prior=list(recent.values())
            offered_mentions={m['id']:m for m in batch}
            offered_mentions.update({d['mention_id']:by_mid[d['mention_id']] for d in prior})
            refs={mid:f'o{i+1}' for i,mid in enumerate(offered_mentions)}
            targets={};rows=[]
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
                    token=f't{len(targets)+1}'
                    targets[token]=(d['outcome'],d['target_id'])
                    choices[key]=(d['outcome'],token,
                        'new_coreference' if d['outcome']=='new' else 'existing_evidence')
                    descriptions[key]=self._subject_context(other)
                    if d['outcome']=='existing' and not any(c['id']==d['target_id'] for c in candidates[m['id']]):
                        candidates[m['id']].append(entity_context[d['target_id']])
                for c in candidates[m['id']]:
                    if c['kind']!=m['kind']: continue
                    token=f't{len(targets)+1}';targets[token]=('existing',c['id'])
                    key='existing:'+token
                    choices[key]=('existing',token,'existing_evidence')
                    descriptions[key]={k:v for k,v in c.items() if k!='id'}
                rows.append(dict(occurrence_ref=refs[m['id']],subject=self._subject_context(m),
                    choices=choices,choice_context=descriptions))
            identity_batch=f'identity-{start+1}-{start+len(batch)}'
            try:
                response=await self.call('identity_slots',IdentityDecisions,dict(source=source,
                    ontology=ontology,identity_occurrences=rows,
                    _passage_ids=self._mention_passages(source,list(offered_mentions.values())),
                    _batch_id=identity_batch))
            except ValueError as exc:
                if 'graph context exceeds hard local model budget' not in str(exc) or len(batch)<=1:
                    raise
                batch_limit=max(1,len(batch)//2)
                self._identity_context_splits+=1
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
        return {k:mention[k] for k in ('surface','kind','char_start','char_end','quote') if k in mention}

    async def _verify_items(self, source: str, items: list[dict], contract_name: str, *,
                            display_contexts: list[str] | None = None,
                            mentions: list[dict] | None = None, candidates: dict | None = None,
                            batch_identity: str | None = None) -> Verification:
        """Require a complete, unique verdict set and hide durable IDs from the wire."""
        result=Verification(verdicts=[])
        by_mid={m['id']:m for m in mentions or []}
        entities={c['id']:c for rows in (candidates or {}).values() for c in rows}
        for start in range(0,len(items),12):
            batch=items[start:start+12]
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
                _batch_id=f'verify-{batch_identity or contract_name}-{start//12+1}'))
            by_ref={v.id:v for v in checked.verdicts}
            if len(checked.verdicts)!=len(batch) or set(by_ref)!=set(refs.values()):
                raise ValueError('verification must contain one unique verdict per offered item')
            for item in batch:
                verdict=by_ref[refs[item['id']]]
                from pipeline.evidence import Verdict
                result.verdicts.append(Verdict(id=item['id'],supported=verdict.supported,reason=verdict.reason))
        return result

    async def _claims_for_focus(self, source: str, verified_mentions: list[dict], ontology: dict,
                                focus: dict, *, passage_chars: int = CLAIM_PASSAGE_CHARS,
                                context_ranges: list | None = None
                                ) -> tuple[list[Claim],list[dict],int]:
        passages=source_passages(source,max_chars=passage_chars,overlap=0)
        try:
            index=next(i for i,p in enumerate(passages) if p['id']==focus['id'])
        except StopIteration:
            return [],[dict(rejection='claim coverage window could not be reconstructed',batch_id=focus['id'])],0
        context=passages[max(0,index-1):min(len(passages),index+2)]
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
        batch_id=f'claims-{focus["char_start"]}-{passage_chars}'
        request=dict(source=source,verified_occurrences=wire_mentions,ontology=ontology,
            focus_passage_ids=[focus['id']],_passage_ids=selected_ids,
            _passage_max_chars=passage_chars,_passage_overlap=0,_batch_id=batch_id,
            _context_ranges=forwarded)
        try:
            response=await self.call('claims',ClaimProposals,request)
        except ValueError as exc:
            if ('graph context exceeds hard local model budget' not in str(exc)
                    or passage_chars<=MIN_CLAIM_PASSAGE_CHARS):
                raise
            self._claim_context_splits=getattr(self,'_claim_context_splits',0)+1
            smaller=max(MIN_CLAIM_PASSAGE_CHARS,passage_chars//2)
            children=[p for p in source_passages(source,max_chars=smaller,overlap=0)
                      if focus['char_start']<=p['char_start'] and p['char_end']<=focus['char_end']]
            if not children:
                raise
            claims=[];rejected=[];proposed=0
            for child in children:
                got,no,count=await self._claims_for_focus(source,verified_mentions,ontology,child,
                                                          passage_chars=smaller,context_ranges=context_ranges)
                claims.extend(got);rejected.extend(no);proposed+=count
            return claims,rejected,proposed
        # Saturation is a property of what the model emitted, not of what survived the
        # focus filter, so prefer the pre-filter count `call` reports back.
        proposed=request.get('_proposed_count',len(response.claims))
        if proposed==CLAIM_LIMIT:
            self._claim_subdivisions=getattr(self,'_claim_subdivisions',0)+1
            if passage_chars<=MIN_CLAIM_PASSAGE_CHARS:
                return [],[dict(rejection='claim coverage failure: smallest source window reached claim limit',
                                batch_id=batch_id,claim_limit=CLAIM_LIMIT)],proposed
            smaller=max(MIN_CLAIM_PASSAGE_CHARS,passage_chars//2)
            children=[p for p in source_passages(source,max_chars=smaller,overlap=0)
                      if focus['char_start']<=p['char_start'] and p['char_end']<=focus['char_end']]
            claims=[];rejected=[]
            for child in children:
                got,no,count=await self._claims_for_focus(source,verified_mentions,ontology,child,
                                                          passage_chars=smaller,context_ranges=context_ranges)
                claims.extend(got);rejected.extend(no);proposed+=count
            if not children:
                rejected.append(dict(rejection='claim coverage failure: saturated window could not be subdivided',
                                     batch_id=batch_id,claim_limit=CLAIM_LIMIT))
            return claims,rejected,proposed
        reverse={ref:mid for mid,ref in occurrence_refs.items()}
        claims=[]
        for proposal in response.claims:
            mids=[reverse.get(ref) for ref in proposal.occurrence_refs]
            if any(mid is None for mid in mids):
                raise ValueError('claim references an unoffered occurrence')
            claims.append(Claim(type=proposal.type,mention_ids=mids,attribute=proposal.attribute,
                value=proposal.value,value_en=proposal.value_en,quote=proposal.quote,
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
                                top_level_batches=len(batches),saturation_retries=0)
        if source.strip():
            collected=[];rejected=[]
            for batch_number,passage_ids in enumerate(batches,1):
                request=dict(source=source,ontology=ontology,_passage_ids=passage_ids,
                             _batch_id=f'names-{batch_number}')
                found=await self.call('name_slots',Names,request)
                if len(passage_ids)>1 and request.get('_proposed_count',len(found.names))==6:
                    self._name_metrics['saturation_retries']+=1
                    for part,passage_id in enumerate(passage_ids,1):
                        child=await self.call('name_slots',Names,dict(source=source,ontology=ontology,
                            _passage_ids=[passage_id],_batch_id=f'names-{batch_number}.{part}'))
                        collected.extend(child.names);rejected.extend(child.rejected)
                else:
                    collected.extend(found.names);rejected.extend(found.rejected)
            unique={}
            for name in collected:
                unique[(name.surface,name.kind,name.evidence_start)]=name
            names=Names(names=list(unique.values()),reviewed_kinds=ontology['kinds'],rejected=rejected)
        else:
            names=Names(names=[],reviewed_kinds=ontology['kinds'])
        mentions=source_mentions(novel,chapter,source,names,ontology)
        coverage={kind:dict(proposed_surfaces=len({n.surface for n in names.names if n.kind==kind}),
                            source_occurrences=sum(m['kind']==kind for m in mentions)) for kind in ontology['kinds']}
        return names,mentions,coverage

    def _runtime_diagnostics(self, source: str, display: str) -> dict:
        """Metrics needed to compare chunk policy across chapter sizes (§5.4)."""
        return dict(
            input_size=dict(source_chars=len(source),source_bytes=len(source.encode()),
                            display_chars=len(display),display_bytes=len(display.encode())),
            chunk_policy=dict(name_passages_per_batch=4,name_slots=6,
                identity_occurrences_per_batch=IDENTITY_BATCH_SIZE,
                claim_focus_chars=CLAIM_PASSAGE_CHARS,claim_min_chars=MIN_CLAIM_PASSAGE_CHARS,
                claims_per_response=CLAIM_LIMIT,verification_items_per_batch=12,
                alignment_display_chars=12000,alignment_mentions=48,
                prompt_hard_bytes=PROMPT_HARD_BYTES),
            name_chunking=getattr(self,'_name_metrics',dict(
                passages=0,top_level_batches=0,saturation_retries=0)),
            claim_subdivisions=getattr(self,'_claim_subdivisions',0),
            claim_context_splits=getattr(self,'_claim_context_splits',0),
            identity_chunking=dict(batch_sizes=list(getattr(self,'_identity_batch_sizes',[])),
                                   context_splits=getattr(self,'_identity_context_splits',0)),
            stage_requests=dict(getattr(self,'_stage_requests',{})),
            stage_cache_hits=dict(getattr(self,'_stage_cache_hits',{})))

    async def extract(self, novel: str, chapter: int, source: str, display: str, target: str,
                      *, include_terms: bool = True, include_facts: bool = True) -> dict:
        await self._ensure_run(novel,chapter,source,display)
        self._stage_requests={};self._stage_cache_hits={}
        self._claim_subdivisions=0;self._claim_context_splits=0
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
            for focus in source_passages(source,max_chars=CLAIM_PASSAGE_CHARS,overlap=0):
                found,no,count=await self._claims_for_focus(source,verified_mentions,ontology,focus)
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
            claim_verdicts=await self._verify_items(source,claim_items,'claims',mentions=mentions)
            verified_claims,no=approved(claim_items,claim_verdicts)
            rejected.extend(no)
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
            batch=mentions[mlo:mhi]
            if not display[lo:hi].strip():
                continue
            alignment=await self.call('align',Alignments,dict(source=source,translation=display[lo:hi],mentions=batch,
                _passage_ids=self._mention_passages(source,batch)))
            spans.extend(aligned_mentions(novel,chapter,source,display[lo:hi],batch,alignment,
                display_offset=lo,display_identity=display_identity))
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
