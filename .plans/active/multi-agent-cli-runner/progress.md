# Progress: multi-agent skill and CLI runner

## Step 1: spikes with real pi (2026-10-08, pi 1.1.0, Bun build)

### 1. pi follows its runner (spec check 7, first half)

Setup: a Python runner starts `pi --mode rpc --no-session` in its own
session, prompts it to start `sleep 6002` with `bg_task` and then run
`sleep 6001` with `bash`. A driver SIGKILLs the runner once both sleeps run.

- Without a guard: pi exits within a second, but both sleeps survive.
  `session_shutdown` never runs.
- Cause: pi 1.1.0's RPC writer (`core/output-guard.js`, `writeRawStdout`)
  ends with `.catch(() => process.exit(1))`. The first failed stdout write
  after the runner dies exits pi before `runtimeHost.dispose()` finishes.
  `takeOverStdout()` binds the raw write before extensions load, and Bun's
  `WriteStream.write` does not go through `_write`, so wrapping the stream
  or adding an `error` listener does not help.
- Guard that works (now part of `scripts/agent-extension.ts`): after `session_start`, the
  extension replaces `process.exit` so that `exit(1)` is deferred. It then
  polls `pi.getActiveTools()`, which throws once `AgentSession.dispose()`
  has run (dispose aborts the agent before it invalidates the extension
  runtime), and exits at that point, or after 10 seconds at the latest. It
  also swallows the unhandled EPIPE rejection from shutdown's final flush,
  and adds an `error` listener to stdout for pi running under Node.
- With the guard: `session_shutdown` runs, the `bg_task` sleep is stopped,
  the `bash` sleep's process tree is killed, and pi exits about 150 ms after
  the runner died.

The repeat with the caller's `bash` tool call aborted from the TUI is part
of step 8.

### 2. Resume by name (spec check 3)

- `--session-dir D --session-id N` creates `D/<timestamp>_N.jsonl`; a second
  run with `--session-dir D --session N` continues the conversation.
- But `--session` matches by prefix: `--session spike` and
  `--session spike-job` both open `spike-job.1`. With several jobs in one
  session directory, `resume explore` could open `explore-2`'s session.
- `--session-dir D --session-id N` reopens the existing session exactly
  (5 messages restored) and does not match prefixes (`spike-job` created a
  new, empty session).
- Decision: `resume` uses `--session-id <name>`, and `status` prints
  `pi --session-dir <dir> --session-id <name>`. Spec updated.
- `--fork FILE --session-id N` works together: the forked session is
  written at once as `D/<timestamp>_N.jsonl` with the source's messages.

### 3. Dialog answer (spec check 4)

Rule used: the user's `bash` rule that makes `rm -rf <non-temp path>` ask.

- With `PI_MINIMAL_PERMISSION_SYSTEM_NONINTERACTIVE=1`: no dialog
  `extension_ui_request` arrives; the tool result is "Bash command ...
  requires approval, but no interactive UI is available ... The operation is
  blocked." The agent continues and reports the block.
- Without the variable: a `confirm` request "Permission Required" arrives;
  answering `cancelled: true` gives the tool result "User denied bash command
  ...", and the agent continues and settles.

Both match the spec.

Other observations:

- pi sends `setWidget` and other fire-and-forget `extension_ui_request`
  records at start-up; the runner ignores them.
- pi prints "Warning: No project session found with id ..." on stderr for a
  new `--session-id`; it lands in `stderr.txt`.

## Steps 2-7: implementation (2026-10-08)

- Layout as in the spec. `scripts/stdout-guard.ts` became
  `scripts/agent-extension.ts`, which also adds the job's prompt text to the
  system prompt (see real-pi check 1 below).
- `tests/fake-pi` implements prompt, steer, abort, clear_queue,
  get_last_assistant_text, dialog requests and session files, with the test
  controls listed in its docstring.
- `python3 -m unittest discover tests`: 46 tests pass with Python 3.14.7 and
  with Python 3.9.25 (`uv run --no-project --python 3.9`), about 40 s.
- SKILL.md and reference.md name no extension or harness tool and do not
  mention permission modes (checked with grep).

Decisions made while implementing, now in the spec:

- `abort` sends `clear_queue` before `abort`, because pi continues queued
  steering messages after an abort. The abort is acknowledged at once.
- The `model` state file records `provider/model` of the latest assistant
  message for the result footer; `runner.log` holds a traceback when the
  runner itself fails.
