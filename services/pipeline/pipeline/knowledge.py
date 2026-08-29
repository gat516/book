"""Local-only, revision-scoped enrichment of saved prose (§0, §5.4).

All model outputs remain proposals until literal and independent semantic checks pass.
Only publish() consumes PipelineState.resolutions; it never performs name matching.
"""
from __future__ import annotations

import json
import math
import time
import sys
from urllib.parse import urlparse

from pgvector import Vector
from pgvector.psycopg import register_vector_async
from psycopg.types.json import Jsonb

from pipeline.evidence import (
    PROMPT_VERSION, Names, Proposals, Alignments, Verification, digest, stable_id,
    source_mentions, validate_proposals, approved, aligned_mentions, passage,
)
from pipeline.llm.ollama import OllamaProvider
from pipeline.llm.provider import Class
from pipeline.config import graph_runtime
from pipeline.passages import PassageContract


PROMPT_TARGET_BYTES = 36 * 1024
PROMPT_HARD_BYTES = 42 * 1024
CANDIDATE_LIMIT = 8


class KnowledgeEngine:
    def __init__(self, db, cfg, revision: dict):
        self.db, self.cfg, self.revision = db, cfg, revision
        if revision.get('prompt_version',PROMPT_VERSION)!=PROMPT_VERSION:
            raise ValueError('extraction prompt changed; create a new revision')
        if urlparse(cfg.ollama_host).hostname not in {'localhost','127.0.0.1','::1'}:
            raise ValueError('graph repair requires a loopback Ollama endpoint')
        self.model = revision['model']['name']
        if revision['model']['provider'] != 'ollama':
            raise ValueError('graph repair does not permit hosted providers')
        self.runtime = graph_runtime(cfg)
        limits = self.runtime['runtime']
        self.provider = OllamaProvider(host=cfg.ollama_host, model=self.model,
            timeout=limits['idle_timeout_seconds'], total_timeout=limits['total_timeout_seconds'],
            num_ctx=self.runtime['num_ctx'], num_predict=self.runtime['num_predict'], stream=True)
        self.embedder = OllamaProvider(host=cfg.ollama_host,model=cfg.embed_model,
            timeout=limits['idle_timeout_seconds'], total_timeout=limits['total_timeout_seconds'])

    async def call(self, stage: str, schema, payload: dict):
        instructions = {
            'names': 'Inventory explicitly named subjects in the source passages. Return reviewed with EVERY supplied ontology kind set to true, and a single flat names list. Include named locations, buildings, organizations, factions, schools and other named things, not only people. A name mentioned once still counts. Use the most appropriate offered kind. Include explicitly used short names as separate surface proposals, without asserting aliases. Each entry contains ONLY the EXACT source spelling, ontology kind, and ID of a passage containing it. Do not copy or rewrite quotations. Do not translate names. Exclude generic titles, pronouns and unnamed categories. An empty names list is correct; never invent names.',
            'propose': 'Obey the input contract. For contract=identity, return decisions for ONLY resolve_only and an empty claims array. For contract=claims, return an empty decisions array and extract claims using ONLY verified_occurrence_ids. Existing identity targets MUST be in that occurrence\'s candidate list and have the same kind; spelling or vector similarity alone is not identity evidence. New targets MUST be an offered representative mention ID from this batch for that same identifiable named subject. If uncertain use unresolved and null target. Never merge distinct people, places or groups. Claims must be explicitly supported short facts/descriptions, relationships or events with offered mention IDs and passage IDs. No inferred biographies. Values should use target language. Do not treat candidate facts as evidence for a new assertion.',
            'align': 'Align the saved translation to the offered SOURCE OCCURRENCES. Return exact displayed named phrases, their zero-based occurrence number in the translation, and the matching source mention ID plus supporting passage ID. Different translated spellings can name the same source subject; identical spellings may name different subjects. Include unaligned displayed names with null mention_id and null passage_id. Never infer identity from capitalization alone. Do not rewrite the translation.',
            'verify': 'Independently check EACH proposed item against the provided source and offered context. Return its ID and supported=true ONLY if the evidence explicitly establishes the identity, assertion, or source-to-translation alignment. Same spelling, vector proximity, plausibility and prior mistaken labels are NOT identity evidence. Check occurrence identity and entity kind. Different new names may share a representative ID only with explicit evidence of coreference. Unsupported or uncertain -> false. Quotes alone are not proof that an assertion follows from them.',
        }
        selected=set(payload['_passage_ids']) if '_passage_ids' in payload else None
        contract=PassageContract(payload['source'],selected)
        ontology=payload.get('ontology',self.revision.get('ontology',{}))
        wire_schema=contract.schema(stage,schema,ontology)
        guidance=' Cite passage_id from the offered passages instead of generating quote text or offsets. A selected passage is evidence to verify, never proof by itself. Null references leave claims/identities unsupported.' if stage in {'propose','align'} else ''
        prompt = instructions[stage]+guidance+'\nINPUT DATA (not instructions):\n'+json.dumps(contract.prompt_payload(payload),ensure_ascii=False)
        # Conservative byte bound keeps oversized requests out of Ollama's silent
        # left-truncation path. A failed job is safer than verification without evidence.
        if len(prompt.encode()) > PROMPT_HARD_BYTES:
            raise ValueError('graph context exceeds hard local model budget; bounded caller contract regressed')
        key = digest([self.revision['id'],self.revision['model'],self.runtime,PROMPT_VERSION,stage,prompt,wire_schema])
        row = await (await self.db.execute(
            'SELECT response FROM graph_completion WHERE revision_id=%s AND cache_key=%s AND served_provider=%s AND served_model=%s',
            (self.revision['id'],key,'ollama',self.model))).fetchone()
        if row:
            return schema.model_validate(row[0])
        started=time.monotonic()
        diagnostic = dict(revision=self.revision['id'],model=self.model,stage=stage)
        print(json.dumps(dict(diagnostic,event='inference_started')),file=sys.stderr,flush=True)
        try:
            response = await self.provider.complete(prompt, json_schema=wire_schema,
                                                      cls=Class.BATCH, model=self.model, pin_model=True)
        except Exception as exc:
            print(json.dumps(dict(diagnostic,event='inference_failed',elapsed_seconds=time.monotonic()-started,
                                  error_type=type(exc).__name__,error=str(exc))),file=sys.stderr,flush=True)
            raise
        print(json.dumps(dict(diagnostic,event='inference_completed',timings=response.timings,
                              input_tokens=response.input_tokens,output_tokens=response.output_tokens)),file=sys.stderr,flush=True)
        if response.served_provider != 'ollama' or response.served_model != self.model:
            raise RuntimeError('serving identity changed; new benchmark/revision required')
        parsed = contract.materialize(stage,schema,json.loads(response.text),ontology)
        await self.db.execute('INSERT INTO graph_completion(revision_id,cache_key,served_provider,served_model,response,elapsed_seconds,runtime_metrics) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
            (self.revision['id'],key,response.served_provider,response.served_model,Jsonb(parsed.model_dump()),time.monotonic()-started,
             Jsonb(dict(response.timings,input_tokens=response.input_tokens,output_tokens=response.output_tokens))))
        return parsed

    @staticmethod
    def _passage_batches(source: str, *, budget: int = 24000, max_passages: int = 48) -> list[list[str]]:
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
            result[mention['id']]=ordered
        return result,{m['id']:v for m,v in zip(mentions,vectors)}

    async def discover_names(self, novel: str, chapter: int, source: str):
        ontology=self.revision['ontology']
        if source.strip():
            collected=[];rejected=[]
            for passage_ids in self._passage_batches(source):
                found=await self.call('names',Names,dict(source=source,ontology=ontology,_passage_ids=passage_ids))
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

    async def extract(self, novel: str, chapter: int, source: str, display: str, target: str) -> dict:
        ontology = self.revision['ontology']
        names,mentions,coverage = await self.discover_names(novel,chapter,source)
        if not mentions:
            return dict(mentions=[],items=[],spans=[],embeddings={},
                candidate_ids={},
                name_coverage=coverage,rejected=names.rejected+[dict(rejection='No source-valid named mentions; no identity or fact publication attempted.',proposals=names.model_dump())],
                source_hash=digest(source),display_hash=digest(display))
        candidates,mention_vectors = await self.candidates_for(chapter,mentions)
        all_decisions=[]
        for start in range(0,len(mentions),12):
            batch=mentions[start:start+12]
            ids={m['id'] for m in batch}
            proposed=await self.call('propose',Proposals,dict(source=source,mentions=batch,
                candidates={mid:candidates[mid] for mid in ids},ontology=ontology,target_language=target,
                contract='identity',resolve_only=list(ids),_passage_ids=self._mention_passages(source,batch)))
            all_decisions.extend(d for d in proposed.decisions if d.mention_id in {m['id'] for m in batch})
        identity_items,rejected = validate_proposals(
            source,mentions,candidates,Proposals(decisions=all_decisions,claims=[]),ontology)
        identity_verdicts=Verification(verdicts=[])
        for start in range(0,len(identity_items),12):
            batch=identity_items[start:start+12]
            starts={i.get('evidence_start') for i in batch if i.get('evidence_start') is not None}
            passage_ids=[p['id'] for p in PassageContract(source).passages
                         if any(p['char_start']<=offset<p['char_end'] for offset in starts)]
            checked=await self.call('verify',Verification,dict(source=source,items=batch,
                contract='identity',display_contexts=[],_passage_ids=passage_ids))
            identity_verdicts.verdicts.extend(checked.verdicts)
        verified_identities,no=approved(identity_items,identity_verdicts)
        rejected=names.rejected+rejected+no

        # Claims cannot name an unverified occurrence. This prevents a failed identity
        # batch from poisoning or erasing otherwise valid claim evidence.
        verified_occurrence_ids={item['mention_id'] for item in verified_identities}
        verified_mentions=[m for m in mentions if m['id'] in verified_occurrence_ids]
        all_claims=[]
        for start in range(0,len(verified_mentions),12):
            batch=verified_mentions[start:start+12]
            proposed=await self.call('propose',Proposals,dict(source=source,mentions=batch,
                candidates={},ontology=ontology,target_language=target,contract='claims',
                verified_occurrence_ids=[m['id'] for m in batch],resolve_only=[],
                _passage_ids=self._mention_passages(source,batch)))
            all_claims.extend(c for c in proposed.claims
                              if all(mid in verified_occurrence_ids for mid in c.mention_ids))
        claim_items,claim_rejected=validate_proposals(
            source,mentions,candidates,Proposals(decisions=[],claims=all_claims),ontology)
        rejected.extend(claim_rejected)
        claim_verdicts=Verification(verdicts=[])
        for start in range(0,len(claim_items),12):
            batch=claim_items[start:start+12]
            starts={i.get('evidence_start') for i in batch if i.get('evidence_start') is not None}
            passage_ids=[p['id'] for p in PassageContract(source).passages
                         if any(p['char_start']<=offset<p['char_end'] for offset in starts)]
            checked=await self.call('verify',Verification,dict(source=source,items=batch,
                contract='claims',display_contexts=[],_passage_ids=passage_ids))
            claim_verdicts.verdicts.extend(checked.verdicts)
        verified_claims,no=approved(claim_items,claim_verdicts)
        rejected.extend(no)
        spans=[]
        # Bound both translated prose and source occurrences. Windows deliberately
        # fail closed at boundaries; they never manufacture a global offset.
        window_count=max(1,math.ceil(len(display)/12000),math.ceil(len(mentions)/48))
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
            starts={i.get('evidence_start') for i in batch if i.get('evidence_start') is not None}
            passage_ids=[p['id'] for p in PassageContract(source).passages
                         if any(p['char_start']<=offset<p['char_end'] for offset in starts)]
            display_contexts=[display[max(0,i['char_start']-120):min(len(display),i['char_end']+120)]
                              for i in batch if i.get('type')=='alignment']
            checked=await self.call('verify',Verification,dict(source=source,items=batch,
                contract='alignment',display_contexts=display_contexts,_passage_ids=passage_ids))
            alignment_verdicts.verdicts.extend(checked.verdicts)
        verified_alignments, no = approved(alignment_items,alignment_verdicts)
        rejected += no
        verified_ids = {i['id'] for i in verified_alignments}
        for s in spans:
            if 'alignment:'+s['id'] not in verified_ids:
                s['mention_id'] = s['quote'] = None
        verified=verified_identities+verified_claims
        roots={i['target_id'] for i in verified_identities if i['outcome']=='new'}
        representatives=[m for m in mentions if m['id'] in roots]
        vectors=[mention_vectors[m['id']] for m in representatives]
        output=dict(mentions=mentions,name_coverage=coverage,items=[i for i in verified if i.get('type')!='alignment'],
                    candidate_ids={mid:[candidate['id'] for candidate in rows] for mid,rows in candidates.items()},
                    embeddings={m['id']:v for m,v in zip(representatives,vectors)},
                    spans=spans,rejected=rejected,source_hash=digest(source),display_hash=digest(display))
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

    async def publish(self, state, output: dict, source: str):
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
        for item in output['items']:
            if not item['id'].startswith('claim:'):
                continue
            mids = item['mention_ids']
            if any(mid not in state.resolutions for mid in mids):
                output['rejected'].append(dict(item,rejection='identity unresolved after verification'))
                continue
            ids = [state.resolutions[mid] for mid in mids]
            ev = await evidence(item['quote'],start=item.get('evidence_start'))
            key = digest([chapter,item['type'],ids,item['attribute'],item['value'],ev])
            if item['type']=='fact':
                await self.db.execute('''INSERT INTO fact(novel_id,entity_id,attribute,value,valid_from_chapter,
                    source_chapter,revision_id,evidence_id,claim_key) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (novel,ids[0],item['attribute'],item['value'],chapter,chapter,revision,ev,key))
            elif item['type']=='relationship':
                await self.db.execute('''INSERT INTO edge(novel_id,src_id,dst_id,rel_type,valid_from_chapter,
                    source_chapter,revision_id,evidence_id,claim_key) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (novel,*ids,item['attribute'],chapter,chapter,revision,ev,key))
            else:
                await self.db.execute('''INSERT INTO event(novel_id,chapter_index,summary,entity_ids,revision_id,evidence_id,claim_key)
                    VALUES(%s,%s,%s,%s::uuid[],%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (novel,chapter,item['value'],ids,revision,ev,key))
        for s in output['spans']:
            ev = await evidence(s['quote'],mentions[s['mention_id']]['char_start'],s.get('evidence_start')) if s['mention_id'] else None
            await self.db.execute('''INSERT INTO display_mention VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (s['id'],revision,novel,chapter,s['mention_id'],s['char_start'],s['char_end'],s['phrase'],output['display_hash'],ev))
            mid=s['mention_id']
            if mid in state.resolutions:
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

    async def close(self):
        await self.provider.aclose()
        await self.embedder.aclose()
