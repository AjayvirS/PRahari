# Plan: Evolving PRahari into a Sophisticated Review Bot

This plan outlines a series of implementable issues to enhance the PRahari review bot, making it more context-aware and robust, inspired by the advanced capabilities of the Claudia agent.

---

### Epic 1: Enhance Interaction & Context-Awareness

*Goal: Make the bot feel less like a script and more like a team member by enabling it to understand conversation history and act on different types of feedback.*

-   **Issue 1.1: Handle Replies to Review Comments**
    -   **Description:** Currently, the bot only posts a top-level summary. It should also process replies to its own review comments.
    -   **Acceptance Criteria:**
        -   The webhook handler should process `issue_comment` and `pull_request_review_comment` events.
        -   If a comment is a reply to one of PRahari's existing comments, enqueue a new `review_follow_up` job.
        -   The worker needs to be updated to handle this new job type, generating a contextual reply instead of a full re-review.

-   **Issue 1.2: Implement On-Demand Reviews via Mentions**
    -   **Description:** Allow trusted users to trigger a review on any PR by mentioning the bot (e.g., `@prahari review`). This provides an explicit way to request a review, bypassing other triggers.
    -   **Acceptance Criteria:**
        -   The webhook handler should scan `issue_comment` bodies for a mention command.
        -   A new configuration setting should define a list of "trusted users" who can perform this action.
        -   If a trusted user triggers the command, a high-priority review job should be enqueued.

-   **Issue 1.3: Add Stateful Re-reviews**
    -   **Description:** Make the re-review process smarter. Instead of running a full review on every `synchronize` event, the bot should understand its previous review state.
    -   **Acceptance Criteria:**
        -   Before starting a review, the worker should check if it has already reviewed the current `head_sha`. If so, the job is skipped.
        -   (Advanced) The bot could check its last review state (e.g., `APPROVED` vs. `CHANGES_REQUESTED`). If it was `APPROVED` and new commits are minor (e.g., docs or comments), it could post a light confirmation instead of a full review.

---

### Epic 2: Improve Configuration & Flexibility

*Goal: Make PRahari more adaptable and easier to manage across different repositories without changing the code.*

-   **Issue 2.1: Introduce a Repository Configuration File**
    -   **Description:** Move from purely environment-variable-based configuration to a `repos.json` file, similar to Claudia. This allows for per-repository settings.
    -   **Acceptance Criteria:**
        -   Create a `repos.json` file at the root.
        -   The bot should load this file on startup.
        -   Initial settings should include `review_label` (a label that must be present on a PR to trigger a review) and `enabled_repos` (a list of repos where the bot is active).
        -   The webhook handler should consult this configuration to decide whether to process an event.

-   **Issue 2.2: Add Per-Repo Prompt Overlays**
    -   **Description:** Allow custom instructions to be provided to the LLM on a per-repository basis. This is crucial for adapting the review style to different projects' coding standards.
    -   **Acceptance Criteria:**
        -   The `repos.json` file should support an optional `prompt_overlay_path` for each repository.
        -   When generating a review, the `review_service` should read the content from this file and prepend it to the system prompt sent to the LLM.

---

### Epic 3: Foundational Robustness

*Goal: Introduce some of Claudia's robustness to make the bot more reliable, while keeping it lightweight.*

-   **Issue 3.1: Add Job Priorities and Debouncing**
    -   **Description:** Not all jobs are equally urgent. An on-demand review should be prioritized over a routine re-review. Debouncing prevents a rapid series of events (like multiple quick pushes) from creating a storm of jobs.
    -   **Acceptance Criteria:**
        -   Update the `review_jobs` table in SQLite to include `priority` and `run_after` columns.
        -   The `enqueue` logic should support setting a priority and a `run_after` timestamp.
        -   When a job is enqueued for a PR that already has a pending job, update the existing job's `run_after` time (debounce) instead of creating a new one.
        -   The worker should be updated to claim jobs based on `priority` and where `run_after` is in the past.

---

### Epic 4: Future Integrations & Value Proposition

*Goal: Define the purpose and value of integrating with enterprise collaboration tools like Microsoft Teams.*

-   **Issue 4.1: Define Use Cases for Microsoft Teams Integration**
    -   **Description:** Brainstorm and document the potential value of a Teams integration. This is a planning issue, not an implementation one.
    -   **Potential Use Cases to Explore:**
        1.  **Review Notifications:** Post a message to a specific Teams channel when a review is started or completed, linking back to the PR. This increases visibility for the team.
        2.  **Daily Digest:** Send a daily summary to a channel with a list of all PRs reviewed, their status, and any open questions. This is great for team leads and managers.
        3.  **On-Demand Triggers from Teams:** Allow trusted users to request a review by sending a message to the bot in Teams (e.g., `@PRahari review <PR_URL>`). This provides a convenient, centralized way to interact with the bot.
        4.  **Alerts for Failures:** If a review job fails repeatedly, send an alert to a designated "dev-ops" or "bot-health" channel in Teams.
    -   **Acceptance Criteria:**
        -   A document is produced outlining the most valuable use cases and a high-level plan for what a Phase 1 integration would look like.

