# Mint reliability benchmark

This benchmark has 70 realistic, often long-horizon tasks, and most of them include a complication. The 20 `hard-` tasks
(`rb/t_hard2.py`) are the hardest: long chains across tools, 3-4 turn conversations that refer back, messy data,
constraints, recovery, undo, an agent handoff and clipboard + files. For each task
the runner sets up the fixtures, sends the request (1 to 3 turns) to the running Mint, waits until Mint is done,
and checks the end state on disk or in the app. It never grades Mint's words. It then cleans up.

```sh
PY="$HOME/Library/Application Support/Mint/.venv/bin/python"
cd ~/Downloads/Projects/projects/Project-Jev/jarvis

"$PY" bench/reliability/run.py --list                        # the tasks
"$PY" bench/reliability/run.py --selftest                    # no Mint: oracles make the right end state; all must PASS
"$PY" bench/reliability/run.py --dry-run                     # no Mint: setup, check, cleanup; all must FAIL cleanly
"$PY" bench/reliability/run.py --only web-local-fact,clip-to-file
"$PY" bench/reliability/run.py --only web-                   # an id prefix ending in "-" matches a group
"$PY" bench/reliability/run.py --only hard-                  # the 20 harder tasks
"$PY" bench/reliability/run.py --category web,files --repeat 3
"$PY" bench/reliability/run.py                               # all 70 (about 2 to 3 hours)
```

Each run writes `results/<stamp>.json` and `results/<stamp>.md`. The JSON has every turn with Mint's
reply, the tool calls and results, errors, the check's verdict, the log excerpt and what cleanup removed.
The Markdown report shows:

- pass rates by category, difficulty and complication
- the top failure reasons
- a table of the tasks
- cleanup side effects

Skipped tasks don't count toward the pass rate.

Options:

- `--no-prompt`: skip tasks that could make macOS show a permission dialog.
- `--fixtures auto|eventkit|direct|mint|off`: choose how the Apple-app fixtures are scripted. See below.
- `--offline`: skip the one task that uses a public website.
- `--keep`: skip cleanup, for debugging one task.
- `--quiet N`: the number of seconds without activity that ends a turn (default 7).
- `--port N`: the port for the local site (default 8765).

## What a run touches (the sandbox)

| Thing | Where | Cleanup |
|---|---|---|
| Files | `~/MintBench` only (fixtures are rebuilt for every task) | removed after each task and at the end |
| Reminders | a "Mint Bench" list | the list is deleted. Reminders made during the task that carry the task's markers are deleted from any list |
| Calendar | a "Mint Bench" calendar | the calendar is deleted, plus marked events from the task window in other calendars |
| Notes | a "Mint Bench" folder | the folder is deleted, plus marked notes made during the task |
| Mail | drafts with "[MintBench]" in the subject, to bench@example.com | compose windows are closed and drafts deleted, again 8 s later and at the end of the run (Mail autosaves compose messages into Drafts a few seconds late). A check fails with SAFETY if any "[MintBench]" mail is in Sent |
| Web | `site/` on `http://127.0.0.1:8765` (started by the runner), plus python.org for one task | - |
| Clipboard | the runner saves your clipboard text at the start | it is restored after every task and at the end, but only while the clipboard holds the benchmark's text, so anything you copy during a run is left alone |
| Mint memory and pins | only facts or pins that mention MintBench (or the task's markers) | removed from `memory/bank.json` and `clipboard/pins.json` (Mint re-reads both on every use) |
| Stray outputs | files Mint saved outside `~/MintBench` during the task whose name contains "mintbench" or a task marker. The runner looks in Mint's Documents folder, Desktop, Downloads, Documents, Movies and Pictures | deleted and listed in the report (they also show that Mint ignored the location) |
| Trash | fixture files Mint moved to the Trash, matched by name and SHA-256 | purged |
| Apps | TextEdit, Preview, Numbers, Reminders, Mail and similar apps, but only if the task started them | asked to quit politely, so unsaved work still gets its dialog |

Nothing is ever sent, and no real person is ever messaged. Between tasks the runner sends "stop" and waits
for the log to go quiet.

Things cleanup does not remove, which the report lists where it can:

- Mint's own history (`history.jsonl`, `summary.md`, `made_files.json`)
- skills Mint learned during the run (listed under "Skills Mint saved")
- tabs left open on the local site
- backups Mint made before overwriting a file
- Mail's scripting objects for closed invisible drafts, which disappear when Mail quits

## How "done" is detected

`rb/mintlink.py` sends each turn with `python -m mint --say` and follows `~/Library/Logs/Mint/mint.log`:

1. **Received:** a `[typed: …]` or `[request: …]` line matches the request. If none arrives within 60 s, the turn
   is `not-received`. That usually means Mint is unloaded or quit.
2. **Done:** after a `mint: …` reply, the log has been quiet for `--quiet` seconds. Orb, island and wake-word
   lines don't count as activity. The `[autopilot: carrying on …]` re-prompt comes about 2 s after a turn ends,
   so it resets the quiet timer.
3. **done-silent:** there has been no reply and no activity for 45 s.
4. **timeout:** the task's `timeout` (per turn) passed. The check still runs.

Checks are read-only. They are polled for the task's `settle` seconds, because video edits, document
conversions and sub-agents finish in the background after Mint's turn ends.

Tool calls are the `[tool_name] result` lines whose name is one of Mint's tools (the list is in
`mintlink.TOOLS`; update it when tools are added). Errors are:

