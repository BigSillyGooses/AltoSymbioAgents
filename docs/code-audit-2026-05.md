# Code Audit — May 2026

**Status:** Historical artifact. All bugs identified below were already fixed in the commits preceding this file's move from the repository root into `docs/`. Each finding's `Status:` / `Fixed in:` block records where the fix landed. Keep this file for traceability — do not treat unmarked findings as open issues.

Findings from a full read of the altosybioagents codebase, organized by severity (most likely to cause real user-facing problems first). Each finding has been verified against the source at the cited file:line locations.

---

## BUG 1 — Dead `_active_mem_suffix` variable causes silent context loss on RAG trimming

**File:** `backend/services/chat_orchestrator.py:552`
**Severity:** Medium
**Status:** ✅ Already fixed.

`_active_mem_suffix` is initialized to `""` and never reassigned, but on line 625 (inside the RAG-trimming branch) it is checked with `if _active_mem_suffix:` and would be appended to `full_system` if truthy. Because it is always empty, any "active memory suffix" content that was intended to be re-injected after trimming is silently dropped.

This looks like an incomplete refactor — upstream code that set this variable was removed, but the consumer was not.

**Practical impact:** when RAG chunks exceed `max_context_items` and get trimmed, the system prompt is rebuilt from `system_prompt + mem_suffix` but whatever `_active_mem_suffix` was supposed to carry is gone.

**Fix:** either remove the dead branch, or restore the upstream assignment.

**Fixed in:** `backend/services/memory_recall.py:123-148`. The dead variable was removed entirely; both the initial recall and the post-trim rebuild now route through `MemoryRecall._assemble()` (single source of truth for system-prompt assembly). The orchestrator calls `self._memory_recall.trim_for_complexity(...)` at `backend/services/chat_orchestrator.py:1202-1208` to rebuild after trim, which internally re-invokes the same `_assemble()` helper. Regression test: `backend/tests/test_chat_orchestrator.py::TestReviewBugFixes::test_bug1_rag_trim_path_does_not_raise`.

---

## BUG 2 — `_invoke_claude` and `_invoke_local` in `hub_router.py` are dead code

**File:** `backend/services/hub_router.py:431-498`
**Severity:** Low (cleanup)
**Status:** ✅ Already fixed.

`_invoke_claude` (lines 431-461) and `_invoke_local` (lines 463-498) are private methods on `HubRouter`. The actual `invoke()` method at line 352 calls `client.stream_unified()` and `client.chat_unified()` directly — never these private methods. A `grep` over `backend/` confirms no production callers; the only references are in test names (which test `hub.invoke()`, not these private methods) and one stale comment in `qwen_thinking.py:79`.

**Practical impact:** ~70 lines of dead code, with two parallel dispatch paths existing in the same module — a maintenance hazard.

**Fix:** delete `_invoke_claude`, `_invoke_local`, and the stale comment in `qwen_thinking.py`.

**Fixed in:** `backend/services/hub_router.py`. Neither `_invoke_claude` nor `_invoke_local` exists in the module any longer; `HubRouter.invoke()` at line 371 is the only dispatch path and calls `client.stream_unified()` / `client.chat_unified()` directly. The `qwen_thinking.py:79` comment ("Worker invocation (used by HubRouter.invoke in Phase 3)") is now accurate — `worker_think()` *is* called by `HubRouter.invoke()` at `hub_router.py:397`.

---

## BUG 3 — `stream_multi_turn` return value inconsistency between clients

**Files:**
- `backend/services/local_client.py:179-180` — returns `str`
- `backend/services/claude_client.py:190-196` — returns `tuple[str, object]`

**Severity:** Low (latent)

The unified interface (`stream_unified`/`chat_unified`) papers over this, but any direct caller of `stream_multi_turn` polymorphically would blow up on tuple unpacking against the local client. The dead `_invoke_claude` (Bug 2) does exactly this — if anyone revives it to handle `local`, it breaks.

**Fix:** make both clients return the same shape, ideally a `dict` like the `*_unified` methods, or document the contract explicitly.

**Fixed in:** both clients now declare `-> tuple[str, object]`:
- `backend/services/local_client.py:412-461` returns `(full_text, None)` — local backends don't report usage, so the second slot is always `None`. Docstring at line 417 documents the contract.
- `backend/services/claude_client.py:193-230` returns `(full_text, usage)` with `usage` carrying `.input_tokens` / `.output_tokens`. Docstring at lines 200-213 documents the contract and the required tuple unpacking.

