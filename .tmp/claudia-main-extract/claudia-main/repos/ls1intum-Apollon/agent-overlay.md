## Repo-Specific: Apollon

Apollon is a UML modeling editor. The repo is an npm-workspaces monorepo:

| Workspace | Path | Role |
|---|---|---|
| `@tumaet/apollon` | `library/` | The UML editor library (the actual modeling engine). |
| `@tumaet/webapp` | `standalone/webapp/` | Vite + React 18 + MUI standalone web app that embeds the library. |
| `@tumaet/server` | `standalone/server/` | Express 5 + Yjs + Redis collaboration backend for the standalone webapp. |
| `apollon-vscode` | `vscode-extension/` | VS Code extension wrapping the library. |

### Environment & Tooling

#### Runtime
- **Node.js**: nvm. Always activate the required version from the repo root before any `npm` / `npx` / Node command:
  ```bash
  [[ -s "$HOME/.nvm/nvm.sh" ]] && source "$HOME/.nvm/nvm.sh"
  nvm install   # reads .nvmrc (currently v22.14.0)
  ```
- **npm**: Apollon's `engines` field requires npm `>=11.1.0`. Node 22.14.0 bundles npm 10.9.2, which is too old. Upgrade once within the nvm-managed prefix (idempotent — skips if already current):
  ```bash
  [ "$(npm -v | cut -d. -f1)" -lt 11 ] && npm install -g npm@^11.1.0
  npm -v  # verify >= 11.1.0
  ```
- All `npm` commands run from the **repo root** unless explicitly noted — workspace targeting is done via `--workspace=<name>` rather than `cd <workspace>`.

#### Local services (only needed for the standalone server / webapp)
The collaboration server uses **Redis**. A docker compose file is provided:
```bash
npm run ensure:localdb   # starts Redis container + waits for it
# under the hood: docker compose -f docker/compose.local.db.yml up -d
```
Server tests use `testcontainers` and spin up their own Redis — no manual setup needed for `npm test`.

#### Dev servers
```bash
npm run dev          # orchestrates lib watch + server + webapp via scripts/dev.mjs
npm run dev:lib      # library tsc --watch
npm run dev:server   # ensure:localdb then tsx watch src/server.ts (default port: see src/server.ts)
npm run dev:webapp   # vite (default :5173)
```

#### Process cleanup
Always kill processes you spawned when done:
```bash
npx @playwright/cli close 2>/dev/null
npx @playwright/cli kill-all 2>/dev/null
pkill -f "vite" 2>/dev/null
pkill -f "tsx watch" 2>/dev/null
docker compose -f docker/compose.local.db.yml down 2>/dev/null
```

### Coding Conventions

- **Conventional commits are MANDATORY** (commitlint runs in the `commit-msg` husky hook AND in CI — `--no-verify` only delays the failure to CI). Commit subjects must match `<type>(<scope>): <subject>`. Allowed types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `perf`, `build`, `ci`, `revert`. Suggested scopes mirror the workspace names: `library`, `webapp`, `server`, `vscode`, `repo` (for cross-cutting tooling). Dry check: `echo "<your message>" | npx commitlint`.

  **Override the default commit-message templates from the generic agent prompts** — they will be rejected by commitlint as-is:

  | Agent | Bad (generic) | Good (Apollon) |
  |---|---|---|
  | `pr-feedback-handler` | `Address review feedback` | `fix(<scope>): address review feedback` |
  | `pr-hygiene-checker` | `Fix PR hygiene issues` | `chore(repo): tidy PR metadata` |
  | `ci-check-handler` | `Fix CI failures` | `fix(<scope>): restore failing check` |
  | `issue-implementer` | (derives from issue) | derive `type` from issue label (`feat`/`fix`/`chore`) and `<scope>` from the affected workspace |

- **Pre-commit hook** runs `npm run format:check` (prettier) and `npm run lint` (eslint across all three workspaces). Run these locally before pushing; if format fails, run `npm run format`.
- **TypeScript everywhere** — `tsc -b` builds workspaces in dependency order.
- **PR title**: no special category-tag format for Apollon (unlike Artemis). A short, descriptive title is enough; conventional-commit syntax is encouraged but not required by CI. The hygiene checker should NOT rewrite PR titles on this repo.
- **PR template**: check `.github/PULL_REQUEST_TEMPLATE.md` if present.

#### Library changes ripple into webapp/server
The webapp and server import `@tumaet/apollon` from the local `library/` workspace. If you change `library/` source, rebuild before running the webapp or capturing screenshots — `npm run build:lib` (one-shot) or `npm run dev:lib` (watch). Otherwise consumers see stale code.

### Testing Commands

Library:
```bash
npm run test --workspace=@tumaet/apollon 2>&1 | tee /tmp/test_output.txt | tail -100
```
Server (uses testcontainers — needs Docker):
```bash
npm run test --workspace=@tumaet/server 2>&1 | tee /tmp/test_output.txt | tail -100
```
Webapp (Vitest unit + RTL):
```bash
npm run test --workspace=@tumaet/webapp 2>&1 | tee /tmp/test_output.txt | tail -100
```
Webapp E2E / visual (Playwright):
```bash
npm run test:e2e --workspace=@tumaet/webapp 2>&1 | tee /tmp/test_output.txt | tail -100
```
Lint everything:
```bash
npm run lint 2>&1 | tee /tmp/lint_output.txt | tail -100
```
Format check (run before committing):
```bash
npm run format:check 2>&1 | tee /tmp/format_output.txt | tail -50
```

### UI Testing & Screenshots
- Screenshots are MANDATORY for any PR that changes visible UI in `library/` or `standalone/webapp/` (React components, MUI usage, Tailwind classes, SVG rendering of UML elements).
- When your changes affect visual UI, read `{{CLAUDIA_DIR}}/repos/ls1intum-Apollon/screenshot-procedure.md` and follow the full procedure.
- **NEVER generate fake, mockup, or synthetic screenshots.**

### Review Notes
- Apollon is consumed by Artemis (modeling exercises) and used standalone. Be alert to public-API changes in `library/` — they ripple into Artemis. Flag any breaking change in the library's exported types or component props.
- The collaboration server uses Yjs CRDTs. Changes to document state, sync protocol, or persistence need careful review for correctness under concurrent edits.
- Comments will be read by student developers and maintainers. Write like a friendly but direct senior developer.