- tool results starting FAILED, REFUSED or NOT RUN
- `session dropped` lines, tracebacks and `ERROR` lines

**Other testers:** the log is shared. If someone else's `[typed: …]` or `you: …` shows up during a task,
it's recorded under `other_requests`, and the report flags that task.

## Apple-app fixtures and permissions

These fixtures need macOS permission:

- Reminders and Calendar use **EventKit** when the process running `run.py` already has full access.
  Reminders' AppleScript took about 110 s for one call on this Mac, so EventKit is much faster.
- Otherwise they use AppleScript: `direct` if macOS already allows your terminal, found with a check that
  never prompts (`AEDeterminePermissionToAutomateTarget`).
- Failing both, they use **Mint.app's** permissions. This goes through Mint's test aid
  `open -g -n -W ~/Applications/Mint.app --args --script steps.json` and its `run_applescript` tool.
- With `--no-prompt`, a task that would need a new permission is SKIPPED, with the reason in the report.

On 29 Sep 2026, from the Claude Code terminal:

| App | Status | Result |
|---|---|---|
| Reminders | EventKit: full access | works |
| Mail | Automation: allowed | works |
| Finder | Automation: allowed | works |
| Notes | would prompt | these tasks skip under `--no-prompt` |
| Calendar | would prompt (both EventKit and Automation) | these tasks skip under `--no-prompt` |

This affects 5 tasks:

- pim-note
- pim-notes-to-reminders
- pim-calendar-event
- pim-calendar-conflict
- multi-research-report

Run once without `--no-prompt`, answer the prompts, and they run from then on. The `mint` route
has not been tried live yet.

## Adding a task

1. Add an entry to `tasks.json`:

```json
{"id": "files-zip", "category": "files", "difficulty": "medium", "complications": ["name typo"],
 "needs": [], "turns": [{"say": "Zip the {root}/Photos folder"},
                        {"say": "Now delete the originals", "check": "optional_mid_check"}],
 "setup": "files_zip", "check": "files_zip_ok", "cleanup": [], "timeout": 240, "settle": 10,
 "markers": ["photos"], "note": "what makes it hard"}
```

`{root}` becomes `~/MintBench` and `{site}` becomes the local site's address. Any string the setup puts in
`ctx.data` can be used as `{name}` too (see `pim_event`'s `{day}`). A turn can have `"wait": false`
with `"delay": N` to interrupt Mint mid-task (see `err-interrupt-resume`).

`needs` can list:

- `reminders`, `calendar`, `notes`, `mail`: the task is skipped when that app can't be scripted, and its
  container is cleaned up afterwards
- `internet`
- `browser`, which is documentation only

`markers` are extra words that identify this task's stray files, reminders, notes and memories for
cleanup. Use words of 5 or more characters that are unlikely in your real data.

2. Write the functions in an `rb/t_*.py` module:

```python
@setup
def files_zip(ctx):                 # build fixtures under ctx.p(...); raise Skip("why") if impossible
    sb.write("Photos/a.jpg", image_bytes("red", "JPEG"))

@check
def files_zip_ok(ctx):              # READ-ONLY; look at the end state, never at Mint's words
    return ok("…") if ctx.p("Photos.zip").exists() else fail("no Photos.zip")
```

3. Add an oracle in `rb/oracles.py` that produces the correct end state. Then run `--selftest --only files-zip`
   (it must PASS) and `--dry-run --only files-zip` (it must FAIL, and nothing may be left behind).

## Files

- `run.py`: the CLI, the task loop, standard cleanup and the reports
- `tasks.json`: the 70 tasks
- `site/`: the local static site (`/slow/` takes 12 s, `/flaky/` returns 503 twice, and `form.html` reports its fields;
  `shop/`, `branches/` (its `riverside.html` is a deliberate 404), `grinders/` and `rates.html` serve the `hard-` tasks)
- `rb/mintlink.py`: `--say`, following the log, parsing tool calls, done detection
- `rb/sandbox.py`: `~/MintBench`, strays, clipboard, memory and pins, readers for xlsx, docx, pdf, rtf and media
- `rb/apple.py`, `rb/ek.py`: the Mint Bench containers in Reminders, Calendar, Notes and Mail
- `rb/t_*.py`: setups and checks by category
- `rb/oracles.py`: the correct end states for `--selftest`

## Hooks that would make this more exact (not in Mint yet)

- **A machine-readable end of request:** a log line such as `[done: <request id> tools=N ok|failed]`. Mint
  would print it when the turn is complete, no tool is running, and autopilot has decided not to carry on.
  Right now "done" is inferred from `mint: …` plus 7 s of quiet, which costs about 7 s a turn and can end too
  early when a background job (an `edit_video`, a sub-agent) reports later.
- **A request id:** `mint --say` could accept `--id X` and echo it in `[typed: …]`, `[tool]` and the done
  line. Log lines could then be attributed exactly even when other testers use the same Mint.
- **A "fresh conversation" command for tests** (not memory-folding): `mint --say` with a flag that starts
  a new live session without summarising into memory. Tasks would then not see earlier tasks' context.
- **Structured tool events:** a JSONL file (tool name, args, full result, ms) next to `mint.log`. The log
  cuts results to 160 characters, and `history.jsonl` records only the minute.
