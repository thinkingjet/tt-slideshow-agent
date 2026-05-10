#!/bin/bash

# Start Celery worker with low resource usage
# Only 1 worker process with minimal concurrency
celery -A celery_worker worker \
  --loglevel=info \
  --queues=video_processing \
  --concurrency=1 \
  --prefetch-multiplier=1 \
  --max-tasks-per-child=10 \
  --optimization=fair 