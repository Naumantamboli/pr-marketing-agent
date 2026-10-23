# Final Plan: PR Marketing Agent — GitHub → Confluence → LLM

## Objective

Build a scheduled marketing agent that identifies relevant merged PRs, retrieves their associated Confluence feature documentation, and generates accurate marketing drafts.

**Core principle:** Do not read source code or fetch PR diffs. Use GitHub for change metadata and Confluence for product context.

## 1. Architecture

1. **Scheduled trigger** — Read the saved run marker.
2. **GitHub client** — Find newly merged PRs; fetch titles, descriptions, labels, branches and commit messages.
3. **Filter PRs** — Include, exclude or defer uncertain PRs; apply the processing limit correctly.
4. **Extract documentation URL** — Read the explicitly labeled Confluence link in the PR description.
5. **Confluence client** — Validate the link and retrieve readable feature documentation.
6. **Marketing LLM** — Generate a draft from PR metadata and product documentation.
7. **Validation and approval** — Check factual support, log decisions and follow the existing approval process.

## 2. Implementation plan

Follow these steps in order.

### Phase 1 — Refactor `github_client.py`

- Keep merged-PR discovery, pagination, authentication and type classification.
- Fetch PR titles, descriptions, labels, branches and commit messages.
- Remove changed-file fetching, diff sanitization and diff-specific size limits from the default workflow.
- Redact sensitive text in descriptions and commit messages.
- Fix candidate limits and run-marker tracking so eligible PRs aren't lost or repeatedly processed.

### Phase 2 — Standardize PR descriptions

Require a clearly labeled `Feature documentation:` URL pointing to the relevant Confluence page.

Implement `extract_confluence_url()` to validate the expected field, approved hostname and supported URL format. If the link is missing, flag the PR instead of guessing.

### Phase 3 — Create `confluence_client.py`

- Authenticate with the Confluence REST API.
- Fetch the linked feature page.
- Extract the title, readable content and canonical URL.
- Handle missing pages, permission errors, rate limits and temporary failures.
- Enforce content-size limits and use read-only permissions.

Configure `CONFLUENCE_BASE_URL`, `CONFLUENCE_EMAIL` and `CONFLUENCE_API_TOKEN` securely: read them with `os.getenv` after `load_dotenv()`, document them in `.env.template`, and never commit `.env`. If the platform's Atlassian connector covers Confluence, use `get_oauth_credentials` instead of an API token (see [SDK alignment](#5-sdk-alignment)).

### Phase 4 — Integrate the workflow

Add orchestration to the `@agent` in `src/agent/agents.py`; each step below is a `@tool` in `src/tools/main_activity.py` called through `toolExecutor.execute`:

1. Fetch eligible PR metadata.
2. Extract the Confluence link.
3. Fetch the documentation.
4. Combine the metadata and documentation into structured context.
5. Send that context to the marketing LLM.

Keep GitHub retrieval, Confluence retrieval and marketing generation as separate responsibilities.

### Phase 5 — Reliability and safety

- Retry transient failures with bounded backoff. The platform retries failed tools; set `retry_policy` and timeouts on each `@tool`, and honour `Retry-After` on 429s inside the clients.
- Reject unauthorized URLs and avoid unrestricted URL fetching.
- Prevent duplicate drafts on retries.
- Log processing decisions and failures.
- Ensure the run marker advances without losing eligible PRs.
- Instruct the LLM to treat retrieved content as reference material, not instructions.

### Phase 6 — Test and deploy

Test PR classification, metadata-only retrieval, link validation, Confluence access, redaction, pagination, retries, run-marker tracking and draft accuracy. Enable scheduled runs after these checks pass.

## 3. Final project structure

