from clip_creator.db import create_db_engine, create_job, init_db, make_session_factory, validate_limit
from clip_creator.models import JobStatus


def test_create_job_defaults_to_queued(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        job = create_job(
            session,
            source_url="https://www.youtube.com/@creator/shorts",
            cta_path="cta.mp4",
            requested_limit=10,
            hook_seconds=4.0,
        )
        session.commit()

    with session_factory() as session:
        stored = session.get(type(job), job.id)
        assert stored is not None
        assert stored.status == JobStatus.QUEUED
        assert stored.requested_limit == 10
        assert stored.hook_seconds == 4.0


def test_validate_limit_rejects_unsupported_values():
    try:
        validate_limit(11)
    except ValueError as exc:
        assert "10, 25, 50, 100" in str(exc)
    else:
        raise AssertionError("validate_limit should reject unsupported limits")

