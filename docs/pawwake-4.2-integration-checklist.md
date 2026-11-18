# Pawwake 4.2 integration — branch handoff

## Branch roles (2026-11-18)
- **Active repaired baseline:** `fix/multimodal-images`. Includes later summary-boundary fixes, memory restore tooling, experience-card UI, and opt-in exact-cue recall. Do not overwrite or reset.
- **Current integration target:** `feature/pawwake-4.2-synced`, created from the latest repaired baseline at `43a7ee014556e2c8ec47dc58ddc6287b29f5c396` and then kept in sync with the latest visibility filter changes.
- **Older branch:** `feature/pawwake-4.2-integration`. Diverged before extensive repairs. Retain for history; DO NOT deploy or merge directly into the active branch.
- **`main`:** Leave untouched until the user explicitly requests a change.

## Existing capabilities in refreshed branch
- Exact-cue retrieval of human-approved, AI-visible experience cards; defaults OFF.
- Session-scoped cooldown and duplicate-card suppression; independent read-only cue preview.
- Compact review cards, visibility filtering, 12-card pagination, atomic selected-only bulk approve/archive (max 30).
- Staged optional `recall_rerank.py` from upstream, NOT wired into production retrieval yet.
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
