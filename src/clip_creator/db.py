from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, ClipJob, JobStatus
from .settings import DEFAULT_DATABASE_URL


ALLOWED_LIMITS = {10, 25, 50, 100}


def normalize_database_url(database_url: str | None = None) -> str:
    url = database_url or DEFAULT_DATABASE_URL
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def create_db_engine(database_url: str | None = None) -> Engine:
    url = normalize_database_url(database_url)
    if url.startswith("sqlite:///"):
        db_path = Path(url.removeprefix("sqlite:///"))
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def validate_limit(limit: int) -> int:
    if limit not in ALLOWED_LIMITS:
        allowed = ", ".join(str(value) for value in sorted(ALLOWED_LIMITS))
        raise ValueError(f"limit must be one of: {allowed}")
    return limit


def create_job(
    session: Session,
    *,
    source_url: str,
    cta_path: str,
    requested_limit: int,
    hook_seconds: float,
) -> ClipJob:
    job = ClipJob(
        source_url=source_url,
        cta_path=cta_path,
        requested_limit=validate_limit(requested_limit),
        hook_seconds=hook_seconds,
        status=JobStatus.QUEUED,
    )
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: str) -> ClipJob | None:
    return session.get(ClipJob, job_id)


def next_queued_job(session: Session) -> ClipJob | None:
    stmt = (
        select(ClipJob)
        .where(ClipJob.status == JobStatus.QUEUED)
        .order_by(ClipJob.created_at)
        .limit(1)
    )
    return session.scalars(stmt).first()