- `wait --timeout` prints `job`, `status: running`, `waited_s` and the last
  progress line before it exits with code 2.
- `status` prints `session_cmd: cd <cwd> && pi --session-dir <dir>
  --session-id <name>`.
- `kill NAME` on a job that is not running prints its status and exits 0.
- `--timeout 0` means no limit.

## Step 8: real-pi verification (2026-10-08)

`skills/multi-agent` is symlinked to `~/.pi/agent/skills/multi-agent`.

### Check 1: `--system-prompt <file> -nc`

Prompts were captured with a test extension in `before_agent_start`, in a
project with `.pi/APPEND_SYSTEM.md` and `AGENTS.md`.

- Replace mode keeps: the profile body, the addendum, the skill list and the
  `<cwd>` block. AGENTS.md is gone (`-nc`). Tool descriptions are not in the
  prompt text in either mode; tools reach the model as tool definitions.
- Finding: any `--append-system-prompt` replaces the `APPEND_SYSTEM.md` file
  pi discovers (project or user), so with the original design every agent
  lost it. Without the flag the file's text appears.
- Fix: the runner no longer passes `--append-system-prompt`. The agent
  extension appends `append-prompt.md` (profile body in append mode, then the
  multi-agent block) to `systemPromptOptions.appendSystemPrompt`.
- Rerun through the script: in both modes the addendum holds the project's
  APPEND_SYSTEM.md, then the profile body (append mode), then the
  `<multi-agent>` block.

### Check 2: `--fork "$PI_SESSION_FILE"` during the caller's `bash` call

A caller pi (RPC, saved session) was told a code word, then ran
`multi_agent.py run --fork ...` with its `bash` tool. The forked agent knew
the code word. pi closes the unfinished tool call in the copy with the
result "No result provided", so the agent sees its own launch command as the
last tool call, already answered. Accepted.

### Check 3: resume

Covered in step 1 and by the tests; resume uses `--session-id`.

### Check 4: approval prompts

Covered in step 1.

### Check 5: herdr pane state

Not observed visually. The herdr extension (`herdr-agent-state.ts`) is
disabled unless `HERDR_ENV=1`, `HERDR_SOCKET_PATH` and `HERDR_PANE_ID` are
all set, and the runner removes all three (tested), so agents send no state
for the caller's pane. A visual check in herdr is left to the user.

### Check 6: start-up time and memory

Model `opencode-go/deepseek-v4.1-flash`, thinking off, prompt "Reply with
just OK.", 16 GB machine:

- Bare `pi --mode rpc` start-up to a `get_state` answer and exit: 0.7 s.
- One background job: 2.8 s from `run` to done; pi peak RSS 223 MB.
- Four background jobs at once: 4.6 s wall; job durations 2-4 s; pi peak RSS
  208-220 MB each. No MCP or other child processes were running under pi.

### Check 7: pi follows its runner, through the finished script

- SIGKILL to a foreground `multi_agent.py run` while its agent runs
  `sleep 6001` with `bash` and has a `bg_task` task `sleep 6002`: within
  0.2 s pi has exited and both sleeps are gone; `status` reports `died`.
- A caller pi runs the foreground command through its `bash` tool, and the
  driver sends RPC `abort` to the caller (the same path as Esc in the TUI):
  the caller's `bash` tool kills the command's process group, and within
  0.2 s the runner, the agent's pi and both sleeps are gone; `status`
  reports `died`.

### Agent session using the skill

Driven over RPC rather than the TUI: a caller pi (default model, HERDR_*
removed) was asked to start two Explore jobs through its background-task
tool, steer one, and later resume one. The run was stopped by the user after
about 30 seconds, before the resume step.

- The caller read SKILL.md, ran `profiles`, and started `ma-a` and `ma-b`
  with `bg_task` as foreground `run` commands. Both task logs began with
  `multi-agent: started job ...`.
- It steered `ma-a` (`steer: queued`); the message shows in `ma-a`'s
  progress log between tool calls, and the agent kept working.
- It waited with `wait ma-a --timeout 120` in a separate tool call.
- `ma-b` finished with status `done`.
- When the caller's session was stopped, `bg_task` sent SIGTERM to `ma-a`'s
  runner, which recorded `stopped by SIGTERM` and status `killed`. No agent
  process was left running.

Still to do in an interactive pi session: `resume` of a finished job and a
`wait` that returns a result to the caller.

## Revision: foreground command, no agent extension (2026-10-08)

