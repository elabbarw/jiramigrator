![Jira Migrator](app/static/jiramigrator.png)

# Jira Migrator

A web application for migrating Jira issues to SharePoint or API endpoints with support for background processing and email notifications.

## Features

- FastAPI backend with RESTful API
- Celery queue for background processing of migration tasks
- Simple jQuery frontend
- Support for exporting Jira issues to:
  - SharePoint document libraries
  - Custom API endpoints
- Email notifications when migration jobs complete
- Various methods to select Jira issues:
  - JQL queries
  - Project keys
  - CSV file uploads
  - Manual issue key entry
- Advanced custom fields options:
  - Field analysis for identifier extraction
  - Show all or selected fields in output
  - Use view screens for field visibility
  - Configurable identifier field terms
  - Configurable important field terms
  - Manual field selection

## Requirements

- Docker and Docker Compose
- Jira instance
- SharePoint site (for SharePoint export)
- SMTP server (for email notifications, optional)

## Installation

1. Clone the repository:
   ```
   git clone <repository-url>
   cd jiramigrator
   ```

2. Create an `.env` file (you can copy from `.env.docker.sample`):
   ```
   cp .env.docker.sample .env
   ```

3. Edit the `.env` file with your actual credentials and settings:
   ```
   # Jira settings
   JIRA_URL = "https://your-jira-instance.com/"
   username = "your-username"
   password = "your-api-token"

   # SharePoint settings
   client_id = "your-client-id"
   SHAREPOINT_SITE = "your-sharepoint-site"

   # Email settings
   SMTP_SERVER = "smtp.office365.com"
   SMTP_PORT = 587
   SMTP_USERNAME = "your-email@company.com"
   SMTP_PASSWORD = "your-email-password"
   EMAIL_FROM = "jira-migration@company.com"

   # Celery settings
   CELERY_BROKER_URL = "redis://redis:6379/0"
   CELERY_RESULT_BACKEND = "redis://redis:6379/0"
   ```

4. Make sure your certificate files are in the `./certs/` directory:
   - certs/certificate.crt
   - certs/certificate.pem

5. Build and start the containers:
   ```
   docker-compose up -d --build
   ```

6. Access the application at http://localhost:8000 and Flower monitoring at http://localhost:5555

> **Note:** If you have Traefik working with SSL certificates, you can change `ports: "8000:8000"` to `expose: "8000"` in `docker-compose.yml` for the web service and access the app through Traefik on HTTPS instead. Update the Traefik host rule label to match your domain.

## Docker Commands

- Start all services: `docker-compose up -d`
- Stop all services: `docker-compose down`
- View logs: `docker-compose logs -f`
- View web service logs: `docker-compose logs -f web`
- View worker logs: `docker-compose logs -f worker`
- Rebuild and restart: `docker-compose up -d --build`
- Stop and remove volumes: `docker-compose down -v`

## Identifier Extraction

During migration, the app can automatically extract identifiers (account numbers, reference IDs, etc.) from Jira issue fields using NLP pattern matching powered by [spaCy](https://spacy.io/). Extracted identifiers are used for naming exported files/folders and are recorded in job logs.

### How It Works

1. The app scans Jira issue fields whose names match your configured **Identifier Field Terms** (e.g., fields named "Account ID", "Reference Number").
2. It also always scans the issue's summary and description.
3. The combined text is run through spaCy's rule-based `Matcher` using patterns defined in `jiramigration/patterns.json`.
4. If a match is found, the first identifier is used for file/folder naming. If not, the Jira issue key is used instead.

### Configuration

**Identifier Field Terms** (set in the web UI under Advanced Options):

Comma-separated terms that identify which Jira fields may contain extractable identifiers. The app matches these terms against field display names (case-insensitive). Defaults: `identifier, id, reference, account`.

**patterns.json** (`jiramigration/patterns.json`):

Defines the spaCy token matcher patterns. Each pattern is a list of token matchers using [spaCy's rule-based matching syntax](https://spacy.io/usage/rule-based-matching). The default patterns match common alphanumeric identifier formats:

```json
{
  "patterns": [
    [{"TEXT": {"REGEX": "[A-Za-z]{2,8}\\d{4,12}"}}],
    [{"ORTH": {"REGEX": "[A-Za-z]{2,8}\\d{4,12}"}}],
    [{"TEXT": {"REGEX": "[A-Za-z0-9]{8,20}"}}]
  ]
}
```

To match your organization's identifier formats, edit this file. For example, to match identifiers like `CUST-123456`:

```json
[{"TEXT": {"REGEX": "[A-Z]{4}-\\d{6}"}}]
```

The spaCy `en_core_web_md` model is included automatically in the Docker image.

## Author

**Wanis S. Elabbar** - [@elabbarw](https://github.com/elabbarw)

## License

This project is licensed under the MIT License - see the LICENSE file for details.
