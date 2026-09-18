"""Ask the model for a chapter's important facts, with a suggested format it may ignore.

Soft conformance on purpose: no JSON mode, no schema, no validation. The only question
this answers is "what does the model think matters?" The raw answer is saved as-is.

The chapter is read from the object store: `--input raw` is the original Chinese
(`chapter.raw_uri`), `--input english` is the pipeline's own translation
(`chapter.translated_uri`), so both inputs are exactly what production holds.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import psycopg
from minio import Minio

from novel_llm.custom import CustomProvider
from novel_llm.deepseek import DeepSeekProvider
from pipeline.config import Config
from pipeline.llm.provider import Class
from pipeline.provider_config import build_provider, resolve_provider_config

HERE = Path(__file__).parent


def make_provider(args, row, cfg):
    """The book's configured provider by default; `--provider deepseek` tries another
    model without touching the book's saved configuration (key from DEEPSEEK_API_KEY)."""
    if args.provider == "deepseek":
        return DeepSeekProvider(model=args.model or "deepseek-v4-flash")
    if args.provider == "local":
        # A llama-server on this machine (OpenAI-compatible); it ignores the key.
        return CustomProvider(model=args.model or "ling-3.0-tiny",
                              base_url="http://127.0.0.1:8090/v1", api_key="local",
                              timeout=300.0)  # CPU: ~40s to read a chapter, ~10 tokens/s to write
    return build_provider(row, cfg)


def read_object(cfg, key):
    store = Minio(cfg.object_endpoint, access_key=cfg.object_access_key,
                  secret_key=cfg.object_secret_key, secure=cfg.object_secure)
    response = store.get_object(cfg.object_bucket, key)
    try:
        return response.read().decode("utf-8")
    finally:
        response.close()
        response.release_conn()


async def main(args):
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url) as db:
        found = await (await db.execute(
            "SELECT raw_uri, translated_uri FROM chapter WHERE novel_id=%s AND chapter_index=%s",
            (args.novel, args.chapter))).fetchone()
        if found is None:
            raise SystemExit(f"no chapter {args.chapter} for novel {args.novel}")
        key = found[0] if args.input == "raw" else found[1]
        if key is None:
            raise SystemExit(f"chapter {args.chapter} has no {args.input} text yet")
        row = await resolve_provider_config(db, args.novel, cfg.llm_provider)
        # Gated on locked_at_chapter <= chapter: a term locked later is future knowledge (§0).
        glossary = await (await db.execute(
            "SELECT source_term, target_term FROM glossary"
            " WHERE novel_id=%s AND locked_at_chapter<=%s AND NOT deleted ORDER BY source_term",
            (args.novel, args.chapter))).fetchall() if args.glossary else []
    chapter_text = read_object(cfg, key)
    provider = make_provider(args, row, cfg)

    system = (HERE / "prompts" / args.prompt).read_text()
    if glossary:
        system += ("\nWhen these names come up, write them in English exactly as given:\n"
                   + "".join(f"- {src} → {dst}\n" for src, dst in glossary))
    try:
        completion = await provider.complete(chapter_text, system=system, cls=Class.BATCH, model=args.model,
                                             max_output_tokens=args.max_output_tokens)
    finally:
        await provider.aclose()

    out = HERE / "results" / args.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "response.md").write_text(completion.text)
    meta = {k: v for k, v in asdict(completion).items() if k != "text"}
    (out / "run.json").write_text(json.dumps({
        "novel_id": args.novel, "chapter": args.chapter, "input": args.input, "object_key": key,
        "prompt": args.prompt, "glossary": dict(glossary), "provider": args.provider,
        "requested_model": args.model, **meta,
    }, indent=2, ensure_ascii=False))
    print(completion.text)
    print(f"\n--- {completion.served_model}: {completion.input_tokens} in / "
          f"{completion.output_tokens} out -> {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--novel", required=True, help="novel UUID")
    parser.add_argument("--chapter", type=int, required=True, help="chapter_index")
    parser.add_argument("--input", choices=["raw", "english"], default="raw",
                        help="original source or the pipeline's translation")
    parser.add_argument("--name", required=True, help="results/<name>/ output folder")
    parser.add_argument("--prompt", default="facts-v2.txt")
    parser.add_argument("--provider", choices=["book", "deepseek"], default="book",
                        help="the book's configured provider, or DeepSeek via DEEPSEEK_API_KEY")
    parser.add_argument("--model", help="override the model (defaults: book's model / deepseek-v4-flash)")
    parser.add_argument("--glossary", action="store_true", help="add the novel's locked glossary names to the prompt")
    parser.add_argument("--max-output-tokens", type=int, default=4000)
    asyncio.run(main(parser.parse_args()))
