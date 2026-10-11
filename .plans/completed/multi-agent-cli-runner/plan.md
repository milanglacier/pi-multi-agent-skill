# Plan: implement the multi-agent skill and CLI runner

The design is in `spec.md` in this directory. This plan covers the order of
work, what each step produces, and how each step is verified.

Status: steps 1-8 and the revision after step 8 are implemented and
committed (7d9fc57). The review in `review.md` led to a second revision
(c9b3c7b), and its second round to a third, both described at the end of
this file. One check from step 8 is still open (see "Open items").

## Scope

- Everything under `pi-multi-agent-skill/`: the script, SKILL.md, reference.md,
  the two built-in profiles, README.md, LICENSE and the tests.
- No changes to other submodules. pi-minimal-permission-system and
  pi-background-tasks already have what the spec relies on.
- `pi-multi-agent-skill/` is a submodule of the monorepo, with its own
  GitHub repo. Replacing pi-subagents in the user's setup is a separate step
  after this plan.

## Step 1: spike the risky assumptions with real pi

These checks decide parts of the design, so they run before the script is
written. Each one uses a throwaway Python snippet that starts
`pi --mode rpc` and speaks the protocol directly.

1. **pi follows its runner** (spec check 7). Start pi in its own session
   with piped stdin and stdout, prompt it to run `sleep 600` with `bash` and
   to start a `bg_task` task, then SIGKILL the parent snippet. Verify that pi
   exits, that the `sleep` process tree is gone, and that the task is
   stopped. If pi exits on EPIPE before shutdown completes, write the
   stdout-guard extension (a few lines that add an error handler to
   `process.stdout`), load it with `-e`, and repeat.
2. **Resume by name** (spec check 3). Start with `--session-dir D
   --session-id N`, end the run, then start with `--session-dir D --session
   N` and confirm that the conversation continues.
3. **Dialog answer** (spec check 4). Configure an `ask` rule, run with
   `PI_MINIMAL_PERMISSION_SYSTEM_NONINTERACTIVE=1`, and confirm that no
   `extension_ui_request` arrives and the tool result carries the no-UI
   reason. Repeat without the variable and answer with `cancelled: true` to
   confirm that the agent continues.

Record the results in a `progress.md` next to this plan. If a result
contradicts the spec, update the spec before going on.

## Step 2: scaffolding

- Create the layout from spec "Repository and layout".
- README.md: what the skill is, how to install it with the symlink, the
  credit to pi-subagents for the `Explore` prompt.
- LICENSE: MIT, as in the other projects.
- `tests/fake-pi`: a Python script that implements the subset of the RPC
  protocol the runner uses (`prompt`, `steer`, `abort`,
  `get_last_assistant_text`, the events in spec "Protocol handling") and the
  test controls listed in spec "Tests". Write it before the runner, so each
  later step can be tested as it is built.

## Step 3: profiles

In `multi_agent.py`:

- Frontmatter parser with the subset in spec "Fields".
- Discovery across the three directories, whole-file replacement and
  `enabled: false`.
- Conversion of a profile plus `run` options into pi arguments, including
  model and thinking precedence.
- The `profiles` command.

Write `profiles/general-purpose.md` and `profiles/Explore.md`. Take the
`Explore` prompt from pi-subagents `src/default-agents.ts`.

Tests: discovery, replacement, parsing, argument building, precedence.

## Step 4: job state

- Project slug and logical cwd, job and session directories.
- Name validation and default names.
- Atomic writes for every state file.
- Reading a job's state, including the `died` check.
- `status`, `peek`, `list`, `result` and `rm`, which only read or delete
  state.

Tests: name rules, slug, `died` detection, the read-only commands on
hand-made job directories.

## Step 5: the runner

- Start pi with the arguments from step 3, in its own session, with the
  child environment from spec "Starting pi".
- Event loop over pi's stdout, splitting records on `\n` only, with a timer
  for the inbox and the timeout.
- Record handling from spec "Protocol handling", including dialog answers.
- Finishing a run, timeout, and signal handling as specified.
- The control inbox on the runner side.

Tests: the runner against fake-pi for each record type, dialog answers,
timeout, each signal, early end of pi's stdout, U+2028 in records, and
SIGKILL to the runner with fake-pi reporting that its stdin closed.

## Step 6: commands

- `run` in the foreground and with `--background`, `resume`, and `--fork`.
- `wait` and the result format and exit codes.
- `steer` and `abort` on the command side of the inbox.
- `kill NAME` and `kill --all`.
- The depth limit.

Tests: the remaining items in spec "Tests", including a background job that
survives its caller's exit.

## Step 7: SKILL.md and reference.md

- SKILL.md as listed in spec "SKILL.md contents". Keep it short; it is read
  on every use.
- reference.md with the full option tables, status meanings, the state
  layout and troubleshooting.
- Check that neither file names a particular extension or harness tool, or
  mentions permission modes.

## Step 8: real-pi verification

- Symlink `skills/multi-agent` to `~/.pi/agent/skills/multi-agent`.
- Run spec checks 1, 2, 5 and 6, and repeat check 7 through the finished
  script.
