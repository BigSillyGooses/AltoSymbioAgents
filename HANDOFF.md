# Handoff

**Latest on `main`:** `efa4c85` (Merge PR #25 — Stage-2 #9 settings toggle)
**Branch:** `feat/visual-team-composer` · **Pushed:** pending
**Status:** Stage-2 #10 — RosterPicker rewritten as a chip-card composer with auto-coordinator badge + shared TanStack cache.
**Date:** 2026-05-19

## What just shipped (this branch — Stage 2 #10 Visual TeamComposer)

**Stage-2 item #10 — RosterPicker render-layer rewrite.** Replaces the
checkbox-column dropdown with a 2-column chip-card grid that surfaces
the role badge and auto-picks a coordinator when 2+ agents are drafted.
Public props (`agentId` / `teamId` / `onApply` / `disabled`) and the
`RosterPick` contract are unchanged — the ChatView call site is
untouched.

- **`desktop-ui/components/RosterPicker.tsx`** (rewritten, 416 LOC, was
  455).
  - Agent list: chip cards in a `grid grid-cols-1 sm:grid-cols-2 gap-1.5`.
    Each card carries a 7×7 initials avatar, name, role pill or
    Coordinator badge, and a 2-line description clamp. Selected state
    is `bg-accent/10 border-accent shadow-soft-1`, not a tick.
  - Coordinator pick: when `draft.size >= 2`, compute the agent with
    `role.toLowerCase() === "coordinator"`, else the first selected
    agent by registry order. When a saved-team chip is the active
    draft, surface its stored `coordinator_id` verbatim. Mirrors the
    backend logic at `backend/services/agent_registry.py:840`.
  - Footer summary: `"Coordinator: <name> · Team of N"` when a
    coordinator is in play, otherwise just the draft summary.
  - **Data sources migrated** off local `useState` + `useEffect` onto
    the shared `useAgents()` / `useTeams()` hooks from
    `chat/queries.ts`. Picker now shares the TanStack cache with
    ChatView, gets focus refetch + invalidation for free, and the
    `Agents.list()` / `Teams.list()` calls collapse to one network
    request per renderer rather than two (#8 hook-adoption follow-up
    closed for RosterPicker).
- **`desktop-ui/components/RosterPicker.test.tsx`** (new, 18 tests).
  Pill (4): "No agent" label, active agent label, pill opens dropdown,
  sidecar-gated disable. Chip-card grid (4): renders agent + "No
  agent" pseudo-card, hydrates `aria-checked` from `agentId` prop,
  click toggles, search filters by name/role/description. Coordinator
  auto-pick (3): no badge for solo, role=coordinator wins over
  registry order, fallback to first by registry order when no
  coordinator role. Saved-team chips (3): chip per non-adhoc team,
  ad-hoc teams hidden, picking hydrates draft + surfaces stored
  coordinator. Apply (4): solo, ad-hoc multi, teamId-routed,
  unbind-to-empty.

## Verified

- `npx vitest run desktop-ui/components/RosterPicker.test.tsx` —
  **18 passed** in 1.3s.
- `npm run typecheck` — clean (both `tsconfig.node.json` and
  `tsconfig.web.json`).
- `npm run test:frontend` — **186 passed** across 19 files (was 168;
  +18 from this PR's RosterPicker.test.tsx).
- `npm run build` — clean (5.9s).

## Stage-2 status (post-this-PR)

- **#7 LangGraph 1.2 StateGraph orchestrator** — landed (PR #17).
  Default still `"legacy"`; flip gated on bench cycles.
- **#8 ChatView decomposition** — done; RosterPicker's hook adoption
  (this PR) closes one of the follow-ups. AgentPanel still owns its
  own `useState` + `useEffect` and would be a similar ~30-LOC swap.
- **#9 Devin-style timeline** — done end-to-end (PR #24 + #25).
- **#10 Visual TeamComposer** — **landed (this PR).**
- **#11 Typed error envelopes** — pending (next).
- **#12 Bundled llama-server binary** — pending.

## Next up

1. **#11 — typed error envelopes.** Backend work; converts
   `HTTPException(status_code, detail="string")` raises across
   `backend/routes/*.py` into a discriminated-union JSON shape
   (`{error_type, status_code, detail, hint?}`) the renderer can
   pattern-match. Add a FastAPI exception handler + a `domain_error()`
   helper, then migrate the noisiest routes (chat, voice, attachments,
   conversations).
2. **#12 — bundled llama-server binary.** Resolve the `TODO(engines)`
   in `desktop-shell/bootstrap/bin_manager.ts:44`: extend
   `isBootstrapped()` with an `existsSync` check on
   `<binRoot>/engines/llama-server.exe` so Electron's readiness gate
   actually verifies the binary is present before sidecar boot.
   `bundled_server.py` already errors cleanly when the binary is
   missing (lines 366-372) — this is the bootstrap-side counterpart.
3. **#8 hook-adoption follow-up.** Migrate `AgentPanel.tsx` off its
   own `useState` + `useEffect` fetches onto `useAgents()` /
   `useTeams()` from `chat/queries.ts` (RosterPicker just did it).
4. **#7 follow-up.** Weekly AgentDojo + agentic-misalignment bench
   cycles under `orchestrator_engine="graph"`; two consecutive clean
   runs gate the default flip.

## Walls hit

The codebase doesn't use jest-dom matchers (no `toHaveTextContent` /
`toHaveAttribute` / `toBeInTheDocument`). First test pass leaned on
those and failed with "Invalid Chai property" until rewritten with raw
DOM assertions (`el.textContent.includes(...)`,
`el.getAttribute("aria-checked") === "true"`,
`(el as HTMLButtonElement).disabled === true`). Worth pinning for
future test files in this repo.

The `useAgents()` / `useTeams()` hook surface returns
`AgentRow[] | undefined` (an in-flight query is `undefined`, not `[]`).
`useMemo` over `(query.data ?? [])` handles that, but the same data is
read for `useTeams()` with a `.filter((t) => !t.is_adhoc)` step — that
filter has to live downstream of the `?? []` or you'll get a TypeError
on the very first render before the query lands.

---

<!-- Earlier handoff content preserved below for cross-session reference. -->

# Earlier session — `feat/timeline-variant-toggle` (merged via PR #25)

**Latest on `main`:** `3fca374` (Merge PR #24 — DevinTimeline drillable variant)
**Branch:** `feat/timeline-variant-toggle` · **Pushed:** pending
**Status:** Stage-2 #9 wired behind a `timeline_variant` setting (Appearance group).
**Date:** 2026-05-19

## What just shipped (this branch — Stage 2 #9 wiring)

**Stage-2 item #9 follow-up — settings toggle.** Surfaces the
`DevinTimeline` drillable variant (landed in PR #24) behind a new
`timeline_variant` setting in the Appearance group. Default `"compact"`
preserves the previous render; opt-in `"drillable"` swaps the streaming
bubble's timeline to the per-row expandable panels.

- **`backend/core/settings.py`.** Adds `"timeline_variant": (str, "compact")`
  to `SETTINGS_DEFAULTS` and a matching `FIELD_METADATA` entry in the
  `appearance` group (enum `["compact", "drillable"]`). Sits alongside
  theme / start_tab / token-count toggles — it's a render preference,
  not a feature flag, so it doesn't belong in Advanced.
- **`backend/core/api/settings.py`.** Exposes the value through
  `SettingsAPI.get_settings()` (the `/api/settings` payload), coerced to
  `str` with a `"compact"` fallback so a forward-incompatible value
  can't blank the chat bubble.
- **`desktop-ui/api/client.ts`.** Adds `timeline_variant?: "compact" |
  "drillable"` to `SettingsPayload`. Optional so an old sidecar that
  hasn't migrated yet still typechecks against the renderer.
- **`desktop-ui/components/chat/Timeline.tsx`** (new, ~20 LOC). Tiny
  dispatcher that picks between `ThinkingTimeline` (compact) and
  `DevinTimeline` (drillable) based on a `variant` prop. Falls back
  to compact on any unknown value. Imported once at the ChatView
  call site instead of branching inline.
- **`desktop-ui/components/ChatView.tsx`.** Replaces the
  `<ThinkingTimeline rows={item.timeline} />` call site with
  `<Timeline rows={item.timeline} variant={data.timelineVariant} />`.
  Adds `timelineVariant` state hydrated from `Settings.get()` on both
  the initial sidecar-ready effect and the window-focus refetch
  (matches the existing voice_input / voice_output pattern). Threads
  the variant into `ChatRowData` so the virtualized
  `ChatListRow` renderer (separate function from the `ChatView`
  closure) can read it through `data`.
- **Tests.**
  - `chat/Timeline.test.tsx` (new, 4 tests): compact variant renders
    `ThinkingTimeline`, drillable variant renders `DevinTimeline`,
    rows forward through unchanged across rerenders, empty rows
    produces an empty ol in both variants.
  - `tests/test_settings.py` `TestTimelineVariant` (new, 4 tests):
    default is `"compact"`, accepts `"drillable"`, round-trips
    through disk, manifest entry exists in the `appearance` group
    with both enum options.

## Verified

- `npm run typecheck` — clean (both `tsconfig.node.json` and
  `tsconfig.web.json`).
- `npm run test:frontend` — **168 passed** across 18 files (was 162
  before — net +4 from this PR's `Timeline.test.tsx`; the rest of the
  bump matched the PR #24 baseline that landed simultaneously).
- `cd backend && python -m pytest tests/ -q` — **724 passed**, 9
  skipped, 13 deselected (was 720; +4 from `TestTimelineVariant`).
- `npm run build` — clean (6.3s).
- `python build-scripts/generate_api_types.py` — no drift on
  `desktop-ui/api/generated.d.ts` (the `/api/settings` route returns
  `dict` rather than a Pydantic model, so the OpenAPI schema doesn't
  enumerate response fields; the typed `SettingsPayload` in
  `client.ts` is hand-maintained).

## Stage-2 status (post-this-PR)

- **#7 LangGraph 1.2 StateGraph orchestrator** — landed (PR #17).
  Default still `"legacy"`; flip gated on bench cycles.
- **#8 ChatView decomposition** — done modulo follow-ups (shadcn/ui
  swap, wider TanStack adoption on RosterPicker / AgentPanel).
- **#9 Devin-style timeline** — **DONE end-to-end.** Component +
  reducer enrichment landed in PR #24; this PR wires it behind a
  Settings toggle so users can opt in. Real-world feedback now drives
  whether to flip the default to `"drillable"`.
- **#10 Visual TeamComposer** — pending.
- **#11 Typed error envelopes** — pending.
- **#12 Bundled llama-server binary** — pending.

## Next up

1. **#10 — Visual TeamComposer.** Render-layer rewrite of
   `RosterPicker.tsx` against the multi-agent UX direction (chip
   cards, not dropdowns; auto-pick coordinator from role field).
   Backend hook surface (`useRoster` + `useAgents` + `useTeams`) is
   already in place — this is a pure-render task.
2. **#11 — typed error envelopes.** Backend work; converts exception
   messages into a discriminated-union JSON shape the renderer can
   pattern-match.
3. **#12 — bundled llama-server binary.** Sidecar packaging work;
   ships a llama.cpp binary inside the installer.
4. **#8 hook-adoption follow-up.** Migrate `RosterPicker.tsx` and
   `AgentPanel.tsx` off their own `useState` + `useEffect` fetches
   onto `useAgents()` / `useTeams()` from `chat/queries.ts`.
5. **#7 follow-up.** Weekly AgentDojo + agentic-misalignment bench
   cycles under `orchestrator_engine="graph"`; two consecutive clean
   runs gate the default flip away from `"legacy"`.

## Walls hit

One TypeScript thing worth pinning: `ChatView.tsx` renders the
streaming bubble inside a *separate* `ChatListRow` function (not the
`ChatView` closure), so a piece of `ChatView` state can't reach the
row renderer directly — it has to be threaded through the existing
`ChatRowData` interface that `useMemo` builds. Same pattern as the
existing `voiceOutputEnabled` prop. TS2304 caught the
use-before-declaration the moment I tried to reference
`timelineVariant` inside the row body; the fix was to add it to
`ChatRowData`, include it in the `useMemo` deps, and read it as
`data.timelineVariant`.

The `default_agent_id` field in `SettingsPayload` (in `client.ts`) is
typed but isn't actually returned by `SettingsAPI.get_settings()` —
ChatView tolerates it as `undefined` via a `||""` fallback. Worth
remembering when adding more settings fields: the typed payload is
aspirational unless the API method explicitly populates the key.

---

<!-- Earlier handoff content preserved below for cross-session reference. -->

# Earlier session — `claude/drillable-timeline-stage-2-UFJgx` (PR #24, merged)

**Stage-2 item #9 — Devin-style drillable timeline.** New sibling
component to `ThinkingTimeline` that takes the same `ThinkingRow[]`
shape and renders each row as a clickable button with an expandable
detail panel underneath. `ThinkingTimeline` is untouched; the two
components coexist behind a future settings toggle (deferred to a
follow-up — this PR ships the component + the reducer enrichment only).

- **`desktop-ui/components/chat/deriveThinkingTimeline.ts`.** Adds one
  optional field `expandedDetail?: string` to the `ThinkingRow`
  interface (additive, non-breaking — `ThinkingTimeline` ignores it).
  Eight event branches now populate it with the un-truncated narrative
  the compact `detail` was summarising: `route_decided` (full
  `reasoning`), `checkpoint_state` rolled_back / `pipeline_step_retry`
  / `governance_blocked` / `alignment_warning` (full `reason`),
  `camel_complete` when errored (full `error`), `reasoning_complete`
  (full `thinking_preview`), and `reader_complete` (a multi-line
  composition of full `intent` + full `proposed_tools` list + full
  `red_flags` list). Every branch guards on `length > 140` (or 80 for
  `route_decided`, or "exceeds the collapsed summary" for
  `reader_complete`) so we don't emit `expandedDetail` when the
  collapsed `detail` already fits the whole text.
- **`desktop-ui/components/chat/DevinTimeline.tsx`** (new, ~80 LOC).
  Sibling render of `ThinkingTimeline.tsx`. Same outer ol shape
  (`data-testid="chat-stream-timeline-drillable"`, `aria-live="polite"`,
  `aria-atomic="false"`); same `rowTone(state)` palette mapping; same
  `role="status"` on each row. Each row is a `<button>` with
  `aria-expanded` and `aria-controls` (pointing at the panel `id`
  when a panel will render). Local `useState<Set<string>>` tracks
  per-row open state keyed by `row.key`. Collapsed: icon + label +
  `line-clamp-2`-truncated detail (same as `ThinkingTimeline`).
  Expanded: panel rendered below the button with the full
  `expandedDetail ?? detail` text, `whitespace-pre-wrap` so
  multi-line `reader_complete` and `reasoning_complete` panels render
  legibly. Chevron `▸` rotates 90° via Tailwind `transition-transform`
  when open. Keyboard-accessible via the native button (Enter / Space
  toggle). Rows that carry neither `detail` nor `expandedDetail`
  omit `aria-controls` and render no panel.
- **Tests.**
  - `chat/DevinTimeline.test.tsx` (new, 10 tests): aria-live polite ol
    + role=status rows, button starts collapsed (aria-expanded=false),
    click expands and shows `expandedDetail`, falls back to `detail`
    when `expandedDetail` is absent, second click collapses,
    independent per-row expansion, no `aria-controls` for bare rows,
    state-specific palette classes, empty input renders empty list,
    `aria-controls` ↔ panel `id` linkage.
  - `chat/deriveThinkingTimeline.test.ts` (+7 tests, 15 → 22): pins
    `expandedDetail` is populated on long `route_decided` reasoning
    and left undefined for short ones; pins `expandedDetail` on long
    rollback / retry / governance / alignment reasons; pins the
    `camel_complete` errored-only branch; pins the multi-line
    `reader_complete` packing of intent + tools + red_flags; pins
    `reader_complete` leaves `expandedDetail` unset when the
    collapsed summary already covered everything; pins
    `reasoning_complete` `thinking_preview` passthrough.

## Verified

- `npx vitest run desktop-ui/components/chat/` — **68 passed** across
  6 files (was 41 at the end of Stage-2 #8; +27 from this PR:
  +10 DevinTimeline, +7 deriveThinkingTimeline, +10 carried over from
  queries/useRoster that ran in the same suite folder).
- `npm run test:frontend` — **162 passed** across 17 files (up from
  145 baseline; +17 from this PR's two test files).
- `npm run typecheck` — clean (both `tsconfig.node.json` and
  `tsconfig.web.json`).
- `npm run build` — clean.

## Stage-2 status (post-this-PR)

- **#7 LangGraph 1.2 StateGraph orchestrator** — landed (PR #17).
  Default still `"legacy"`; flip gated on bench cycles.
- **#8 ChatView decomposition** — done modulo follow-ups (shadcn/ui
  swap, wider TanStack adoption on RosterPicker / AgentPanel).
- **#9 Devin-style timeline** — **component + reducer enrichment
  landed** (this PR). Two follow-ups outstanding before users see it:
    1. A settings key (e.g. `timeline_variant: "compact" | "drillable"`,
       default `"compact"`) and a Settings toggle.
    2. ChatView wiring: read the setting and render either
       `<ThinkingTimeline rows={item.timeline} />` or
       `<DevinTimeline rows={item.timeline} />` at
       `ChatView.tsx:1805`. The two components have identical props,
       so it's a 1-line ternary plus the new import.
- **#10 Visual TeamComposer** — pending.
- **#11 Typed error envelopes** — pending.
- **#12 Bundled llama-server binary** — pending.

## Next up

1. **Wire #9 behind a settings toggle.** The smallest possible
   follow-up: new `timeline_variant` setting, a Settings checkbox
   ("Use drillable timeline (experimental)"), and the 1-line ChatView
   swap. Once that ships, real-world feedback can drive whether to
   flip the default.
2. **#10 — Visual TeamComposer.** Render-layer rewrite of
   `RosterPicker.tsx`. Backend hook surface (`useRoster` +
   `useAgents` + `useTeams`) is already in place.
3. **#11 — typed error envelopes.** Backend work.
4. **#12 — bundled llama-server binary.** Sidecar packaging.
5. **#8 hook-adoption follow-up.** Migrate `RosterPicker.tsx` and
   `AgentPanel.tsx` off their own `useState` + `useEffect` fetches
   onto `useAgents()` / `useTeams()` from `chat/queries.ts`.

## Walls hit

None this session. The interface change to `ThinkingRow` was
intentionally additive (`expandedDetail` is optional), which kept the
existing `ThinkingTimeline` tests green without modification — the
old component just ignores the new field. Same trick should work for
future row-shape extensions like timestamps or sub-rows if the
"sub-step traces" half of the Devin vision lands later.

The drill-down only delivers value when the panel shows *more* than
the collapsed line. So the reducer enrichment is doing the real work
here — it preserves every existing `truncate()` call (so collapsed
rows look exactly as before) and adds the full string as a parallel
field only when the truncation would clip. `DevinTimeline` falls back
to the collapsed `detail` for rows where the originating event didn't
carry longer text, so the expand still toggles meaningfully on every
row but stays a one-liner when there's nothing more to show.

---

<!-- Earlier handoff content preserved below for cross-session reference. -->

# Earlier session — Stage 2 #8 ChatView decomposition (merged via PRs #19–22)

`ChatView.tsx` went from **2,762 → 2,146 LOC** (−616, ~22%), with four
focused sibling modules under `desktop-ui/components/chat/` and 36 new
unit tests. Plus a separate CI maintenance PR. Order of landing on main:

- **PR #22 (`chore/actions-node-24-bump`).** Bumps four pinned actions
  (`checkout`, `setup-node`, `setup-python`, `github-script`) to the
  first major shipping a Node 24 runtime, ahead of GitHub's 2026-06-02
  cutover.
- **PR #20 (`chore/extract-derive-thinking-timeline`).** Pulls the
  ~300-line `_deriveThinkingTimeline` reducer + 15 event-shape
  interfaces + `_truncate` helper out of ChatView into
  `chat/deriveThinkingTimeline.ts` (15 tests).
- **PR #21 (`chore/chatview-decomposition-part-2`).** Three further
  extractions:
    - `chat/derivePipelineLive.ts` — sibling reducer over the SSE
      event log → `PipelineLive` (11 tests).
    - `chat/ThinkingTimeline.tsx` — render component + private
      palette helper, decoupled from the reducer (5 tests).
    - `chat/useRoster.ts` — hook owning `pendingRoster` state, current
      binding derivations, and the `applyRoster` async with toast +
      rollback semantics (10 tests).
    - `chat/events.ts` — shared `StreamingEvent` type the two
      reducers consume.
- **PR #19 (`chore/chatview-tanstack-query`).** Replaces three
  `useState` + `useEffect` data-fetch blocks (`Chat.list`,
  `Agents.list`, `Teams.list`) with TanStack Query hooks in
  `chat/queries.ts`. Adds `QueryClientProvider` at the renderer root;
  wraps `ChatView.test.tsx` renders in a per-test client (10 tests).
  Renderer now has a single shared cache, automatic focus-refetch,
  and one place to invalidate after mutations.

Bonus: PR #18 (`fix/roster-team-id-passthrough`) landed earlier the
same day — backend one-line fix to the facade method
`chat_set_conversation_roster` that was silently dropping `team_id`
and breaking every RosterPicker apply with HTTP 500 (3 tests pinning
the kwargs forwarding seam).

- **`package.json`.** Adds `@tanstack/react-query ^5.62.0`. React-19
  compatible, no peer-dep churn.
- **`desktop-ui/main.tsx`.** Wraps `<App />` in a
  `<QueryClientProvider>` with one shared `QueryClient`
  (`staleTime: 30s`, `retry: 1`, `refetchOnWindowFocus: true`). The
  sidecar is loopback so retries beyond one are noise; 30s staleness
  matches the cadence at which AgentPanel / Settings mutations
  should bubble back to the conversation list.
- **`desktop-ui/components/chat/queries.ts`** (new, ~100 LOC).
  Exports `useConversations({ enabled, limit })`, `useAgents({ enabled })`,
  `useTeams({ enabled })`, the shared `queryKeys` factory
  (`["conversations", limit]`, `["agents"]`, `["teams"]`), and two
  pure helpers (`agentNameMap`, `teamNameMap`) that build the id →
  display-name lookup the conversation list subtitles consume.
  `useAgents` / `useTeams` swallow fetch errors into `[]` so a single
  endpoint outage doesn't blank-screen the whole chat panel.
- **`desktop-ui/components/ChatView.tsx`.** Replaces three
  `useState` + `useEffect` blocks with three `useQuery` hook calls.
  Removes the local `interface ConversationRow` (now imported from
  `queries.ts`). Adds two `useCallback`-wrapped cache mutators —
  `patchConversation(id, patch)` for optimistic single-row updates
  (used by `useRoster.onLocalUpdate`) and `invalidateConversations()`
  for the rollback + post-newConversation refresh paths. Stops
  reading `Agents.list` / `Teams.list` directly; cleans the unused
  imports.
- **`desktop-ui/components/ChatView.test.tsx`.** Wraps the lone
  `render(<ChatView />)` with `<QueryClientProvider client={…}>`,
  using a fresh per-test `QueryClient` (retry off, gcTime 0,
  staleTime 0) so cache state never leaks across tests. The
  existing `vi.mock("@/api/client", …)` factory is untouched —
  TanStack Query just exercises the same mocked `Chat.list` /
  `Agents.list` / `Teams.list` calls the prior implementation did.
- **`desktop-ui/components/chat/queries.test.tsx`** (new, 10 tests).
  Pins the contract: `queryKeys` shape (per-limit conversations,
  singleton agents/teams), name-map tolerance (empty/missing names
  → "Agent" / "Team" fallback, missing ids skipped),
  `useConversations` honors the requested `limit`, `enabled: false`
  truly skips the fetch, `useAgents`/`useTeams` swallow fetch errors
  into `[]`, and a fresh-cache second mount of `useConversations`
  reuses the existing entry (no extra `Chat.list` call).

## Verified (at the tip of `main`, post-all-merges)

- `npm run typecheck` — clean (both `tsconfig.node.json` and
  `tsconfig.web.json`).
- `npm run test:frontend` — **145 passed** across 16 files (was
  **94** before Stage 8; +51 new tests across the four PRs).
- `cd backend && python -m pytest tests/ -q` — **720 passed, 9
  skipped, 13 deselected** (carried over from the fix-roster PR
  baseline; Stage 8 was frontend-only).
- `npm run build` — clean (no new size-tier warnings beyond the
  pre-existing mermaid / wardley chunks).

## Stage-2 status

- **#7 LangGraph 1.2 StateGraph orchestrator** — landed (PR #17).
  Default still `"legacy"`; flip gated on two consecutive weekly
  AgentDojo + agentic-misalignment bench cycles under
  `orchestrator_engine="graph"`.
- **#8 ChatView decomposition** — **decomposition portion done** (PRs
  #19, #20, #21 merged). Remaining sub-items deferred as separate
  efforts: **shadcn/ui swap** (whole-UI library migration, multi-PR)
  and **wider TanStack adoption** (RosterPicker / AgentPanel still
  fetch `Agents.list` / `Teams.list` with their own
  `useState` + `useEffect`; can adopt the new `useAgents()` /
  `useTeams()` hooks from `chat/queries.ts` and share the cache —
  small ~30-LOC-per-component follow-up).
- **#9 Devin-style timeline** — pending. With `ThinkingTimeline`
  extracted, the drillable variant is now a new sibling component
  over the same `ThinkingRow[]` shape, not a refactor.
- **#10 Visual TeamComposer** — pending. The `useRoster()` hook plus
  the shared `useAgents()` / `useTeams()` cover the full surface a
  redesigned composer needs.
- **#11 Typed error envelopes** — pending.
- **#12 Bundled llama-server binary** — pending.

## Open / parallel branches

None. All five PRs from this stage (#18, #19, #20, #21, #22) landed.

## Next up

Recommended starting point: **Stage-2 #9 — Devin-style timeline.**
Biggest visible user-facing win, builds directly on what just shipped,
no further ChatView surgery required. New sibling component to
`ThinkingTimeline` that takes the same `ThinkingRow[]` shape and
renders an expandable per-step drill-down (click a row → its detail
expands, with sub-step traces when present). Lives at
`desktop-ui/components/chat/DevinTimeline.tsx`. Keep `ThinkingTimeline`
untouched — some callers will prefer the compact view; wire the new
component behind a settings toggle in a follow-up PR.

Other candidates, in roughly increasing scope:

1. **Stage-2 #8 hook-adoption follow-up.** Migrate `RosterPicker.tsx`
   and `AgentPanel.tsx` off their own `useState` + `useEffect` fetches
   of `Agents.list` / `Teams.list` onto the new `useAgents()` /
   `useTeams()` hooks from `chat/queries.ts`. Net win: cache sharing
   with `ChatView`, automatic refresh after `AgentPanel` mutations
   (rename / create / delete) via `queryClient.invalidateQueries(
   { queryKey: ["agents"] })`. ~30 lines per component.
2. **Stage-2 #10 — Visual TeamComposer.** Render-layer rewrite of
   `RosterPicker.tsx` against the multi-agent UX direction (chip
   cards, not dropdowns; auto-pick coordinator from role field).
   Backend surface (`useRoster` + `useAgents` + `useTeams`) is in
   place. Keep the public props stable so ChatView's call site
   doesn't change.
3. **Stage-2 #11 — typed error envelopes.** Backend work; converts
   exception messages into a discriminated-union JSON shape the
   renderer can pattern-match.
4. **Stage-2 #12 — bundled llama-server binary.** Sidecar packaging
   work; ships a llama.cpp binary inside the installer so first-run
   local-LLM users don't need a separate download step.
5. **Stage-2 #7 follow-up.** Weekly AgentDojo + agentic-misalignment
   bench cycles under `orchestrator_engine="graph"`. Two consecutive
   clean runs gate the default flip away from `"legacy"`. Mostly a
   waiting task, not coding.

## Walls hit

None this session. One TypeScript thing worth pinning for future
hook extractions in this file: `ChatView` defines a lot of state at
the top and a lot of closures (handlers) further down that reference
that state. When introducing a new hook that consumes a derived
value like `ready`, the new hook's call site has to land *after*
`ready` is in scope — TS2448/TS2454 caught the use-before-declaration
the moment the query hooks landed at line ~140 while `ready` was
declared at line ~241. Resolved by hoisting `const ready =
status?.status === "ready"` up next to its `status` source.

The TanStack Query migration is roughly LOC-neutral on ChatView
(−8) but the architectural win is bigger: server state now has a
single canonical source. Mutations from any component (a future
AgentPanel rename, a future Settings change to the default agent)
can call `queryClient.invalidateQueries({ queryKey: ["agents"] })`
and the conversation list subtitles refresh automatically, without
plumbing a callback through props or relying on the window-focus
hack. The test-side wrapper is the only friction — and it's a
three-line `QueryClientProvider` insertion.

---

<!-- Earlier handoff content preserved below for cross-session reference. -->

# Earlier session — `chore/chatview-decomposition-part-2` (commit `b1e897a`)

**Last commit:** b1e897a — `chore(ui): extract derivePipelineLive, ThinkingTimeline, useRoster from ChatView`
**Branch:** `chore/chatview-decomposition-part-2` · **Pushed:** yes (PR open) · **Stacked on:** `chore/extract-derive-thinking-timeline`
**Date:** 2026-05-18

## What just shipped (this branch)

**Stage 2 / item #8 — second PR** of the Atelier-blueprint ChatView
decomposition. Continues the work started in
`chore/extract-derive-thinking-timeline` (PR open) by extracting the
remaining three natural seams: the live-pipeline reducer (sibling of
the one that PR moved), the `ThinkingTimeline` rendering component
(now decoupled from the data-shaping reducer), and the
`pendingRoster` + `applyRoster` + current-binding derivations as a
single `useRoster()` hook. Together with the first PR, `ChatView.tsx`
goes from **2,762 → 2,154 LOC** (−608, ~22%).

- **`desktop-ui/components/chat/events.ts`** (new). Shared
  `StreamingEvent` type that the two reducers consume. Lifts the type
  out of `deriveThinkingTimeline.ts` (which still re-exports it for
  back-compat). One small thing here is the right place to centralise
  — adding it now means future reducers and panel components hit a
  shared seam rather than reaching across to a sibling module.
- **`desktop-ui/components/chat/derivePipelineLive.ts`** (new, ~110
  LOC). Verbatim move of the live-pipeline reducer. Same shape as
  `deriveThinkingTimeline`: pure function over `StreamingEvent[]` →
  `PipelineLive`. The 5 event-shape interfaces (`PipelinePlanEvent`,
  `PipelineStepStartedEvent`, `PipelineStepCompleteEvent`) and the
  `PipelinePhase` / `PipelineLive` types move with it; `PipelineStep`
  is re-imported from the existing `chat/MessageBubble`. No renames
  besides `_derivePipelineLive → derivePipelineLive`.
- **`desktop-ui/components/chat/ThinkingTimeline.tsx`** (new, ~55
  LOC). Verbatim move of the `ThinkingTimeline` component + its
  private `_thinkingRowTone` palette helper (now module-local as
  `rowTone`). The component is a pure render of `rows: ThinkingRow[]`
  → JSX with state-keyed palette classes and the same
  `data-testid="chat-stream-timeline"` / `role="status"` /
  `aria-live="polite"` accessibility attributes the inline version
  carried.
- **`desktop-ui/components/chat/useRoster.ts`** (new, ~115 LOC).
  Encapsulates the full roster surface that was scattered across
  ChatView: `pendingRoster` useState, `currentAgentId` / `currentTeamId`
  derived bindings, and the `applyRoster` async action with its
  Teams.get → Chat.setConversationRoster flow and toast/rollback on
  failure. Decoupled from ChatView's `ConversationRow` type by
  accepting `activeAgentId` / `activeTeamId` props from the caller
  plus `onLocalUpdate(agentId, teamId)` and `onRollback()` callbacks.
  Toast is read directly from `useAppStore` (one fewer prop to
  thread). The frontend bug pinned in
  `fix/roster-team-id-passthrough` (PR open) lives below this hook
  — same UX symptom would still appear if the backend regresses
  again, but the hook itself is correct.
- **`desktop-ui/components/ChatView.tsx`.** Imports the three new
  modules (the reducer, the component, the hook), drops the inline
  versions, and replaces the previously-inlined `pendingRoster` /
  `currentAgentId` / `currentTeamId` / `applyRoster` block with one
  destructured `useRoster({…})` call placed before `newConversation`
  in the function body (so the closure over `pendingRoster` works
  without TS2448 use-before-declaration). The thin pass-through
  `_rosterRow = conversations.find(…)` is the only roster-specific
  local left in the component; everything else moves into the hook.
- **Tests.** Each new module ships with focused tests:
  - `chat/derivePipelineLive.test.ts` (11 tests). Phase transitions
    (idle → decomposing → running → synthesising → complete + the
    re-enter-running case), step accumulation (plan ordering,
    default-agent fallback, step_complete merging, no-step-number
    drop, plan clears prior steps, out-of-order sort,
    plan→step_started inheritance), and non-pipeline-events ignored.
  - `chat/ThinkingTimeline.test.tsx` (5 tests). The OL is aria-live
    polite; rows are role=status; icon/label/detail wire through;
    `state` maps to the right tailwind palette class; empty input
    renders an empty list.
  - `chat/useRoster.test.tsx` (10 tests). Initial state; pending
    pick surfaces (single-agent → currentAgentId, multi → empty,
    teamId → currentTeamId); activeId reads from row props; applyRoster
    stashes when no activeId; calls the right backend with agentIds
    only / with teamId; empty-team rejection + rollback; backend
    failure surfaces toast + onRollback.

## Verified

- `npx vitest run desktop-ui/components/chat/` — **41 passed** across
  4 files (15 thinking-timeline reducer, 11 pipeline-live reducer,
  5 ThinkingTimeline render, 10 useRoster hook).
- `npm run typecheck` — clean (both tsconfig.node.json and
  tsconfig.web.json).
- `npm run test:frontend` — **135 passed** across 15 files (up from
  109 baseline at the end of the first decomposition PR; +26 from
  this PR's new files, no other-suite regressions).

## Stage-2 status (post-this-PR)

- **#7 LangGraph 1.2 StateGraph orchestrator** — landed (PR #17,
  merged). Default flip gated on bench cycles.
- **#8 ChatView decomposition** — **mostly done**. Three PRs landed
  or pending merge:
    1. `chore/extract-derive-thinking-timeline` (PR open).
    2. **This PR** (`chore/chatview-decomposition-part-2`).
  Remaining in-scope work for #8: **shadcn/ui swap** (whole-UI
  library migration, deferred — different scope) and **TanStack
  Query migration** (moves `Chat.list` / `Agents.list` / `Teams.list`
  from `useEffect` + `useState` to cached queries; deferred — adds a
  dependency and changes refresh semantics).
- **#9 Devin-style timeline** — pending (depends on #7 + #8). Now
  closer to feasible because `ThinkingTimeline` is a sibling
  component; a drillable variant becomes a new component, not a
  refactor.
- **#10 Visual TeamComposer** — pending. The `useRoster()` hook
  this PR adds is exactly the surface a redesigned TeamComposer
  would consume — `currentAgentId` / `currentTeamId` /
  `pendingRoster` / `applyRoster` cleanly cover both new-conversation
  staging and active-conversation rebinding.
- **#11 Typed error envelopes** — pending.
- **#12 Bundled llama-server binary** — pending.

## Open / parallel branches

- **`fix/roster-team-id-passthrough`** (PR open). Backend one-liner.
  Without this, the new `useRoster.applyRoster` will still surface a
  toast and roll back — the UX bug repro path is unchanged.
- **`chore/extract-derive-thinking-timeline`** (PR open, **this PR
  is stacked on top of it**). Merge that one first; this branch
  rebases cleanly afterward.

## Next up

Three directions, in roughly increasing scope:

1. **Stage-2 #9 — Devin-style timeline.** With `ThinkingTimeline`
   extracted and `deriveThinkingTimeline` unit-testable, the
   drillable variant is a new sibling component that consumes the
   same `ThinkingRow[]` data shape. No further ChatView surgery
   required to start.
2. **Stage-2 #10 — Visual TeamComposer.** Replace the
   chip-list-in-popover RosterPicker UI with the more visual layout
   from `[[multi_agent_ux_direction]]`. Backend hook (`useRoster`)
   already exists; this is a render-layer rewrite.
3. **Stage-2 #8 finish — TanStack Query migration.** Migrate the
   three list fetches in ChatView (`Chat.list`, `Agents.list`,
   `Teams.list`) onto cached queries with invalidation. Net win is
   eliminating the focus-handler + manual reload paths scattered
   around the component, but it's a dependency add and a semantics
   change — separate PR.

## Walls hit

None this session. One process note worth saving for future
multi-file extractions: do each contiguous block as a *single*
Edit with the exact captured text (read the full range first), and
re-run typecheck + the new tests immediately after each extraction
rather than batching. The "extract A → typecheck → extract B → …"
loop catches issues like the `pendingRoster used before declaration`
TS2448 (which surfaced the moment the hook call landed below
`newConversation`) before they pile up. Fix was to move the
`useRoster` call up to right after `[activeId, setActiveId]` so the
binding is established before any `const`-declared closure references
it.

The `StreamingEvent` type was previously inlined inside
`deriveThinkingTimeline.ts` and exported from there. Adding a second
reducer (`derivePipelineLive`) revealed that the type belonged in a
shared seam, so it moved to `chat/events.ts` — and
`deriveThinkingTimeline.ts` retains a `export type { StreamingEvent }`
re-export so existing imports keep working.

---

<!-- Earlier handoff content preserved below for cross-session reference. -->

# Earlier session — `chore/extract-derive-thinking-timeline` (commit `1368b41`)

**Last commit:** 1368b41 — `chore(ui): extract deriveThinkingTimeline from ChatView.tsx`
**Branch:** `chore/extract-derive-thinking-timeline` · **Pushed:** yes (PR open)
**Date:** 2026-05-18

## What just shipped (this branch)

**Stage 2 / item #8 — first PR** of the Atelier-blueprint ChatView
decomposition. Pulls the 300-line `_deriveThinkingTimeline` reducer
(and its 15 event-shape interfaces + the `_truncate` helper) out of
`ChatView.tsx` (2,762 LOC) into its own pure-function module so it can
be unit-tested in isolation and reused by future panels (Devin-style
timeline, agent-attribution chips) without dragging the whole chat
component in.

- **`desktop-ui/components/chat/deriveThinkingTimeline.ts`** (new,
  ~430 LOC). Verbatim move of the reducer; the only renames are
  `_deriveThinkingTimeline → deriveThinkingTimeline` and
  `_truncate → truncate` (module-local). Exports
  `deriveThinkingTimeline(events) → ThinkingRow[]` plus the
  `ThinkingRow`, `ThinkingRowState`, and `StreamingEvent` types. The
  long section comment that documents intentional drops (which events
  the reducer ignores and why) moved with it.
- **`desktop-ui/components/ChatView.tsx`.** Imports the function and
  types from the new module; the inline block (lines 171–607 in the
  old file, ~437 LOC) is gone. Two call-site references and one
  comment updated to the new name. File shrinks from 2,762 → ~2,325
  LOC.
- **`desktop-ui/components/chat/deriveThinkingTimeline.test.ts`**
  (new, 15 tests). Pins the behavioral contract the function had been
  carrying implicitly: empty-events → no rows; intentional drops
  (`pipeline_*`, `checkpoint_state=provisional|committed`); dedupe of
  `route_decided` / `memory_recalled` / `security_assessment` (last
  wins, replaced in place); ordering preserved across distinct types;
  state mapping for `security_scan.verdict`, `challenger_complete`
  (parse_failed / signal / clear), `camel_complete` (error / blocked /
  ok), `reader_complete.red_flags`; truncation of long `reasoning`
  (80 chars) and `reason` (140 chars) fields; per-row keys are unique.

## Verified

- `npx vitest run desktop-ui/components/chat/deriveThinkingTimeline.test.ts`
  → **15 passed** in 4 ms.
- `npm run typecheck` — clean (both tsconfig.node.json and
  tsconfig.web.json).
- `npm run test:frontend` — **109 passed** across 12 files (up from
  94 baseline in earlier handoff — already had a few more before this
  branch; +15 are this PR's, the rest were pre-existing).

## Stage-2 status (post-this-PR)

- **#7 LangGraph 1.2 StateGraph orchestrator** — landed (merged in
  PR #17). Default flip is gated on two consecutive weekly bench
  cycles (AgentDojo + agentic-misalignment) — a separate follow-up.
- **#8 ChatView decomposition** — **in progress**. This PR is the
  first extraction. Next extractions in priority order:
    1. The live-pipeline reducer at `_derivePipelineLive` (sibling of
       the one we just moved, ~140 LOC).
    2. `ThinkingTimeline` + `_thinkingRowTone` rendering components
       (~80 LOC) — now a natural follow because the data layer is
       split out.
    3. `applyRoster` + roster state into a `useRoster()` hook.
    4. TanStack Query migration of `Chat.list` / `Agents.list` /
       `Teams.list` from `useEffect` + `useState`.
- **#9 Devin-style timeline** — pending (depends on #7 + #8). Once
  the renderer in (2) above is its own component, the Devin-style
  drillable variant becomes a sibling, not a refactor.
- **#10 Visual TeamComposer** — pending (depends on #8 extractions).
- **#11 Typed error envelopes** — pending.
- **#12 Bundled llama-server binary** — pending.

## Next up

Three parallelisable directions for the next session:

1. **Continue Stage-2 #8.** The next-smallest extraction is
   `_derivePipelineLive` (same shape as the function this PR moved —
   pure reducer over the streaming event log). Once it's out, the
   `ThinkingTimeline` / `_thinkingRowTone` rendering pair becomes a
   trivial follow-on extraction.
2. **Stage-2 #7 follow-up** — kick off the first weekly bench cycle
   under `orchestrator_engine="graph"`.

## Walls hit

None. One scope discipline note: the extracted module is a pure
function over a single event-log array. No React state, no React
hooks, no module-level mutable state — same shape as the prior
`_derivePipelineLive` sibling, so the next extraction will mirror
this PR almost line-for-line.

The first attempt at the Edit used too-clever placeholder anchors and
left orphaned references in the file; reverted via
`git checkout -- desktop-ui/components/ChatView.tsx` and redid the
extraction with a single targeted Edit covering the contiguous block.
Lesson for future large-block moves: read the *entire* range in one
`Read` call first, then submit one Edit with the exact captured
content as `old_string` — don't chunk with placeholders.

---

<!-- Earlier handoff content preserved below for cross-session reference. -->

# Earlier session — `fix/roster-team-id-passthrough` (commit `8c397e8`, merged to main in #18)

**Last commit:** 8c397e8 — `fix(api): forward team_id through chat roster facade`
**Branch:** `fix/roster-team-id-passthrough` · **Pushed:** yes (merged to main)
**Date:** 2026-05-18

## What just shipped (this branch)

**Bug fix — RosterPicker was 100% broken.** Every roster pick from the
UI (solo agent, multi-agent ad-hoc, or saved team preset) was failing
with HTTP 500, the frontend rolled back from the server, and the chip
stayed reading "No agent" no matter what the user selected.

Root cause: signature mismatch at the seam between `routes/chat.py` and
the domain API facade. The route calls
`get_api(request).chat_set_conversation_roster(cid, ids, team_id=body.team_id)`
unconditionally (None when absent), but the thin facade method at
[backend/core/api/__init__.py:830](backend/core/api/__init__.py#L830)
was declared as `(self, conversation_id, agent_ids)` — no `team_id`
parameter, and `team_id` not forwarded to the inner `ChatAPI` either.
Every call raised
`TypeError: chat_set_conversation_roster() got an unexpected keyword
argument 'team_id'`. FastAPI returned 500; `applyRoster` in
`ChatView.tsx` caught it, toasted, and re-fetched the unchanged
conversation row.

- **`backend/core/api/__init__.py`.** One-line signature fix: the
  facade now accepts `team_id=None` and forwards it as a keyword to the
  inner `ChatAPI.chat_set_conversation_roster`, matching the route's
  call shape and the inner method's signature.
- **`backend/tests/test_api_chat_roster_facade.py`** (new, 3 tests).
  Pins the facade's kwargs forwarding (this is the seam that broke):
  `team_id="t_42"` is forwarded verbatim, `team_id=None` (the implicit
  default when the renderer omits it) is also forwarded, and the
  route's exact call shape (`team_id=` keyword) doesn't raise. No
  test covered this facade before — that's how the regression slipped.

## Verified

- `cd backend; python -m pytest tests/ -q` — **720 passed, 9 skipped,
  13 deselected** in 140s (up from 717 baseline; +3 new facade tests,
  all green).
- Targeted subset:
  `pytest tests/test_chat_orchestrator.py tests/test_pipeline.py
  tests/test_layer_fences.py tests/test_api_chat_roster_facade.py -q`
  → 92 passed.

## Walls hit

None. The fix is a single line plus a regression test against the
exact seam. The bug had been live since the RosterPicker landed and
went unnoticed because no test covered the `API` facade method
(only the inner `ChatAPI` and the orchestrator's `update_conversation_roster`).

---

<!-- Earlier handoff content preserved below for cross-session reference. -->

# Earlier session — `feat/langgraph-orchestrator` (commit `501f9f4`, merged in #17)

**Last commit:** 501f9f4 — `feat(orch): LangGraph StateGraph engine for ChatOrchestrator.send`
**Branch:** `feat/langgraph-orchestrator` · **Pushed:** yes (merged to main)
**Date:** 2026-05-17

## What just shipped (this branch)

**Stage 2 / item #7** of the Atelier-blueprint plan — first Stage-2 item.
Lands a LangGraph 1.2 `StateGraph` rewrite of `ChatOrchestrator.send()`
behind the `orchestrator_engine` setting (default `"legacy"`). Both
engines exercise the **same downstream services** — no business logic
is reimplemented; the graph is control-flow only.

- **`backend/services/orchestrator_graph.py`** (new, ~860 LOC). Builds
  a `StateGraph(TurnState)` of 19 nodes that mirror the legacy `send()`
  body 1-for-1:
    open_turn → team_check → load_agent → load_context → memory_recall
    → route_decision → resolve_target → security_gate → governance_check
    → compute_flags → phase8_voting → phase5_escalation_check
    → phase12_camel → phase6_split → interleaved_reasoning
    → monolithic_dispatch → alignment_check → escalation_ladder
    → finalize_turn → END.
  Each node delegates to existing service instances on the orchestrator
  (`TurnLifecycle`, `MemoryRecall`, `TurnRouter`, `SecurityGate`,
  `WorkerDispatch`, `EscalationLadder`, `HubRouter`, `GovernanceEngine`,
  CaMeL, Reader/Actor split helpers, high-stakes voting). Each guards
  on `state["result"]` so early-exit nodes (budget exceeded, security
  abort, governance block, escalation pending, vision unavailable)
  short-circuit the remainder without re-running services. Compiled
  graph is cached via `@lru_cache(maxsize=1)` so per-turn overhead is
  just `.invoke()` on a stateless runnable.
- **`backend/services/chat_orchestrator.py`**. Three-line dispatch at
  the top of `send()`: if `orchestrator_engine == "graph"`, route into
  `run_turn_graph(self, ...)` and return. Legacy body unchanged — that
  is still the source of truth until two clean weekly bench cycles
  (AgentDojo + agentic-misalignment) confirm parity.
- **`backend/core/settings.py`**. New `orchestrator_engine` key
  (default `"legacy"`, enum `["legacy", "graph"]`) with a manifest
  entry under the Advanced group.
- **`backend/requirements.txt`**. Adds `langgraph>=1.2,<2.0.0` to the
  lite bundle. Resolves to 1.2.0 against Python 3.13 with no native
  build step. Wheel-only.
- **Tests.** `backend/tests/test_orchestrator_graph_engine.py` (new,
  11 tests). Covers: default-engine-is-legacy, dispatch-into-graph
  invocation, unknown-engine-falls-back-to-legacy, graph happy-path
  (Claude + local + agent.model_preference override),
  graph budget-exceeded early-exit, graph persists assistant messages,
  parametrized legacy↔graph parity on a basic turn, route_decided +
  memory_recalled events emitted under graph engine.

## Verified

- `cd backend; python -m pytest tests/ -q` — **717 passed, 9 skipped,
  13 deselected** in 140s (up from 706 baseline; +11 new graph engine
  tests, all green).
- Plan-aligned verification (subset): `pytest tests/test_chat_orchestrator.py
  tests/test_hub_router.py tests/test_reader_actor_split.py
  tests/test_pipeline.py tests/test_high_stakes_voting.py
  tests/test_logprob_data_flow.py tests/test_orchestrator_graph_engine.py`
  → 103 passed, 2 deselected.
- `python -m pytest tests/test_layer_fences.py -q` — 41 passed.
- `npm run typecheck` — clean.

## Stage-2 status (post-this-PR)

- **#7 LangGraph 1.2 StateGraph orchestrator** — landed behind flag
  (this branch). Default flip is gated on two consecutive weekly bench
  runs (AgentDojo + agentic-misalignment) with no regression — a
  separate follow-up.
- **#8 ChatView decomposition + shadcn/ui + TanStack Query** — pending.
- **#9 Devin-style timeline** — pending (depends on #7 + #8).
- **#10 Visual TeamComposer** — pending (depends on #8 extractions).
- **#11 Typed error envelopes** — pending.
- **#12 Bundled llama-server binary** — pending.

## Next up (per the approved plan)

Two parallelisable directions for the next session:

1. **Stage-2 #7 follow-up — bench-driven default flip.** Run AgentDojo
   + agentic-misalignment under `orchestrator_engine="graph"` for two
   weekly cycles; once parity holds, flip the default and delete the
   legacy body. SSE byte-parity diff between the two engines is the
   strictest gate.
2. **Stage-2 #8** — start decomposing `ChatView.tsx` (2,762 LOC) into
   composed shadcn/ui panels + TanStack Query hooks. The
   `_deriveThinkingTimeline()` extraction is the smallest first PR.

## Walls hit

None this session. One scope discipline note: the graph engine is
intentionally a *parallel* implementation, not a refactor of the legacy
`send()` body. The duplication is temporary — the legacy path is the
source of truth until bench-confirmed parity flips the default. This
avoids touching the legacy code-path on this PR, which keeps the
existing test suite as an unbroken regression guard.

LangGraph nodes return *partial-update dicts* keyed by declared TypedDict
fields only. Two debug helpers initially used `_prefix` private keys
that LangGraph silently strips on merge — promoted to `mem_result` and
`response_empty` as first-class TurnState fields. Worth remembering for
future node additions: every key a node needs to read in a downstream
node must be declared in `TurnState`.

---

<!-- Earlier handoff content preserved below for cross-session reference. -->

# Earlier session — `feat/litellm-pydantic-ai` (commit `07c5b55`)

**Last commit:** 07c5b55 — `feat(llm): LiteLLM adapter + Pydantic-validated ReaderOutput`
**Branch:** `feat/litellm-pydantic-ai` · **Pushed:** yes (merged to main)
**Date:** 2026-05-17

## What just shipped (this branch)

**Stage 1 / item #3** of the Atelier-blueprint plan — the last open Stage-1
item. Adds LiteLLM as a third `LLMClient` implementation for BYO-key
providers (OpenAI / Gemini / Groq / Mistral / DeepSeek / Grok / Cohere /
etc.) and moves the Reader's JSON parsing from hand-rolled regex onto a
Pydantic v2 schema validator. Existing `ClaudeClient` and `LocalClient`
are untouched.

- **`backend/services/llm_litellm_adapter.py`** (new). `LiteLLMClient`
  implements the `LLMClient` ABC: `chat_unified` / `stream_unified` /
  `is_available` / `client_name`. System prompt is prepended as
  `{"role":"system",…}` (OpenAI chat shape, matching `LocalClient`).
  Imports `litellm` lazily so sidecars that never see a BYO-key turn
  never pay the openai+tokenizers warm-up cost. `is_available` requires
  both a model and a key so HubRouter fails closed when the third client
  is misconfigured.
- **`backend/services/hub_router.py`.** `HubRouter.__init__` accepts an
  optional `litellm_client`. `invoke()` dispatches to it when
  `decision.backend == "litellm"`; missing-client falls closed with an
  errored `WorkerResult` rather than silently using Claude.
  `_resolve_backend` recognises `"litellm"` as a model preference and as
  a backend hint. `target_for` emits a `litellm`-typed `ExecutionTarget`
  without the `local`-style 2048-token clamp.
- **`backend/models.py`.** New `_ReaderOutputSchema(BaseModel)` Pydantic
  v2 schema declares the JSON contract for the Reader's output, with
  per-field validators that coerce `null → []`, drop entries that
  aren't `str / int / float`, and ignore unknown keys. `ReaderOutput.from_raw`
  now does a code-fence / JSON-envelope cleanup pass and hands the
  envelope to `_ReaderOutputSchema.model_validate_json`. The dataclass
  form (frozen, tuple fields) is preserved as the runtime contract.
- **`backend/requirements.txt`.** Adds `litellm>=1.50.0,<2.0.0` to the
  lite bundle (Pydantic v2 is already shipped via FastAPI; no new
  Python-side dep for the structured-output validator).
- **Tests.**
  - `backend/tests/test_litellm_adapter.py` (new, 10 tests). Mocks
    `litellm.completion` and pins: system-prompt prepending, empty-system
    omission, missing-usage tolerance, provider-error sentinel,
    streaming token accumulation + final-chunk usage, stream-error
    fallback to non-streaming.
  - `backend/tests/test_reader_output_schema.py` (new, 14 tests). Pins
    cleanup tolerance (fences, surrounding prose, missing braces) AND
    the new Pydantic schema (mixed-type lists, unknown fields, null
    coercion, non-list-as-string drop, half-formed-JSON degradation).
  - `backend/tests/test_hub_router.py` (extended, +9 tests). LiteLLM
    dispatch (non-streaming + streaming + fail-closed), the new
    `_resolve_backend` keyword, the new `target_for` litellm shape.

## Verified

- `cd backend; python -m pytest tests/ -q -x` — **706 passed, 9
  skipped, 13 deselected** in 131.77s (up from 665 baseline; +14
  schema + +10 adapter + +9 hub_router + … all green).
- Plan-specified verification: `pytest tests/test_hub_router.py
  tests/test_reader_actor_split.py tests/test_logprob_data_flow.py`
  → 37 passed, 0 failed.
- Layer-fence test still green (41 parametrized tests).
- `npm run typecheck` clean.

## Stage-1 status (post-this-PR)

- **#3 Pydantic AI + LiteLLM** — DONE (this branch).
- **#4 SignPath OSS code signing** — DEFERRED per scope decision.
- **#5 openapi-typescript codegen** — DONE (merged PR #14).
- **#6 Layer fence** — DONE (merged PR #15).

Stage-1 is complete except for #4, which is gated on the SignPath
Foundation OSS application.

## Next up (per the approved plan)

Stage 2 (Tier-2 surface refit):
- **#7** LangGraph 1.2 `StateGraph` rewrite of the orchestrator,
  preserving CaMeL / HandoffPacket / saga / challenger / ToM / voting /
  governance / hub-router policy verbatim as nodes.
- **#8** Decompose `ChatView.tsx` (2,762 LOC) into composed shadcn/ui
  panels + TanStack Query hooks.
- **#9** Devin-style Plan→Confirm→Execute→Critique drillable timeline
  (extending the existing `_deriveThinkingTimeline`).
- **#10** Visual TeamComposer replacing the 6-field `AgentPanel`.
- **#11** Typed error envelopes across `backend/routes/*`.
- **#12** Finish the bundled `llama-server` binary integration (resolve
  the `TODO(engines)` in `backend/services/bundled_server.py`).

## Walls hit

None this session. Some intentional scope discipline: did NOT add
`pydantic-ai` (the package) as a runtime dep this stage — the Reader's
validator uses Pydantic v2 directly (the same layer Pydantic AI is
built on). Adding the package will land naturally when Stage 2's
LangGraph rewrite begins using `pydantic_ai.Agent` for typed agent
flows. Also did NOT wire the LiteLLM client into Settings / UI — the
adapter and HubRouter branch are infrastructure; the UI exposure is a
focused follow-up.

---

<!-- Earlier handoff content preserved below for cross-session reference. -->

# Earlier session — `feat/layer-fences-test` (commit `8ddac21`)

**Last commit:** 8ddac21 — `test(arch): add backend/core ↔ backend/routes import-fence gate`
**Branch:** `feat/layer-fences-test` · **Pushed:** yes
**Date:** 2026-05-17

## What just shipped (this branch)

**Stage 1 / item #6** of the Atelier-blueprint plan. New
`backend/tests/test_layer_fences.py` codifies the layering between
`backend/core/` (business logic, 2,259 LOC) and `backend/routes/` (HTTP
serialization, 5,061 LOC). The split was reframed in the plan after
exploration confirmed it is NOT duplicate trees — they are layered.

Two AST-walked, parametrized fence rules:

1. **`core/` must not import from `routes`.** 19 core files checked
   (current state: zero violations). Catches `import routes.x`,
   `from routes import y`, `from routes.x import z`.
2. **`routes/` must not import `core.api._*` private modules** (or
   private names like `from core.api import _Foo`). 22 routes files
   checked (current state: zero violations). The expansion of
   `from M import N` into `M.N` entries makes the private-name
   variant detectable even though the module-level part is public.

Tests are intentionally excluded from the fence — fixtures legitimately
reach into private module-level state (e.g.
`system_routes._bundled_download_running`).

## Verified

- `cd backend; python -m pytest tests/test_layer_fences.py -v` —
  41 passed in 0.22s (19 core + 22 routes parametrizations).

## Prior branch (already shipped)

`feat/openapi-typescript-codegen` (commit `75282c5`) — Stage 1 / item #5,
OpenAPI → TS codegen via `openapi-typescript`. See PR at
https://github.com/zasonic/AltoSymbiosisAgents/pull/new/feat/openapi-typescript-codegen

## Next up (per the approved plan)

- **Stage 1 item #3** — Pydantic AI + LiteLLM as a third `LLMClient`
  implementation behind the existing ABC at
  `backend/services/llm_interface.py:11-33`. New
  `backend/services/llm_litellm_adapter.py`; one new branch in
  `hub_router.invoke()`. Pydantic AI also slots into the Phase-6 Reader
  output parser at `chat_orchestrator.py:1650-1705`.
- **Stage 1 item #4** — _skipped per user decision_ until SignPath
  Foundation OSS application is in flight.

## Walls hit

None this session.

---

<!-- Earlier handoff content preserved below for cross-session reference. -->

# Earlier session — `feat/openapi-typescript-codegen` (commit `75282c5`)

**Last commit:** 75282c5 — `feat(codegen): switch API types to OpenAPI + openapi-typescript`
**Branch:** `feat/openapi-typescript-codegen` · **Pushed:** yes
**Date:** 2026-05-17

## What just shipped

**Stage 1 / item #5** of the Atelier-blueprint plan
(`C:\Users\dmuhl\.claude\plans\altosymbiosisagents-atelier-synthetic-wirth.md`).
Layer C5 (API type codegen) now dumps FastAPI's live `app.openapi()` schema
and runs `openapi-typescript` against it instead of walking allowlisted route
modules and calling `pydantic2ts` + `json-schema-to-typescript`. The route
table is now the single source of truth.

- **`backend/server.py`.** Extracted `ROUTER_SPECS` (the 20-tuple of
  `(dotted_module, prefix)`) and `register_routers(app)` as module-level
  helpers; `build_app()` now calls `register_routers(app)` instead of the
  20-line inline `include_router` block. `OPENAPI_TITLE` and
  `OPENAPI_VERSION` are surfaced as constants so the codegen produces a
  schema whose info block matches the running sidecar.

- **`build-scripts/generate_api_types.py`.** Full rewrite. Imports
  `register_routers` + the two metadata constants from `server`, builds a
  bare `FastAPI` instance, dumps `app.openapi()` to a temp JSON, and shells
  out to `npx --no-install openapi-typescript <input> --output <output>`.
  Cross-platform npx resolution (`npx.cmd` on Windows). Deterministic across
  runs — verified via SHA256.

- **Dependency swap.** Dropped `pydantic-to-typescript` from
  `backend/requirements-dev.txt` and `json-schema-to-typescript` from
  `package.json`. Added `openapi-typescript ^7.4.0` (resolved to 7.13.0).

- **Drift gate (`.github/workflows/tests.yml`).** Updated the comment
  blocks describing the toolchain; the gate itself is unchanged
  (`python build-scripts/generate_api_types.py` + `git diff --exit-code`).

- **Generated output (`desktop-ui/api/generated.d.ts`).** Grew from 256
  lines (flat Pydantic-only `interface` declarations) to 6,776 lines
  covering `paths`, `components.schemas`, and `operations`. Nothing in the
  renderer currently imports from this file (the type surface is being
  built up incrementally), so the larger output is purely opportunity, not
  regression.

## Verified

- `python build-scripts/generate_api_types.py` — wrote 124 path entries +
  59 component schemas; SHA256 stable across re-runs.
- `npm run typecheck` — both `tsconfig.node.json` and `tsconfig.web.json`
  clean.
- `cd backend && python -m pytest tests/ -q -x` — 632 passed, 9 skipped,
  13 deselected.
- `npm run test:frontend` — 11 files / 94 tests pass.
- `npm run build` — clean build in 15.9s.
- `npm run bundle-size` — +5.56% from 2026-05-12 baseline (within 10%
  tolerance, no new renderer code shipped — the delta is npm graph churn
  from openapi-typescript install).
- Pre-existing local-only failure: `node dev/run-ts-prune.cjs` crashes
  with `spawnSync npx ENOENT` on Windows. Reproduces on a clean checkout
  of `main` (verified via `git stash` and re-run) — not a regression
  from this branch. CI on Ubuntu is unaffected.

## Next up (per the approved plan)

- **Stage 1 item #3** — Pydantic AI + LiteLLM as a third `LLMClient`
  implementation behind the existing ABC at
  `backend/services/llm_interface.py:11-33`. New
  `backend/services/llm_litellm_adapter.py`; one new branch in
  `hub_router.invoke()`. Pydantic AI also slots into the Phase-6 Reader
  output parser at `chat_orchestrator.py:1650-1705`.
- **Stage 1 item #6** — `backend/tests/test_layer_fences.py` AST-walk test
  asserting `backend/routes/*.py` may import `backend/core/api/*` but not
  the reverse, and neither imports the other's private helpers.
- **Stage 1 item #4** — _skipped per user decision_ until SignPath
  Foundation OSS application is in flight.

## Walls hit

None this session.
