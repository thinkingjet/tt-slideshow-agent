from pathlib import Path

from clip_creator.processor import normalize_video_command


def test_normalize_command_trims_hook_and_maps_source_audio():
    command = normalize_video_command(
        Path("raw.mp4"),
        Path("hook.mp4"),
        duration_seconds=4.0,
        include_source_audio=True,
    )

    assert command[:4] == ["ffmpeg", "-y", "-i", "raw.mp4"]
    assert "-t" in command
    assert command[command.index("-t") + 1] == "4.0"
    assert "-map" in command
    assert "0:a:0" in command
    assert command[-1] == "hook.mp4"


def test_normalize_command_adds_silent_audio_when_needed():
    command = normalize_video_command(
        Path("raw.mp4"),
        Path("hook.mp4"),
        include_source_audio=False,
    )

    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in command
    assert "1:a:0" in command

