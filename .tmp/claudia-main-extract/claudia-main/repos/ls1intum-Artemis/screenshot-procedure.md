# Screenshot Capture Procedure (Artemis-specific)

This procedure is for the Artemis project (Spring Boot + Angular, localhost:8080/9000, PostgreSQL).
If Claudia is later used on non-Artemis repos, this file will need a repo-specific variant.

## Hard Rules

- **NEVER generate fake, mockup, synthetic, or static HTML screenshots.** Only real browser screenshots of the running application are acceptable.
- **NEVER give up on fixable problems.** If the backend or frontend fails to start, **read the error output, diagnose the issue, fix it, and retry.** Missing config properties? Add them to `application-local.yml`. Missing dependencies? Install them. Port in use? Kill the process. You are a developer — debug and fix, don't give up.
- **Give up ONLY for truly unfixable problems** — e.g., the codebase itself won't compile due to unrelated broken code, or a required external service is unreachable. Even then, try at least 3 times with fixes between attempts.
- **Kill all background processes** (bootRun, npm start) when done, whether you succeeded or failed.

## Step 1: Check Local Config

`src/main/resources/config/application-local.yml` exists on this machine with PostgreSQL and module configs. Treat it like your own local dev config — modify freely.

Before starting the backend, **proactively check** that the config has the properties needed for the active profiles. Look at `src/main/resources/config/application-dev.yml`, `application-artemis.yml`, and other profile-specific configs in that directory to understand what properties are expected. If `application-local.yml` is missing required properties (e.g., `artemis.user-management.use-external`, `artemis.version-control.*`, `artemis.continuous-integration.*`), **add sensible defaults now** rather than waiting for the startup to fail.

## Step 2: Start the Backend

```bash
[[ -s "$HOME/.sdkman/bin/sdkman-init.sh" ]] && source "$HOME/.sdkman/bin/sdkman-init.sh"
sdk env install 2>/dev/null || true   # .sdkmanrc may not exist; fall back to system Java

SPRING_PROFILES_INCLUDE=dev,artemis,localci,localvc,scheduling,buildagent,core,ldap,local ./gradlew bootRun &
BACKEND_PID=$!
```

Wait for the backend to be ready (health check with timeout):

```bash
for i in $(seq 1 60); do
  curl -sf http://localhost:8080/management/health > /dev/null 2>&1 && break
  sleep 5
done
```

### If the backend fails to start

**Do NOT give up.** This is the most common failure point and it is almost always fixable:

1. **Kill the failed process**: `kill $BACKEND_PID 2>/dev/null; pkill -f bootRun 2>/dev/null`
2. **Read the error output** — look at the Gradle/Spring output for the actual error message. Common causes:
   - **Missing config property** (e.g., `Could not resolve placeholder 'artemis.user-management.use-external'`): Add the missing property to `src/main/resources/config/application-local.yml` with a sensible default. Check the profile-specific YAML files in `src/main/resources/config/` for reference values.
   - **Port already in use**: `kill $(lsof -ti :8080) 2>/dev/null`
   - **Database connection refused**: Check PostgreSQL is running (`pg_isready`), check credentials in `~/.pgpass`
   - **Missing Java version**: Check `.sdkmanrc` or project docs for required JDK
3. **Fix the issue and restart** — go back to the start of Step 2
4. Repeat up to **5 times**. Only give up if the error is completely outside your control (e.g., broken code on the branch that has nothing to do with config).

## Step 3: Start the Frontend

```bash
[[ -s "$HOME/.nvm/nvm.sh" ]] && source "$HOME/.nvm/nvm.sh"
nvm install

# package.json is at the repo root, NOT src/main/webapp/
npm install
npm start &
FRONTEND_PID=$!
```

Wait for the frontend to be ready:

```bash
for i in $(seq 1 60); do
  curl -sf http://localhost:9000 > /dev/null 2>&1 && break
  sleep 5
done
curl -sf http://localhost:9000 > /dev/null 2>&1 || { echo "Frontend failed to start"; kill $FRONTEND_PID $BACKEND_PID 2>/dev/null; exit 1; }
```

## Step 4: Set Up Test Data (if needed)

