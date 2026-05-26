# Screenshot Capture Procedure (Thesis Management)

This procedure is for the thesis-management project (Spring Boot 4 + React 19/Mantine, PostgreSQL via Docker, Keycloak OIDC auth).

## Hard Rules

- **NEVER generate fake, mockup, synthetic, or static HTML screenshots.** Only real browser screenshots of the running application are acceptable.
- **NEVER give up on fixable problems.** If the backend or frontend fails to start, **read the error output, diagnose the issue, fix it, and retry.** Port in use? Kill the process. Docker not running? Start it. You are a developer — debug and fix, don't give up.
- **Give up ONLY for truly unfixable problems** — e.g., the codebase itself won't compile due to unrelated broken code, or Docker is unavailable. Even then, try at least 3 times with fixes between attempts.
- **Kill all background processes** (bootRun, webpack, docker) when done, whether you succeeded or failed.

## Step 1: Start Docker Services

Docker must be running. Start PostgreSQL and Keycloak from the **repo root**:

```bash
docker compose up -d
```

Wait for both services to be ready:

```bash
# Wait for Keycloak
for i in $(seq 1 30); do
  curl -sf http://localhost:8081/realms/thesis-management > /dev/null 2>&1 && break
  sleep 5
done
curl -sf http://localhost:8081/realms/thesis-management > /dev/null 2>&1 || { echo "Keycloak failed to start"; exit 1; }

# Wait for PostgreSQL (port 5144)
for i in $(seq 1 12); do
  pg_isready -h localhost -p 5144 > /dev/null 2>&1 && break
  sleep 5
done
```

### If Docker services fail to start

1. Check if Docker is running: `docker info > /dev/null 2>&1`
2. Check for port conflicts: `lsof -ti :5144 :8081`
3. Check container logs: `docker compose logs --tail=50`
4. If ports are occupied, kill the conflicting processes and retry

## Step 2: Start the Backend

```bash
[[ -s "$HOME/.sdkman/bin/sdkman-init.sh" ]] && source "$HOME/.sdkman/bin/sdkman-init.sh"
sdk install java 25-open 2>/dev/null || true
sdk use java 25-open

cd server && ./gradlew bootRun --args='--spring.profiles.active=dev' &
BACKEND_PID=$!
```

Wait for the backend to be ready (health check with timeout):

```bash
for i in $(seq 1 60); do
  curl -sf http://localhost:8080/api/actuator/health > /dev/null 2>&1 && break
  sleep 5
done
curl -sf http://localhost:8080/api/actuator/health > /dev/null 2>&1 || { echo "Backend failed to start"; kill $BACKEND_PID 2>/dev/null; exit 1; }
```

### If the backend fails to start

**Do NOT give up.** Common issues:

1. **Kill the failed process**: `kill $BACKEND_PID 2>/dev/null; pkill -f bootRun 2>/dev/null`
2. **Read the error output** — common causes:
   - **Port already in use**: `kill $(lsof -ti :8080) 2>/dev/null`
   - **Database connection refused**: Ensure docker compose is up and PostgreSQL is ready on port 5144
   - **Keycloak not ready**: The backend needs Keycloak to validate JWTs. Ensure Keycloak is fully started.
   - **Wrong Java version**: Must be Java 25. Check with `java -version`.
3. **Fix the issue and restart** — go back to the start of Step 2
4. Repeat up to **5 times**.

## Step 3: Start the Frontend

```bash
[[ -s "$HOME/.nvm/nvm.sh" ]] && source "$HOME/.nvm/nvm.sh"
nvm install 24

cd client && npm install && npm run dev &
FRONTEND_PID=$!
```

Wait for the frontend to be ready:

```bash
for i in $(seq 1 60); do
  curl -sf http://localhost:3000 > /dev/null 2>&1 && break
  sleep 5
done
curl -sf http://localhost:3000 > /dev/null 2>&1 || { echo "Frontend failed to start"; kill $FRONTEND_PID $BACKEND_PID 2>/dev/null; exit 1; }
```

## Step 4: Capture Screenshots

Use the `@playwright/cli` **command-line tool** (NOT the Node.js library). This is a stateful interactive CLI — each command runs against the same persistent browser session. Install browsers if needed: `npx playwright install chromium`

