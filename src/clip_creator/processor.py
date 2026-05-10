from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


VIDEO_FILTER = (
    "scale=1080:1920:force_original_aspect_ratio=increase,"
    "crop=1080:1920,setsar=1,fps=30,format=yuv420p"
)


class ProcessingError(RuntimeError):
    pass


def run_command(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        rendered = " ".join(shlex.quote(part) for part in command)
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ProcessingError(f"command failed: {rendered}\n{stderr}")


def ensure_ffmpeg_available() -> None:
    run_command(["ffmpeg", "-version"])
    run_command(["ffprobe", "-version"])


def has_audio_stream(path: Path) -> bool:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def normalize_video_command(
    input_path: Path,
    output_path: Path,
    *,
    duration_seconds: float | None = None,
    include_source_audio: bool = True,
) -> list[str]:
    command = ["ffmpeg", "-y", "-i", str(input_path)]
    if not include_source_audio:
        command.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
    if duration_seconds is not None:
        command.extend(["-t", str(duration_seconds)])

    command.extend(["-vf", VIDEO_FILTER, "-map", "0:v:0"])
    if include_source_audio:
        command.extend(["-map", "0:a:0"])
    else:
        command.extend(["-map", "1:a:0"])

    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-b:a",
            "128k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return command


class MediaProcessor:
    def __init__(self, tmp_root: Path | str) -> None:
        self.tmp_root = Path(tmp_root)
        self.tmp_root.mkdir(parents=True, exist_ok=True)

    def process_clip(
        self,
        *,
        job_id: str,
        youtube_id: str,
        raw_path: Path | str,
        cta_path: Path | str,
        output_path: Path | str,
        hook_seconds: float,
    ) -> Path:
        job_tmp = self.tmp_root / job_id
        job_tmp.mkdir(parents=True, exist_ok=True)

        raw = Path(raw_path)
        cta = Path(cta_path)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        hook_normalized = job_tmp / f"{youtube_id}_hook.mp4"
        cta_normalized = job_tmp / f"{youtube_id}_cta.mp4"
        concat_file = job_tmp / f"{youtube_id}_concat.txt"

        run_command(
            normalize_video_command(
                raw,
                hook_normalized,
                duration_seconds=hook_seconds,
                include_source_audio=has_audio_stream(raw),
            )
        )
        run_command(
            normalize_video_command(
                cta,
                cta_normalized,
                include_source_audio=has_audio_stream(cta),
            )
        )

        concat_file.write_text(
            f"file '{hook_normalized.resolve()}'\nfile '{cta_normalized.resolve()}'\n",
            encoding="utf-8",
        )
        run_command(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        return output