- In an interactive pi session, have the agent use the skill for a parallel
  `Explore` task through `bg_task`, steer one job, and resume a finished one.
- Record results in `progress.md` and update the spec where real behavior
  differs.

## Done when

- `python3 -m unittest discover tests` passes with Python 3.9 and the
  current Python.
- All seven real-pi checks pass or have a recorded, accepted outcome.
- The interactive session in step 8 works without manual cleanup of
  processes or state.

## Revision after step 8: foreground command, no agent extension

Steps 1-8 shipped with `scripts/agent-extension.ts`, an extension the runner
loaded into every agent with `-e`. It did two things, and both are replaced.

### Why

1. **APPEND_SYSTEM.md.** `--append-system-prompt` makes pi skip the
   `APPEND_SYSTEM.md` it would otherwise load (real-pi check 1), so the
   extension added the job's prompt text itself. Decision: pass
   `--append-system-prompt` and accept that agents do not get
   `APPEND_SYSTEM.md`. The file is rarely used; the alternatives were the
   extension, or copying pi's lookup of the file into the script, which
   would break quietly when pi changes it.
2. **Clean shutdown after the runner dies.** The foreground `run` process was
   the runner. A `bash` tool abort sends SIGKILL to the command's process
   group, so the runner died, pi saw its stdin close while nothing read its
   stdout, and pi 1.1.0 exited on the first EPIPE before its shutdown ran.
   The agent's `bash` command and `bg_task` tasks survived. In a nested chain
   (main pi → A → B) the survivor was B's runner, so agent B kept working,
   spending tokens and changing files, with nobody reading its result. The
   extension fixed this by deferring pi's `process.exit(1)` until dispose,
   which replaces `process.exit` and depends on pi internals.

### Options weighed for 2

- Keep the extension: works, but fragile across pi updates.
- Write pi's stdout to a file the runner tails: no EPIPE, but polling,
  partial lines and a growing file. Not tried.
- Background runs only: no crash, but only because nothing stops the job.
  Esc and the end of the caller's session would no longer stop agents,
  `kill` would not reach the jobs a job started, and every job would need a
  `run` plus `wait` calls.
- **Chosen: foreground command plus detached runner.** Every runner is
  detached. A foreground `run`/`resume` starts it, keeps the only write end of
  a pipe whose read end the runner watches, waits for it and prints the
  result. When the foreground command ends, SIGKILL included, the runner
  reads end of file and stops pi through its stdin while it keeps reading
  pi's stdout, so pi's shutdown completes. Nested foreground runs stop level
  by level.

Trade-offs accepted: two processes per foreground run, and SIGKILL to the
runner itself (`kill -9`, OOM killer) still leaves the agent's commands
running, with status `died`. No harness path sends SIGKILL to the runner.

### Steps

1. Spec: "Foreground and background runs", "Concepts", "Starting pi",
   "Signals and the end of the foreground command", "Lifetime" (with the
   reasoning above), the job directory table, tests and check 7.
2. Script: always start a detached runner; the foreground command passes the
   pipe's read end (`__runner JOB_DIR FD`), forwards SIGTERM/SIGHUP/SIGINT by
   closing its write end and prints the result; the runner treats end of file
   on that pipe like a stop signal. Pass `--append-system-prompt`; delete
   `scripts/agent-extension.ts`.
3. fake-pi: `FAKE_PI_RUN` runs a command as a tool call in its own process
   group and kills that group when stdin closes, as pi's shutdown does.
4. Tests: runner detached from the foreground command, SIGKILL to the
   foreground command's group, two nested foreground runs, `kill` on a
   foreground run, the new pi arguments.
5. reference.md: lifetime, `killed` and `died`, `APPEND_SYSTEM.md`.
6. Real-pi checks: the system prompt of each profile; SIGKILL to the
   foreground command's group; RPC abort of a caller pi's `bash` call; a
   nested chain of two foreground runs; SIGKILL to the runner itself, to
   confirm the accepted gap. Record results in `progress.md`.

Done when the suite passes on Python 3.9 and the current Python, and the
real-pi checks show no process left after each path except the accepted one.

## Revision after review

`review.md` lists ten findings on commit 7d9fc57. The user's decisions:

| # | Finding | Decision |
|---|---|---|
| 1 | `kill` SIGKILLs the runner while pi is still shutting down | Fix |
| 2 | The runner busy-loops after a timeout | Fix |
| 3 | Without `/proc`, `kill` can signal unrelated processes | Fix |
| 4 | Result text can come from an earlier run or the caller | Fix |
| 5 | `resume` can start two runners on one session | Fix |
| 6 | `resume` bypasses the depth limit | Won't fix for now: rare, and needs an agent to resume a job another agent started |
| 7 | A failed runner start deletes the previous result | Fix |
| 8 | The multi-agent block costs agents their `APPEND_SYSTEM.md` | Won't fix: valid, but rare; the decision in "Revision after step 8" stands |
| 9 | Foreground runs have no default timeout | Keep the design: a foreground run stops with its caller, so it needs no limit of its own. Fix SKILL.md, which is ambiguous about which runs have a timeout |
| 10 | Test gaps and stale plan text | Fix |

