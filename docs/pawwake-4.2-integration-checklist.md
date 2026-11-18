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

## 2026-11-18 shared-budget staging
- Added pure `recall_selection.py` with `select_recall_items(memories, cards, max_items=8, max_chars=6000)`. It is deliberately NOT wired to live chat, because current memory rows may not carry `source_message_ids` and shared budgeting must be integrated consistently across three constructors.
- Candidate order: approved/AI-visible experience first, then active memory; only identical normalized content or >=70% Jaccard overlap of positive source-message IDs in the same nonempty session counts as duplicate. Nonmatching source identities are retained.
- No historical writes or schema migration; data records are not mutated. Text is never truncated inside this selector. This budget counts characters, not model tokens, and does not yet include wrappers or dates.
- Added `tests/test_recall_selection.py` (10 targeted tests). Executed locally in an isolated Python 3.13 runtime: `python -m unittest discover -s tests -p 'test_*.py' -v` -> 10 passed; `python -m compileall -q recall_selection.py tests` -> passed. This local test environment had ONLY this module and its tests, not the whole gateway.
- Remaining before production integration: obtain source IDs for ordinary memories with explicit migration approval, verify result types from database queries, apply one budget across all constructors, preserve citation/detail metadata, add real PostgreSQL tests, then do production-like end-to-end tests.