**Always pass `--browser chromium`** to the `open` command.

### Open the browser and navigate

```bash
npx @playwright/cli open --browser chromium "http://localhost:3000/dashboard"
```

This will redirect to the Keycloak login page.

### Login via Keycloak

The redirect goes to `http://localhost:8081/realms/thesis-management/protocol/openid-connect/auth?...`. Use `snapshot` to find element refs on the Keycloak login page:

```bash
npx @playwright/cli snapshot
```

The Keycloak login form has these well-known IDs:
- Username field: `#username`
- Password field: `#password`
- Login button: `#kc-login`

```bash
# Fill Keycloak login form (use refs from snapshot output)
npx @playwright/cli fill <username-ref> "admin"
npx @playwright/cli fill <password-ref> "admin"
npx @playwright/cli click <kc-login-ref>
```

After login, Keycloak redirects back to `http://localhost:3000/dashboard`. Wait for the page to load by checking that the Mantine loader spinner disappears:

```bash
# Wait for the app to finish loading — take a snapshot and look for:
# - The Dashboard heading to be visible
# - No .mantine-Loader-root elements visible
npx @playwright/cli snapshot
```

If a `.mantine-Loader-root` element is still visible, wait a few seconds and snapshot again.

### Navigate to the target page

```bash
npx @playwright/cli goto "http://localhost:3000/<target-path>"
```

After navigation, wait for the Mantine loader to disappear. Run `snapshot` to confirm content has loaded before screenshotting.

### Take screenshots

Use a UUIDv4 for each filename to avoid collisions:

```bash
IMG_NAME=$(uuidgen | tr '[:upper:]' '[:lower:]').png
npx @playwright/cli screenshot --filename=/tmp/$IMG_NAME
```

For full-page screenshots: add `--full-page`. Generate a new UUID for each screenshot.

### Command reference

| Command | Usage |
|---------|-------|
| `open --browser chromium <url>` | Open Chromium browser and navigate |
| `goto <url>` | Navigate to URL |
| `snapshot` | Get element refs for the page |
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

The raw URL for each image is:
```
https://raw.githubusercontent.com/Claudia-Anthropica/pr-screenshots/main/<filename>.png
```

## Step 6: Add Screenshots to PR

Update the PR body to include a `## Screenshots` section with the uploaded images:

```bash
gh pr edit <number> --repo <repo> --body "<updated-body-with-screenshots-section>"
```

Use markdown image syntax: `![Description](https://raw.githubusercontent.com/Claudia-Anthropica/pr-screenshots/main/<filename>.png)`

## Step 7: Clean Up

Close the browser and kill all background processes:

```bash
npx @playwright/cli close 2>/dev/null
npx @playwright/cli kill-all 2>/dev/null
kill $FRONTEND_PID $BACKEND_PID 2>/dev/null
pkill -f "bootRun" 2>/dev/null
pkill -f "webpack" 2>/dev/null
docker compose down 2>/dev/null
```

## Step 8: Save What You Learned

If you hit problems during this procedure and figured out how to fix them, **save that knowledge**:

```bash
MEMORIES_DIR=<memories-dir> bash <claudia-dir>/append-knowledge.sh "<memories-dir>/knowledge/<repo-slug>/tooling-notes.jsonl" "$(date +%Y-%m-%d)" "PR #<number>" "<what you learned — max 200 chars>"
```

Examples:
- "Keycloak takes ~60s to start on first run — realm import is slow"
- "Must wait for .mantine-Loader-root to disappear before screenshots — 30s timeout"
- "Port 8081 sometimes held by old Keycloak container — docker compose down first"

## On Failure

Only give up after you have genuinely tried to fix every error (at least 3-5 attempts). If truly unfixable:

1. Kill all processes and stop Docker (Step 7)
2. **Save what you learned** (Step 8)
3. Send a Slack alert:
   ```bash
   python3 <claudia-dir>/slack.py ':warning: Could not capture screenshots for PR #<number>: <reason>. Tried: <what you attempted>'
   ```
4. Add a note to the PR body: "Screenshots could not be captured automatically: `<reason>`. Manual screenshots needed."
5. **Continue with the rest of your workflow.**
