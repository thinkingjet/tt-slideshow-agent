# Clip Creator

CLI backend worker for scraping YouTube Shorts, keeping the first hook segment, stitching a CTA video onto the end, and writing ready-to-upload MP4 files.

This is the first backend slice of the product. It is intentionally CLI-first so the scraper and video pipeline can be tested before building the dashboard and scheduler.

The app now also includes a local web dashboard for uploading CTAs, creating scrape jobs, previewing generated clips, and scheduling them through PostBridge.

## Requirements

- Python 3.11+
- FFmpeg and FFprobe on your `PATH`
- Network access to YouTube

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Add your PostBridge key to `.env`:

```bash
POSTBRIDGE_API_KEY=your_key_here
```

Add your OpenAI key for slideshow generation:

```bash
OPENAI_API_KEY=your_key_here
OPENAI_TEXT_MODEL=gpt-4.1-mini
```

Initialize the local SQLite queue:

```bash
clip-creator init-db
```

## Usage

Create a job from a YouTube Shorts page, channel, playlist, or single video URL:

```bash
clip-creator enqueue \
  --url "https://www.youtube.com/@creator/shorts" \
  --cta ./cta.mp4 \
  --limit 10
```

Run one queued job:

```bash
clip-creator work
```

Inspect job status:

```bash
clip-creator status <job-id>
```

Print stitched output files:

```bash
clip-creator list-outputs <job-id>
```

The first version stores artifacts locally:

- `data/raw/<job-id>/` for downloaded Shorts
- `data/cta/<job-id>.*` for copied CTA videos
- `data/outputs/<job-id>/` for stitched MP4 outputs
- `data/tmp/<job-id>/` for normalized intermediate files

## Worker Mode

For a hosted worker process, run:

```bash
clip-creator work --loop --sleep 10
```

The included `Dockerfile` installs FFmpeg and starts the worker in loop mode by default. Railway can provide `DATABASE_URL`; local development defaults to `sqlite:///data/clip_creator.db`.

## Web Dashboard

Run the local dashboard:

```bash
clip-creator-web
```

Open `http://localhost:8000`.

The dashboard lets you:

- upload a CTA video
- paste a YouTube Shorts URL
- generate TikTok slideshow scripts from saved research
- generate 10, 25, 50, or 100 stitched clips
- preview and download finished 1080x1920 clips
- load TikTok, Instagram, and YouTube accounts from PostBridge
- schedule selected clips with a caption, start time, and stagger interval
- see scheduled clips in a calendar-style view

PostBridge scheduling uses this flow per clip:

1. Request a signed upload URL from `/v1/media/create-upload-url`.
2. Upload the generated MP4 to the signed URL.
3. Create a scheduled post with `/v1/posts`.

There is no PostBridge bulk endpoint, so the app schedules clips one by one and waits briefly between clips to stay under the current 10 requests/second API limit.

## Processing Details

Each output uses:

- first `3` seconds of the scraped short by default
- user CTA appended after the hook
- normalized 1080x1920 video
- H.264 video, AAC audio, 30 FPS, `yuv420p`

If a source clip has no audio stream, the worker inserts silent AAC audio so FFmpeg concat remains stable.

## Legal Note

You are responsible for ensuring you have the rights to any content you upload, scrape, stitch, schedule, or distribute through this tool.

## Tests

```bash
pytest
```

The automated tests avoid real YouTube and FFmpeg network/media work. Use the CLI flow above as the smoke test for live scraping and stitching.

