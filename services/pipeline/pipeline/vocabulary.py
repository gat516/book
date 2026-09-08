"""Novel-scoped attribute/relation vocabulary runtime (Phase C.3-C.6).

The vocabulary is deliberately data-driven.  This module contains only normalization,
chapter-visible lookup, and the corroboration ledger; embedding neighbors remain
suggestions and never become aliases automatically.
"""
from __future__ import annotations

import hashlib
import json
import re

from pipeline.evidence import digest
from psycopg.types.json import Jsonb

VOCAB_MIN_PROPOSALS = 2
VOCAB_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{1,39}$')


def normalize_name(value: str) -> str:
    """Normalize a proposed vocabulary name without inventing semantic aliases."""
    if not isinstance(value, str):
        return ''
    return re.sub(r'[\s/]+', '_', value.strip().lower())


def valid_name(value: str) -> bool:
    return isinstance(value, str) and bool(VOCAB_NAME_RE.fullmatch(value))


async def load_visible(db, novel_id: str, chapter: int, *, term_type: str | None = None,
                       kinds: list[str] | None = None) -> list[dict]:
    """Load entries visible at ``chapter``; future names and metadata stay out."""
    where = ['novel_id=%s', 'first_seen_chapter<=%s']
    params: list = [novel_id, chapter]
    if term_type:
        where.append('term_type=%s'); params.append(term_type)
    rows = await (await db.execute(
        'SELECT term_type,name,kinds,dst_kinds,status,gloss,first_seen_chapter,admitted_at_chapter '
        'FROM novel_vocabulary WHERE ' + ' AND '.join(where) +
        ' ORDER BY term_type,name', tuple(params))).fetchall()
    result=[]
    for row in rows:
        item=dict(zip(('term_type','name','kinds','dst_kinds','status','gloss',
                       'first_seen_chapter','admitted_at_chapter'), row))
        canonical_row = await (await db.execute(
            'SELECT canonical_novel_vocabulary_name(%s,%s,%s,%s)',
            (novel_id,item['term_type'],item['name'],chapter))).fetchone()
        if canonical_row and canonical_row[0] != item['name']:
            continue
        # Gloss is knowledge-bearing prompt context.  Prefer the latest audited edit
        # known by this chapter; when looking earlier than the first edit, reconstruct
        # the prior value from that edit's old_value rather than leaking current text.
        old = await (await db.execute('''SELECT new_value->>'gloss' FROM novel_vocabulary_changelog
            WHERE novel_id=%s AND term_type=%s AND name=%s AND action='gloss'
              AND changed_at_chapter<=%s AND new_value ? 'gloss'
            ORDER BY changed_at_chapter DESC, seq DESC LIMIT 1''',
            (novel_id,item['term_type'],item['name'],chapter))).fetchone()
        if old and old[0] is not None:
            item['gloss']=old[0]
        else:
            future = await (await db.execute('''SELECT old_value->>'gloss' FROM novel_vocabulary_changelog
                WHERE novel_id=%s AND term_type=%s AND name=%s AND action='gloss'
                  AND changed_at_chapter>%s AND old_value ? 'gloss'
                ORDER BY changed_at_chapter ASC, seq ASC LIMIT 1''',
                (novel_id,item['term_type'],item['name'],chapter))).fetchone()
            if future and future[0] is not None:
                item['gloss']=future[0]
        if kinds and not set(kinds).intersection(item['kinds'] or []):
            continue
        result.append(item)
    return result


async def resolve(db, novel_id: str, term_type: str, raw: str, chapter: int) -> dict | None:
    """Resolve exact canonical/visible alias and return status/kind metadata."""
    name=normalize_name(raw)
    if not valid_name(name):
        return None
    row=await (await db.execute(
        'SELECT canonical_novel_vocabulary_name(%s,%s,%s,%s)',
        (novel_id,term_type,name,chapter))).fetchone()
    canonical=row[0] if row and row[0] else name
    row=await (await db.execute(
        '''SELECT term_type,name,kinds,dst_kinds,status,gloss,first_seen_chapter,admitted_at_chapter
             FROM novel_vocabulary WHERE novel_id=%s AND term_type=%s AND name=%s
             AND first_seen_chapter<=%s''',
        (novel_id,term_type,canonical,chapter))).fetchone()
    if not row:
        return dict(term_type=term_type,name=canonical,status='unknown',kinds=[],dst_kinds=[],gloss='')
    return dict(zip(('term_type','name','kinds','dst_kinds','status','gloss',
                     'first_seen_chapter','admitted_at_chapter'),row))


