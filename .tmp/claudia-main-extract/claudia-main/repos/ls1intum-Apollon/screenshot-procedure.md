# Screenshot Capture Procedure (Apollon)

This procedure is for the Apollon project (UML modeling editor — npm-workspaces monorepo with `library/`, `standalone/webapp/`, `standalone/server/`, `vscode-extension/`). The standalone webapp is **public** (no auth) — for most UI changes you only need the webapp (`/` route), not the server.

## Hard Rules

- **NEVER generate fake, mockup, synthetic, or static HTML screenshots.** Only real browser screenshots of the running app are acceptable.
- **NEVER give up on fixable problems.** Diagnose, fix, retry — at least 3-5 attempts before giving up.
- **Kill all background processes** (vite, tsx watch, docker) when done.

## Decide Which Route to Target

| Change scope | Route | Server needed? | Library rebuild? |
|---|---|---|---|
| Library rendering (UML elements, canvas, palette) | `/` (ApollonLocal) | No | **Yes** — webapp imports `@tumaet/apollon` from the workspace |
| Webapp shell, navbar, theme, navigation | `/` | No | Only if webapp imports changed lib code |
| `/playground` page | `/playground` | No | Same as above |
| Collaborative diagram (`/:diagramId`) | `/<id>` | **Yes** — Redis + standalone server | Same as above |

Default to `/` unless the diff explicitly touches `ApollonWithConnection`, server code, or the collab/Yjs layer.

## Step 1: Initialize Node + npm

```bash
[[ -s "$HOME/.nvm/nvm.sh" ]] && source "$HOME/.nvm/nvm.sh"
nvm install   # reads .nvmrc (v22.14.0)
[ "$(npm -v | cut -d. -f1)" -lt 11 ] && npm install -g npm@^11.1.0   # engines require npm >=11.1.0
```

## Step 2: Pin Ports + Pick Launcher

Apollon reads `APOLLON_WEBAPP_PORT` / `APOLLON_SERVER_PORT` / `APOLLON_WS_PORT` / `APOLLON_REDIS_PORT` (defaults: 5173 / 8000 / 4444 / 6379). The orchestrator `scripts/dev.mjs` will shift to free ports if defaults are busy, so **never assume a port — read the printed URL from the launcher output**. Pin them explicitly to avoid surprises:

```bash
export APOLLON_WEBAPP_PORT=5173
export APOLLON_SERVER_PORT=8000
export APOLLON_WS_PORT=4444
export APOLLON_REDIS_PORT=6379
```

### Launcher choice

| Target route | Command | What it does |
|---|---|---|
| `/` or `/playground` (no collab) | `npm run build:lib && npm run dev:webapp &` | Build library once, start Vite. Faster startup. |
| `/<id>` (collab) | `npm run dev &` | Orchestrates Redis + lib watch + server + webapp. Slower but everything. |
| Any route, **library changes in this PR** | `npm run dev &` | Watch mode picks up library edits automatically. |

Capture the launcher's stdout/stderr so we can parse the printed URL:

```bash
DEV_LOG=/tmp/apollon-dev.log
: > "$DEV_LOG"                                          # truncate any prior run
npm run dev > "$DEV_LOG" 2>&1 &                         # OR: { npm run build:lib && npm run dev:webapp; } > "$DEV_LOG" 2>&1 &
LAUNCHER_PID=$!
```

### Wait for the webapp to be reachable

`scripts/dev.mjs` (and Vite under it) prints a `http://localhost:<port>` line for the webapp once ready. Wait for the log line, extract the URL, then probe it:

```bash
for i in $(seq 1 60); do
  grep -E "http://localhost:[0-9]+" "$DEV_LOG" > /dev/null 2>&1 && break
  sleep 2
done
WEBAPP_URL=$(grep -oE "http://localhost:[0-9]+" "$DEV_LOG" | head -1)

# Fallback (only if the log line never appeared — should not normally happen)
WEBAPP_URL=${WEBAPP_URL:-http://localhost:$APOLLON_WEBAPP_PORT}

for i in $(seq 1 30); do
  curl -sf "$WEBAPP_URL" > /dev/null 2>&1 && break
  sleep 2
done
curl -sf "$WEBAPP_URL" > /dev/null 2>&1 || { echo "Webapp failed to start — tail of $DEV_LOG:"; tail -50 "$DEV_LOG"; kill $LAUNCHER_PID 2>/dev/null; exit 1; }
```

### If the launcher fails to start