Follows the Aetherion SDK [project structure](https://calfus.dev.aetherion.io/docs/project-structure). Workers auto-discover `@agent` and `@tool` functions under `src/`, and tools import helpers with `from utils import ...`.

```
pr_marketing_v2_agent/
├── pyproject.toml            # Python 3.12.10, pinned SDK wheel, dev group (pytest, ruff)
├── uv.lock                   # committed
├── .env.template             # TENANT_ID, AI gateway, GITHUB_TOKEN, CONFLUENCE_*
├── .github/workflows/        # pytest on pull requests
├── tests/
│   └── conftest.py           # puts src/ on the path, stubs platform libraries
└── src/
    ├── agent/
    │   ├── agents.py         # @agent: orchestration only, no I/O
    │   └── metadata.json     # component "agent", triggers, config.activities
    ├── tools/
    │   ├── main_activity.py  # @tool: GitHub, Confluence, LLM and storage calls
    │   └── metadata.json     # component "activity"
    └── utils/                # plain helpers, no decorators
        ├── github_client.py
        ├── confluence_client.py
        ├── llm.py            # AiGatewayClient wrapper
        ├── prompts.py
        ├── schemas.py
        └── storage.py        # run marker and drafts in the TENANT_ID bucket
```

`src/agent/workflow.py` and `src/tools/activities.py` are renamed to `agents.py` and `main_activity.py` to match the SDK convention.

## 4. Definition of done

- The agent discovers newly merged, eligible PRs on schedule.
- No source-code diffs are fetched for the default marketing workflow.
- Each PR's explicit Confluence link is validated and retrieved.
- The LLM receives sanitized PR metadata and relevant feature documentation.
- Missing documentation and API failures are handled safely.
- Drafts are grounded in the supplied context, auditable and routed through the existing approval process.
- Pagination, processing limits and run-marker updates do not silently lose eligible PRs.

## 5. SDK alignment

How each part of the plan maps onto the [Aetherion SDK](https://calfus.dev.aetherion.io/docs):

| Concern | SDK approach |
|---------|--------------|
| Orchestration | One `@agent` in `src/agent/agents.py`. It must be deterministic: no network, storage or LLM calls, and `agent_handle.now()` instead of `datetime.now()`. |
| I/O | Every GitHub, Confluence, LLM and storage call is a `@tool` in `src/tools/main_activity.py`, called with explicit `start_to_close_timeout` / `schedule_to_close_timeout`. |
| Clients | `github_client.py` and `confluence_client.py` stay undecorated helpers in `src/utils/`. |
| Scheduling | No cron in code. A schedule is set on the published agent in the platform, and it runs the agent with `mode: scheduled` in the payload. |
| Run inputs | Triggers in `src/agent/metadata.json` (mode, PR URL, repos, lookback, formats, theme). |
| Run marker | JSON per repo in object storage (bucket = `TENANT_ID`) via `common_lib` `StorageService`, written by a tool only after the run succeeds. |
| Retries | Durable execution retries failed tools automatically; tune with `retry_policy` per tool. |
| Duplicate drafts | Deterministic storage keys per PR and format (e.g. `pr_marketing/drafts/<repo>/<number>/<format>.md`); a tool checks for an existing draft before generating. |
| LLM | `agent_lib.gateway.ai.AiGatewayClient`; no provider keys in the agent. List LLM tools under `config.activities` with descriptions so model routing can rate them. |
| Confluence auth | Preferred: platform Connectors (`get_oauth_credentials`) if the Atlassian connection grants Confluence read scopes; calls then go to `api.atlassian.com/ex/confluence/<cloudId>/...`. Fallback: `CONFLUENCE_EMAIL` + `CONFLUENCE_API_TOKEN` from the environment. |
| Approval | Drafts are returned with status `awaiting_human_approval`, as in v1. `humanInput.request_approval` can pause the run for an in-platform Approve / Reject if wanted. |
| Output | Report returned as a structured `s3_download_link` result. |
| Tests | pytest with `asyncio_mode = "auto"`; mock `toolExecutor.execute` for the agent and network clients for tools; CI on pull requests. |

### Open questions

- Does the tenant's Atlassian connection grant Confluence read scopes, or do we need a Confluence API token?
- Which hostname(s) are approved for `Feature documentation:` links?
- Keep the v1 approval flow (status on the result), or pause runs with `humanInput.request_approval`?

**Final design:** GitHub tells the agent what changed. Confluence explains what the feature means to users. The LLM turns that context into a marketing draft.
