# Product Slideshow Automation

One-click automation that calls the existing `ai-ugc` app APIs to:

1. run or load product research,
2. select the top research hook,
3. auto-pick a non-empty slideshow image collection,
4. generate a hook + product slideshow draft,
5. start the exported slide rendering job,
6. show progress and the finished slideshow.

## Run

```bash
pnpm install
pnpm dev
```

By default the app runs on `http://localhost:3010` and proxies API requests to `http://localhost:3000`.

Set `AI_UGC_BASE_URL` if your `ai-ugc` app is running somewhere else:

```bash
AI_UGC_BASE_URL=http://localhost:3000 pnpm dev
```

Open the automation in a browser where you are already signed in to the `ai-ugc` app so the proxy can forward your session cookies.

## Railway Backend Mode

To run the full workflow on the separate automation backend, set:

```bash
NEXT_PUBLIC_AUTOMATION_BACKEND_URL=https://your-automation-backend.up.railway.app
```

Then paste a Supabase access token into the UI. Leaving the token blank keeps the app in local `ai-ugc` proxy mode.