async def record_candidate(db, novel_id: str, term_type: str, name: str, kind: str,
                           chapter: int, *, revision_id: str | None = None,
                           evidence: dict | None = None,
                           dst_kind: str | None = None) -> dict:
    """Record one idempotent chapter vote and admit after two distinct chapters."""
    name=normalize_name(name)
    if not valid_name(name):
        raise ValueError('invalid vocabulary name')
    await db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(novel_id,))
    current=await (await db.execute(
        'SELECT status FROM novel_vocabulary WHERE novel_id=%s AND term_type=%s AND name=%s',
        (novel_id,term_type,name))).fetchone()
    if current and current[0] in {'banned','retired'}:
        return dict(name=name,status=current[0],proposals=0)
    await db.execute('''INSERT INTO novel_vocabulary
        (novel_id,term_type,name,kinds,dst_kinds,status,gloss,first_seen_chapter,last_seen_chapter)
        VALUES(%s,%s,%s,'{}'::text[],'{}'::text[],
               'candidate','',%s,%s)
        ON CONFLICT(novel_id,term_type,name) DO UPDATE SET
          first_seen_chapter=least(novel_vocabulary.first_seen_chapter,EXCLUDED.first_seen_chapter),
          last_seen_chapter=greatest(novel_vocabulary.last_seen_chapter,EXCLUDED.last_seen_chapter)''',
        (novel_id,term_type,name,chapter,chapter))
    entries=[(kind,dict(evidence or {},role='src'))]
    if term_type == 'relation' and dst_kind:
        entries.append((f'dst:{dst_kind}',dict(evidence or {},role='dst')))
    for ledger_kind, ledger_evidence in entries:
        await db.execute('''INSERT INTO novel_vocabulary_chapter
            (novel_id,term_type,name,kind,chapter_index,revision_id,evidence)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (novel_id,term_type,name,ledger_kind,chapter,revision_id,Jsonb(ledger_evidence)))
    role_count=await (await db.execute('''SELECT count(DISTINCT chapter_index)
        FROM novel_vocabulary_chapter WHERE novel_id=%s AND term_type=%s AND name=%s AND kind=%s''',
        (novel_id,term_type,name,kind))).fetchone()
    changed_visibility=False
    if int(role_count[0]) >= VOCAB_MIN_PROPOSALS:
        updated=await db.execute('''UPDATE novel_vocabulary SET
            kinds=ARRAY(SELECT DISTINCT k FROM unnest(kinds || ARRAY[%s]::text[]) k ORDER BY k),
            updated_at=now() WHERE novel_id=%s AND term_type=%s AND name=%s
            AND status NOT IN ('banned','retired') AND NOT (%s=ANY(kinds)) RETURNING name''',
            (kind,novel_id,term_type,name,kind))
        changed_visibility |= bool(await updated.fetchone())
    if term_type == 'relation' and dst_kind:
        dst_count=await (await db.execute('''SELECT count(DISTINCT chapter_index)
            FROM novel_vocabulary_chapter WHERE novel_id=%s AND term_type=%s AND name=%s
              AND kind=%s''',(novel_id,term_type,name,f'dst:{dst_kind}'))).fetchone()
        if int(dst_count[0]) >= VOCAB_MIN_PROPOSALS:
            updated=await db.execute('''UPDATE novel_vocabulary SET
                dst_kinds=ARRAY(SELECT DISTINCT k FROM unnest(dst_kinds || ARRAY[%s]::text[]) k ORDER BY k),
                updated_at=now() WHERE novel_id=%s AND term_type=%s AND name=%s
                AND status NOT IN ('banned','retired') AND NOT (%s=ANY(dst_kinds)) RETURNING name''',
                (dst_kind,novel_id,term_type,name,dst_kind))
            changed_visibility |= bool(await updated.fetchone())
    row=await (await db.execute('''SELECT count(DISTINCT chapter_index)
        FROM novel_vocabulary_chapter WHERE novel_id=%s AND term_type=%s AND name=%s''',
        (novel_id,term_type,name))).fetchone()
    count=int(row[0])
    current=await (await db.execute(
        'SELECT status FROM novel_vocabulary WHERE novel_id=%s AND term_type=%s AND name=%s',
        (novel_id,term_type,name))).fetchone()
    if count >= VOCAB_MIN_PROPOSALS and current and current[0]=='candidate':
        await db.execute('''UPDATE novel_vocabulary SET status='admitted',
            proposals=%s,admitted_at_chapter=%s,updated_at=now()
            WHERE novel_id=%s AND term_type=%s AND name=%s AND status='candidate' ''',
            (count,chapter,novel_id,term_type,name))
        await _admission_changelog(db,novel_id,term_type,name,chapter)
        changed_visibility=True
    else:
        await db.execute('''UPDATE novel_vocabulary SET proposals=%s,
            updated_at=now() WHERE novel_id=%s AND term_type=%s AND name=%s''',
            (count,novel_id,term_type,name))
    if changed_visibility:
        # Admission or role widening changes render visibility. One call gets one bump.
        await db.execute('''UPDATE graph_revision SET version=version+1
            WHERE novel_id=%s AND state IN ('active','staging')''', (novel_id,))
    row=await (await db.execute(
        'SELECT status,proposals FROM novel_vocabulary WHERE novel_id=%s AND term_type=%s AND name=%s',
        (novel_id,term_type,name))).fetchone()
    return dict(name=name,status=row[0],proposals=row[1])


async def _admission_changelog(db, novel_id: str, term_type: str, name: str, chapter: int) -> None:
    previous=await (await db.execute(
        'SELECT seq,row_hash FROM novel_vocabulary_changelog WHERE novel_id=%s ORDER BY seq DESC LIMIT 1',
        (novel_id,))).fetchone()
    seq=(previous[0] if previous else 0)+1
    prev_hash=previous[1] if previous else ''
    payload=json.dumps([novel_id,seq,term_type,name,'admit',chapter,prev_hash],
                       ensure_ascii=False,separators=(',',':'))
    row_hash=hashlib.sha256((prev_hash+payload).encode()).hexdigest()
    await db.execute('''INSERT INTO novel_vocabulary_changelog
        (novel_id,seq,term_type,action,name,changed_at_chapter,prev_hash,row_hash,created_by)
        VALUES(%s,%s,%s,'admit',%s,%s,%s,%s,'pipeline')''',
        (novel_id,seq,term_type,name,chapter,prev_hash or None,row_hash))


def vocabulary_prompt(rows: list[dict]) -> dict:
    return dict(attributes=[dict(name=r['name'],gloss=r['gloss']) for r in rows if r['term_type']=='attribute'],
                relations=[dict(name=r['name'],gloss=r['gloss']) for r in rows if r['term_type']=='relation'])


async def embedding_suggestions(db, embedder, novel_id: str, term_type: str,
                               value, *, limit: int = 5) -> list[dict]:
    """Read-only nearest admitted/candidate vocabulary suggestions.

    Similarity is advisory only: this function never writes aliases, status, or
    embeddings and deliberately has no threshold that could turn a suggestion into
    an automatic merge.
    """
    limit=max(1,min(int(limit),20))
    vector = (await embedder.embed([value]))[0] if isinstance(value, str) else value
    vector_text='['+','.join(str(float(x)) for x in vector)+']'
    rows=await (await db.execute('''SELECT name,status,
        embedding <=> %s::vector AS distance
        FROM novel_vocabulary WHERE novel_id=%s AND term_type=%s
          AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s''',
        (vector_text,novel_id,term_type,vector_text,limit))).fetchall()
    return [dict(name=r[0],status=r[1],distance=float(r[2])) for r in rows]
