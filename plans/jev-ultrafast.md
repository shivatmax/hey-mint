# What Mint can take from jev-ultrafast (plan, 7 Oct 2026)

Reference: `refs/jev-ultrafast` (browser-use/jev-ultrafast, MIT, ~900 lines). A browser agent where **Jev is the
policy** and an LLM only writes text. Google Flights Zürich→London in 7.1 s: 17 Jev requests (median 178 ms),
10 interactions, 2 text calls (Mercury, 346-581 ms).

## How it works

1. **One atomic page snapshot** (`snapshot.js`, one browser call): visible controls as an indexed table, each with
   its role, name, current value and checked, selected or expanded state, plus the visible page text only. A
   WeakMap keeps a stable identity for each real DOM node, so the model never invents a selector.
2. **One Jev request per step, several heads:**
   - `operation`: CLICK, TYPE_TEXT, SELECT, SCROLL, WAIT, DONE or BLOCKED;
   - one target question per operation (`click_target`, `type_text_target`, ...).

   The heads are answered at the same time, and only the head of the chosen operation is used. That gives two
   decisions in one round trip, and a target can never be incompatible with its operation.
3. **A small LLM only for TYPE_TEXT.** It returns strict JSON `{"text": ...}` from the goal, the field and the
   page. The result is cached while its input is unchanged.
4. **Guards:**
   - A page fingerprint is checked before acting; a stale decision is thrown away and the step is chosen again.
   - Geometry is read again and the click target is hit-tested for anything covering it.
   - A decision is used once, so a retry can never double-click.
   - Three steps in a row with no change mean BLOCKED.
   - Model output never becomes a selector, coordinates or code.
5. **Event-based waits:** after an action, at most 2 animation frames or 50 ms; after typing into a combobox,
   until its suggestions show (up to 200 ms). No fixed sleeps.
6. **A background tab that keeps rendering:** `Target.createTarget(background=true)` plus
   `Emulation.setFocusEmulationEnabled`. The agent works in a tab the user never sees and needs no screen.
7. **Independent verification and matched benchmarks.** The model's DONE is not trusted; a separate check confirms
   the result.

## How Mint does the same things today

| | jev-ultrafast | Mint now |
|---|---|---|
| Who picks each web step | Jev, ~180 ms | The Live voice model, one tool call per step (1-4 s, 15 s stalls seen 6 Oct), or the background worker LLM (~17k tool tokens per step) |
| Browser driver | CDP: one snapshot call, real input events, background tab | AppleScript + JS (`harness_tools.browser`): needs "Allow JavaScript from Apple Events", acts on the front tab, finds targets by text each call |
| Screen | Not needed (background tab) | The front window: jobs must take turns on the screen lease |
| Text to type | Small LLM, strict JSON | The Live model writes it inside the tool call |
| Waits | Events, ≤200 ms | `_SETTLE = 2.0` s after any open, and fixed sleeps |
| Jev use | The whole action policy | ~20 one-shot "pick one of N" resolvers (Chrome profile, app, control, skill, mood) |
| Native apps | n/a | `desktop` = jev-use (Swift): Jev picks from one flat list of actions, in batches; ui_act runs through the LLM |
| Proof | Independent checker, timed runs | groundbench for clicks; no timed web or app suites |

Measured on 6 Oct: a background Calculator job ("56+44") took about 110 s; after fixes, 12×12 took about 45 s. A
Flights-style web task through Mint would need 15-20 LLM steps, which is minutes.

## Plan (in priority order)

### P1. `web_goal`: a fast Jev browser engine (new `mint/webgoal/`)
- Port the loop: `snapshot.js` (as is), the operation/target action space with one-request heads, the guards, the
  event waits, and the text-helper contract.
  - Text helper: gemini-3.5-flash-lite or 3.6-flash through `llm.py`. Our OpenRouter key is refused, so
    Mercury is not available.
  - Use Mint's existing CDP client from `meet_call.py` (the `--remote-debugging-pipe` code), factored into
    `mint/tools/cdp.py`.
- Where it runs:
  - (a) **Mint's own Chrome profile, in a background tab.** No screen, true parallelism with the user and other
    jobs. The default for background jobs and for public sites.
  - (b) **The user's own logged-in Chrome**, through Chrome's "Allow remote debugging" prompt (the Browser Harness
    flow). It needs the user's one-time yes in Chrome, which we ask for and never click.
  - (c) Fallback: run the same snapshot through AppleScript JS in the front tab (the current permission), with
    Mint's keyboard typing for fields that reject synthetic input.
- Expose it as one tool, `web_goal(goal, url?, where?)`, to both the Live model and the background worker. One
  call runs the whole loop and returns a verified result, so there is no per-step voice round trip.
- Keep every rule: no password fields, the guard before send, post, buy or delete (DONE stops before submitting
  unless asked), page text is untrusted (fenced), and step and request budgets (60 / 120).
- Tests: port their offline tests (the model contract, stale retries, double-act protection, helper validation)
  and add a local fixture page plus a live smoke test (Wikipedia).

### P2. The same heads for native apps (`ui_goal`, and jev-use)
- Build a Python fast loop over `ground.inventory()`, which already gives an indexed AX element table:
  - operation heads CLICK, TYPE_TEXT, PRESS_KEY, SCROLL, DONE and BLOCKED, with per-operation target heads;
  - the text helper only for TYPE_TEXT;
  - fingerprint, occlusion and freshness checks, reusing `axkit.element_at` and `own_window_at`.
- Target: a Calculator-class job in about 3-5 s instead of 45 s.
- jev-use (Swift): move from one flat action list with batches to operation + target heads, and add the text helper
  instead of extracting typed text from the command.

### P3. The background worker plans, the fast loops act
- The worker LLM keeps planning and checking. It hands each "do this on a site" or "do this in an app" sub-goal to
  `web_goal` or `ui_goal`. Fewer 17k-token LLM steps per job, so it is faster, cheaper and kinder to the free tier.

### P4. Jev as the fast router in hot paths (one multi-head request, under 200 ms)
- Routing a request: instant, quick tool, background job, named agent or answer. This makes the "jobs can't use the
  screen" refusal and the Astra echo impossible to repeat.
- Memory: the kind, section and supersede decision in `membank.add` (replacing the LLM call).
- Keep it as an advisor: the Live model still answers, and the router's choice comes in as a short hint.

### P5. Event-based waits everywhere
- Replace `_SETTLE` 2 s and the other fixed sleeps with readiness signals: the app is frontmost and AX-ready, the
  window title changed, the page fingerprint changed, combobox options appeared, all with caps.

### P6. Proof: benchmarks with independent checks
- A web suite (Wikipedia article, a local filter fixture, a Flights-style search with a route and date checker) and
  an app suite (Calculator, Notes, Finder). Record Jev requests, latency, steps and pass or fail, so every
  optimisation is measured as jev-ultrafast did (matched runs, all attempts kept).

## Risks and unknowns
- **TypeSafe usage and cost:** the Flights run used about 90k Jev input tokens. Track it in `usage.py`.
- **Choice limits:** Jev takes at most 255 choices per question, so candidates are capped at 250.
- **The user's main Chrome over CDP** needs their consent in Chrome (Chrome 136+ blocks quiet remote debugging of
  the default profile).
- **Pages the loop can't handle:** shadow DOM, iframes, canvas, uploads and nested scrolling (their stated limits).
- **Reliability:** two sites prove little, so P6 comes before P1 is turned on by default.
