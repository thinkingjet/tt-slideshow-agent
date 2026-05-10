# Automation Backend

Separate Railway backend for one-click product slideshow automation.

This is a copy of the `ai-ugc/backend` queue backend with extra automation endpoints:

- `POST /automation/product-slideshow` runs product research, chooses a hook, picks a slideshow image collection, generates a draft, creates an export row, and enqueues rendering.
- `GET /automation/product-slideshow/{generated_slideshow_id}` returns export status for the authenticated user.
- `GET /automation/products/context` returns saved research projects and saved products for the authenticated user.
- `POST /process-slideshow` remains compatible with the existing `ai-ugc` frontend export route.

## Railway Services

Create three Railway services from this folder:

1. API

```bash
python -m uvicorn task_queue_server:app --host 0.0.0.0 --port $PORT
```

2. Worker

```bash
celery -A celery_worker worker --loglevel=info --queues=video_processing --concurrency=1 --prefetch-multiplier=1
```

3. Redis

Use Railway Redis and set `REDIS_URL` on both API and worker.

## Required Environment

Copy `.env.example` into Railway variables for both API and worker. The API needs Supabase, OpenAI, Perplexity, and Redis variables. The worker needs Supabase, R2, and Redis variables.

## Frontend Wiring

Set this in `product-slideshow-automation` when you want the UI to call this backend directly:

```bash
NEXT_PUBLIC_AUTOMATION_BACKEND_URL=https://your-automation-backend.up.railway.app
```

The automation backend expects:

```text
Authorization: Bearer <supabase access token>
```

For local fallback, leave `NEXT_PUBLIC_AUTOMATION_BACKEND_URL` empty and the automation UI will keep using the local `ai-ugc` proxy mode.

## Existing Frontend Export Wiring

The existing `ai-ugc` app can use this backend for exports by setting:

```bash
NEXT_PUBLIC_API_BASE_URL=https://your-automation-backend.up.railway.app
```

That will make `ai-ugc/app/api/slideshows/generate-images/route.ts` submit to this backend's `/process-slideshow` route.
