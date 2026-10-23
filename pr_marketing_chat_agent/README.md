# PrMarketingChatAgent

A chat agent for the Calfus marketing team. Ask it in plain English what the product team has
shipped or is building; it looks the answer up in the product docs (GitHub, `CalfusAgentFoundry/lore`)
and Jira, explains it without jargon, and turns it into on-brand content:

- LinkedIn / X posts, blog drafts, release notes, email announcements, marketing briefs, weekly digests
- Calfus-branded social images: a ready-to-post 1080×1350 `.png` plus an editable `.pptx`, built
  from the Calfus social-media template
- Blog documents as a branded Word file (`.docx`) and `.pdf`

Every draft is checked against the source text it was written from; unsupported claims are
removed and listed. Drafts still need a human review before publishing.

## Example conversation

> **What shipped in the last two weeks?**
> **Explain the Jira connector like I'm new to it.**
> **Write a LinkedIn post about it.** → draft `d1`
> **Make it shorter and more upbeat.** → revises `d1` into `d2`
> **Make the image with the dark layout.** → `.png` + `.pptx` downloads
> **Now a blog post, and give me the Word file.** → `.docx` + `.pdf` downloads

## How it fits the platform

- Registered with `@register(chat_enabled=True, chat_direct=True)` (SDK ≥ 0.0.106). In
  **Conversations**, `@PrMarketingChatAgent` once; the chat keeps it selected and starts one run
  per message with the message as `requirement`.
- The chat doesn't pass earlier messages to the run, so the agent keeps its own memory: one JSON
  object per conversation in the tenant bucket (`pr_marketing_chat/conversations/…`), holding
  the last turns, drafts (`d1`, `d2`, …) and files.
- Each turn is a loop: the model (`CHAT_MODEL`) replies or calls tools; tools run as Temporal
  activities. The loop stops after 8 steps or 8 minutes (the chat stops waiting at 15).
- No HITL: `hitl_enabled` would route the next chat message into a paused run instead.

| Model tool | What it does |
|---|---|
| `list_docs`, `read_doc`, `whats_new_in_docs` | Docs index, page text, recent doc commits (`DOCS_PATHS` only) |
| `find_jira_work`, `jira_issue` | Jira search (JQL built in code, scoped to `JIRA_PROJECTS`) and one issue |
| `write_draft` | Template + brand theme + re-fetched sources → draft → claim check; also revises |
| `make_social_visual` | Draft → copy for a template layout → `.png` + `.pptx` |
| `make_blog_document` | Draft → `.docx` + `.pdf` |

## Setup

```bash
uv sync                      # needs CodeArtifact auth for aetherion_sdk
cp .env.template .env        # then fill it in
```

Credentials (least privilege):

- `GITHUB_TOKEN`: fine-grained token with **Contents: Read** on `CalfusAgentFoundry/lore` only.
- `JIRA_EMAIL` + `JIRA_API_TOKEN`: a read-only service account with Browse on the projects in
  `JIRA_PROJECTS`. The token acts with that account's permissions.
- `AI_GATEWAY_URL` / `BIFROST_VIRTUAL_KEY` come from the platform environment.

## Run and test

```bash
uv run pytest                                   # unit tests + a sandboxed Temporal run (needs the temporal CLI)
uv run python scripts/chat_local.py             # chat locally, files land in ./output
uv run python scripts/chat_local.py "What's new in the docs?" "Write an X post about it"
```

Publish as usual; bump `version` in `src/agent/metadata.json` and `src/tools/metadata.json`
each time.

## Brand assets

`src/brand/` holds the layouts (`layouts.json`: slot shape ids, boxes, fonts, character limits)
and the assets cut from the template: one single-slide `.pptx` per layout, a text-free background
plate per layout, the logo and the template's embedded fonts (OFL, see `assets/fonts/FONTS.md`).
When marketing updates the template, rebuild on a Mac:

```bash
uv run python scripts/build_brand_assets.py "Calfus -Social media templates.pptx"
```

Layouts shipped: `headline_light` (slide 1), `headline_dark` (slide 4), `three_steps` (slide 6).
The quote and photo testimonial slides are left out: they need real customer quotes and photos.

## Limits

- Attachments in chat aren't read yet (the agent says so).
- A docs page is cut at 12,000 characters per read (Canvas, CLI and SDK Utilities are longer).
- `kb/` in lore is excluded by default because it is older than `content/docs`.
