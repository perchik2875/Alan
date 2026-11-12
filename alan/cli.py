"""Command line interface: run the bot, inspect the base, import, export.

    python -m alan init
    python -m alan bot
    python -m alan doctor --ping
    python -m alan import ./result.json --limit 40
    python -m alan search "learning plan"
    python -m alan export
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import Config, load_config, require_llm, require_telegram_token
from .db import Database
from .errors import AlanError
from .llm import build_embedder, build_llm
from .store import export as export_store
from .store import imports as imports_store
from .store import lessons, notes as notes_store, raw, search

log = logging.getLogger("alan.cli")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _config(args: argparse.Namespace) -> Config:
    cfg = load_config(args.env)
    if getattr(args, "data_dir", None):
        cfg.data_dir = Path(args.data_dir).resolve()
        cfg.db_path = cfg.data_dir / "alan.db"
    cfg.ensure_dirs()
    return cfg


def _service(cfg: Config, db: Database):
    from .core.service import AlanService

    llm = build_llm(require_llm(cfg))
    embedder = build_embedder(cfg.embeddings)
    return AlanService(cfg=cfg, db=db, llm=llm, embedder=embedder), llm, embedder


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    cfg = _config(args)
    from .core import prompt_loader

    prompt_loader.prompts_library(cfg)
    db = Database(cfg.db_path)
    migrated = notes_store.migrate_legacy_paths(db, cfg)
    stats = notes_store.stats(db)
    print(f"Alan {__version__} initialised.")
    print(f"  data:  {cfg.data_dir}")
    print(f"  db:    {cfg.db_path}")
    print(f"  raw:   {cfg.raw_dir}")
    print(f"  vault: {cfg.vault_dir}")
    print(f"  prompts: {cfg.prompts_dir}")
    if migrated:
        print(f"  migrated notes into the vault: {len(migrated)}")
    print(f"  notes in base: {stats['notes']}, raw captures: {stats['raw']}")
    if not cfg.telegram_token:
        print("\nNext: set ALAN_TELEGRAM_TOKEN and ALAN_LLM_API_KEY in .env, then run `python -m alan bot`.")
    db.close()
    return 0


def cmd_bot(args: argparse.Namespace) -> int:
    cfg = _config(args)
    _setup_logging(cfg.log_level)
    require_telegram_token(cfg)
    require_llm(cfg)
    from .bot.app import run_bot

    asyncio.run(run_bot(cfg))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _config(args)
    print(f"Alan {__version__}")
    ok = True

    print(f"data dir: {cfg.data_dir} {'ok' if cfg.data_dir.exists() else 'MISSING'}")
    print(f"vault: {cfg.vault_dir} {'ok' if cfg.vault_dir.exists() else 'MISSING'}")
    print(f"prompts: {cfg.prompts_dir} ({(cfg.prompts_dir / 'persona.md').exists() and 'installed' or 'not installed'})")
    print(f"db: {cfg.db_path}")
    if cfg.db_path.exists():
        db = Database(cfg.db_path)
        stats = notes_store.stats(db)
        print("  " + json.dumps(stats, ensure_ascii=False))
        fts = db.scalar("SELECT COUNT(*) FROM notes_fts")
        print(f"  fts rows: {fts}")
        db.close()
    else:
        print("  not created yet (run `python -m alan init`)")

    print(f"telegram token: {'set' if cfg.telegram_token else 'MISSING'}")
    print(f"owner ids: {cfg.owner_ids or 'unrestricted'}")
    print(f"llm: {cfg.llm.provider}/{cfg.llm.model} key={'set' if cfg.llm.api_key else 'MISSING'}")
    print(f"embeddings: {cfg.embeddings.provider}/{cfg.embeddings.model}")

    if args.ping:
        from .llm.base import Message

        async def ping() -> int:
            # Fail fast: a health check should report a problem, not hang on it.
            llm_cfg = replace(
                require_llm(cfg),
                timeout=min(cfg.llm.timeout, 45.0),
                max_retries=0,
            )
            emb_cfg = replace(cfg.embeddings, timeout=min(cfg.embeddings.timeout, 45.0))
            llm = build_llm(llm_cfg)
            embedder = build_embedder(emb_cfg)
            try:
                print(f"llm ping: {llm_cfg.provider}/{llm_cfg.model} ...", flush=True)
                response = await llm.complete(
                    system="You are a health check. Reply with the single word OK.",
                    messages=[Message(role="user", content="ping")],
                    max_tokens=64,
                    temperature=0.0,
                )
                print(f"llm ping: OK ({response.model}) {response.text.strip()[:40]}")
                if getattr(embedder, "enabled", False):
                    print(f"embedding ping: {emb_cfg.provider}/{emb_cfg.model} ...", flush=True)
                    vectors = await embedder.embed(["alan health check"])
                    print(f"embedding ping: OK ({len(vectors[0])} dims)")
                else:
                    print("embedding ping: skipped (disabled)")
                return 0
            except Exception as exc:
                print(f"ping failed: {exc}")
                message = str(exc).lower()
                if "timed out" in message or "timeout" in message:
                    print(
                        "hint: the provider did not answer in time. Try a lighter model "
                        "(ALAN_LLM_MODEL) or check network access to the API host."
                    )
                elif "401" in message or "403" in message:
                    print("hint: the API key was rejected — check ALAN_LLM_API_KEY.")
                elif "404" in message:
                    print("hint: model not found — check ALAN_LLM_MODEL and the provider.")
                return 1
            finally:
                await llm.aclose()
                await embedder.aclose()

        code = asyncio.run(ping())
        ok = ok and code == 0

    return 0 if ok else 1


def cmd_import(args: argparse.Namespace) -> int:
    cfg = _config(args)
    _setup_logging(cfg.log_level)
    db = Database(cfg.db_path)
    service, llm, embedder = _service(cfg, db)

    async def progress(text: str) -> None:
        print(text)

    async def run() -> int:
        try:
            if args.cancel:
                ok = service.cancel_import(args.cancel)
                print(f"cancelled {args.cancel}" if ok else f"no active job {args.cancel}")
                return 0 if ok else 1

            if args.resume:
                result = await service.resume_import(args.resume, progress=progress)
                print(f"\nitems: {result.items}  captures: {result.captures}  notes: {result.notes}")
                return 0

            if not args.path:
                print("error: a path is required unless --resume/--cancel is used", file=sys.stderr)
                return 2

            if args.no_analyze:
                job = service.start_import(args.path, chat_id=args.chat)
                print(f"raw captures stored. job: {job['id']} ({job['total']} items)")
                print(f"continue with: python -m alan import --resume {job['id']}")
                return 0

            result = await service.import_path(
                args.path,
                chat_id=args.chat,
                limit=args.limit,
                make_notes=not args.no_notes,
                progress=progress,
            )
            print(
                f"\nitems: {result.items}  captures: {result.captures}  notes: {result.notes}  "
                f"duplicates: {result.duplicates}  conflicts: {result.conflicts}\n"
            )
            print(result.inventory)
            return 0
        finally:
            await llm.aclose()
            await embedder.aclose()
            db.close()

    return asyncio.run(run())


def cmd_imports(args: argparse.Namespace) -> int:
    cfg = _config(args)
    db = Database(cfg.db_path)
    jobs = imports_store.list_jobs(db, limit=args.limit)
    if not jobs:
        print("No import jobs.")
        db.close()
        return 0
    for job in jobs:
        print(
            f"{job['id']}  {job['status']:<9} {job['captured']}/{job['total']} items  "
            f"notes={job['notes_made']} dup={job['duplicates']} conflicts={job['conflicts']}  "
            f"{job['source']}  {job['created_at'][:16]}"
        )
    db.close()
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    cfg = _config(args)
    _setup_logging(cfg.log_level)
    db = Database(cfg.db_path)

    count = search.fts_rebuild(db)
    print(f"exact-term index rebuilt: {count} notes")

    if args.without_embeddings:
        db.close()
        return 0

    embedder = build_embedder(cfg.embeddings)
    if not getattr(embedder, "enabled", False):
        print("embeddings disabled; meaning-based index skipped")
        db.close()
        return 0

    async def run() -> int:
        from .textutil import now_iso

        total = 0
        try:
            while True:
                pending = search.missing_embeddings(db, embedder.model, limit=50)
                if not pending:
                    break
                vectors = await embedder.embed([search.embedding_text(n) for n in pending])
                for note, vector in zip(pending, vectors):
                    search.save_embedding(db, note.id, embedder.model, vector, at=now_iso())
                    total += 1
                print(f"embedded {total} notes…")
            print(f"meaning-based index updated: {total} notes")
            return 0
        finally:
            await embedder.aclose()
            db.close()

    return asyncio.run(run())


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _config(args)
    db = Database(cfg.db_path)
    archive = export_store.export_all(db, cfg, include_audit=not args.no_audit)
    db.close()
    print(str(archive))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = _config(args)
    db = Database(cfg.db_path)
    print(json.dumps(notes_store.stats(db), ensure_ascii=False, indent=2))
    print(f"lessons: {lessons.count_active(db)}")
    db.close()
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    cfg = _config(args)
    db = Database(cfg.db_path)
    note = notes_store.find(db, args.ref)
    if note is None:
        print("No such note.", file=sys.stderr)
        db.close()
        return 1
    print(note.body_md)
    db.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cfg = _config(args)
    db = Database(cfg.db_path)

    async def run() -> int:
        service, llm, embedder = _service(cfg, db)
        try:
            hits = await service.search_notes(args.query, limit=args.limit, include_sensitive=args.private)
            if not hits:
                print("No matches.")
                return 0
            for hit in hits:
                print(f"{hit.score:.3f}  {hit.note.type}/{hit.note.status}  {hit.note.title}  [{hit.note.slug}]  {'/'.join(hit.methods)}")
            return 0
        finally:
            await llm.aclose()
            await embedder.aclose()

    code = asyncio.run(run())
    db.close()
    return code


def cmd_raw(args: argparse.Namespace) -> int:
    cfg = _config(args)
    db = Database(cfg.db_path)
    if args.ref.startswith("r_"):
        capture = raw.get(db, args.ref)
        captures = [capture] if capture else []
    else:
        note = notes_store.find(db, args.ref)
        captures = raw.for_note(db, note.id) if note else []
    if not captures:
        print("No raw capture found.", file=sys.stderr)
        db.close()
        return 1
    for capture in captures:
        print(f"--- {capture.id} {capture.received_at} {capture.path}")
        print(capture.text)
    db.close()
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alan", description="Alan — private knowledge assistant")
    parser.add_argument("--env", default=".env", help="path to .env")
    parser.add_argument("--data-dir", default=None, help="override data directory")
    parser.add_argument("--version", action="version", version=f"Alan {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create data directories and the database").set_defaults(func=cmd_init)
    sub.add_parser("bot", help="run the Telegram bot").set_defaults(func=cmd_bot)

    doctor = sub.add_parser("doctor", help="check configuration and storage")
    doctor.add_argument("--ping", action="store_true", help="make a live model call")
    doctor.set_defaults(func=cmd_doctor)

    imp = sub.add_parser("import", help="import a Telegram export or notes folder")
    imp.add_argument("path", nargs="?", default=None)
    imp.add_argument("--chat", default="import", help="chat id to associate with the material")
    imp.add_argument("--limit", type=int, default=60, help="maximum notes to draft")
    imp.add_argument("--no-notes", action="store_true", help="store raw captures only")
    imp.add_argument("--no-analyze", action="store_true", help="stop after raw captures are stored")
    imp.add_argument("--resume", default="", help="resume an import job by id")
    imp.add_argument("--cancel", default="", help="cancel an import job by id")
    imp.set_defaults(func=cmd_import)

    imports_cmd = sub.add_parser("imports", help="list import jobs and progress")
    imports_cmd.add_argument("--limit", type=int, default=20)
    imports_cmd.set_defaults(func=cmd_imports)

    reindex = sub.add_parser("reindex", help="rebuild retrieval indexes")
    reindex.add_argument("--without-embeddings", action="store_true")
    reindex.set_defaults(func=cmd_reindex)

    export = sub.add_parser("export", help="write a portable archive")
    export.add_argument("--no-audit", action="store_true")
    export.set_defaults(func=cmd_export)

    sub.add_parser("stats", help="print knowledge base statistics").set_defaults(func=cmd_stats)

    note = sub.add_parser("note", help="print a note")
    note.add_argument("ref")
    note.set_defaults(func=cmd_note)

    search_cmd = sub.add_parser("search", help="search notes")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--limit", type=int, default=8)
    search_cmd.add_argument("--private", action="store_true", help="include private notes")
    search_cmd.set_defaults(func=cmd_search)

    raw_cmd = sub.add_parser("raw", help="print raw captures")
    raw_cmd.add_argument("ref")
    raw_cmd.set_defaults(func=cmd_raw)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
