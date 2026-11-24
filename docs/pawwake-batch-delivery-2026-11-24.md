# Pawwake 4.2 adaptation — integrated experimental delivery (2026-11-24)

## Branch and safety

- Development only: `feature/pawwake-4.2-synced`.
- Current production code: `fix/multimodal-images`; `main` unchanged.
- Active-branch commits reconciled into the development branch via PR #2 (merge commit `6801915dc1f874e19555b9da5682a3418715f8a7`).
- **No live database migration, Coolify deployment, or Kelivo production replay has been performed.**
- A code commit or local test does not mean the production gateway has changed.

## Integrated adaptations

| Workstream | Code locations | Default behavior |
|---|---|---|
| Shared ordinary memory + approved experience recall | `main.py`, `database.py`, `recall_budget.py`, `experience_recall.py` | Experience recall OFF; if enabled, one card, <=600 chars and <=20% of shared budget; >=4 user rounds between successful recalls |
| User-pinned stable memories | `database.py`, `memory_recall_extensions.py`, `main.py`, Dashboard | OFF; explicit pin only; at most four active pins and 900 chars; avoid duplicate injection when same memory is also retrieved |
| Proper noun dictionary | `memory_recall_extensions.py`, `main.py`, Dashboard | Empty. Bounded literal alias JSON, no regex or model calls |
| Optional memory reranker | `recall_rerank.py`, `main.py`, Dashboard | OFF; provider API may incur extra costs; max 20 candidates; technical errors fall back to pre-rerank order |
| Conservative deduplication | `recall_selection.py`, `main.py`, `database.py`, Dashboard | OFF; same-session identity/text evidence; unknown-session records are kept separate |
| Verified source IDs | `database.py`, `main.py` | New nullable `memories.source_message_ids`, legacy NULL, no guessed historical backfill. Explicit API writes verify each original message's session and existence; reads fail closed if stale |
| Conversation recycle bin | `database.py`, `main.py`, Dashboard | Whole-session deletion saves actual deleted rows and cache state atomically. Restore preserves IDs, timestamps, multimodal metadata and summary cursor, blocks reused session IDs. Permanent purge requires confirmation; no auto-purge |
| Read-only external MCP subset | `mcp_readonly.py`, `main.py` | OFF; stateless JSON-RPC tool list/search/lineage only. Must set `MCP_READONLY_ENABLED=true` **and** a nonempty `GATEWAY_SECRET`, send exact `X-Gateway-Key`; no mutation tools |
| Existing dashboard experience cards and provenance | `templates/dashboard.html`, `static/js/dashboard.js` | Preserve pagination, visibility/status filtering, selected-only bulk approvals, source preview and current branding |
| Dependencies | `requirements.txt` | Updated, pinned to tested releases in experimental branch only |

## CI and acceptance

- Workflows: `.github/workflows/gateway-regression.yml` (all `test_*.py`, disposable PostgreSQL 16) and `.github/workflows/integration-smoke.yml` (targeted tests, Python compile, JS syntax).
- The PostgreSQL suites use hard-coded `postgresql://test:test@127.0.0.1:5432/gateway_test` and skip if `TEST_DATABASE_URL` is not exactly that value.
- Test coverage includes deletion/restore of image+tool metadata and summary, concurrent-safe archive query, source-ID session validation and stale refs, pins, MCP auth, settings/recall construction, legacy schema migration twice, and preservation of the baseline chat regression suite.
- Check the **latest head SHA** for both green workflows; an earlier green commit does not validate later changes.
- **Do not deploy until** both final CI jobs pass, a disposable PostgreSQL migration check passes, and a separate staging gateway is exercised from Kelivo with text, images, tool calls, cache rotation, source queries and recap recall.
- Make production database + active branch snapshots before any promotion. Confirm no unsupported old DB customizations; plan a revert of the integration commit and schema strategy.
- Rerank needs a real opted-in provider test before enabling in production; never assume a provider accepts these request bodies solely from unit tests.
- Historical memory sources cannot be reconstructed with certainty; no automated source-ID backfill was implemented. Memory provenance is only verified for explicitly attached, existing same-session message IDs.
- MCP is a **minimal stateless read-only subset**, not a promise of full MCP SDK transport compatibility.
- The separate damaged-summary recovery and Pawwake proactive-learning plans are not automatically considered completed by this integration.

## Configuration guidance (experimental only)

```env
EXPERIENCE_RECALL_ENABLED=false
MEMORY_PINNED_ENABLED=false
MEMORY_RERANK_ENABLED=false
MEMORY_RERANK_MODEL=
MEMORY_DEDUP_ENABLED=false
MEMORY_QUERY_ALIASES_JSON=
MCP_READONLY_ENABLED=false
# GATEWAY_SECRET is mandatory before MCP can be enabled.
```

New options that are persisted by the Dashboard remain off unless explicitly changed. The development branch is not live.
