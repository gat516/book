"""Save a chapter for isolated experiments; no provider calls or database writes."""
import argparse
import json
from pathlib import Path

from minio import Minio
import psycopg

from pipeline.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--chapter", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    cfg = Config.load()
    # §0: experiment inputs are chapter-scoped; never change published knowledge.
    with psycopg.connect(cfg.database_url, options="-c default_transaction_read_only=on") as db:
        row = db.execute("SELECT raw_uri FROM chapter WHERE novel_id=%s AND chapter_index=%s",
                         (args.novel, args.chapter)).fetchone()
    if row is None:
        raise SystemExit("Chapter not found")
    client = Minio(cfg.object_endpoint, access_key=cfg.object_access_key,
                   secret_key=cfg.object_secret_key, secure=cfg.object_secure)
    response = client.get_object(cfg.object_bucket, row[0])
    try:
        source = response.read().decode("utf-8")
    finally:
        response.close()
        response.release_conn()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as destination:
        json.dump({"novel_id": args.novel, "case": {"chapter": args.chapter, "source": source}},
                  destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    print(json.dumps({"chapter": args.chapter, "characters": len(source), "case": str(args.output)}))


if __name__ == "__main__":
    main()
