from pathlib import Path

from clip_creator.db import create_db_engine, create_job, init_db, make_session_factory
from clip_creator.models import JobStatus
from clip_creator.storage import LocalStorage
from clip_creator.worker import ClipWorker
from clip_creator.youtube import ShortMetadata


class FakeYouTubeClient:
    def discover_shorts(self, source_url, limit):
        return [
            ShortMetadata(
                youtube_id="dQw4w9WgXcQ",
                title="Test short",
                source_url="https://www.youtube.com/shorts/dQw4w9WgXcQ",
                thumbnail_url=None,
                view_count=100,
                duration=12.0,
            )
        ]

    def download_short(self, metadata, output_template):
        path = Path(output_template.replace("%(id)s", metadata.youtube_id).replace("%(ext)s", "mp4"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raw", encoding="utf-8")
        return path


class FakeProcessor:
    def process_clip(self, *, output_path, **_kwargs):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stitched", encoding="utf-8")
        return path


def test_worker_processes_one_job(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    init_db(engine)
    session_factory = make_session_factory(engine)
    storage = LocalStorage(tmp_path / "data")
    storage.ensure()
    cta_path = tmp_path / "cta.mp4"
    cta_path.write_text("cta", encoding="utf-8")

    with session_factory() as session:
        job = create_job(
            session,
            source_url="https://www.youtube.com/@creator/shorts",
            cta_path=str(cta_path),
            requested_limit=10,
            hook_seconds=4.0,
        )
        session.commit()
        job_id = job.id

    worker = ClipWorker(
        session_factory=session_factory,
        storage=storage,
        youtube_client=FakeYouTubeClient(),
        processor=FakeProcessor(),
    )

    assert worker.run_once() == job_id

    with session_factory() as session:
        stored = session.get(type(job), job_id)
        assert stored.status == JobStatus.COMPLETED
        assert stored.completed_videos == 1
        assert stored.videos[0].output_path is not None

