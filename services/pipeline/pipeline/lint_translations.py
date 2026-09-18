"""Apply TRANSLATE's lint to translations stored before it existed.

Usage: python -m pipeline.lint_translations --novel UUID [--apply]

Some models (gpt-oss) wrote names as "Ling<U+00A0>Feng"; translation.lint_translation now
normalizes that at write time, and this brings earlier chapters in line. Without
--apply it only reports what would change. A changed chapter gets a new object and a new
chapter_translation_version (reason 'lint'); the previous version stays on record
(append-only, §0). Every replacement is one character for one, so mention_span offsets
into the text remain valid.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import uuid

import psycopg
from minio import Minio

from pipeline.config import Config
from pipeline.translation import lint_translation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", type=uuid.UUID, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    cfg = Config.load()
    store = Minio(cfg.object_endpoint, access_key=cfg.object_access_key,
                  secret_key=cfg.object_secret_key, secure=cfg.object_secure)
    novel = str(args.novel)
    with psycopg.connect(cfg.database_url) as db:
        chapters = db.execute(
            "SELECT chapter_index, translated_uri, translated_by, glossary_version FROM chapter "
            "WHERE novel_id=%s AND translated_uri IS NOT NULL ORDER BY chapter_index", (novel,)).fetchall()
        for index, uri, translated_by, glossary_version in chapters:
            response = store.get_object(cfg.object_bucket, uri)
            try:
                text = response.read().decode("utf-8")
            finally:
                response.close()
                response.release_conn()
            linted = lint_translation(text)
            changed = sum(a != b for a, b in zip(text, linted))
            print(f"chapter {index}: {changed} characters to normalize", flush=True)
            if not changed or not args.apply:
                continue
            body = linted.encode("utf-8")
            new_uri = f"translated/{novel}/{index}/lint-{hashlib.sha256(body).hexdigest()[:16]}.txt"
            store.put_object(cfg.object_bucket, new_uri, io.BytesIO(body), length=len(body),
                             content_type="text/plain; charset=utf-8")
            with db.transaction():
                fingerprint = db.execute(
                    "SELECT translation_fingerprint FROM chapter_translation_version "
                    "WHERE novel_id=%s AND chapter_index=%s ORDER BY version DESC LIMIT 1",
                    (novel, index)).fetchone()
                version = db.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM chapter_translation_version "
                    "WHERE novel_id=%s AND chapter_index=%s", (novel, index)).fetchone()[0]
                db.execute(
                    "INSERT INTO chapter_translation_version (novel_id,chapter_index,version,translated_uri,"
                    "translated_by,glossary_version,translation_fingerprint,reason) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'lint')",
                    (novel, index, version, new_uri, translated_by, glossary_version,
                     fingerprint[0] if fingerprint else None))
                db.execute("UPDATE chapter SET translated_uri=%s WHERE novel_id=%s AND chapter_index=%s",
                           (new_uri, novel, index))
            print(f"  -> saved as version {version}", flush=True)


if __name__ == "__main__":
    main()