### Design of the fixes

- **Job lock (3, 5).** Each job directory has a file `lock`. `run` and
  `resume` take an exclusive `flock` on it before they change any state, and
  hand the open descriptor to the runner, which holds it until it exits. A
  job is running exactly while the lock is held. This replaces the pid and
  command-line check for the runner, and the 10-second start grace. A second
  `resume` fails to take the lock, so two runners can never share a session.
  `kill` signals pids only while the lock shows the job running, and checks
  pi's pid against the job directory in pi's command line, through `/proc`
  or `ps`.
- **Kill grace (1).** `kill` waits for the runner longer than the runner's
  own stop sequence (pi's exit grace plus its SIGTERM grace, plus a margin)
  before it sends SIGKILL. The runner already escalates against pi.
- **Timeout loop (2).** Once the timeout has fired, the event loop waits on
  the settle deadline only.
- **Output of this run (4).** The runner keeps the text of this run's latest
  assistant message from `message_end` events, with the same rules as pi's
  `get_last_assistant_text`, and no longer asks pi for it. A run that
  produces no message has no output.
- **Failed start (7).** `start_run` checks that the job's cwd exists before
  it changes any state. If starting the runner fails anyway, the run is
  recorded as `failed` with the error, and the command fails with a message.
- **SKILL.md (9).** State the timeout rules plainly: a foreground run has
  no timeout unless `--timeout` is given and stops when its command ends; a
  background run stops after 3600 seconds by default. Under a background
  tool with its own timeout, either pass `--timeout` shorter than the tool's
  or disable the tool's timeout and accept that the job runs until it
  finishes or the caller's session ends.
- **Tests (10).** The detachment test checks the runner's session. fake-pi
  gains a pi that ignores `abort`, a pi that keeps writing during a slow
  shutdown, and a session that records replies, so `get_last_assistant_text`
  behaves like pi's. New tests: CPU use after a timeout, a slow shutdown
  under `kill`, SIGHUP and SIGINT to the runner, output of an early-ended
  resume and fork, concurrent resumes, a resume whose cwd is gone, and
  `kill` with a recycled runner pid.

## Revision after review, round 2

The second part of `review.md` lists ten findings on commit c9b3c7b. Most
come from the lock being held for a whole run while some code read or wrote
job state without it, and from the lock covering the runner but not pi. The
user's decisions:

| # | Finding | Decision |
|---|---|---|
| 1 | `kill` can mark a new run as `killed` | Fix, through the short lock |
| 2 | `resume` can start a second pi while the old one outlives its runner | Fix: a live pi keeps the job running |
| 3 | `runner_pid` is written after the lock is released | Fix, through the short lock |
| 4 | "Shorter than the tool's" timeout is not enough | Fix SKILL.md: at least 60 seconds shorter |
| 5 | `rm` deletes a job without the lock | Fix, through the short lock |
| 6 | Without working `flock`, `run` fails and jobs read as `died` | Fix: go on without the lock |
| 7 | Jobs of the previous version cannot be stopped | Accepted: no backward compatibility |
| 8 | The end of a run is written in three places | Fix: one helper |
| 9 | The `ps` fallback starts a process on every poll | Fix: check the command line once, then poll the pid |
| 10 | Stale plan text | Fix |

### Design: a short lock, liveness by pid

- **Liveness.** A job is running while its runner or its pi is alive. Each
  pid counts only while its command line names the job: `__runner <job
  dir>` for the runner, the job's `append-prompt.md` path for pi.
  `process_info` joins the arguments with spaces and ends the command line
  with a space, so a marker matches whole arguments, and job `foo` does not
  match the runner of job `foo-2`. This fixes 2.
- **Short lock.** `job_lock` takes an exclusive `flock` on `<job>/lock` only
  around state changes: `run` and `resume` check liveness, reset the state,
  start the runner and write `runner_pid` under it (3); `kill` reads the
  pids and the `runs` count under it, and records `killed` under it again
  only if `runs` has not changed and nothing is alive (1); `rm` checks and
  deletes under it (5). The runner no longer receives the lock, and also
  writes its own pid. A command that waited for the lock and finds the lock
  file replaced or gone fails with "job NAME was removed". The lock waits up
  to `LOCK_TIMEOUT_S` (5 s), then fails with "job NAME is busy".
- **Readers.** `job_status` counts the job as running while another command
  holds the lock, because that command may be starting a run.
- **No flock (6).** When `flock` fails with an error other than
  `EWOULDBLOCK`, commands go on without the lock.
- **`process_watch` (9).** Checks a pid's command line once and returns a
  check that only tests that the pid exists, plus the zombie state where
  there is `/proc`. `kill` polls the runner and pi with it.
- **`record_end` (8).** Writes `output.txt` (optional), `ended_at`, `status`
  and, last, `exit_code`, then the `run N ended` progress line. The runner,
  the failed runner start and `kill` use it.

### Open items

- Step 8's interactive session did not reach `resume` of a finished job or a
  `wait` that returns a result to the caller. Both are covered by tests
  against fake-pi and by direct runs of the script with real pi, but not yet
  by an agent using the skill.
