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

## 2026-11-18 provenance safety checkpoint
- Checked live schema definition: ordinary `memories` has `source_session` and merged memories have `merged_from`; existing `shared_experience_cards` already has `source_message_ids`. **No old message IDs may be inferred from text.**
- Added `memory_provenance.py` as a pure read-only resolver: walks `merged_from` lineage with cycle/node limits, unions only explicitly recorded IDs associated with a single source session, reports `complete=false` for legacy, missing, mixed-session, or ambiguous evidence.
- Source lineage **is not connected to production queries**, because historic ordinary memories lack verified source-message IDs. No implicit migration, reprocessing, or production writes.
- Adjusted `recall_selection.py` to accept mapping-like database rows and return sorted JSON-safe source-message IDs, with two new boundary tests.
- Actually executed in an isolated container using Python 3.13.5: `python -m unittest discover -s tests -p 'test_*.py' -v` -> **22 targeted tests passed** (`test_recall_selection.py` 12 + `test_memory_provenance.py` 10), and `python -m compileall -q recall_selection.py memory_provenance.py tests` -> succeeded.
- This is a staged-module test subset; the full gateway regression suite, real PostgreSQL integration, and Kelivo end-to-end tests are **not verified** for these new commits. The official GitHub Actions result has not been retrieved.

## 2026-11-18 read-only lineage preview integrated (experimental branch only)
- Added `memory_lineage_query.py`, bounded to 100 ancestry nodes and 16 levels by default. It follows `memories.merged_from` using only existing `id`, `merged_from`, `source_session`; checks missing parents, cycles, malformed references, node limits and mixed-session ambiguity.
- Added `database.get_memory_lineage_readonly()` with SELECT-only reads, and an authenticated GET `/api/memories/{memory_id}/lineage` returning 404 for a missing root.
- Added a `来源链` button in the existing Dashboard memory table. It reports memory ancestry only and prominently states that memory IDs are **not** source conversation IDs.
- No schema changes, history backfill, conversation writes, runtime memory injection, summary or persona changes. The deployed branch is untouched.
- Added `tests/test_memory_lineage_query.py` (10 isolated async tests), `tests/test_memory_lineage_db.py` (database adapter/API mocks), `tests/test_memory_lineage_wiring.py` (static integration guards).
- Actual isolated tests executed in local Python 3.13.5 environment: 32/32 passed across `test_recall_selection.py`, `test_memory_provenance.py`, `test_memory_lineage_query.py`; `python -m compileall -q recall_selection.py memory_provenance.py memory_lineage_query.py tests` passed. **These 30 do not include full application database/route mocks or static wiring tests**; they require the full checked-out repository and dependencies.
- Release blockers: run full GitHub Actions regression on this branch; confirm FastAPI route and Dashboard JS syntax; verify a real staged PostgreSQL with copy/synthetic data; then Kelivo end-to-end tests. GitHub Actions status for the current commit has not been independently verified.

## 2026-11-20 provenance regression fix
- Fixed staged `memory_provenance.trace_memory_provenance`: a shared ancestor in a diamond-shaped merge DAG is no longer incorrectly flagged as a cycle. Actual ancestor-path cycles still fail closed.
- On any incomplete lineage (missing ancestor, invalid source, cycle, over-limit), `source_message_ids` is now empty instead of leaking partially verified message IDs.
- Added two regression cases and tightened the missing-parent assertion in `tests/test_memory_provenance.py`.
- **Verification pending:** GitHub source was updated but no full Python test execution or CI success has been observed for these commits. Earlier 32/32 isolated results apply only to the earlier commit, not to this revision.
- No schema, production data, deployed branch, or active retrieval behavior changed.

## 2026-11-20 recall quality and provenance safety pass
- `memory_lineage_query.py`: replaced ad-hoc cycle detection with iterative 3-colour traversal; flag a missing session on any leaf as incomplete; added shared-DAG and missing-session tests.
- `recall_selection.py`: preserve word boundaries in normalized text, avoid cross-session exact-text deduplication when both sessions are known, and require substantial text overlap in addition to >=70% shared message-source overlap. This protects distinct observations made during the same conversation.
- Updated tests for exact-text deduplication, source overlap, distinct observation, and cross-session repeated wording.
- **Migration design, not execution**: the current `memories` schema does not contain `source_message_ids`. Before adding it, define canonical semantics for each source (single session + positive conversation message IDs); use additive nullable column migration, backfill only where provenance can be verified against `conversations` with session match, batch with audit counts, snapshot and dry-run, and leave all ambiguous legacy memories NULL. A merge of multiple sessions must retain session-scoped evidence separately, not union numeric IDs into a single session.
- Existing `merged_from` values are memory IDs, not message IDs. Never infer the latter from content similarity.
- **Release status**: commits are saved to the Pawwake development branch; full tests/CI and production-like PostgreSQL/Kelivo integration are not verified. The previously reported 32 passing tests predate this change and do not validate it. No production migration or deployment.
