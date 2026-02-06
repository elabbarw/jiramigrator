# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Jira to SharePoint Migration App - A FastAPI web application with Celery background workers for migrating Jira issues to SharePoint or custom API endpoints. Uses Redis as message broker, SQLite/PostgreSQL for persistence, and includes real-time WebSocket updates.

## Commands

### Development
```bash
# Install dependencies
pip install -r requirements.txt

# Start Redis (required for Celery)
redis-server

# Start Celery worker (separate terminal)
celery -A app.celery.celery_app worker --loglevel=info

# Start FastAPI server with hot reload
uvicorn app.main:app --reload

# Or run directly
python -m app.main
```

### Background Services
```bash
# Celery Beat scheduler
celery -A app.celery.celery_app beat --loglevel=info

# Flower monitoring UI (http://localhost:5555)
celery -A app.celery.celery_app flower --port=5555
```

### Docker
```bash
docker-compose up -d              # Start all services
docker-compose up -d --build      # Rebuild and start
docker-compose logs -f web        # Web service logs
docker-compose logs -f worker     # Worker logs
docker-compose down               # Stop all services
```

## Architecture

### Service Layer
```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   FastAPI   │────▶│   Celery    │────▶│    Redis    │
│  (app/main) │     │  (worker)   │     │  (broker)   │
└─────────────┘     └─────────────┘     └─────────────┘
       │                   │
       ▼                   ▼
┌─────────────┐     ┌─────────────┐
│  WebSocket  │     │  SQLite/    │
│  (realtime) │     │  PostgreSQL │
└─────────────┘     └─────────────┘
```

### Key Directories

**`app/`** - FastAPI application
- `main.py` - Application entry point, routes, WebSocket endpoints
- `api/` - REST endpoints: `endpoints.py` (migration), `jobs.py` (job mgmt), `auth.py` (OAuth)
- `celery/` - `celery_app.py` (config), `worker.py` (task definitions)
- `core/` - `config.py` (settings), `database.py` (SQLAlchemy), `auth.py` (Entra ID middleware)
- `models/` - SQLAlchemy models: `job.py` (Job, JobLog, JobProgress), `settings.py` (EnvironmentVariable)
- `templates/` - Jinja2 HTML templates
- `static/js/app.js` - Frontend JavaScript (jQuery-based)

**`jiramigration/`** - Core migration logic
- `sharepoint_cmd.py` - Main migration orchestrator (JiraMigration class)
- `modules/uploadsharepoint.py` - SharePoint upload with MSAL certificate auth
- `modules/spacymatcher.py` - NLP identifier extraction using spaCy (PatternMatcher class)
- `modules/PreparePDF.py` - PDF generation from issue data
- `patterns.json` - NLP patterns for field extraction

### Data Flow
1. User submits migration via `/api/migrate` endpoint
2. FastAPI creates Job record in database
3. Celery task `process_migration` is queued
4. Worker fetches Jira issues, processes each (extract identifiers, generate PDF, upload)
5. Progress updates stored in `JobProgress`, broadcast via WebSocket
6. Results logged to `JobLog`, email notification sent on completion

### Database Models
- **Job**: Migration job metadata (status, total/processed/failed counts, config snapshot)
- **JobLog**: Per-issue results (success/failure, extracted_identifier, error details)
- **JobProgress**: Real-time progress for WebSocket updates
- **EnvironmentVariable**: Encrypted settings storage
- **Configuration**: Reusable migration profiles

### Authentication
- Microsoft Entra ID (Azure AD) OAuth 2.0 with certificate-based auth (not client secret)
- Certificate paths configured in `.env` (AZURE_CERT_PATH, AZURE_KEY_PATH)
- MSAL library for SharePoint authentication

## Key Patterns

### Parallel Issue Processing
The worker uses ThreadPoolExecutor for parallel processing. Parallelism limit configurable in `app/celery/worker.py`.

### WebSocket Updates
`app/api/websocket.py` manages connections. Broadcast to all clients or job-specific channels:
- `/ws` - Dashboard updates
- `/ws/job/{job_id}` - Job-specific updates

### Error Handling
Uses tenacity for retry logic with exponential backoff. Failed issues logged to database; jobs can be resumed to retry only failed issues.

## Environment Configuration

Key `.env` variables:
- **Jira**: `JIRA_URL`, `username`, `password` (API token)
- **SharePoint**: `client_id`, `SHAREPOINT_SITE`
- **Azure**: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CERT_PATH`, `AZURE_KEY_PATH`
- **Celery**: `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`
- **SMTP**: `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`

## Export Destinations
1. **SharePoint** - Document library with certificate auth
2. **Custom API** - Generic HTTP endpoint with configurable headers

## PDF Generation
Uses wkhtmltopdf via pdfkit. Installed system-wide in Docker containers from GitHub releases.
