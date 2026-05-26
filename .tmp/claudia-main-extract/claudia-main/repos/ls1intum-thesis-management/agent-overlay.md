## Repo-Specific: Thesis Management

### Environment & Tooling

#### Runtime tools
- **Node.js**: The system uses `nvm`. Before any `npm`, `npx`, or Node commands, initialize and activate the correct version:
  ```bash
  [[ -s "$HOME/.nvm/nvm.sh" ]] && source "$HOME/.nvm/nvm.sh"
  nvm install 24   # thesis-management requires Node.js >=24.7.0; no .nvmrc in repo
  ```
  Run these two lines at the start of any phase that touches JavaScript/TypeScript.

- **Java**: The system uses SDKMAN. Before any `./gradlew` or Java commands, initialize and activate the correct version:
  ```bash
  [[ -s "$HOME/.sdkman/bin/sdkman-init.sh" ]] && source "$HOME/.sdkman/bin/sdkman-init.sh"
  sdk install java 25-open 2>/dev/null || true
  sdk use java 25-open
  ```
  There is no `.sdkmanrc` in this repo. Java 25 is required (Spring Boot 4).

#### Docker services (PostgreSQL + Keycloak)
This project uses `docker compose` for PostgreSQL and Keycloak. **Docker must be running** before starting services.

```bash
# Start database and Keycloak from repo root
docker compose up -d

# Wait for Keycloak to be ready
for i in $(seq 1 30); do
  curl -sf http://localhost:8081/realms/thesis-management > /dev/null 2>&1 && break
  sleep 5
done

# Wait for PostgreSQL to be ready (port 5144)
for i in $(seq 1 12); do
  pg_isready -h localhost -p 5144 > /dev/null 2>&1 && break
  sleep 5
done
```

- PostgreSQL: `localhost:5144`, user `thesis-management-postgres`, password `thesis-management-postgres`, database `thesis-management`
- Keycloak: `localhost:8081`, admin console with `admin`/`admin`
- Keycloak realm `thesis-management` is auto-imported on first start with all test users and roles

#### Backend
```bash
cd server && ./gradlew bootRun --args='--spring.profiles.active=dev' &
```
- The `dev` profile activates Liquibase seed data (test users, theses, topics)
- Backend serves at `http://localhost:8080` with context path `/api`
- Health check: `http://localhost:8080/api/actuator/health`
- CORS is configured to allow `http://localhost:3000` (the client)

#### Frontend
```bash
cd client && npm install && npm run dev &
```
- Frontend serves at `http://localhost:3000`
- Uses Webpack dev server (React 19 + Mantine UI)

#### Authentication
- Keycloak OIDC: navigating to any protected route (e.g., `/dashboard`) redirects to Keycloak login
- Test users (password = username): `admin`, `supervisor`, `supervisor2`, `advisor`, `advisor2`, `student`, `student2`, `student3`
- Role terminology: server/Keycloak uses `supervisor`/`advisor`; UI displays "Examiner"/"Supervisor"

#### Process Cleanup
Always kill processes you spawned when you're done — no orphaned processes:
```bash
npx @playwright/cli close 2>/dev/null
npx @playwright/cli kill-all 2>/dev/null
pkill -f "bootRun" 2>/dev/null
pkill -f "webpack" 2>/dev/null
docker compose down 2>/dev/null
```

### UI Testing & Screenshots
- Screenshots are MANDATORY for any PR that changes visual UI (React components, styles, HTML).
- When your changes affect visual UI, read `{{CLAUDIA_DIR}}/repos/ls1intum-thesis-management/screenshot-procedure.md` and follow the full procedure.
- On failure: send a Slack alert and add a "manual screenshots needed" note to the PR.
- **NEVER generate fake, mockup, or synthetic screenshots.**

### Coding Conventions
- **PR template**: `.github/PULL_REQUEST_TEMPLATE.md`
- **Java**: Use `record` types for DTOs. Service layer patterns, Spring Boot 4 conventions, JPA/Hibernate.
- **TypeScript/React**: Mantine UI components, React hooks, functional components.
- **Code formatting**: Server uses Spotless (`cd server && ./gradlew spotlessApply`). Client uses ESLint (`cd client && npx eslint src/`).
- **Type checking**: `cd client && npx tsc --noEmit` (ignore mantine-datatable type errors).

### Testing Commands

For server changes — run the relevant test classes:
```bash
[[ -s "$HOME/.sdkman/bin/sdkman-init.sh" ]] && source "$HOME/.sdkman/bin/sdkman-init.sh"
sdk install java 25-open 2>/dev/null || true
sdk use java 25-open
cd server && ./gradlew test --tests '<TestClass>' 2>&1 | tee /tmp/test_output.txt | tail -100
```

For client changes — run the relevant test suites:
```bash
[[ -s "$HOME/.nvm/nvm.sh" ]] && source "$HOME/.nvm/nvm.sh"
nvm install 24
cd client && npx eslint src/ 2>&1 | tee /tmp/lint_output.txt | tail -50
cd client && npx tsc --noEmit 2>&1 | tee /tmp/typecheck_output.txt | tail -50
```

### Review Notes
- Comments will be read by student developers. Write like a friendly but direct senior developer.
