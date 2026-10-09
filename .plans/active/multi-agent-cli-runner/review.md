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

# Review, round 2: the fixes (commit c9b3c7b)

Scope: the diff from 7d9fc57 to c9b3c7b, which covers the script, fake-pi,
the tests, SKILL.md, reference.md and the plan files. The suite passes:
59 tests in about 88 s on Python 3.14.7.

## Overall

The round-1 fixes hold. `kill` now waits longer than the runner's stop
sequence, the timeout loop sleeps, the output comes from this run's own
`message_end`, and a failed start keeps or records the result. The job lock
is the right mechanism, and it removes the pid and command-line checks for
the runner.

The new problems are at the edges of the lock. Some code still reads or
writes job state without holding the lock, and the lock covers the runner
but not pi. Items 1 and 2 were reproduced with fake-pi by a throwaway test
that is not in the repo. The others come from reading the code.

## Findings (most severe first)

### 1. `kill` can mark a new run as `killed` while it keeps running

`multi_agent.py:1522-1526`.

`kill_job` writes `status: killed` and `exit_code` when it finds no
`exit_code` after the runner is gone. It does this without the lock. Suppose
the runner exits and another caller's `resume` takes the lock and starts a
new run before `kill_job` reaches that check. The new run has no `exit_code`
yet, so `kill_job` writes `killed` over it. `job_status` trusts `exit_code`
and never checks the lock, so `status`, `wait` and `list` report `killed`
while the new agent runs. A second `kill` answers "not running" and cannot
stop it. A third `resume` fails with "is running".

Reproduced: `kill` printed `status: killed`, the status file said `killed`,
and the lock was held by the new runner.

Fix: take the lock with `lock_job` before the final writes, and skip them if
another run holds it. The same applies to `rm` (item 5).

### 2. The lock covers the runner but not pi, so `resume` can start a second pi on a live session

`multi_agent.py:542-556`, `559-566`; spec "Job lock".

pi does not inherit the lock, so the lock is free as soon as the runner
dies. pi can outlive it. This happens after the runner is SIGKILLed, by
`kill`'s own escalation or by the OOM killer, and also while pi is still
shutting down or is wedged. `kill_job` itself waits up to 10 s for pi after
the runner has gone. During that time `job_status` says `died` or `killed`,
and `resume` starts a second pi on the same `--session-id`. The spec's
"two runners never write one session" holds, but two pis can still write
it.

Reproduced with fake-pi under `FAKE_PI_IGNORE_EOF=1`: after SIGKILL to the
runner, `status` said `died`, `resume` returned 0, and both pis were alive.

Fix: in `job_running`, also count the job as running while `pi_pid` is
alive and its command line contains the job's `append-prompt.md`. The same
check is already in `kill_job`. Do not hand the lock to pi: its tool
processes would inherit the descriptor, and background tasks would hold the
lock long after pi exits.

### 3. `runner_pid` is written by the starting command after it drops the lock

`multi_agent.py:1130-1136`, `1499-1503`.

`start_run` closes its copy of the lock and only then writes `runner_pid`.
This causes two problems:

- If the command dies between `Popen` and the write, for example from
  SIGKILL or a closed terminal, `runner_pid` is never written. `kill_job`
  waits 10 s for it and then never signals the runner. It then SIGKILLs pi
  directly, which leaves the agent's `bash` and `bg_task` processes running,
  the leak this design exists to prevent. It also writes `killed` under a
  live runner, which later overwrites it with `failed`.
- If the runner ends at once and another `resume` starts a run before the
  late write, `runner_pid` names the old runner. `kill` then signals a pid
  that may have been reused, because the lock only proves that some runner
  is alive.

Fix: have the runner write `os.getpid()` to `runner_pid` itself, while it
holds the lock and before it starts pi. Then `start_run` needs no write
after `Popen`.

### 4. SKILL.md's "shorter than the tool's" timeout is not enough

`SKILL.md:81-83`; spec "SKILL.md contents".

After `--timeout` fires, the runner waits up to `SETTLE_TIMEOUT_S` (30 s)
for pi to settle and then up to 15 s more to stop pi. With `--timeout 590`
under a 600 s tool timeout, the tool kills the foreground command first.
The job then ends as `killed`, not `timeout`, and the agent's tool call
returns no result. That is what the advice is meant to prevent.

Fix: tell the agent to leave a margin of at least a minute, for example
"`--timeout` at least 60 seconds shorter than the tool's".

### 5. `rm` deletes a job without the lock

`multi_agent.py:1667-1675`.

`cmd_rm` checks `job_status` and then deletes the session file and the job
directory. A `resume` that takes the lock between the check and the
`rmtree` starts a run whose job directory and session file are then
deleted. The runner fails on its next state write, pi keeps running without
a session file, and nothing can find the job to stop it.

Fix: take `lock_job` in `rm` and hold it until the directory is gone.

### 6. Without working `flock`, `run` gives a traceback and running jobs read as `died`

`multi_agent.py:532-540`, `549-553`.

`lock_job` catches only `BlockingIOError`. On a file system where `flock`
fails with another error, such as `ENOLCK` or `EOPNOTSUPP` on some network
or FUSE mounts under `XDG_STATE_HOME`, every `run` ends with a traceback and
leaks the descriptor. On such a file system, `job_running` returns `False`
for every job, so `status` and `wait` report a running job as `died`.