The only direct callers are `qwen_thinking.worker_think()` (`text, _usage = local_client.stream_multi_turn(...)`) and the `stream_unified` wrappers — both already unpack the tuple safely.

---

## BUG 4 — `response_empty` evaluated before escalation can replace the response

**File:** `backend/services/chat_orchestrator.py:929` (computed) → `:1003` (logged)
**Severity:** Medium (analytics corruption)
**Status:** ✅ Already fixed.

`response_empty` is computed as `len((response_text or "").strip()) < 20` at line 929. Lines 930-992 can escalate to Claude and replace `response_text` entirely (line 984: `response_text = esc_result.text`). Line 1003 then logs `response_empty` to `router_log` — but it is still the stale value from before escalation.

**Practical impact:** the `router_log` records "response was empty" even when escalation produced a full response, corrupting router-accuracy analytics.

**Note:** there is also a side-issue at line 933 — the escalation gate is `not response_empty`, so a fully empty local response **skips** escalation, even though that is the case where it is most needed.

**Fix:** recompute `response_empty` after escalation, and reconsider the gate logic.

**Fixed in:** `backend/services/escalation_ladder.py:71-129`. `EscalationLadder.maybe_escalate()` returns an `EscalationOutcome` whose `response_empty` is recomputed at line 128 (`outcome.response_empty = self._is_empty(outcome.response_text)`) *after* any escalation has run. The orchestrator threads `esc_outcome.response_empty` into the `router_log` write at `backend/services/chat_orchestrator.py:1760, 1789`. The side-issue at the old line 933 was also addressed: an empty local response now triggers the "Rung 1" empty-response gate at `escalation_ladder.py:110-115`, which fires escalation specifically because it's empty (no longer skipped). Regression test: `backend/tests/test_chat_orchestrator.py::TestReviewBugFixes::test_bug4_router_log_records_post_escalation_response_empty`.

---

## BUG 5 — Budget check uses pre-response `spent` instead of post-response total

**File:** `backend/services/chat_orchestrator.py:454-470, 1070-1074`
**Severity:** Medium (race condition)
**Status:** ✅ Already fixed.

`spent` is fetched at line 461 inside `_db._lock`, but the lock is released after the user-message INSERT at line 470. The LLM call then runs (possibly tens of seconds), and the budget warning at line 1071 calculates `new_spent = spent + cost` using the stale `spent`.

