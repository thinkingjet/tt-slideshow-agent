from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class JobStatus:
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class VideoStatus:
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ScheduledPostStatus:
    SCHEDULED = "scheduled"
    FAILED = "failed"


def new_id() -> str:
    return uuid.uuid4().hex


class ClipJob(Base):
    __tablename__ = "clip_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    cta_path: Mapped[str] = mapped_column(Text, nullable=False)
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    hook_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=JobStatus.QUEUED, index=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total_videos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_videos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_videos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    videos: Mapped[list["ClipVideo"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="ClipVideo.created_at",
    )


class ClipVideo(Base):
    __tablename__ = "clip_videos"
    __table_args__ = (
        UniqueConstraint("job_id", "youtube_id", name="uq_clip_video_job_youtube_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("clip_jobs.id"), nullable=False, index=True)
    youtube_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    view_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=VideoStatus.PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    job: Mapped[ClipJob] = relationship(back_populates="videos")
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(
        back_populates="video",
        cascade="all, delete-orphan",
        order_by="ScheduledPost.created_at",
    )


class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    video_id: Mapped[str] = mapped_column(ForeignKey("clip_videos.id"), nullable=False, index=True)
    postbridge_post_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    postbridge_media_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    social_accounts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ScheduledPostStatus.SCHEDULED, index=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    video: Mapped[ClipVideo] = relationship(back_populates="scheduled_posts")


class GeneratedSlideshow(Base):
    __tablename__ = "generated_slideshows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    product_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hook: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_collection: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    slideshow_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

