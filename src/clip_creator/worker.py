from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import ClipJob, ClipVideo, JobStatus, VideoStatus
from .processor import MediaProcessor, ensure_ffmpeg_available
from .storage import LocalStorage
from .youtube import ShortMetadata, YouTubeShortsClient


class ClipWorker:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        storage: LocalStorage,
        youtube_client: YouTubeShortsClient | None = None,
        processor: MediaProcessor | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.youtube_client = youtube_client or YouTubeShortsClient()
        self.processor = processor or MediaProcessor(storage.tmp_dir)
        self.max_attempts = max_attempts

    def run_once(self) -> str | None:
        with self.session_factory() as session:
            job = self._next_job(session)
            if job is None:
                return None
            job.status = JobStatus.PROCESSING
            job.error = None
            session.commit()
            job_id = job.id

        self.process_job(job_id)
        return job_id

    def run_loop(self, sleep_seconds: float) -> None:
        ensure_ffmpeg_available()
        while True:
            processed_job_id = self.run_once()
            if processed_job_id is None:
                time.sleep(sleep_seconds)

    def process_job(self, job_id: str) -> None:
        with self.session_factory() as session:
            job = session.get(ClipJob, job_id)
            if job is None:
                raise ValueError(f"job not found: {job_id}")
            source_url = job.source_url
            limit = job.requested_limit

        try:
            shorts = self.youtube_client.discover_shorts(source_url, limit)
            if not shorts:
                raise RuntimeError(f"no shorts found for {source_url}")
            self._upsert_videos(job_id, shorts)
            self._process_pending_videos(job_id)
            self._finish_job(job_id)
        except Exception as exc:
            with self.session_factory() as session:
                job = session.get(ClipJob, job_id)
                if job:
                    self._refresh_counts(session, job)
                    job.status = JobStatus.FAILED
                    job.error = str(exc)
                    session.commit()
            raise

    def _next_job(self, session: Session) -> ClipJob | None:
        stmt = (
            select(ClipJob)
            .where(ClipJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING]))
            .order_by(ClipJob.created_at)
            .limit(1)
        )
        return session.scalars(stmt).first()

    def _upsert_videos(self, job_id: str, shorts: list[ShortMetadata]) -> None:
        with self.session_factory() as session:
            job = session.get(ClipJob, job_id)
            if job is None:
                raise ValueError(f"job not found: {job_id}")
            for short in shorts:
                existing = session.scalars(
                    select(ClipVideo).where(
                        ClipVideo.job_id == job_id,
                        ClipVideo.youtube_id == short.youtube_id,
                    )
                ).first()
                if existing is not None:
                    continue
                session.add(
                    ClipVideo(
                        job_id=job_id,
                        youtube_id=short.youtube_id,
                        title=short.title,
                        source_url=short.source_url,
                        thumbnail_url=short.thumbnail_url,
                        view_count=short.view_count,
                        duration=short.duration,
                        status=VideoStatus.PENDING,
                    )
                )
            self._refresh_counts(session, job)
            session.commit()

    def _process_pending_videos(self, job_id: str) -> None:
        while True:
            with self.session_factory() as session:
                video = session.scalars(
                    select(ClipVideo)
                    .where(
                        ClipVideo.job_id == job_id,
                        ClipVideo.status.in_(
                            [
                                VideoStatus.PENDING,
                                VideoStatus.DOWNLOADING,
                                VideoStatus.DOWNLOADED,
                                VideoStatus.PROCESSING,
                                VideoStatus.FAILED,
                            ]
                        ),
                        ClipVideo.attempts < self.max_attempts,
                    )
                    .order_by(ClipVideo.created_at)
                    .limit(1)
                ).first()
                if video is None:
                    return
                video.status = VideoStatus.DOWNLOADING
                video.error = None
                video.attempts += 1
                self._refresh_counts(session, video.job)
                session.commit()
                video_id = video.id

            try:
                self._process_one_video(video_id)
            except Exception as exc:
                with self.session_factory() as session:
                    failed_video = session.get(ClipVideo, video_id)
                    if failed_video:
                        failed_video.status = VideoStatus.FAILED
                        failed_video.error = str(exc)
                        self._refresh_counts(session, failed_video.job)
                        session.commit()

    def _process_one_video(self, video_id: str) -> None:
        with self.session_factory() as session:
            video = session.get(ClipVideo, video_id)
            if video is None:
                raise ValueError(f"video not found: {video_id}")
            job = video.job
            current_job_id = job.id
            cta_path = Path(job.cta_path)
            if not cta_path.exists():
                raise FileNotFoundError(f"CTA file does not exist: {cta_path}")
            raw_path = Path(video.raw_path) if video.raw_path else None
            output_path = self.storage.stitched_output_path(current_job_id, video.youtube_id)
            metadata = ShortMetadata(
                youtube_id=video.youtube_id,
                title=video.title,
                source_url=video.source_url,
                thumbnail_url=video.thumbnail_url,
                view_count=video.view_count,
                duration=video.duration,
            )
            output_template = self.storage.raw_output_template(current_job_id)
            hook_seconds = job.hook_seconds

        if raw_path is None or not raw_path.exists():
            raw_path = self.youtube_client.download_short(metadata, output_template)

        with self.session_factory() as session:
            video = session.get(ClipVideo, video_id)
            if video is None:
                raise ValueError(f"video not found: {video_id}")
            video.raw_path = str(raw_path)
            video.status = VideoStatus.PROCESSING
            self._refresh_counts(session, video.job)
            session.commit()

        final_path = self.processor.process_clip(
            job_id=current_job_id,
            youtube_id=metadata.youtube_id,
            raw_path=raw_path,
            cta_path=cta_path,
            output_path=output_path,
            hook_seconds=hook_seconds,
        )

        with self.session_factory() as session:
            video = session.get(ClipVideo, video_id)
            if video is None:
                raise ValueError(f"video not found: {video_id}")
            video.output_path = str(final_path)
            video.status = VideoStatus.COMPLETED
            video.error = None
            self._refresh_counts(session, video.job)
            session.commit()

    def _finish_job(self, job_id: str) -> None:
        with self.session_factory() as session:
            job = session.get(ClipJob, job_id)
            if job is None:
                raise ValueError(f"job not found: {job_id}")
            self._refresh_counts(session, job)
            exhausted_failures = session.scalars(
                select(ClipVideo).where(
                    ClipVideo.job_id == job_id,
                    ClipVideo.status == VideoStatus.FAILED,
                    ClipVideo.attempts >= self.max_attempts,
                )
            ).all()
            if job.completed_videos > 0 and not exhausted_failures:
                job.status = JobStatus.COMPLETED
                job.error = None
            elif job.completed_videos > 0:
                job.status = JobStatus.COMPLETED
                job.error = f"{len(exhausted_failures)} videos failed after retries"
            else:
                job.status = JobStatus.FAILED
                job.error = "all videos failed"
            session.commit()

    def _refresh_counts(self, session: Session, job: ClipJob) -> None:
        videos = session.scalars(select(ClipVideo).where(ClipVideo.job_id == job.id)).all()
        job.total_videos = len(videos)
        job.completed_videos = sum(video.status == VideoStatus.COMPLETED for video in videos)
        job.failed_videos = sum(video.status == VideoStatus.FAILED for video in videos)

