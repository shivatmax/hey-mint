# Hand-labeled agent requests

36 real Claude Code requests, each with a person's answer for what Mint's coding-agent checks
(`mint/agent_tests.py`) should say about it. `tests/accuracy/score.py` scores Mint against them, and
`tests/test_accuracy.py` keeps the score from slipping below `tests/accuracy/baseline.json`.

## Credit

These cases come from [dotpals](https://github.com/rikinshah787/dotpals) (`test/accuracy/cases`), used under its
MIT License. They are copied unchanged except as noted below.

```text
MIT License

Copyright (c) 2026 dotpals contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Changes: in `2026-10-01-claude-964b-82310.json` (once) and `2026-10-02-claude-964b-97004.json` (twice), the
made-up Windows home folders (`C:/Users/` then `dev` or `me`) inside text a command wrote into a file read
`C:/Home/...`, so the export's personal-data check (it refuses any macOS-style home path) passes. They are in heredoc bodies,
never a command that ran, so no claim depends on them; dotpals' own scorer gives the same result before and after.

## Format

Each file is one request:

```json
{
  "name": "2026-10-04-claude-582e-32918",
  "source": "real",
  "labeled": true,
  "note": "what happened, in a person's words",
  "truth": { "tests": {}, "why": {}, "risky": [], "retries": {}, "weakened": null,
             "ready": false, "stopped": "done", "changed": [] },
  "turn": { "harness": "claude", "prompt": {}, "steps": [], "end": {} }
}
```

Only cases with `"labeled": true` count. `turn.steps` are the request's tool calls, oldest first:

| field | meaning |
| --- | --- |
| `id` | `e2`, `e3` ...: what the truth's keys refer to |
| `kind` | `run` (a shell command), `edit`, `write`, `read`, `search`, `tool`, `web`, `agent`, `skill` |
| `status` | `ok`, `failed` (a non-zero exit, a denied or cut-short call), `stopped` |
| `at` | when it ran (ms since 1970) |
| `body.command` | a `run` step's command line (long ones are stored cut, with `…`) |
| `body.output` | its output (stdout and stderr), when kept |
| `error` | a failed step's message: `Exit code N ...` when the command ran and exited non-zero |
| `body.patch` | an `edit`'s diff: `-old line` / `+new line` |
| `files` | `[{path, change}]`: the files a step read or changed (`read`, `edit`, `write`, `delete`) |
| `exitUnknown` | the exit code wasn't recorded |

`truth`, by claim:

| claim | answer |
| --- | --- |
| `tests` | `{stepId: "passed" \| "failed" \| "unclear"}`: each finished test run |
| `why` | `{stepId: text \| null}`: why a failed run failed, in the runner's words (`null`: the output doesn't say) |
| `risky` | `[text]`: dotpals' warnings ("Force-stopped programs", "Threw away changes in git" ...) |
| `retries` | `{stepId: "fixed" \| "failing"}`: a failed step the agent tried again, and how it ended |
| `weakened` | `text \| null`: tests made to pass by changing them ("changed what an assertion expects in math.test.js") |
| `ready`, `stopped`, `changed` | dotpals-only claims (ready to merge, why it stopped, files changed); not scored for Mint |
