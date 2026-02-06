# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Jira to SharePoint Migration App - A Dockerized FastAPI web application with Celery background workers for migrating Jira issues to SharePoint or custom API endpoints. Uses Redis as message broker, SQLite for persistence, and includes real-time WebSocket updates.

## Commands

This is a Docker Compose-only application. All services run in containers.

```bash
docker-compose up -d --build      # Build and start all services
docker-compose down               # Stop all services
docker-compose logs -f web        # Web service logs
docker-compose logs -f worker     # Worker logs
docker-compose down -v            # Stop and remove volumes
```

Services: `traefik` (reverse proxy), `web` (FastAPI :8000), `redis` (:6379), `worker` (Celery), `beat` (Celery Beat), `flower` (monitoring :5555).

## Architecture

```
Traefik (SSL/TLS)
       │
  ┌────┼─────────────────┐
  │    │                  │
FastAPI(web)    Celery(worker)   Flower(5555)
  │              │
  └──────┬───────┘
         │
    ┌────┴────┐
  Redis    SQLite
 (broker)  (persistence)
```

### Request Flow
1. User submits migration via web UI -> `app/api/endpoints.py` creates Job record in SQLite
2. Celery task `process_migration` queued via Redis -> `app/celery/worker.py`
3. Worker instantiates `JiraMigration` from `jiramigration/sharepoint_cmd.py`
4. Each Jira issue processed in parallel via `ThreadPoolExecutor`: extract identifiers (spaCy NLP), generate PDF (wkhtmltopdf/pdfkit), upload to SharePoint or custom API
5. Progress callback updates `JobProgress` -> broadcast via WebSocket (`/ws/job/{job_id}`)
6. Per-issue results saved to `JobLog`, counters updated on `Job`

### Two Parallel Codepaths
- **`jiramigration/sharepoint_cmd.py`** (`JiraMigration` class) - Used by the web app via Celery worker. Handles both SharePoint and custom API exports. This is the primary orchestrator.
- **`jiramigration/api_export.py`** (`ApiExporter` class) - Standalone CLI script for API-only exports. Shares `PatternMatcher` and `patterns.json` but has its own Jira client setup and field scanning logic.

### Key Modules

**`app/`** - FastAPI application
- `main.py` - Entry point, page routes, WebSocket endpoints (`/ws`, `/ws/job/{job_id}`)
- `api/endpoints.py` - `POST /api/migration/submit` (main migration trigger)
- `api/jobs.py` - Job CRUD, logs, CSV export, resume/restart failed jobs
- `api/configurations.py` - Saved migration profiles CRUD
- `api/websocket.py` - `ConnectionManager` class for real-time broadcast
- `celery/worker.py` - `process_migration` task, `record_issue_result` (single DB session for progress + log + counters)
- `core/constants.py` - `DEFAULT_IDENTIFIER_FIELD_TERMS`, `DEFAULT_IMPORTANT_FIELD_TERMS`
- `models/job.py` - `Job`, `JobLog`, `JobProgress` models
- `models/configuration.py` - `Configuration`, `Certificate` models
- `models/settings.py` - `EnvironmentVariable` with Fernet encryption
- `static/js/app.js` - jQuery frontend, form submission, WebSocket listeners

**`jiramigration/`** - Core migration logic
- `sharepoint_cmd.py` - `JiraMigration` class: Jira API calls, field extraction, PDF generation, SharePoint/API upload, retry with tenacity
- `modules/spacymatcher.py` - `PatternMatcher` class (aliased as `SpacyMatcher` for backward compat): spaCy `en_core_web_md` model + rule-based `Matcher` using patterns from `patterns.json`
- `modules/uploadsharepoint.py` - `SharepointUpload` class: MSAL certificate auth, Office365-REST-Python-Client
- `modules/PreparePDF.py` - `TicketPDF()` function: HTML-to-PDF via pdfkit
- `patterns.json` - spaCy matcher patterns for identifier extraction (customizable per organization)

## Identifier Extraction

The `PatternMatcher` in `modules/spacymatcher.py` extracts identifiers from Jira issue text:
1. Fields matching `identifier_field_terms` (default: `["identifier", "id", "reference", "account"]`) are scanned by comparing terms against Jira field display names via `self.field_mapping`
2. Summary and description are always included
3. Combined text is run through spaCy's rule-based `Matcher` with patterns from `patterns.json`
4. Extracted identifier influences file/folder naming in SharePoint and is sent as a parameter to custom APIs

To customize: edit `jiramigration/patterns.json` with [spaCy matcher patterns](https://spacy.io/usage/rule-based-matching).

## Key Patterns

### Progress Callback Chain
`JiraMigration.set_progress_callback()` -> called per-issue from `run_migration()` -> `worker.py:progress_callback()` -> `record_issue_result()` (single DB transaction for progress + log + job counters)

### Retry Logic
- `tenacity` decorator on `migrate_jira_issue()`: retries on `ClientRequestException`, `RequestException`, `AttributeError` with exponential backoff (3 attempts)
- Job-level resume: `POST /api/jobs/{id}/resume` re-queues only failed/unprocessed issues, skipping successful ones via `skip_issues` set

### Connection Pooling
`requests.adapters.HTTPAdapter` configured with `pool_connections=100, pool_maxsize=100` at module level in `sharepoint_cmd.py` to support high parallelism.

### Temporary File Management
`JiraMigration` creates a `base_temp_dir` (system temp) per migration run. Per-issue temp dirs are tracked in `batch_temp_dirs` and cleaned up after the entire batch completes (not per-issue, to avoid race conditions during retries).

## Environment Configuration

Key `.env` variables:
- **Jira**: `JIRA_URL`, `username`, `password` (API token)
- **SharePoint**: `client_id`, `SHAREPOINT_SITE`
- **Azure**: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CERT_PATH`, `AZURE_KEY_PATH`
- **Celery**: `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` (default: `redis://redis:6379/0`)
- **SMTP**: `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`
- **Database**: `DATABASE_URL` (default: `sqlite:///./jira_migration.db`)

Certificates go in `./certs/` (mounted into containers).

## Docker Setup

- `Dockerfile.web` and `Dockerfile.worker` both use Python 3.12-slim-bullseye
- wkhtmltopdf installed from GitHub releases (for PDF generation)
- spaCy `en_core_web_md` model downloaded at build time
- Traefik v3.0 handles SSL termination and routing (config in `traefik/`)
