# PrMarketingAgent

An Aetherion agent (Kepler SDK) that reads GitHub pull requests and tells the marketing team, in plain
English, **what changed, who it helps, and whether it's worth posting about**. It then drafts content
in the formats they pick, using templates and brand themes kept in `src/utils/`. **It never publishes.**
Every draft comes back as `draft_needs_approval`.

## How it works

```
 on_demand: pr_url                              scheduled (cron): repos[]
        │                                               │
        │                                   list_merged_prs_tool  (since last cursor, S3)
        ▼                                               ▼
 fetch_pr_tool ── GitHub REST, read-only token; lockfiles/binaries/.env skipped,
        │         secrets redacted, diff capped at DIFF_CHAR_BUDGET
        ▼
 triage_tool ──── Haiku: category, user_facing, feature flag, score 0-100
        │         verdict decided in code: post_worthy · release_notes_only · hold · internal
        │         + which formats to write (social posts only for post_worthy)
        ▼
 analyze_tool ─── Sonnet: headline, plain-English summary, verified facts (with source),
        │         benefits (each tied to a fact), assumptions, missing info, dev questions
        ▼
 generate_content_tool ── Sonnet (Opus for blog_draft): template + theme → draft
        ▼
 verify_draft_tool ────── Haiku: removes any claim not backed by a verified fact
        ▼
 render_report_tool ───── PDF + Markdown → S3 download links
        ▼
 (scheduled) weekly_digest over all worthy PRs, then save_cursors_tool
```

| Verdict | When | Content written |
|---|---|---|
| `post_worthy` | score ≥ 70 and user-facing | all requested formats |
| `release_notes_only` | score 40-69, or not user-facing | owned/internal channels only (no social) |
| `hold` | draft, not merged, or behind a feature flag | none |
| `internal` | score < 40 (refactor, tests, CI, deps…) | none |

`force_generate=true` writes drafts regardless of the verdict.

## Templates and themes (yours to edit)

```
src/utils/templates/<format>.md     # front-matter rules + shape
src/utils/themes/<theme>.json       # brand voice
```
Drop a new file in and it's a new option. No code change needed. Template front-matter keys the agent reads:

| Key | Meaning |
|---|---|
| `channel` | `social` (post-worthy only), `owned` or `internal` |
| `model` | `longform` uses `LONGFORM_MODEL` (Opus); default uses `CONTENT_MODEL` |
| `scope` | `multi` = scheduled-digest template (fed every worthy PR) |
| anything else | passed to the writer as rules (`max_chars`, `tone`, `must_include`, `avoid`, …) |

If you add a template or theme, also add its name to the `formats` / `theme` descriptions in
`src/agent/metadata.json` so it shows in the run form.

## Output

The run returns JSON. The UI shows it, and `report` holds download links:

```json
{
  "mode": "on_demand",
  "status": "awaiting_human_approval",
  "message": "Post Worthy (score 82): 2 draft(s) ready for review.",
  "items": [{
    "pr": {"url": "…", "repo": "acme/web", "number": 412, "title": "…", "merged": true},
    "verdict": {"decision": "post_worthy", "score": 82, "category": "new_feature",
                "reasons": ["…"], "formats_to_generate": ["linkedin_post", "release_notes"],
                "skipped_formats": []},
    "summary": {"headline": "…", "plain_english": "…", "who_benefits": "…",
                "verified_facts": [{"fact": "…", "source": "src/reports/ExportButton.tsx"}],
                "customer_benefits": [{"benefit": "…", "evidence": "…"}],
                "assumptions": ["…"], "missing_info": ["Release date"], "questions_for_dev": ["…"]},
    "drafts": [{"format": "linkedin_post", "theme": "default", "content": "…", "char_count": 912,
                "claims_checked": true, "flags": [], "warnings": [],
                "status": "draft_needs_approval"}]
  }],
  "report": {"pdf": {"type": "s3_download_link", "file_key": "…"}, "markdown": {"…": "…"}}
}
```
Scheduled runs return the same `items` (worthy PRs only) plus `period`, `totals`, `digest`,
`other_prs` (internal/hold, one line each) and `truncated`. If nothing worthy merged, the status is
`nothing_to_announce` and the PDF says so.

`flags` lists claims the verifier removed. `warnings` lists other problems to fix by hand,
such as going over the template's `max_chars`.

## Run it on demand

```bash
cd pr_marketing_agent
cp .env.template .env      # BIFROST_URL, BIFROST_VIRTUAL_KEY, GITHUB_TOKEN, TENANT_ID, task queues
uv sync
```
Then run with `aetherion run` and payload `{"pr_url": "https://github.com/owner/repo/pull/123",
"formats": "linkedin_post,release_notes"}`. Set `LOCAL_OUTPUT=true` to write reports and cursors to
`./output/` instead of S3.

## Run it on a schedule

Create a Milkyway schedule (UI, or `POST /schedules/create-schedule`):
```json
{
  "agent_name": "PrMarketingAgent",
  "cron_expression": "0 9 * * 1",
  "agent_payload": {"mode": "scheduled", "repos": "acme/web\nacme/api",
                    "formats": "release_notes", "theme": "default", "lookback_days": 7}
}
```
Each run picks up PRs merged into the default branch since the last successful run. Cursors are
stored in S3 at `pr_marketing/state/<owner>__<repo>.json`. The first run looks back `lookback_days`. At most
`MAX_PRS_PER_RUN` PRs per run; the rest roll over to the next run. Cursors only advance when the
run succeeds.

## Models

| Step | Env | Default |
|---|---|---|
| triage, verify | `TRIAGE_MODEL`, `VERIFY_MODEL` | `claude-haiku-5-5` |
| analysis, most content | `ANALYSIS_MODEL`, `CONTENT_MODEL` | `claude-sonnet-5-5` |
| blog drafts | `LONGFORM_MODEL` | `claude-opus-5-5` |

All calls go through Bifrost. A per-run model override set by the platform replaces all of these.

## Security

- GitHub token: fine-grained, read-only (Contents: Read, Pull requests: Read), from env/secret.
- Only the sanitized diff reaches the LLM; no code goes into drafts or the PDF.
- PR text is wrapped as data in prompts, so instructions inside a PR description are ignored.
- There is no publish tool; publishing stays a human step.
- The SDK's `call_llm_sync` logs the full prompt at INFO, so sanitized diffs show up in worker
  logs. Treat those logs as code-sensitive.
