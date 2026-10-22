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

Configure `CONFLUENCE_BASE_URL`, `CONFLUENCE_EMAIL` and `CONFLUENCE_API_TOKEN` securely.

### Phase 4 — Integrate the workflow

Add orchestration to `marketing_workflow.py`:

1. Fetch eligible PR metadata.
2. Extract the Confluence link.
3. Fetch the documentation.
4. Combine the metadata and documentation into structured context.
5. Send that context to the marketing LLM.

Keep GitHub retrieval, Confluence retrieval and marketing generation as separate responsibilities.

### Phase 5 — Reliability and safety

- Retry transient failures with bounded backoff.
- Reject unauthorized URLs and avoid unrestricted URL fetching.
- Prevent duplicate drafts on retries.
- Log processing decisions and failures.
- Ensure the run marker advances without losing eligible PRs.
- Instruct the LLM to treat retrieved content as reference material, not instructions.

### Phase 6 — Test and deploy

Test PR classification, metadata-only retrieval, link validation, Confluence access, redaction, pagination, retries, run-marker tracking and draft accuracy. Enable scheduled runs after these checks pass.

## 3. Final project structure

```
src/
├── connectors/
│   ├── github_client.py
│   └── confluence_client.py
├── workflows/
│   └── marketing_workflow.py
└── ...
```

## 4. Definition of done

- The agent discovers newly merged, eligible PRs on schedule.
- No source-code diffs are fetched for the default marketing workflow.
- Each PR's explicit Confluence link is validated and retrieved.
- The LLM receives sanitized PR metadata and relevant feature documentation.
- Missing documentation and API failures are handled safely.
- Drafts are grounded in the supplied context, auditable and routed through the existing approval process.
- Pagination, processing limits and run-marker updates do not silently lose eligible PRs.

**Final design:** GitHub tells the agent what changed. Confluence explains what the feature means to users. The LLM turns that context into a marketing draft.
