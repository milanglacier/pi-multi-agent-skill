# Review: multi-agent skill and CLI runner (commit 7d9fc57)

Scope: the whole initial commit, which includes the spec, plan, progress notes,
`multi_agent.py`, SKILL.md, reference.md, the profiles, `tests/fake-pi` and
`tests/test_multi_agent.py`. The suite passes: 50 tests in about 42 s on
Python 3.14.

## Overall

The design holds up. Running each agent as a `pi --mode rpc` subprocess
removes the SDK wiring that pi-subagents has to maintain. The
foreground-command-plus-detached-runner split fixes the root cause of the
EPIPE leak. That is better than the extension that patched `process.exit`.
Several decisions were spiked against real pi before they were written down:
`--session-id` instead of `--session`, `clear_queue` before `abort`, and the
no-UI env var. The code follows the spec closely, and the tests cover the
lifetime paths that matter most, including SIGKILL to the caller's group and
nested foreground runs.

The problems are mostly at the edges of the lifetime design. Two timers
disagree, one loop busy-waits, pid liveness is weak without `/proc`, and one
result path can report text from the wrong conversation. Items 1 and 2 were
reproduced with a stub pi. The others come from reading the code and pi's
RPC docs.

## Findings (most severe first)

### 1. `kill` SIGKILLs the runner during pi's shutdown, which brings the leak back

`multi_agent.py:1424`, constants at lines 51-56.

`kill_job` waits `KILL_GRACE_S` (10 s) after SIGTERM and then sends the runner
SIGKILL. The runner's own stop path takes up to 2 s
(`SIGNAL_TEXT_TIMEOUT_S`), plus 10 s for pi to exit after its stdin closes
(`PI_EXIT_GRACE_S`), plus 5 s after SIGTERM (`PI_TERM_GRACE_S`). So a pi
whose shutdown takes more than about 10 s loses its stdout reader in the
middle of `session_shutdown`. pi 1.1.0 then calls `exit(1)` on the next
write, and the agent's `bash` tree and `bg_task` tasks survive. The design
exists to prevent exactly this.

Reproduced: a stub pi that writes one event per second for 12 s after stdin
EOF got EPIPE at step 10, with `kill` returning after 11 s.

The spec has the same conflict. "kill" step 2 sends SIGKILL after 10 s, while
"Limits" says "nothing in the normal flow sends SIGKILL to a runner".

Fix: make the runner's grace in `kill` longer than the runner's whole stop
sequence, for example `SIGNAL_TEXT_TIMEOUT_S + PI_EXIT_GRACE_S +
PI_TERM_GRACE_S + 3`. The runner already escalates against pi itself, so
`kill` only needs SIGKILL for a runner that is truly wedged.

### 2. The runner busy-loops for up to 30 s after a timeout

`multi_agent.py:937-941`.

Once the deadline passes, `deadline` stays in the `(deadline,
self.stop_deadline)` list. `wait = min(wait, deadline - now)` is then
negative, so `pump(0)` returns at once and the loop spins until pi settles or
`SETTLE_TIMEOUT_S` runs out. Reproduced with a stub pi that ignores `abort`:
the runner used 74-85% CPU. With 4 parallel jobs that time out together, 4
cores are pinned. The tests miss it because fake-pi settles as soon as it
receives `abort`.

Fix: skip `deadline` once `self.timing_out` is set. Only `stop_deadline`
matters after that point.

### 3. Without `/proc`, `kill` can signal unrelated processes

`multi_agent.py:195-196, 510, 1419-1435`.

`process_alive` skips the marker check when `/proc/self` is missing (macOS),
and only Windows is refused. Suppose a job's runner dies without recording a
result, through `kill -9`, the OOM killer or a reboot, and its pid is later
reused. `status`, `wait` and `list` then report the job as `running` for as
long as the new process lives. `kill NAME` or `kill --all` sends that process
SIGTERM, then SIGKILL, and then SIGKILLs whatever holds the old `pi_pid`.
Even on Linux, the marker `multi_agent` matches any `multi_agent.py`
process, such as a `wait` command polling in a loop.

Fix at the root: have the runner hold an `fcntl.flock` on a file in the job
directory for its whole life. The job is running exactly while the lock is
held. That is portable and immune to pid reuse, and the same lock also
solves item 5. Signal pids only after confirming that the lock is held.

### 4. Result text can come from an earlier run, or from the caller's conversation

`multi_agent.py:955-958, 981-985`.

