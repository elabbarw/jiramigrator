#!/bin/bash

# Start the Redis server if not already running
redis-server --daemonize yes

# Start the Celery worker in background
celery -A app.celery.celery_app worker --loglevel=info &
CELERY_PID=$!

# Start the FastAPI server
python -m app.main

# When FastAPI server exits, terminate the Celery worker
kill $CELERY_PID 