See "Revision after step 8" in plan.md for the reasons. Done:

- `scripts/agent-extension.ts` deleted. The runner passes
  `--append-system-prompt <append-prompt.md>`; agents do not get
  `APPEND_SYSTEM.md` (accepted).
- Every runner is detached. A foreground `run`/`resume` passes the read end
  of a pipe to the runner (`__runner JOB_DIR FD`), keeps the write end, waits
  for the runner and prints the result. On SIGTERM/SIGHUP/SIGINT it closes
  the write end and still prints the result. The runner treats end of file
  on the pipe like a stop signal (`stopped: the caller's command ended`,
  status `killed`).
- The runner notices the end of the foreground command through a pipe, not
  by polling its pid as first proposed: end of file arrives at once, works
  for SIGKILL, and cannot be fooled by a reused pid.
- fake-pi: `FAKE_PI_RUN` runs a command as a tool call and kills its process
  group when stdin closes.
- Tests: 50 pass with Python 3.14.7 and 3.9.25. New: runner detached from
  the foreground command, SIGKILL to the foreground command's group, two
  nested foreground runs, `kill` on a foreground run.
- Spec, reference.md updated. SKILL.md needed no change.

Real-pi results (`opencode-go/deepseek-v4.1-flash`, thinking off; the agent
runs `bg_task` `sleep 6002`, then `bash` `sleep 6001`):

- System prompt (project with `.pi/APPEND_SYSTEM.md` and `AGENTS.md`):
  general-purpose has the multi-agent block and AGENTS.md; a profile with an
  append body has the body and the block; Explore has its replace body and
  the block, no AGENTS.md. None has APPEND_SYSTEM.md, as accepted.
- SIGKILL to the foreground command's process group: within 0.2 s the
  runner, pi and both sleeps are gone; status `killed`.
- Caller pi runs the foreground command with `bash`, then gets RPC `abort`
  (Esc in the TUI): within 0.3 s everything is gone; status `killed`.
- Nested chain: a foreground run whose agent runs a second foreground run
  with `bash`; SIGKILL to the outer command's group. Within 0.3 s the outer
  runner and pi, the inner command, the inner runner and pi, and both sleeps
  are gone; both jobs `killed`, and the inner progress log ends with
  `stopped: the caller's command ended`. (A first attempt did not reach the
  kill: the inner agent passed an invalid `timeout` to `bash`; the task text
  now says not to.)
- SIGKILL to the runner itself, the accepted gap: pi exits, the `bash`
  `sleep 6001` survives (the `bg_task` sleep was gone), status `died`. The
  check script killed the leftover sleep.

Check jobs and their session directories were removed afterwards.

## Revision after review (2026-10-08)

See "Revision after review" in plan.md for the decisions and the design,
and the fix summary at the end of review.md.

- Fixed findings 1, 2, 3, 4, 5, 7 and 10, and the SKILL.md part of 9.
  Findings 6 and 8 are not fixed, by decision.
- Tests: 59 pass with Python 3.14.7 and 3.9.25, about 85 s. Run against the
  committed script, the new tests fail for findings 1, 2, 4 and 7. The
  concurrent-resume test passed there too, because the race did not happen
  in that run; it guards the lock from now on.
- Real pi (`opencode-go/deepseek-v4.1-flash`, thinking off), through the
  script: a background run and `wait` returned the result; a foreground
  `resume` continued the conversation (the agent knew its earlier answer);
  `kill` during a `bash` `sleep 6001` took 0.26 s, and pi and the sleep were
  gone; the killed run's output was `(no output)`, not the earlier answer.
  The job and its session were removed afterwards.

## Revision after review, round 2 (2026-10-08)

See "Revision after review, round 2" in plan.md for the decisions and the
design, and the fix summary at the end of review.md.

- Replaced the run-long lock with a short lock around state changes, and
  runner liveness with pid and command-line checks of both the runner and
  pi. Fixed findings 1, 2, 3, 4, 5, 6, 8, 9 and 10.
- Finding 7 accepted: no backward compatibility with jobs of 7d9fc57. (The
  pid check happens to recognize their runners anyway, whose command line
  also starts with `__runner <job dir>`.)
- Tests: 66 pass with Python 3.14.7 and 3.9, about 95 s. Against c9b3c7b,
  the new tests for findings 1 and 2 fail. Without `/proc`, `kill` on a pi
  with a 2 s shutdown now starts 2 `ps`, against 22 with the old polling.
