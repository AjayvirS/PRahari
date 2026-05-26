## Repo-Specific: Artemis

### Environment & Tooling

#### Runtime tools
- **Node.js**: The system uses `nvm`. Before any `npm`, `npx`, or Node commands, initialize and activate the correct version (run from the repo root so `.nvmrc` is found):
  ```bash
  [[ -s "$HOME/.nvm/nvm.sh" ]] && source "$HOME/.nvm/nvm.sh"
  nvm install   # reads .nvmrc from the repo root if present, otherwise installs latest LTS
  ```
  Run these two lines at the start of any phase that touches JavaScript/TypeScript.

- **Java**: The system uses SDKMAN. Before any `./gradlew`, `mvn`, or Java commands, initialize and activate the correct version:
  ```bash
  [[ -s "$HOME/.sdkman/bin/sdkman-init.sh" ]] && source "$HOME/.sdkman/bin/sdkman-init.sh"
  sdk env        # reads .sdkmanrc from the repo root — installs the required JDK if missing
  ```
  Run these two lines from the repo root at the start of any phase that touches Java. If no `.sdkmanrc` exists, use `sdk use java <version>` with whatever the project README specifies.

#### Database
- PostgreSQL is installed on this machine. Your database superuser is `claudia_agent`.
- Credentials are in `~/.pgpass`. You can `cat ~/.pgpass` to read the password when needed. Format is `hostname:port:database:username:password`.
- `src/main/resources/config/application-local.yml` exists with PostgreSQL and module configs. Modify it as needed — treat it like your own local dev config.
  ```bash
  SPRING_PROFILES_INCLUDE=dev,artemis,localci,localvc,scheduling,buildagent,core,ldap,local ./gradlew bootRun
  ```
- **If the backend fails to start**, read the error output, diagnose the issue, and fix it. Missing config properties → add them to `application-local.yml` (check other profile YAML files in `src/main/resources/config/` for reference values). Never give up on a fixable config issue.

#### Process Cleanup
Always kill processes you spawned when you're done — no orphaned processes:
```bash
npx @playwright/cli close 2>/dev/null
npx @playwright/cli kill-all 2>/dev/null
pkill -f "bootRun" 2>/dev/null
pkill -f "ng serve" 2>/dev/null
pkill -f "webpack-dev-server" 2>/dev/null
```

### UI Testing & Screenshots
- Screenshots are MANDATORY for any PR that changes visual UI (Angular components, templates, styles, HTML).
- When your changes affect visual UI, read `{{CLAUDIA_DIR}}/repos/ls1intum-Artemis/screenshot-procedure.md` and follow the full procedure.
- On failure: send a Slack alert and add a "manual screenshots needed" note to the PR.
- **NEVER generate fake, mockup, or synthetic screenshots.**

### Coding Conventions
- Read `documentation/docs/developer/guidelines/` for project conventions.
- PR template: `.github/PULL_REQUEST_TEMPLATE.md`
- **PR title format**: The category tag MUST be wrapped in backticks. Format: `` `Category`: Verbal description of the change ``
  - Examples: `` `Programming exercises`: Fix null pointer in grading service ``, `` `General`: Add timezone support to date picker ``, `` `Quiz`: Implement drag-and-drop question type ``
  - The category should match the area (e.g., `Programming exercises`, `Quiz`, `Exam`, `Communication`, `General`). Derive from the issue labels or affected code area. Use Title Case for the category.
- **Java**: Service layer patterns, JPA/Hibernate conventions, REST endpoint patterns. Watch for JPA/Hibernate pitfalls (lazy loading outside transaction, missing cascade, query correctness across MySQL/PostgreSQL).
- **TypeScript/Angular**: Signals, standalone components, proper RxJS patterns (unsubscribe handling).
- **Tests**: Match existing test patterns for similar features.
- Review checklist item: `[x] I have read and followed the [coding guidelines](https://docs.artemis.cit.tum.de/dev/guidelines/)`

### Testing Commands

For server changes — run the relevant test classes:
```bash
[[ -s "$HOME/.sdkman/bin/sdkman-init.sh" ]] && source "$HOME/.sdkman/bin/sdkman-init.sh"
sdk env install 2>/dev/null || true
./gradlew test --tests '<TestClass>' 2>&1 | tee /tmp/test_output.txt | tail -100
```

For client changes — run the relevant test suites:
```bash
[[ -s "$HOME/.nvm/nvm.sh" ]] && source "$HOME/.nvm/nvm.sh"
nvm install
npm test -- --include='<pattern>' 2>&1 | tee /tmp/test_output.txt | tail -100
```

### Fundamental Rework & Draft Conversion
When handling feedback (pr-feedback-handler), be particularly attentive to the "fundamental rework" assessment. In Artemis, PRs go through student review cycles — if the feedback is so extensive that the PR needs a near-complete rework, **other student reviewers should not waste their time reviewing the current state**. Converting to draft and removing the `ready for review` label is the right call in these cases. Err on the side of keeping it open for incremental fixes; only draft it when the rework is truly fundamental.

### Review Notes
- Your comments will be read by students. Write like a friendly but direct senior developer.
- The Slack channel is a **private team channel** — only supervisors read it, not students. You can vent freely there.