- Kill stale processes: `pkill -f vite 2>/dev/null; pkill -f "tsx watch" 2>/dev/null`
- Port collision: `kill $(lsof -ti :$APOLLON_WEBAPP_PORT) 2>/dev/null` (and similarly for server/ws ports) then retry
- Docker not running (only for collab launcher): `docker info > /dev/null 2>&1 || systemctl --user start docker`
- Library build error: run `npm run build:lib 2>&1 | tail -50` standalone and fix the error
- Try 3-5 times with a fix between each attempt before giving up

## Step 4: Capture Screenshots

Use the `@playwright/cli` command-line tool (NOT the Node.js library). Stateful interactive CLI — each command runs against the same browser session. Install browsers if needed: `npx playwright install chromium`.

**Always pass `--browser chromium`** to `open`.

### Open the browser
Use `$WEBAPP_URL` from Step 2 (do NOT hardcode `localhost:5173` — the launcher may have shifted ports):
```bash
npx @playwright/cli open --browser chromium "$WEBAPP_URL/"
```

### Wait for the editor canvas to render
```bash
npx @playwright/cli snapshot
# Look for: `data-testid="editor-area"` populated, no loading spinners,
# and at least the navbar + canvas visible.
```

If the canvas is still loading, wait a few seconds and snapshot again.

### Interact with the editor (if your change needs a specific state)

Common interactions — find refs from `snapshot` first:
- Click a UML element type in the palette to start placing
- Click on the canvas to drop an element
- Open the diagram-type selector in the navbar
- Switch theme via navbar settings
- Open the import/export menu

```bash
npx @playwright/cli click <ref>
npx @playwright/cli fill <ref> "<text>"
```

### Take screenshots

Use a UUIDv4 for each filename:
```bash
IMG_NAME=$(uuidgen | tr '[:upper:]' '[:lower:]').png
npx @playwright/cli screenshot --filename=/tmp/$IMG_NAME
```

For full-page: add `--full-page`. Generate a new UUID for each screenshot.

### Command reference

| Command | Usage |
|---|---|
| `open --browser chromium <url>` | Open Chromium and navigate |
| `goto <url>` | Navigate to URL |
| `snapshot` | Get element refs for the current page |
| `fill <ref> <text>` | Fill a text input |
| `click <ref>` | Click an element |
| `screenshot --filename=<path>` | Capture screenshot |
| `close` | Close the browser |

### Close the browser when done
```bash
npx @playwright/cli close
```

## Step 5: Upload Screenshots

Push screenshots to the `~/pr-screenshots` repo (clone of `Claudia-Anthropica/pr-screenshots`):

```bash
cp /tmp/<uuid1>.png /tmp/<uuid2>.png ~/pr-screenshots/
cd ~/pr-screenshots
git add *.png
git commit -m "Add screenshots for PR #<number>"
git push
```

Raw URL: `https://raw.githubusercontent.com/Claudia-Anthropica/pr-screenshots/main/<filename>.png`

## Step 6: Add Screenshots to PR

```bash
gh pr edit <number> --repo <repo> --body "<updated-body-with-screenshots-section>"
```

Markdown: `![Description](https://raw.githubusercontent.com/Claudia-Anthropica/pr-screenshots/main/<filename>.png)`

## Step 7: Clean Up

```bash
npx @playwright/cli close 2>/dev/null
npx @playwright/cli kill-all 2>/dev/null
kill $LAUNCHER_PID 2>/dev/null   # the orchestrator (npm run dev) propagates SIGTERM to children
pkill -f "vite" 2>/dev/null
pkill -f "tsx watch" 2>/dev/null
pkill -f "scripts/dev.mjs" 2>/dev/null
docker compose -f docker/compose.local.db.yml down 2>/dev/null
```

## Step 8: Save What You Learned

If you hit problems and figured out fixes, save them:

```bash
MEMORIES_DIR=<memories-dir> bash <claudia-dir>/append-knowledge.sh "<memories-dir>/knowledge/<repo-slug>/tooling-notes.jsonl" "$(date +%Y-%m-%d)" "PR #<number>" "<what you learned — max 200 chars>"
```

Examples:
- "Vite uses port 5174 when 5173 is held by another worker — read startup log for actual port"
- "Library workspace must be built (`npm run build:lib`) before webapp can resolve `@tumaet/apollon` types"

## On Failure

After 3-5 genuine fix attempts:

1. Kill all processes and stop Docker (Step 7)
2. Save what you learned (Step 8)
3. Slack alert:
   ```bash
   python3 <claudia-dir>/slack.py ':warning: Could not capture screenshots for Apollon PR #<number>: <reason>. Tried: <what you attempted>'
   ```
4. Add to PR body: "Screenshots could not be captured automatically: `<reason>`. Manual screenshots needed."
5. **Continue with the rest of your workflow.**
