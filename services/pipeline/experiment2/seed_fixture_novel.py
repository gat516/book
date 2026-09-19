"""Load fixtures/repeat/ as a separate test novel, bypassing ingest (no queue, no provider calls).

Chapters arrive as already translated (translated_by='external'), with a locked glossary,
so experiments can run in isolation from the real book. Prints the new novel id.
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid

import psycopg
from minio import Minio

from facts import HERE
from pipeline.config import Config

GLOSSARY = [  # source, target, constraint_class
    ("凌峰", "Ling Feng", "character_name"),
    ("龍飛", "Long Fei", "character_name"),
    ("阿瑞斯", "Aries", "character_name"),
    ("亞巴頓", "Abaddon", "character_name"),
    ("芙蕾雅", "Freya", "character_name"),
    ("梅塔特隆", "Metatron", "character_name"),
    ("混沌星神體", "Chaos Star God Body", "semantic_term"),
    ("迂迴星路", "Twisting Star Path", "semantic_term"),
    ("秩序神殿", "Order Temple", "semantic_term"),
    ("天龍門", "Heavenly Dragon Gate", "semantic_term"),  # in no chapter: must never be listed
]


def main() -> None:
    cfg = Config.load()
    store = Minio(cfg.object_endpoint, access_key=cfg.object_access_key,
                  secret_key=cfg.object_secret_key, secure=cfg.object_secure)
    folder = HERE / "fixtures" / "repeat"
    novel = str(uuid.uuid4())
    with psycopg.connect(cfg.database_url) as db:
        db.execute("INSERT INTO novel (id, title, source_lang, target_lang, ontology) VALUES (%s,%s,'zh','en','{}')",
                   (novel, "Fixture: repeated plot points"))
        for index in (1, 2, 3):
            keys = {}
            for lang in ("zh", "en"):
                body = (folder / f"ch{index}.{lang}.txt").read_bytes()
                keys[lang] = f"fixtures/{novel}/{index}.{lang}.txt"
                store.put_object(cfg.object_bucket, keys[lang], io.BytesIO(body), length=len(body),
                                 content_type="text/plain; charset=utf-8")
            raw_hash = "sha256:" + hashlib.sha256((folder / f"ch{index}.zh.txt").read_bytes()).hexdigest()
            db.execute(
                """INSERT INTO chapter (novel_id, chapter_index, raw_hash, raw_uri, translated_uri, translated_by,
                                        translation_ready, status, source_meta)
                   VALUES (%s,%s,%s,%s,%s,'external',true,'done',%s)""",
                (novel, index, raw_hash, keys["zh"], keys["en"], json.dumps({})))
        for version, (source, target, kind) in enumerate(GLOSSARY, 1):
            db.execute("""INSERT INTO glossary (novel_id, source_term, target_term, version, locked_at_chapter, constraint_class)
                          VALUES (%s,%s,%s,%s,1,%s)""", (novel, source, target, version, kind))
    print(novel)


if __name__ == "__main__":
    main()
