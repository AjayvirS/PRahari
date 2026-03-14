# PRahari

A webhook-driven service for durable pull request review processing.

---

## Architecture overview

```text
GitHub --> /webhook --> review_jobs (SQLite) --> worker --> GitHub PR comment
```

| Module | Path | Responsibility |
|---|---|---|
| Web app and health | `app/main.py` | FastAPI app, `/health`, startup, shutdown |
| Webhook receiver | `app/webhook.py` | Validate GitHub signatures, parse PR events, and route them into durable jobs |
| Enqueue layer | `app/enqueue.py` | Convert supported PR events into review jobs with dedup handling |
| Database | `app/database.py` | SQLite setup and migration runner |
| Review jobs | `app/review_jobs.py` | Review job repository, dedup, claim, complete, and fail transitions |
| Worker | `app/worker.py` | Claim pending jobs, fetch PR data, post placeholder comments, and mark job status |
| GitHub client | `app/github_client.py` | GitHub REST API wrapper for pull request fetch and PR comment posting |
| Reviewer | `app/reviewer.py` | Placeholder for future review generation logic |
| Config | `app/config.py` | `pydantic-settings` based env and `.env` loading |
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
| `APP_ENV` | `development` or `production` | No |
| `APP_HOST` | Bind host | No |
| `APP_PORT` | Bind port | No |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | No |
| `DATABASE_PATH` | SQLite database file path | No |
| `WORKER_POLL_INTERVAL` | Seconds between worker polls when no jobs are pending | No |

### 4. Start the service

```bash
python -m app.main
```

Or:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The service will create the SQLite database at `DATABASE_PATH` on startup and apply pending migrations from `app/migrations/`.

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

Dedup is enforced with a unique index on `(job_type, repo, pr_number, head_sha)`.
That prevents repeated webhook deliveries for the same PR head SHA from creating
duplicate review jobs while still allowing a new job when the head SHA changes.

## Processing flow

Supported `pull_request` webhook events create rows in `review_jobs`.
The worker polls for the oldest `pending` job, marks it `processing`, fetches
the pull request from GitHub, posts a placeholder PR comment, and then marks
the job `completed` or `failed`.

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
|   |-- __init__.py
|   |-- config.py
|   |-- database.py
|   |-- enqueue.py
|   |-- github_client.py
|   |-- logging_config.py
|   |-- main.py
|   |-- migrations/
|   |   `-- 001_create_review_jobs.sql
|   |-- review_jobs.py
|   |-- reviewer.py
|   |-- webhook.py
|   `-- worker.py
|-- tests/
|   |-- test_config.py
|   |-- test_enqueue.py
|   |-- test_health.py
|   |-- test_review_jobs.py
|   |-- test_webhook.py
|   `-- test_worker.py
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

- OpenAI-powered review generation
- Richer inline review comments beyond the placeholder PR comment
- Authentication and multi-tenant support