`finish` and `finish_killed` take the output from RPC
`get_last_assistant_text`, which returns the last assistant message in the
session, not the last one from this run. If a `resume` is killed, times out,
or fails before the agent writes a new assistant message, `output.txt` holds
the previous run's final answer, labelled with the new status. With `run
--fork` the session starts as a copy of the caller's, so an early `killed`,
`timeout` or `failed` returns the caller's own last message as the agent's
result. The caller cannot tell either case apart from real output.

Fix: the runner already handles every `message_end`, so it can keep the
text of the latest assistant message of this run. Use that, and fall back to
RPC only when this run has produced a message. This also removes the
duplicated request blocks in `finish` and `finish_killed`.

### 5. `resume` can start two runners on one session

`multi_agent.py:1322-1330`.

The `job_status(...) == "running"` check and `start_run` are not atomic. Two
`resume` calls for the same name, from two callers or from a retried tool
call, both pass the check. Both start pi on the same `--session-id` and
write to the same session file, and the second overwrites `runner_pid`, so
the first runner can no longer be found. The flock from item 3, or an
`O_EXCL` lock file, closes this race.

### 6. `resume` bypasses the depth limit

`multi_agent.py:1324, 595`.

`cmd_resume` calls `check_depth()` and discards the result. The agent then
gets the depth stored at creation (`job["depth"]`). Consider a depth-1 agent
B that resumes job A, which the user's agent created at depth 1. A then runs
at depth 1, not 2, and can start a depth-2 child. The chain becomes root → B
→ A → C, one level past `MULTI_AGENT_MAX_DEPTH`. `parent` is also left at the
original creator.

Fix: store `check_depth()`'s value and the caller's `MULTI_AGENT_JOB` in
`job` before `start_run`.

### 7. A failed runner start deletes the previous result and leaves a fake `running`

`multi_agent.py:1088-1117`.

`start_run` deletes `exit_code`, `output.txt` and the other previous-run
files, and writes `status: running`, before `subprocess.Popen` of the runner.
If that Popen raises, for example because the job's `cwd` was deleted
before a `resume`, the user gets a Python traceback instead of a
`CommandError`. The previous run's result is gone, and the job shows
`running` for 10 s and then `died`.

Fix: check that `job["cwd"]` exists before touching state. Catch the Popen
error, write status `failed` with the error, and raise a `CommandError`.

### 8. The multi-agent block costs every agent its `APPEND_SYSTEM.md`, which is avoidable

`multi_agent.py:607`; spec "Starting pi".

The runner always passes `--append-system-prompt`, and that is the only
reason agents lose `APPEND_SYSTEM.md`. That includes `general-purpose`, the
default, whose body is empty. The spec weighed two alternatives: copying
pi's lookup, which is trust-gated per `configuration.md` and `security.md`,
and an extension. It did not consider a third: send the multi-agent block as
a preamble of the first RPC `prompt` message. The block would then stay in
the session for later resumes. Pass `--append-system-prompt` only for an
append-mode profile with a non-empty body. `general-purpose` and replace-mode
profiles would then keep the user's `APPEND_SYSTEM.md` with no extra
machinery.

### 9. Foreground runs have no default timeout, but SKILL.md assumes one

`SKILL.md:62-65`; spec "run" options.

SKILL.md tells the agent to run long or parallel work as a foreground `run`
under the harness's background tool. It also says to "disable [the tool's
timeout] or make it longer than the job's `--timeout`". A foreground run has
no `--timeout` by default, so following that advice leaves a looping agent
bounded only by the end of the caller's session, and it keeps spending
tokens until then.

Fix: either give foreground runs a default too (for example 3600 s, as in
the background), or have SKILL.md tell the agent to pass `--timeout` with
the background-tool pattern.

### 10. Test gaps and stale plan text

- `test_runner_is_detached_from_the_caller` (test file line 546) asserts on
  pi's session id (`start["sid"]`), never on the runner's. It would still
  pass if the runner shared the caller's session.
- No test has a pi that does not settle after `abort`, which would have
  caught item 2 and exercised the `stop_deadline` path. No test has a pi
  whose shutdown takes more than 10 s, which would catch item 1.
  `test_kill_escalates_when_pi_ignores_everything` checks only the final
  status, not that the runner kept reading pi's stdout until pi exited.
- Runner signal handling for SIGHUP and SIGINT has no test. Only SIGTERM is
  tested, through `kill`. `test_signals_stop_the_run` signals the foreground
  command, not the runner.
- plan.md still says "Draft; not yet approved" (line 6), "stays a plain
  directory" (line 14) and "Nothing is committed unless the user asks"
  (line 17). It is now committed as a submodule. Its "Done when" requires the
  interactive step-8 session to work, but progress.md line 180 still lists
  `resume` and a `wait` that returns a result as not done. Either finish
  those checks or record them as open, and update or move the plan before it
  is called done.

## Smaller notes (not blocking)

- `project_slug` collides for different paths: `/a/my-proj`, `/a/my_proj`
  and `/a/my/proj` all map to the same slug. Those projects then share one
  job namespace, `list` and `kill --all`. The spec accepts this to match the
  handoff skill, but a short hash suffix would make it safe.
- `Args` treats any one-line prompt or steer message that starts with `-` as
  an option and exits with code 64. Recommending `--` or `-` with a heredoc
  in reference.md would cover it.
- The runner writes the prompt to pi's stdin with a blocking write before it
  starts reading stdout. A prompt over the pipe buffer (64 KiB) can block the
  runner while pi starts up, and signals and the caller's EOF are not handled
  during that time. pi's RPC docs ask clients to honor stdin backpressure.
- The progress line `run N started` reads `runs` again from disk. That is
  harmless, and the value is already known in `start_run`.

## Decisions and fix summary

Decisions are recorded in plan.md ("Revision after review").

| # | Decision | What changed |
|---|---|---|
| 1 | Fixed | `kill` waits `RUNNER_STOP_GRACE_S` (20 s) for the runner before SIGKILL, longer than the runner's own stop sequence for pi (10 s + 5 s). |
| 2 | Fixed | After the timeout fires, the event loop waits on the settle deadline only, so it sleeps instead of spinning. |
| 3 | Fixed | Runner liveness is an `flock` on `<job>/lock`, held by the runner for its whole life; the pid and command-line check for the runner and the 10 s start grace are gone. `kill` signals the runner only while the lock is held, and pi only while its command line contains the job's `append-prompt.md` path. `process_alive` reads `/proc` or falls back to `ps`. |
| 4 | Fixed | The runner keeps the text of this run's latest assistant message from `message_end`, with the rules of pi's `get_last_assistant_text`, and no longer asks pi. `request()` and the two text timeouts are removed. |
| 5 | Fixed | `start_run` takes the job lock before it changes any state and hands it to the runner, so a second `run`/`resume` fails with "job NAME is running". The command closes its copy right after the runner starts, also in the foreground. |
| 6 | Won't fix for now | Rare; needs one agent to resume a job another agent started. |
| 7 | Fixed | `start_run` checks the job's cwd before it changes any state. A failed runner start is recorded as `failed` with the error and raised as a `CommandError`. |
| 8 | Won't fix | Valid but rare; agents keep losing `APPEND_SYSTEM.md`, as decided in the earlier revision. |
| 9 | Design kept; SKILL.md fixed | Foreground runs still have no default timeout. SKILL.md has a "Timeouts" section: foreground has none unless `--timeout` is given and stops with its command; background stops after an hour; under a background tool with its own timeout, pass a shorter `--timeout`. |
| 10 | Fixed | See below. |

Tests (59, up from 50; pass on Python 3.14.7 and 3.9.25, about 85 s):

- fake-pi gained `FAKE_PI_IGNORE_ABORT`, `FAKE_PI_SLOW_EXIT` (writes during
  a slow shutdown and exits with reason `epipe` if a write fails), and
  session replies, so `get_last_assistant_text` returns the session's last
  reply as pi does.
- `test_runner_is_detached_from_the_caller` checks the runner's own session
  while it runs.
- New: CPU use of the runner while pi does not settle after a timeout, and
  the `timeout` result after the settle deadline; `kill` on a 12 s shutdown
  that ignores SIGTERM; SIGTERM, SIGHUP and SIGINT to the runner; output of a
  resume and a fork that end before their first message; concurrent resumes;
  a resume whose cwd is gone; a runner that cannot start; `kill` with
  recycled runner and pi pids; `process_alive` through `ps` (including a
  zombie); the lock in `job_status`.
- Against the committed script, the new tests fail for findings 1, 2, 4 and
  7. The concurrent-resume test did not catch finding 5 there, because the
  race is timing-dependent; it guards the lock going forward.

Docs: spec ("resume", "kill", "Finishing a run", "Timeout", "Signals",
"Limits", the job directory table, a new "Job lock" section, "Tests"),
reference.md (the `lock` file, empty output, two troubleshooting entries),
SKILL.md ("Timeouts"), plan.md (status, scope, the revision and open
items).

Checked with real pi through the script: background run and `wait`,
foreground `resume` with the earlier conversation, and `kill` during a
`bash` sleep (0.26 s, no process left, output `(no output)`).

Still open: step 8's interactive check, where an agent uses the skill to
`resume` a finished job and `wait` for its result.