Fix: in `lock_job`, catch `OSError`, close the descriptor and raise a
`CommandError` that names the state directory.

### 7. Jobs started by the previous version cannot be stopped

`multi_agent.py:542-548`, `1495-1497`.

A job started by 7d9fc57 has no `lock` file. `job_running` returns `False`,
so such a job reads as `died` while it runs. `kill` answers "not running"
and signals nothing, and `resume` starts a second runner on its session.
This affects only jobs that were running during the upgrade.

Fix: either note it in progress.md as accepted, or fall back to the old pid
check when the `lock` file does not exist.

### 8. The end-of-run state is written in three places

`multi_agent.py:1054-1065`, `1194-1199`, `1522-1526`.

`Runner.conclude`, the `Popen` failure path in `start_runner` and `kill_job`
each write `output.txt`, `ended_at`, `status`, `exit_code` and a
`run N ended` progress line. They write different subsets in the same
order, and the order matters because `exit_code` must be written last. A
helper such as `record_end(job_dir, status, output)` would keep the order in
one place. It would also make the lock check from item 1 a single change.

### 9. The `ps` fallback starts a process on every poll

`multi_agent.py:196-208`, `1517`.

Without `/proc`, `process_alive` runs `ps` for each call. `kill_job` polls
pi through `wait_while` every 0.1 s for up to 10 s, which starts up to 100
`ps` processes per job, and `kill --all` does this in parallel for each
job. Polling with `os.kill(pid, 0)` and checking the command line once at
the start would do the same job. The marker only guards against reuse,
which cannot happen while the pid stays alive.

### 10. Stale plan text

plan.md line 20 still says "Nothing is committed unless the user asks". The
round-1 table marks item 10 as fixed, and both commits exist. Remove the
line or reword it.

## Test gaps

- No test covers `kill` racing a `resume` (item 1), or `resume` while pi
  outlives a dead runner (item 2). Both can be written with fake-pi: wrap
  `ma.wait_while` to run a `resume` once the runner has gone, and use
  `FAKE_PI_IGNORE_EOF` with SIGKILL to the runner.
- `test_sigkill_to_runner` checks the `died` status but not that a `resume`
  is refused while the old pi is still alive.

## Round 2: decisions and fix summary

Decisions are recorded in plan.md ("Revision after review, round 2"). The
lock no longer lasts a whole run: it serializes state changes only, and
liveness comes from the runner's and pi's pids.

| # | Decision | What changed |
|---|---|---|
| 1 | Fixed | `kill` reads the pids and `runs` under the lock, and records `killed` under the lock again only if `runs` is unchanged and neither the runner nor pi is alive. It leaves a run started meanwhile alone and reports it as running. |
| 2 | Fixed | `job_running` counts the job as running while its runner or its pi is alive, each checked against its command line. |
| 3 | Fixed | `start_run` writes `runner_pid` before it releases the lock, and the runner writes its own pid first thing. The 10 s start grace in `kill` is gone. |
| 4 | Fixed | SKILL.md and the spec ask for a `--timeout` at least 60 seconds shorter than the tool's, and say why. |
| 5 | Fixed | `rm` checks and deletes the job under the lock. A command that waited for the lock and finds the lock file gone or replaced fails with "job NAME was removed". |
| 6 | Fixed | When `flock` fails with an error other than `EWOULDBLOCK`, commands go on without the lock. Liveness no longer depends on it. |
| 7 | Accepted | No backward compatibility. |
| 8 | Fixed | `record_end` writes the end of a run in one place, with `exit_code` last. |
| 9 | Fixed | `process_watch(pid, marker)` checks the command line once and returns a check that only tests that the pid exists (`os.kill(pid, 0)`, plus the zombie state from `/proc` where there is one). A pid cannot be reused while its process lives, so the marker needs no second look. `kill` polls the runner and pi with it, so without `/proc` it starts about two `ps` per job instead of one every 0.1 s. One-off checks in `status`, `list` and `wait` still use `process_alive`. |
| 10 | Fixed | The stale line is removed from plan.md. |

Tests (66, up from 59; pass on Python 3.14.7 and 3.9, about 95 s):

- New: `kill` with a `resume` once the runner is gone (finding 1); a pi
  that outlives its SIGKILLed runner keeps the job running, `resume` is
  refused, and `kill` stops it (finding 2); liveness through the runner's
  or pi's command line, and not through a job whose name extends this one's;
  `job_lock` on a removed job; `process_watch` with and without `/proc`
  (one `ps`, then none while polling); `kill` without `/proc` on a pi with
  a 2 s shutdown starts fewer than 10 `ps` (2 now, 22 with the old polling).
- `test_died_detection` checks the short lock: a held lock makes the job
  read as running for others, not for the holder, and a second taker fails
  with "is busy".
- Against c9b3c7b, the tests for findings 1 and 2 fail.
- Not new: with two suites running at once (3.14 and 3.9 in parallel),
  `test_signals_stop_the_run` and `test_signals_to_the_runner` can fail on
  the slower run. c9b3c7b fails them the same way under the same load; both
  pass when the suite runs alone.

Docs: spec ("resume", "kill", "Finishing a run", "Limits", the job
directory table, "Job lock", the SKILL.md timeouts, "Tests"), reference.md
(`died`, lifetime, `lock`, the pid files, two troubleshooting entries),
SKILL.md (timeout margin), plan.md (round-2 revision, stale scope line).
