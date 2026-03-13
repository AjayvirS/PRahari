# PRahari

A webhook/event-driven platform which facilitates auto-reviewing PRs for given repositories.

---

## Architecture overview

```
GitHub  ──webhook──▶  /webhook  ──enqueue──▶  asyncio Queue  ──dequeue──▶  Worker
                      (FastAPI)                                              │
                                                                            ▼
                                                                      Reviewer (placeholder)
                                                                            │
                                                                            ▼
                                                                      GitHub Client (placeholder)
```

| Module | Path | Responsibility |
|---|---|---|
| Web app & health | `app/main.py` | FastAPI app, `/health`, lifespan management |
| Webhook receiver | `app/webhook.py` | Accept GitHub events, HMAC verification, enqueue |
| Queue | `app/queue.py` | Thin asyncio.Queue wrapper (swap for Redis later) |
| Database | `app/database.py` | SQLite setup and migration runner |
| Review jobs | `app/review_jobs.py` | Durable review job repository and dedup logic |
| Worker | `app/worker.py` | Consume events, dispatch to reviewer |
| Reviewer | `app/reviewer.py` | Placeholder — LLM review logic goes here |
| GitHub client | `app/github_client.py` | Placeholder — GitHub REST API wrapper |
| Config | `app/config.py` | `pydantic-settings` based env/`.env` loading |
| Logging | `app/logging_config.py` | Structured JSON logging via `structlog` |

---

## Prerequisites

- Python 3.12+
- (Optional) Docker + Docker Compose for the containerised setup

---

## Running locally (Python)

### 1. Clone & enter the repository

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
| `GITHUB_TOKEN` | Personal access token for GitHub API calls | Yes (for review features) |
| `GITHUB_WEBHOOK_SECRET` | Secret set on the GitHub webhook | No (skips HMAC check when empty) |
| `APP_ENV` | `development` or `production` | No (default: `development`) |
| `APP_HOST` | Bind host | No (default: `0.0.0.0`) |
| `APP_PORT` | Bind port | No (default: `8000`) |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | No (default: `INFO`) |
| `DATABASE_PATH` | SQLite database file path | No (default: `data/prahari.db`) |
| `WORKER_POLL_INTERVAL` | Seconds between worker retries on error | No (default: `5`) |

### 4. Start the service

```bash
python -m app.main
```

Or with explicit uvicorn options:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The service is now reachable at **http://localhost:8000**.
On startup the app creates the SQLite database at `DATABASE_PATH` and applies
pending migrations from `app/migrations/`.

---

## Running with Docker Compose

```bash
cp .env.example .env   # fill in your values
docker compose up --build
```

---

## Available endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns `{"status": "ok"}` — use for liveness probes |
| `POST` | `/webhook` | GitHub webhook receiver |
| `GET` | `/docs` | Interactive Swagger UI |
| `GET` | `/openapi.json` | OpenAPI schema |

## Review job schema

The `review_jobs` table stores:

- `job_id` as the primary key
- `job_type` and `status`
- `repo`, `pr_number`, and `head_sha`
- `retry_count`, `max_retries`, and `last_error`
- `created_at`, `updated_at`, `claimed_at`, `completed_at`, and `failed_at`

Dedup is enforced with a unique index on `(job_type, repo, pr_number, head_sha)`.
That prevents repeated webhook deliveries for the same PR head SHA from creating
duplicate review jobs while still allowing a new job for a new head SHA.

### Configure a GitHub webhook

1. Go to your repository → **Settings → Webhooks → Add webhook**.
2. Set **Payload URL** to `http://<your-host>:8000/webhook`.
3. Set **Content type** to `application/json`.
4. Set **Secret** to the value of `GITHUB_WEBHOOK_SECRET` in your `.env`.
5. Select **Let me select individual events** → tick **Pull requests**.

---

## Running tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

---

## Project structure

```
PRahari/
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI app & lifespan
│   ├── config.py         # Settings (pydantic-settings)
│   ├── logging_config.py # Structured JSON logging
│   ├── webhook.py        # POST /webhook receiver
│   ├── queue.py          # asyncio.Queue wrapper
│   ├── worker.py         # Background worker loop
│   ├── github_client.py  # GitHub REST API client (stub)
│   └── reviewer.py       # PR review logic (placeholder)
├── tests/
│   ├── test_config.py
│   ├── test_health.py
│   └── test_webhook.py
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── requirements.txt
└── requirements-dev.txt
```

---

Database-related files added for the durable review job layer:

- `app/database.py`
- `app/migrations/001_create_review_jobs.sql`
- `app/review_jobs.py`
- `tests/test_review_jobs.py`

## What is not implemented yet

- LLM-based review logic (see `app/reviewer.py`)
- Inline PR comments via the GitHub Reviews API (see `app/github_client.py`)
- Durable worker claiming and execution
- Authentication / multi-tenant support

