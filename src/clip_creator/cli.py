from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from .db import create_db_engine, create_job, get_job, init_db, make_session_factory, session_scope
from .models import ClipVideo
from .processor import ensure_ffmpeg_available
from .settings import load_settings
from .storage import LocalStorage
from .worker import ClipWorker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clip-creator")
    parser.add_argument("--database-url", help="Database URL. Defaults to DATABASE_URL or local SQLite.")
    parser.add_argument("--data-dir", help="Artifact directory. Defaults to CLIP_CREATOR_DATA_DIR or ./data.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create database tables and local artifact directories.")

    enqueue = subparsers.add_parser("enqueue", help="Create a Shorts scraping/stitching job.")
    enqueue.add_argument("--url", required=True, help="YouTube channel, shorts page, playlist, or video URL.")
    enqueue.add_argument("--cta", required=True, help="Path to the CTA video to append.")
    enqueue.add_argument("--limit", type=int, choices=[10, 25, 50, 100], required=True)
    enqueue.add_argument("--hook-seconds", type=float, default=None, help="Seconds to keep from each scraped short.")

    work = subparsers.add_parser("work", help="Run the queue worker.")
    work.add_argument("--loop", action="store_true", help="Keep polling for new jobs.")
    work.add_argument("--sleep", type=float, default=10.0, help="Seconds to sleep between loop polls.")
    work.add_argument("--max-attempts", type=int, default=3, help="Retries per video.")

    status = subparsers.add_parser("status", help="Show a job and its videos.")
    status.add_argument("job_id")

    outputs = subparsers.add_parser("list-outputs", help="Print completed output paths for a job.")
    outputs.add_argument("job_id")

    return parser


def _runtime(args: argparse.Namespace):
    settings = load_settings(database_url=args.database_url, data_dir=args.data_dir)
    storage = LocalStorage(settings.data_dir)
    engine = create_db_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    return settings, storage, engine, session_factory


def cmd_init_db(args: argparse.Namespace) -> int:
    _settings, storage, engine, _session_factory = _runtime(args)
    storage.ensure()
    init_db(engine)
    print("database initialized")
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    settings, storage, engine, session_factory = _runtime(args)
    storage.ensure()
    init_db(engine)

    with session_scope(session_factory) as session:
        job = create_job(
            session,
            source_url=args.url,
            cta_path=str(Path(args.cta)),
            requested_limit=args.limit,
            hook_seconds=args.hook_seconds or settings.hook_seconds,
        )
        copied_cta_path = storage.copy_cta(job.id, args.cta)
        job.cta_path = str(copied_cta_path)
        print(job.id)
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    _settings, storage, engine, session_factory = _runtime(args)
    storage.ensure()
    init_db(engine)
    ensure_ffmpeg_available()

    worker = ClipWorker(session_factory=session_factory, storage=storage, max_attempts=args.max_attempts)
    if args.loop:
        worker.run_loop(args.sleep)
        return 0

    processed_job_id = worker.run_once()
    if processed_job_id is None:
        print("no queued jobs")
    else:
        print(processed_job_id)
    return 0


def _job_to_dict(job) -> dict:
    return {
        "id": job.id,
        "source_url": job.source_url,
        "cta_path": job.cta_path,
        "requested_limit": job.requested_limit,
        "hook_seconds": job.hook_seconds,
        "status": job.status,
        "error": job.error,
        "total_videos": job.total_videos,
        "completed_videos": job.completed_videos,
        "failed_videos": job.failed_videos,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "videos": [
            {
                "id": video.id,
                "youtube_id": video.youtube_id,
                "title": video.title,
                "source_url": video.source_url,
                "thumbnail_url": video.thumbnail_url,
                "view_count": video.view_count,
                "duration": video.duration,
                "raw_path": video.raw_path,
                "output_path": video.output_path,
                "status": video.status,
                "attempts": video.attempts,
                "error": video.error,
            }
            for video in job.videos
        ],
    }


def cmd_status(args: argparse.Namespace) -> int:
    _settings, _storage, engine, session_factory = _runtime(args)
    init_db(engine)
    with session_factory() as session:
        job = get_job(session, args.job_id)
        if job is None:
            print(f"job not found: {args.job_id}", file=sys.stderr)
            return 1
        print(json.dumps(_job_to_dict(job), indent=2))
    return 0


def cmd_list_outputs(args: argparse.Namespace) -> int:
    _settings, _storage, engine, session_factory = _runtime(args)
    init_db(engine)
    with session_factory() as session:
        job = get_job(session, args.job_id)
        if job is None:
            print(f"job not found: {args.job_id}", file=sys.stderr)
            return 1
        paths = session.scalars(
            select(ClipVideo.output_path)
            .where(ClipVideo.job_id == job.id)
            .where(ClipVideo.output_path.is_not(None))
            .order_by(ClipVideo.created_at)
        ).all()
        print(json.dumps(paths, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "init-db": cmd_init_db,
        "enqueue": cmd_enqueue,
        "work": cmd_work,
        "status": cmd_status,
        "list-outputs": cmd_list_outputs,
    }
    try:
        return commands[args.command](args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