**Practical impact:** if two concurrent sends on the same conversation overlap (one starts before the other's `token_usage` INSERT commits), both will use the same `spent` value, and the sum can exceed the budget without either one triggering the warning.

**Fix:** re-read the running total just before the warning check, inside `_db._lock`.

**Fixed in:** `backend/services/turn_lifecycle.py:105-183`. `TurnLifecycle.close()` runs the three writes (assistant `messages` INSERT + conversations UPDATE + `token_usage` INSERT) and the post-write budget re-read inside one `_db.transaction()` context (lines 138-181). The `SELECT COALESCE(SUM(cost_usd), 0) ... WHERE conversation_id = ?` at lines 166-170 fires *after* this turn's `token_usage` row is inserted but *before* the lock releases, so the warning sees the true cumulative total including any concurrent send's already-committed row. Regression test: `backend/tests/test_chat_orchestrator.py::TestReviewBugFixes::test_bug5_concurrent_sends_share_budget_state`.

---

## BUG 6 — Two separate `commit()` calls for one logical operation in the send path

**File:** `backend/services/chat_orchestrator.py:1028, 1061`
**Severity:** Medium (durability)
**Status:** ✅ Already fixed.

Line 1028 commits the assistant message + conversation update, then line 1061 commits the `token_usage` row separately. A crash (or process kill) between the two commits leaves the conversation with a message but no corresponding `token_usage` row, causing under-counting in budget checks and the token-stats view.

**Fix:** issue both INSERTs and the UPDATE in a single transaction, then `commit()` once.

**Fixed in:** `backend/services/turn_lifecycle.py:138-181`. The assistant `messages` INSERT, conversations UPDATE, and `token_usage` INSERT all run inside one `_db.transaction()` block. A crash or `OperationalError` between them rolls all three back together — no torn writes. Regression test: `backend/tests/test_chat_orchestrator.py::TestReviewBugFixes::test_bug6_sqlite_error_rolls_back_assistant_and_token_usage` injects a mid-write `OperationalError` and asserts both the assistant row and the `token_usage` row are absent after rollback.

---

## BUG 7 — `_risk_history` dict grows unbounded across conversations

**File:** `backend/services/chat_orchestrator.py:173, 251, 745-748`
**Severity:** Low (slow leak)
**Status:** ✅ Already fixed.

`_risk_history` is keyed by `conversation_id`. Each list is capped at 5 entries by `del history[:-5]` (line 748), but the dict itself has no eviction — entries are removed only in `delete_conversation` (line 251). Active conversations that go quiet but are never deleted leave entries forever.

**Practical impact:** a slow memory leak in long-running sessions with many conversations.

**Fix:** add an LRU bound on the dict, or evict on conversation archival.

**Fixed in:** `backend/services/security_gate.py:50, 64-69, 183-196`. `SecurityGate._risk_history` is now an `OrderedDict` capped at `DEFAULT_RISK_HISTORY_MAX_CONVERSATIONS = 256`. `_record_history()` calls `move_to_end()` on each access (LRU touch) and `popitem(last=False)` to evict the oldest entry when the cap is exceeded. `SecurityGate.forget()` (line 119) is invoked from `ChatOrchestrator.delete_conversation()` at `chat_orchestrator.py:315` so archived conversations are evicted immediately rather than waiting for LRU pressure.

---

## BUG 8 — `app://-` CORS origin does not match Electron production loader

**File:** `backend/server.py:225` and `desktop-shell/main.ts:166`
**Severity:** Low
**Status:** ✅ Already fixed.

`allow_origins` includes `"app://-"` commented as "electron-vite production." But `desktop-shell/main.ts:166` shows production loads via `mainWindow.loadFile(...)`, which serves from the `file://` protocol — not `app://`.

**Practical impact:** the documented production origin in CORS is wrong. In practice this is moot today because the renderer's outbound calls are to `localhost`, not the renderer origin, and `BearerAuthMiddleware` is the actual auth gate. But the dead allow-list entry is misleading and would matter the moment the renderer issued a true cross-origin request.

**Fix:** remove the `app://-` entry, or replace it with the actual loader origin.

**Fixed in:** `backend/server.py:184-200`. The `app://-` entry has been removed from `allow_origins`; only the localhost dev-server origins (`http://localhost:5173` / `:5174` and their `127.0.0.1` counterparts) remain. The comment at lines 186-190 documents that packaged Electron loads via `file://` (Origin `null`), which is not gated by this list — `BearerAuthMiddleware` is the actual auth gate.

---

## BUG 9 — `@app.on_event("startup")` is deprecated

**File:** `backend/server.py:238`
**Severity:** Low (forward-compat)
**Status:** ✅ Already fixed.

`@app.on_event("startup")` has been deprecated since FastAPI 0.93 in favor of lifespan context managers. It still works in current FastAPI, but it is a ticking clock for a future framework upgrade.

**Fix:** migrate to `@asynccontextmanager` lifespan handlers via `FastAPI(lifespan=...)`.

**Fixed in:** `backend/server.py:163-180`. The startup hook is now an `@asynccontextmanager`-decorated `_lifespan()` function passed via `FastAPI(..., lifespan=_lifespan)` at line 179. The body still attaches the SSE event loop and runs the `workflow_checkpoints` orphaned-row sweep (unchanged from the prior `on_event("startup")` body), then `yield`s. `lifespan="on"` is also forwarded to uvicorn at line 311 so the handler actually fires.

---

## BUG 10 — `session_facts.status` column added in both `_create_schema` AND migrations

**File:** `backend/db.py:190` (schema) and `backend/db.py:662-664` (migration)
**Severity:** Low (noise)
**Status:** ✅ Already fixed.

`_create_schema` issues `ALTER TABLE session_facts ADD COLUMN status` (with a `try/except OperationalError` to swallow duplicate-column errors). The migration `"session_facts.status"` does the same `ALTER TABLE`. For a fresh database the schema creates the column, then the migration tries to add it again, hitting the duplicate-column error path on every fresh-database startup.

**Fix:** remove the migration entry once the schema covers it, or remove it from the schema (preferred — let migrations own column adds).

**Fixed in:** `backend/db.py:220-231, 701-703`. The `ALTER TABLE session_facts ADD COLUMN status` was removed from `_create_schema()`; only the migration `("session_facts.status", ...)` (lines 701-703) owns the column add. A comment at lines 229-231 documents the choice ("The `status` and `last_accessed` columns are added by the migrations below — keeping column adds in one place avoids duplicate-column noise on every fresh-database startup.").

---

## BUG 11 — `agent_performance` table created in both `_create_schema` AND migrations

**File:** `backend/db.py:230-242` (schema) and `backend/db.py:667-678` (migration)
**Severity:** Low (redundancy)
**Status:** ✅ Already fixed.

Same pattern as Bug 10. Lines 230-242 `CREATE TABLE IF NOT EXISTS agent_performance` and the index. Migration `"agent_performance.1.0"` does the same. Harmless because of `IF NOT EXISTS`, but redundant.

**Fix:** consolidate into one source of truth.

**Fixed in:** `backend/db.py:259-271, 706-717`. The `agent_performance` `CREATE TABLE` and its index were removed from `_create_schema()`; only the migration `("agent_performance.1.0", ...)` (lines 706-717) owns them. A comment at lines 268-271 documents the choice ("`agent_performance` is created by its own migration below (`agent_performance.1.0`). Keeping the create in one place avoids the duplicate `CREATE TABLE`/`CREATE INDEX` calls that fired on every startup.").

---

## BUG 12 — Savings calculation in `get_token_stats` uses hardcoded Sonnet input price

**File:** `backend/services/chat_orchestrator.py:1213-1215`
**Severity:** Low (analytics accuracy)
**Status:** ✅ Already fixed.

`estimated_savings_usd` multiplies local-token counts by `3.0 / 1_000_000` (Sonnet input price). If the user has custom model prices set, or default routing would have gone to Haiku ($0.80/MTok) or Opus ($15.00/MTok), the savings estimate is wrong.

**Fix:** use the same `_estimate_cost` logic with the appropriate fallback model, or expose a configurable "comparison model" setting.

**Fixed in:** `backend/services/chat_orchestrator.py:1971-2000`. `get_token_stats()` now reads the comparison model name from the configurable `savings_comparison_model` setting (defaulting to `"claude-sonnet"`) at line 1984-1986, then routes each row through the module-level `_estimate_cost()` helper at lines 1988-1994. `_estimate_cost()` itself (lines 111-136) delegates to `core.model_catalog.get_catalog().prices_for_model()`, which threads the user's `model_prices` overrides through before falling back to catalog defaults — so per-model output prices and user overrides both reach the savings calc.

---

## BUG 13 — `ExecutionClassifier` constructed via `getattr(self.api, "_claude", None)`

**File:** `backend/server.py:160-162`
**Severity:** Low (encapsulation)
**Status:** ✅ Already fixed.

`getattr(self.api, "_claude", None)` reaches into the API facade's private attribute. If `_claude` init failed (it is wrapped in `_safe_init`), this silently passes `None` to the classifier, which may not handle it gracefully in all paths.

**Fix:** expose a public accessor on the API facade, and validate in `ExecutionClassifier.__init__` that the client is non-None or document the None-safe behavior.

**Fixed in:** `backend/core/api/__init__.py:281-296`. The `API` facade now exposes typed public properties `claude_client` (line 288-291) and `local_client` (line 293-296), each returning `None` when `_safe_init()` left the corresponding client unconfigured. The accessor comment at lines 283-286 documents the rationale and explicitly names the prior `getattr(api, "_claude", None)` pattern as the smell being replaced. The `ExecutionClassifier` construction site in `server.py` is no longer present — collaborators that need the client read `api.claude_client` instead.

---

## MINOR — Operator-precedence ambiguity in `hub_router.py:437-438`

```python
tokens_in = getattr(usage, "input_tokens", 0) or 0 if usage else 0
```

Parsed as `(getattr(...) or 0) if usage else 0` due to Python's ternary precedence — happens to work, but reads like it might mean `getattr(...) or (0 if usage else 0)`. (Inside dead code per Bug 2, so it goes away if that is deleted; otherwise add parens.)

**Status:** ✅ Already fixed (carried by Bug 2's removal). The lines no longer exist in `hub_router.py` — `_invoke_claude` was deleted alongside `_invoke_local`. The equivalent expression now lives in `backend/services/claude_client.py:246-247` (`stream_unified`) with the parentheses tightened: `(getattr(usage, "input_tokens", 0) or 0) if usage else 0`, so the ambiguity is gone.

---

## ARCHITECTURE NOTE — Single SQLite connection shared across all threads

**File:** `backend/db.py`
**Severity:** Informational
**Status:** Acknowledged; no change. This is the correct shape for a single-user desktop sidecar (WAL + serialized writes, lock contention bounded by chat concurrency = 1 user). The hardcoded sentinel for "this is fine" is `BearerAuthMiddleware` binding to `127.0.0.1` and the one-process-per-user lifecycle in `desktop-shell/main.ts`. Revisit if the sidecar ever serves multiple sessions concurrently.

The module uses one `sqlite3.Connection` with `check_same_thread=False` and a module-level `threading.Lock`. Correct for WAL mode with serialized writes, but every read also acquires `_lock`, so under concurrent load (multiple chat sends, Docker health checks, background indexer) lock contention is the ceiling. For a single-user desktop app this is fine — worth noting if the app ever serves multiple sessions.

---

## CORRECTIONS / FINDINGS THAT DID NOT HOLD UP

The following were considered during review but rejected after closer inspection:

### NOT A BUG — `startChatStream` race against `appendChatToken`

The concern was that `chat_token` SSE events could arrive before `startChatStream` set `activeChat`. **Verified false:** `desktop-ui/components/ChatView.tsx:208` calls `startChatStream(activeId)` synchronously **before** `await Chat.send(...)` on line 210. The store is populated before the POST is even issued, so no race window exists.

### NOT A BUG — Double `container.shutdown()` on SIGTERM

The concern was that the SIGTERM handler at `backend/server.py:371` and the `finally` block at line 386 would both run. **Verified false:** the SIGTERM handler ends with `os._exit(0)` (line 373), which bypasses Python cleanup, including the `finally` block. Only the `KeyboardInterrupt` / normal-exit path reaches the `finally` block, and that path does not run the signal handler. No double-shutdown is possible.

---

## Summary

| # | Severity | Original site | Status | Current fix location |
|---|----------|---------------|--------|----------------------|
| 1 | Medium | `chat_orchestrator.py:552` | ✅ Fixed | `services/memory_recall.py:123-148`; trim path at `chat_orchestrator.py:1202-1208` |
| 2 | Low | `hub_router.py:431-498` | ✅ Fixed | Methods removed; only `HubRouter.invoke()` at `hub_router.py:371` |
| 3 | Low | `local_client.py` / `claude_client.py` | ✅ Fixed | Both return `tuple[str, object]` (`local_client.py:412-461`, `claude_client.py:193-230`) |
| 4 | Medium | `chat_orchestrator.py:929,1003` | ✅ Fixed | `services/escalation_ladder.py:128` recomputes after escalation |
| 5 | Medium | `chat_orchestrator.py:1071` | ✅ Fixed | `services/turn_lifecycle.py:166-181` re-reads `SUM` inside the same transaction |
| 6 | Medium | `chat_orchestrator.py:1028,1061` | ✅ Fixed | `services/turn_lifecycle.py:138-181` runs all three writes in one `_db.transaction()` |
| 7 | Low | `chat_orchestrator.py:173` | ✅ Fixed | `services/security_gate.py:50,64-69,119,183-196` OrderedDict-LRU + `forget()` on delete |
| 8 | Low | `server.py:225` | ✅ Fixed | `server.py:184-200` — `app://-` removed, comment explains why |
| 9 | Low | `server.py:238` | ✅ Fixed | `server.py:163-180` — `@asynccontextmanager` lifespan + `FastAPI(lifespan=...)` |
| 10 | Low | `db.py:190,662` | ✅ Fixed | `db.py:701-703` — schema ALTER removed; migration owns it |
| 11 | Low | `db.py:230,667` | ✅ Fixed | `db.py:706-717` — schema CREATE removed; migration owns it |
| 12 | Low | `chat_orchestrator.py:1214` | ✅ Fixed | `chat_orchestrator.py:1984-1995` — `_estimate_cost(comparison_model, ...)` with `savings_comparison_model` setting |
| 13 | Low | `server.py:161` | ✅ Fixed | `core/api/__init__.py:288-296` — `claude_client` / `local_client` public properties |

The Medium-severity items (1, 4, 5, 6) are the priority targets — they affect correctness in normal use. The rest are cleanup, forward-compat, or analytics accuracy.

---

## Update — second pass (verification against current source)

A second pass against the current tree confirms every bug listed above has a corresponding fix in code and (for the Medium items) a passing regression test. The line numbers in the original report no longer match the current source because the orchestrator was extracted into six cooperating modules (`turn_context`, `turn_lifecycle`, `memory_recall`, `turn_router`, `security_gate`, `worker_dispatch`, `escalation_ladder`) as part of the Layer 3 decomposition; each module's docstring or inline comment names the original bug it closes.

Regression tests (`backend/tests/test_chat_orchestrator.py::TestReviewBugFixes`) exist for the four Medium-severity bugs (1, 4, 5, 6) and pass: 553 tests pass / 9 skip on `pytest backend/tests`.
