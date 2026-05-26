<p align="center">
  <a href="https://github.com/Claudia-Anthropica">
    <img src="https://avatars.githubusercontent.com/u/262612686?v=4" width="128" height="128" style="border-radius: 50%;" alt="Claudia Anthropica" />
  </a>
</p>

<h1 align="center">Claudia</h1>
<p align="center">Autonomous developer agent powered by Claude Code</p>
<p align="center">
  <a href="https://github.com/Claudia-Anthropica">@Claudia-Anthropica on GitHub</a>
</p>

---

Claudia reviews pull requests, implements issue fixes, handles review feedback, and maintains PR hygiene — all autonomously. She runs as a persistent worker on a dedicated VM, processing jobs from a PostgreSQL queue fed by GitHub webhooks and periodic polling.

## Supported Repositories

Configured in [`repos.json`](repos.json):

| Repository | Review Label | Screenshots |
|---|---|---|
| [ls1intum/Artemis](https://github.com/ls1intum/Artemis) | `ready for review` | Yes |
| [ls1intum/thesis-management](https://github.com/ls1intum/thesis-management) | — (all open PRs) | Yes |
| [ls1intum/Apollon](https://github.com/ls1intum/Apollon) | — (all open PRs) | Yes |

## Architecture

```
GitHub Webhooks ──► webhook_receiver.py (FastAPI) ──► PostgreSQL Job Queue
                                                            │
Periodic Polling ──► worker.py ─────────────────────────────┤
                         │                                  │
                         ▼                                  │
                    Claim Job ◄─────────────────────────────┘
                         │
                         ▼
               ┌─────────────────┐
               │  Claude Code    │
               │  (headless)     │
               │                 │
               │  preamble.md    │
               │  + agent/*.md   │
               │  + overlay.md   │
               └────────┬────────┘
                        │
                  ┌─────┴──────┐
                  ▼            ▼
              GitHub API   Slack Alerts
```

### Components

| File | Role |
|---|---|
| `worker.py` | Long-running process. Polls for new PRs, claims jobs from the queue, spawns Claude Code sessions, handles retries and failures. |
| `webhook_receiver.py` | FastAPI server. Validates GitHub webhook signatures, deduplicates deliveries, classifies events, enqueues jobs. |
| `db.py` | PostgreSQL layer. Atomic job claiming via `SELECT ... FOR UPDATE SKIP LOCKED`. |
| `slack.py` | Slack notification helper. |
| `utils.py` | Shared utilities (repo config loading, GitHub user detection, lock files). |
| `stream-log.py` | Parses Claude Code stream-json output into human-readable logs. |
| `append-knowledge.sh` | Validated helper for appending to knowledge JSONL files. |

### Agents

Each agent is a self-contained prompt in [`agents/`](agents/) with YAML frontmatter (name, model, max_turns, tools):

| Agent | Description |
|---|---|
| `pr-reviewer` | Reviews a single PR — full first-time review or follow-up on previous `CHANGES_REQUESTED`. Submits structured inline comments via GitHub API. Converts PR to draft if fundamental rework is needed. |
| `pr-feedback-handler` | Handles review feedback on Claudia's own authored PRs — implements fixes, pushes back, or complies. Resolves merge conflicts. Converts to draft and back if rework is fundamental. |
| `issue-implementer` | Implements an assigned issue end-to-end: creates branch, writes code, runs tests, opens PR. |
| `pr-hygiene-checker` | Checks Claudia's own PRs for convention violations (title format, missing screenshots) and fixes them. |
| `ci-check-handler` | Handles CI check failures on Claudia's authored PRs. |
| `memory-processor` | Deduplicates and streamlines knowledge JSONL files. |

### Per-Repo Overlays

Each repo can have an overlay in `repos/<slug>/agent-overlay.md` that gets appended to every agent prompt for that repo. Contains repo-specific tooling setup, coding conventions, testing commands, and review guidance.

Screenshot procedures live in `repos/<slug>/screenshot-procedure.md`.

## Job Types & Priority

Jobs are processed in priority order (lower = higher priority):

| Priority | Type | Trigger |
|---|---|---|
| 10 | `feedback` | Review comments on Claudia's PRs |
| 20 | `ci_check` | CI failures on Claudia's PRs |
| 30 | `review` | New/updated PRs ready for review |
| 40 | `hygiene` | Periodic convention checks on own PRs |
| 50 | `memory` | Periodic knowledge file maintenance |

## Prerequisites

| Tool | Purpose |
|---|---|
| Python 3.10+ | Worker and webhook server |
| `claude` | [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) |
| `gh` | [GitHub CLI](https://cli.github.com/) — authenticated with a fine-grained PAT |
| PostgreSQL | Job queue storage |
| `git` | Repository operations |

### GitHub PAT Permissions

Fine-grained PAT scoped to the target repositories:

- **Pull requests**: read & write (reviews, creating PRs)
- **Issues**: read & write (reading details, commenting)
- **Checks**: read (CI results)
- **Contents**: read & write (pushing branches)

## Deployment

Claudia runs as two systemd services on a dedicated VM:

```bash
# Worker (processes jobs from the queue)
sudo systemctl start claudia-worker.service

# Webhook receiver (FastAPI on port 8080)
sudo systemctl start claudia-webhook.service
```

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Slack Bot User OAuth Token (`xoxb-...`), needs `chat:write` scope |
| `SLACK_CHANNEL` | Yes | Slack channel ID |
| `SLACK_REVIEW_CHANNEL` | No | Slack channel ID for review-request notifications (per-PR + daily digest). Default: `C012NFRM76F` (`#artemistest`) |
| `WEBHOOK_SECRET` | Yes | GitHub webhook HMAC-SHA256 secret |
| `MEMORIES_DIR` | No | Base directory for persistent memory (default: `~/memories`) |
| `GITHUB_USER` | No | GitHub username (auto-detected from `gh api user`) |

## Memory System

### Knowledge Files (JSONL)

Long-term learning stored per repository in `~/memories/knowledge/<repo-slug>/`:

- `coding-patterns.jsonl` — Project conventions and patterns
- `review-lessons.jsonl` — Lessons from review feedback cycles
- `common-mistakes.jsonl` — Recurring issues to watch for

Each line: `{"date":"YYYY-MM-DD","source":"PR #N","pattern":"<max 200 chars>"}`. Strict validation prevents injection.

## Security

- Dedicated VM with restricted network access
- Fine-grained PAT scoped to specific repositories
- All PR/issue content treated as untrusted data in all prompts
- Post-checkout sanitization: removes `CLAUDE.md`, `AGENTS.md`, `.claude/` instruction files
- Webhook HMAC-SHA256 signature validation
- Knowledge JSONL has strict schema validation
- Branch names validated with regex + `git check-ref-format`
