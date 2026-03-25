# PRahari

A webhook-driven service for durable pull request review processing.

---

## Architecture overview

```text
GitHub --> /webhook --> review_jobs (SQLite) --> worker --> GitHub PR comment
```

| Layer | Path | Responsibility |
|---|---|---|
| API | `app/api/` | FastAPI route handlers and request validation |
| Business | `app/business/` | Enqueue logic, review orchestration, reviewer identity, and worker flow |
| Services | `app/services/` | GitHub API access and OpenAI-backed review generation |
| Database | `app/database/` | SQLite connections, migrations, and review job persistence |
| App bootstrap | `app/main.py` | FastAPI app startup, `/health`, and worker lifecycle |
| Config | `app/config.py` | `pydantic-settings` based env loading |
| Logging | `app/logging_config.py` | Structured JSON logging via `structlog` |

---

## Prerequisites

- Python 3.12+
- Optional: Docker and Docker Compose

---

## Running locally

### 1. Clone the repository

```bash
git clone https://github.com/AjayvirS/PRahari.git
cd PRahari
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description | Required |
|---|---|---|
| `GITHUB_TOKEN` | Personal access token used for GitHub API calls | Yes |
| `GITHUB_WEBHOOK_SECRET` | Secret configured on the GitHub webhook | No |
| `REVIEW_PROVIDER` | `deterministic` or `openai` | No |
| `OPENAI_API_KEY` | API key used when `REVIEW_PROVIDER=openai` | Only for OpenAI mode |
| `OPENAI_MODEL` | OpenAI model name for review generation | No |
| `OPENAI_BASE_URL` | Base URL for the OpenAI-compatible API | No |
| `OPENAI_TIMEOUT_SECONDS` | Timeout for OpenAI API calls | No |
| `APP_ENV` | `development` or `production` | No |
| `APP_HOST` | Bind host | No |
| `APP_PORT` | Bind port | No |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | No |
| `DATABASE_PATH` | SQLite database file path | No |
| `WORKER_POLL_INTERVAL` | Seconds between worker polls when no jobs are pending | No |

Only `.env` is loaded at runtime by default. `.env.example` is a template and is
not used unless you copy it to `.env`.

### 4. Start the service

```bash
python -m app.main
```

Or:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The service creates the SQLite database at `DATABASE_PATH` on startup and applies
pending migrations from `app/database/migrations/`.

---

## Available endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns `{"status": "ok"}` |
| `POST` | `/webhook` | GitHub webhook receiver |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/openapi.json` | OpenAPI schema |

### Configure a GitHub webhook

1. Open your repository settings on GitHub.
2. Go to `Settings -> Webhooks -> Add webhook`.
3. Set the payload URL to `http://<your-host>:8000/webhook`.
4. Set content type to `application/json`.
5. Set the secret to match `GITHUB_WEBHOOK_SECRET`.
6. Select the `Pull requests` event.

---

## Review job schema

The `review_jobs` table stores:

- `job_id` as the primary key
- `job_type` and `status`
- `repo`, `pr_number`, and `head_sha`
- `retry_count`, `max_retries`, and `last_error`
- `created_at`, `updated_at`, `claimed_at`, `completed_at`, and `failed_at`

Job statuses currently include `pending`, `processing`, `completed`, `failed`,
and `stale`. Stale jobs are terminal skips used when a pull request head SHA has
changed before the worker posts its generated review comment.

Dedup is enforced with a unique index on `(job_type, repo, pr_number, head_sha)`.
That prevents repeated webhook deliveries for the same PR head SHA from creating
duplicate review jobs while still allowing a new job when the head SHA changes.

## Processing flow

Supported `pull_request` webhook events create rows in `review_jobs`.
The worker polls for the oldest `pending` job, marks it `processing`, fetches
the pull request and changed files from GitHub, checks for an existing review
comment from the authenticated reviewer for the same SHA, generates a structured
review summary comment, re-checks the pull request head SHA immediately before
posting, and then marks the job `completed`, `stale`, or `failed`.

By default, review generation stays deterministic. Set `REVIEW_PROVIDER=openai`
and provide `OPENAI_API_KEY` to enable the OpenAI-backed reviewer. If the API
call fails or returns an invalid payload, PRahari logs the error and falls back
to the deterministic review summary instead of failing the job.

Reviewer identity for duplicate comment suppression is derived from the
authenticated GitHub user behind `GITHUB_TOKEN`.

---

## Running tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

---

## Project structure

```text
PRahari/
|-- app/
|   |-- api/
|   |   `-- webhook.py
|   |-- business/
|   |   |-- enqueue.py
|   |   |-- reviewer.py
|   |   |-- reviewer_identity.py
|   |   `-- worker.py
|   |-- database/
|   |   |-- migrations/
|   |   |   `-- 001_create_review_jobs.sql
|   |   |-- connection.py
|   |   `-- review_jobs.py
|   |-- services/
|   |   |-- github_client.py
|   |   `-- review_service.py
|   |-- __init__.py
|   |-- config.py
|   |-- logging_config.py
|   `-- main.py
|-- tests/
|   |-- api/
|   |-- business/
|   |-- config/
|   |-- database/
|   `-- services/
|-- .env.example
|-- .gitignore
|-- docker-compose.yml
|-- Dockerfile
|-- pyproject.toml
|-- requirements.txt
`-- requirements-dev.txt
```

---

## What is not implemented yet

- Repository-local prompt files and richer review policy controls
- Richer inline review comments beyond the top-level structured summary
- Authentication and multi-tenant support
