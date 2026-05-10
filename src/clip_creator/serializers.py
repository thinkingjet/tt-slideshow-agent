from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from .models import ClipJob, ClipVideo, ScheduledPost, VideoStatus


def video_url(path: str | None) -> str | None:
    if not path:
        return None
    media_path = Path(path)
    if media_path.is_absolute():
        try:
            media_path = media_path.relative_to(Path.cwd())
        except ValueError:
            media_path = Path(media_path.name)
    return f"/media-files/{quote(media_path.as_posix())}"


def scheduled_post_to_dict(post: ScheduledPost) -> dict:
    return {
        "id": post.id,
        "video_id": post.video_id,
        "postbridge_post_id": post.postbridge_post_id,
        "postbridge_media_id": post.postbridge_media_id,
        "caption": post.caption,
        "scheduled_at": post.scheduled_at.isoformat() if post.scheduled_at else None,
        "social_accounts": json.loads(post.social_accounts_json or "[]"),
        "status": post.status,
        "error": post.error,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "updated_at": post.updated_at.isoformat() if post.updated_at else None,
    }


def clip_video_to_dict(video: ClipVideo) -> dict:
    return {
        "id": video.id,
        "job_id": video.job_id,
        "youtube_id": video.youtube_id,
        "title": video.title,
        "source_url": video.source_url,
        "thumbnail_url": video.thumbnail_url,
        "view_count": video.view_count,
        "duration": video.duration,
        "raw_path": video.raw_path,
        "raw_url": video_url(video.raw_path),
        "output_path": video.output_path,
        "output_url": video_url(video.output_path),
        "status": video.status,
        "attempts": video.attempts,
        "error": video.error,
        "scheduled_posts": [scheduled_post_to_dict(post) for post in video.scheduled_posts],
    }


def clip_job_to_dict(job: ClipJob) -> dict:
    videos = list(job.videos)
    total_videos = len(videos) or job.total_videos
    completed_videos = sum(video.status == VideoStatus.COMPLETED for video in videos)
    failed_videos = sum(video.status == VideoStatus.FAILED for video in videos)
    active_videos = sum(
        video.status in {VideoStatus.DOWNLOADING, VideoStatus.PROCESSING}
        for video in videos
    )
    pending_videos = sum(
        video.status in {VideoStatus.PENDING, VideoStatus.DOWNLOADED}
        for video in videos
    )
    denominator = total_videos or job.requested_limit or 1
    progress_percent = round(((completed_videos + failed_videos) / denominator) * 100)
    return {
        "id": job.id,
        "source_url": job.source_url,
        "cta_path": job.cta_path,
        "requested_limit": job.requested_limit,
        "hook_seconds": job.hook_seconds,
        "status": job.status,
        "error": job.error,
        "total_videos": total_videos,
        "completed_videos": completed_videos,
        "failed_videos": failed_videos,
        "active_videos": active_videos,
        "pending_videos": pending_videos,
        "progress_percent": min(progress_percent, 100),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "videos": [clip_video_to_dict(video) for video in videos],
    }

