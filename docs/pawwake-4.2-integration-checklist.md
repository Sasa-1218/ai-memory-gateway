# Pawwake 4.2 integration — branch handoff

## Branch roles (2026-11-18)
- **Active repaired baseline:** `fix/multimodal-images`. Includes later summary-boundary fixes, memory restore tooling, experience-card UI, and opt-in exact-cue recall. Do not overwrite or reset.
- **Current integration target:** `feature/pawwake-4.2-synced`, created from repaired baseline `43a7ee014556e2c8ec47dc58ddc6287b29f5c396`. Current dashboard JS and HTML were compared byte-for-byte with active `fix/multimodal-images` on 2026-11-18 and matched; the branch still has distinct history, so do not force-merge it.
- **Older branch:** `feature/pawwake-4.2-integration`. Diverged before extensive repairs. Retain for history; DO NOT deploy or merge directly into the active branch.
- **`main`:** Leave untouched until the user explicitly requests a change.

## Existing capabilities in refreshed branch
- Exact-cue retrieval of human-approved, AI-visible experience cards; defaults OFF.
- Session-scoped cooldown and duplicate-card suppression; independent read-only cue preview.
- Compact review cards, visibility filtering, 12-card pagination, atomic selected-only bulk approve/archive (max 30).
- Staged optional `recall_rerank.py` from upstream, NOT wired into production retrieval yet. 2026-11-18 safety pass: gate plus per-item relevance threshold, finite-score validation, 20-candidate cap, 500-character query limit, bounded candidate text, and no paid request for empty pools. Failures return `None` for caller fallback; valid no-match returns `[]`.
- Regression CI and additional independent integration-smoke CI.

## Remaining Pawwake 4.2 upstream work
- Pinned memories and stable injection safety
- Proper noun dictionary and query matching
- Optional rerank wiring with runtime configuration and cost controls
- MCP endpoints and authentication
- Conversation recycle bin and recoverability
- Remaining search/scoring fixes, settings, Dashboard enhancements
- Dependency compatibility review, migrations, and staging DB tests
- Preserve custom multimodal handling, summary repair, memory/experience cards, shadow push, i/o context, Dashboard styling

## Mandatory invariants
- No automatic second-stage folding of existing summaries.
- Keep current 600-character summary prompt and 8-round cadence unless user requests changes.
- Only approved + AI-visible experience cards may enter chat.
- Test on isolated PostgreSQL before production deployment. CI passing does NOT mean Kelivo to model E2E tested.
- Never point experimental code at production database for write tests.
- Before integrating later active-branch fixes, compare commits and selectively merge into this refreshed integration branch, never overwrite newer repaired files.


## 2026-11-18 selective adaptation checkpoint
- Retained existing Rora gateway memory scoring, recent-summary behavior, experience approval controls, and multimodal persistence. No main/active branch writes.
- Current experiment: reranking is isolated, **not turned on by deployment**. It is not yet approved to run on each chat because that would incur provider costs and possibly reject otherwise useful retrievals.
- Required before enabling: opt-in UI setting, bounded costs, representative Chinese-personal-memory benchmark, fail-open wiring in both chat constructors, data compatibility, and real provider tests.
- Real production PostgreSQL and Kelivo-to-OpenRouter end-to-end tests **not performed**. No database migrations or historical-data recalculation.
- The existing 12-card Dashboard pagination loads at most 1000 cards; large-card paging remains separate future work. Do not claim unlimited paging.
- Risk: stricter candidate threshold may return a valid empty selection; fallback should only occur on technical failures, and no caller is connected yet.
- Rollback: restore earlier Pawwake branch commit `c4cd77d9ad5e23515847dee6ce8dddaaa6910d0d` (or revert specific rerank commits) on this experimental branch only.
