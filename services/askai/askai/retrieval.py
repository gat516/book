from __future__ import annotations

from dataclasses import dataclass
from psycopg import AsyncConnection

@dataclass(frozen=True)
class Source:
    kind: str
    id: int | str
    chapter: int
    text: str

def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"

async def retrieve(conn: AsyncConnection, novel_id: str, at: int, embedding: list[float] | None, *, question: str = "", embedding_space: str | None = None, max_chunks: int) -> list[Source]:
    """Retrieve the nearest translated chunks under the reader chapter gate.

    Every predicate is repeated here because Ask AI's connection role may be privileged
    in a deployment and RLS is only one layer of the spoiler boundary. ``embedding=None``
    (semantic retrieval unavailable) retrieves nothing.
    """
    if not embedding:
        return []
    async with conn.cursor() as cur:
        await cur.execute("""SELECT id, chapter_index, text FROM chunk
      WHERE novel_id=%s AND chapter_index<=%s AND embedding IS NOT NULL
        AND embedding_space IS NOT DISTINCT FROM %s
      ORDER BY embedding <=> %s::vector, id LIMIT %s""",
            (novel_id, at, embedding_space, vector_literal(embedding), max_chunks))
        return [Source("chunk", row[0], row[1], row[2]) for row in await cur.fetchall()]

def build_context(sources: list[Source], max_chars: int) -> tuple[str, list[dict]]:
    parts: list[str] = []
    used: list[dict] = []
    total = 0
    for source in sources:
        item = f"[{source.kind}:{source.id} ch:{source.chapter}]\n{source.text}\n"
        if total + len(item) > max_chars:
            continue
        parts.append(item)
        used.append({"kind": source.kind, "id": source.id, "chapter": source.chapter})
        total += len(item)
    return "\n".join(parts), used