If the screenshots require specific data (courses, exercises, users), set it up via the API or UI before capturing. Use the admin account available in local dev mode.

## Step 5: Capture Screenshots

Use the `@playwright/cli` **command-line tool** (NOT the Node.js library). This is a stateful interactive CLI — each command runs against the same persistent browser session. Install browsers if needed: `npx playwright install chromium`

**Always pass `--browser chromium`** to the `open` command. Do NOT use the default browser.

### Open the browser and navigate

```bash
npx @playwright/cli open --browser chromium "http://localhost:9000"
```

### Login (if needed)

Artemis requires authentication. Use `snapshot` to find element refs, then fill and click:

```bash
# Get element refs for the page
npx @playwright/cli snapshot

# Fill login form (use the refs from snapshot output)
npx @playwright/cli fill <username-ref> "artemis_admin"
npx @playwright/cli fill <password-ref> "artemis_admin"
npx @playwright/cli click <sign-in-button-ref>
```

**Important: Dismiss modals.** After login, Artemis may show modals (e.g., passkey promo). Run `snapshot` to check, then click the dismiss button (e.g., "Set Up Later", "Remind Me in 30 Days"). Modals block all other clicks until dismissed.

### Navigate to the target page

```bash
npx @playwright/cli goto "http://localhost:9000/courses/<id>/exercises/<id>"
```

For Angular SPA pages, the page may need a moment to render. Run `snapshot` after navigation to confirm content has loaded before screenshotting.

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

## Step 6: Upload Screenshots

Push screenshots to the `~/pr-screenshots` repo (clone of `Claudia-Anthropica/pr-screenshots`):

Copy only the screenshot files you captured (tracked by their UUID filenames):

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

## Step 7: Add Screenshots to PR

Update the PR body to include a `## Screenshots` section with the uploaded images:

```bash
gh pr edit <number> --repo <repo> --body "<updated-body-with-screenshots-section>"
```

Use markdown image syntax: `![Description](https://raw.githubusercontent.com/Claudia-Anthropica/pr-screenshots/main/<filename>.png)`

## Step 8: Clean Up

Close the browser and kill all background processes:

```bash
npx @playwright/cli close 2>/dev/null
npx @playwright/cli kill-all 2>/dev/null
kill $FRONTEND_PID $BACKEND_PID 2>/dev/null
# Also clean up any orphaned processes
pkill -f "bootRun" 2>/dev/null
pkill -f "ng serve" 2>/dev/null
pkill -f "webpack-dev-server" 2>/dev/null
```

## Step 9: Save What You Learned

If you hit problems during this procedure and figured out how to fix them (config issues, missing properties, port conflicts, modal dismissals, Angular rendering quirks, etc.), **save that knowledge** so future runs don't repeat the same struggle:

```bash
MEMORIES_DIR=<memories-dir> bash <claudia-dir>/append-knowledge.sh "<memories-dir>/knowledge/<repo-slug>/tooling-notes.jsonl" "$(date +%Y-%m-%d)" "PR #<number>" "<what you learned — max 200 chars>"
```

Examples of things worth saving:
- "application-local.yml needs artemis.user-management.use-external=false for dev profile to start"
- "After login, must dismiss passkey modal via 'Set Up Later' button before any other clicks"
- "Angular pages need 3-5s after navigation before screenshot — use snapshot to confirm content loaded"
- "Port 8080 sometimes held by orphaned bootRun — always pkill -f bootRun before starting"

Do this whether you succeeded or failed. Even failed attempts produce useful knowledge.

## On Failure

Only give up after you have genuinely tried to fix every error you encountered (at least 3-5 attempts with fixes between each). If the problem is truly unfixable:

1. Kill all background processes (Step 8)
2. **Save what you learned** (Step 9) — even failures produce knowledge
3. Send a Slack alert **that includes what you tried and why it's unfixable**:
   ```bash
   python3 <claudia-dir>/slack.py ':warning: Could not capture screenshots for PR #<number>: <reason>. Tried: <what you attempted>'
   ```
4. Add a note to the PR body: "Screenshots could not be captured automatically: `<reason>`. Manual screenshots needed."
5. **Continue with the rest of your workflow.**
