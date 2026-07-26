# Memory System Design

## Current Scope

The current memory system keeps three layers:

- Fragment memories
- Event memories
- Core memories

At this stage, core memories are an explicit manual management label only.

Current behavior remains unchanged:

- Core memories can be promoted manually.
- Core memories are not generated automatically.
- Core memories are not selected automatically by importance.
- Core memories do not receive extra priority during context injection.
- Memory retrieval and chat context injection continue to use the existing active-memory relevance retrieval flow.

This is intentional. The system is currently evaluating the new memory model and the new summary event-card structure. The important things to observe first are:

- Memory extraction accuracy.
- Whether people and subjects are attributed correctly.
- Whether facts and emotions are separated cleanly.
- Whether suggested topic classification is stable.

Automatic core-memory judgment is deferred so issues in extraction, summarization, and retrieval are easier to isolate.

## Current Retrieval Model

Current flow:

```text
active memories
-> relevance retrieval
-> context injection
```

The `layer` field remains available for management and review, but the current runtime behavior should not assume that `core` implies a separate injection path.

## Core Memory Priority (Future)

Goal: make core memories represent long-term stable information that receives higher priority when building AI context.

Possible future retrieval flow:

```text
core memories
-> fixed priority injection

event memories
-> relevance retrieval

fragment memories
-> low priority supplement
```

Future core-memory management may include:

- A clearer core-memory view in Dashboard.
- Manual promote and demote actions.
- A visible reason for why a memory is marked as core.
- Optional model-suggested core memories.

Model-suggested core memories must require human confirmation. They must not be upgraded automatically.

## Explicit Non-Goals For Now

The current phase must not change:

- Database structure.
- `memory_extractor.py`.
- Memory retrieval logic.
- Chat context injection.
- The current DeepSeek memory-model test.
- Automatic judgment of core memories.
