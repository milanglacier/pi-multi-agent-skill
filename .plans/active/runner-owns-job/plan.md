# Plan: the runner owns its job

Status: done (2026-10-09); see `progress.md`. The current design is in
`../multi-agent-cli-runner/spec.md` (commit fe5d747). This plan changes how
commands decide whether a job is running and how they reach its runner. It
updates that spec in place rather than adding a new one.

## Why

Every command decides on its own whether a job is running. It reads the pid
files, checks each pid's command line through `/proc` or `ps` against a
marker, and probes a short-lived flock. `run`, `resume`, `kill` and `rm` also
write the job's state, so several processes write one job directory, each
from its own reconstruction of the truth.

The two review rounds in `../multi-agent-cli-runner/review.md` show the
cost. About 11 of their 20 findings come from this: recycled pids, a second
runner from `resume`, a lock that covered the runner but not pi,
`runner_pid` written after the lock was released, `kill` recording a new
run as `killed`, `rm` without the lock, file systems without flock, the end
of a run written in three places, and a `ps` per poll. The other findings
(the busy loop, the wrong result text, the depth limit, the docs) would
exist under any design.

Round 1 had the runner hold the lock for its whole life. Round 2 gave that
up because pi can outlive a runner killed with SIGKILL, and a second pi must
not start on its session, so liveness moved to pids. This plan returns to
the run-long lock and accepts that one case instead (see "Accepted limits").

## The design

The rule: **only the runner writes a run's state, and only kernel objects
say whether it is alive.** The files give jobs their names and keep their
results. They never decide liveness.

| Question | Answered by |
|---|---|
| Which jobs exist | Job directories, as now |
| Is a runner alive | An exclusive flock on `<job>/lock`, held by the runner for its whole life |
| How to reach it | A Unix socket whose path is computed from the job directory |
| How did a run end | The result files, written only by the runner, read only when the lock is free |

### The lock

- `run` takes the exclusive lock right after it creates the job directory,
  so a job directory never exists without a lock file. `resume` takes it
  without blocking, retrying for `LOCK_TIMEOUT_S` (5 s) as `job_lock` does
  now. If it stays taken, the job is running (or another command is
  starting it), and the command fails.
- With the lock held, the starter checks the cwd, resets the run's state,
  binds the socket (below), and starts the runner with `pass_fds` holding
  both the locked lock fd and the listening socket. Then it closes its own
  copies. An flock belongs to the open file, so the lock passes to the
  runner with no gap in which another starter could take it.
- The runner keeps both fds until it exits. The kernel releases the lock
  when the runner dies, SIGKILL included.
- pi never receives the lock fd. A spike showed that neither pi's Bun build
  nor Node passes an inherited fd on to the processes they spawn, but that
  is how the runtimes behave today, not a contract. If pi closed the fd, a
  free lock would report a live pi as gone, and `resume` would start a
  second pi on its session without warning.

### Reading a job's state

`job_state(job_dir)` replaces `job_running`, `lock_held` and `job_status`:

1. Try `flock(LOCK_SH | LOCK_NB)`. If it fails, the job is `running`.
2. If it succeeds, read `status` and `exit_code` **while holding the shared
   lock**, then release it. If `exit_code` exists, the status is final:
   `done`, `failed`, `aborted`, `timeout` or `killed`. If not, the runner
   died without recording a result: `died`.

Holding the shared lock while reading makes each reading a consistent
snapshot. No runner can start while a reader holds the shared lock, so the
files cannot change while a reader reads them. A starter that collides with
a reader retries within its 5 s.

`status`, `list`, `result`, `wait`, `kill --all` and `rm` all use it. `wait`
polls it every 0.5 s, as it polls `job_status` now. `list --all` scans
every project as now.

### The socket

- Path: `<state root>/sockets/<first 16 hex digits of sha256(job dir)>.sock`.
  Any command can compute it from the job directory, so no file maps names
  to sockets. The hash fixes the variable part, so the path is the state
  root plus about 30 bytes: about 75 bytes for the user's paths, well under
  the 104-byte `sun_path` limit of macOS (108 on Linux). It exceeds the
  limit only when the state root is longer than about 70 bytes. `run` then
  fails with "the socket path for this job is too long; set XDG_STATE_HOME
  to a shorter path".
- The starter removes a leftover socket file, then binds and listens, all
  while holding the exclusive lock. The socket is listening before the runner
  starts, so a `steer` sent right after `run` waits in the listen backlog
  until the runner accepts it.
- A leftover socket file is never trusted: a connect to it fails with
  `ECONNREFUSED`. `rm` deletes it with the job.
- The socket lives under the state root, not `$XDG_RUNTIME_DIR`. logind
  removes the runtime directory when the user's last login session ends,
  even while a background runner keeps running, which would leave a live
  runner without its socket.

### Requests

One request per connection: the command sends one JSON line and reads one
JSON line back.

| Request | Runner's reply |
|---|---|
| `{"type": "steer", "message": ...}` | After pi answers RPC `steer`: `{"ok": true, "disposition": ...}` or `{"ok": false, "error": ...}` |
| `{"type": "abort"}` | At once: `{"ok": true, "disposition": "requested"}`, then RPC `clear_queue` and `abort` as now |
| `{"type": "kill"}` | At once: `{"ok": true, "disposition": "stopping"}`, then the stop sequence of "Signals and the end of the foreground command" |

After `agent_settled`, or once the runner is finishing, every request gets
`{"ok": false, "error": "job is not running; use resume"}`. The runner
registers the listening socket and each accepted connection in its existing
selector, and keeps accepting while `stop_pi` pumps, so a late request gets
that answer instead of hanging.

On the command side, a refused or missing socket means no runner is
listening. The command then reads the job's state: if the lock is held, the
starter is between taking the lock and binding, so it retries briefly;
otherwise it fails with "job is not running; use resume".

### kill

1. Connect and send `kill`. If no reply arrives within `ACK_TIMEOUT_S`
   (10 s), fail with "the runner of job NAME is not responding" and the
   command a person can run: `pkill -KILL -f '__runner <job dir> '`.
2. After the reply, wait until the lock is free, for at most the runner's
   stop sequence plus a margin (`RUNNER_STOP_GRACE_S`, 20 s). If it is still
   held, fail with the same message.
3. Print the job's state.

`kill` writes nothing. The runner records `killed` itself, as it already
does for signals and the end of the foreground command. Whichever run is
listening when `kill` connects is the run it stops, so a run that starts
after `kill` returns is never marked `killed` by it.

`kill --all` reads each job's state, sends `kill` to every running job in
parallel, and prints each outcome.

### rm

Take the exclusive lock without blocking (with the usual 5 s of retries).
If it is taken, the job is running: fail. Otherwise delete the session
files, the socket file and the job directory, all while holding the lock.

### What stays

- The foreground pipe. A foreground `run` still holds the only write end of
  a pipe whose read end the runner watches, and end of file still stops the
  run. It works and depends only on the kernel.
- pi in its own session, stopped only by the runner closing its stdin, with
  the same escalation in `stop_pi`.
- `record_end`, the result format, profiles, the depth limit, `progress.log`
  and `peek`.

### What goes

- `process_info`, `process_alive`, `process_watch`, `runner_marker`,
  `pi_marker`, `job_running`, `lock_held`, and the `ps` fallback.
- The files `runner_pid`, `pi_pid` and `killed`, and `inbox/` with its
  request and ack files.
- `send_control`'s polling, `check_inbox`, `ack`, `INBOX_POLL_S`.
- `kill_job`'s SIGTERM and SIGKILL escalation against the runner and pi,
  the `runs` comparison, and `PI_KILL_GRACE_S`.
- Running without flock. `run` fails with "flock does not work on STATE
  DIR; set XDG_STATE_HOME to a local file system" (see "Decisions").

## Accepted limits

- **A runner killed with SIGKILL orphans its pi.** Only a person running
  `kill -9` on the runner, or a stuck-runner `pkill` from "kill", singles
  out the runner. A harness that kills a process tree reaches pi too (pi
  is the runner's child), stopping a cgroup kills both, and the OOM killer
  picks pi first because it uses far more memory. When it happens, the job
  reads as `died` once the runner is gone. pi exits on its own when its
  stdin closes, usually on the first failed write to its stdout, and the
  agent's commands may survive, as the spec's "Limits" already says. A
  `resume` in those few seconds would put two writers on one session
  file. reference.md tells the reader to wait a few seconds before resuming
  a `died` job.
- **A stuck runner is not killed automatically.** It needs a bug in our
  own code: every wait in the runner has a limit, and `stop_pi` escalates to
  SIGKILL against pi. `kill` prints the `pkill` command instead. If stuck
  runners show up in practice, the runner can write its pid as a pointer
  that is read only while the lock is held, so it is always current.

## Steps

### Step 1: spike

Throwaway snippets, results recorded in `progress.md` in this directory:

1. A process takes `flock(LOCK_EX)`, passes the fd to a child with
   `pass_fds` and closes its own copy. The lock stays held until the child
   exits, then a `LOCK_SH | LOCK_NB` probe succeeds.
2. A process binds and listens on a Unix socket and passes it to a child
   that starts accepting only after 2 s. A client that connects at once is
   queued, and gets its reply once the child accepts.
3. A client connected to a listener that is SIGKILLed before accepting gets
   `ECONNRESET` or end of file, not a hang.
4. Binding a socket path over the limit fails with a clear `OSError`, so
   `run` can check the length up front and give its own message.

Already done (2026-10-09): a Python process passed a locked fd to
`pi --mode rpc` (1.1.0, Bun build) and closed its own copy, and pi ran
`sleep 30 &` through the RPC `bash` command. The shell and the `sleep` did
not hold the fd, and the lock was free once pi exited. Node 24's
`child_process.spawn` behaved the same. The design does not rely on this;
see "The lock".

### Step 2: spec

Update `../multi-agent-cli-runner/spec.md`:

- "kill", "status, peek, list, profiles, rm": the procedures above.
- "Control inbox" becomes "Control socket": the path rule, the requests and
  replies, and why the socket lives under the state root. Drop the old
  paragraph that rules out a socket because of `sun_path`.
- "Signals and the end of the foreground command": the `kill` request
  joins the signals and the pipe's end of file as a way to stop the run.
- "Limits": the two accepted limits above.
- "Job directory": drop `runner_pid`, `pi_pid`, `killed` and `inbox/`.
- "Job lock" becomes "Liveness": the run-long lock, the handoff and
  `job_state`.
- "Tests": the list in step 4.

### Step 3: script

1. `job_lock` for starters and `rm`, and `job_state` for readers. `run`
   takes the lock right after `mkdir`. Remove the no-flock path.
2. Socket path, bind, and the handoff in `start_runner`
   (`__runner JOB_DIR LOCK_FD SOCK_FD [CALLER_FD]`).
3. In `Runner`: register the listening socket, accept, read one request
   per connection, reply; keep `pending_acks` mapping a steer's RPC id to
   its connection. Replace `check_inbox`. Handle `kill` like a stop signal.
4. Commands: `steer`, `abort` and `kill` over the socket; `kill --all`;
   `rm`; `status`, `list`, `wait` and `result` through `job_state`.
5. Remove everything in "What goes".

### Step 4: tests

Remove the tests of the removed mechanisms:
`test_running_by_runner_or_pi`, `test_process_alive_without_proc`,
`test_process_watch`, `test_process_watch_without_proc`,
`test_kill_ignores_recycled_pids`, `test_kill_without_proc_starts_few_ps`,
`test_kill_leaves_a_new_run_alone`, and
`test_pi_that_outlives_its_runner_keeps_the_job_running`.

Keep the rest. They test behavior from outside and should pass unchanged,
apart from hand-made job directories in `test_died_detection` and
`test_read_only_commands_on_hand_made_jobs`, which need a lock file.

Add:

- A `steer` sent at once after a background `run` is queued and answered.
- `steer`, `abort` and `kill` on a finished job fail with "job is not
  running; use resume", also with a leftover socket file after SIGKILL to
  the runner.
- SIGKILL to the runner: the job reads as `died` once the runner is gone,
  `wait` returns, and a later `resume` starts normally after binding over
  the leftover socket.
- A stuck runner (SIGSTOP): `kill` fails within about 10 s with the `pkill`
  hint, and the job still reads as `running`.
- `list` while a `resume` starts: every row is `running` or the previous
  final status, never `died`.
- Concurrent `resume`s start one run (kept from the current suite).
- `rm` of a running job fails; `rm` removes the socket file.
- `run` with an `XDG_STATE_HOME` long enough to put the socket path over
  the limit fails with the "too long" message and leaves no job directory.
- `kill` on a foreground run: the foreground command prints `killed` and
  exits with code 1 (kept, now through the socket).

### Step 5: docs

- reference.md: the "State" table, the "Lifetime" bullets on SIGKILL to the
  runner, and "Troubleshooting": `died` (wait a few seconds before
  `resume`), "the runner is not responding", and the flock error.
- SKILL.md: check that nothing it says changes. The commands and their
  output stay the same.

### Step 6: real pi

Through the script with a real model: background `run` and `wait`, `steer`,
`abort`, `kill` during a `bash` `sleep`, foreground `resume`, two nested
foreground runs stopped by SIGKILL to the outer command's group, and SIGKILL
to a runner followed by `status` and a `resume` after pi has exited. Check
with `ps` that no process is left except in the accepted case.

## Done when

- `python3 -m unittest discover tests` passes on Python 3.9 and the current
  Python.
- `grep` finds no `runner_pid`, `pi_pid`, `/proc`, `ps` or `inbox` in
  `multi_agent.py`.
- The real-pi checks in step 6 pass.

## Decisions

The user decided these on 2026-10-09.

1. **No running without flock.** Every Unix system has `flock(2)`; whether
   it works depends on the file system. It works on local file systems and
   on FUSE file systems, where the kernel handles the lock locally when the
   file system does not. It can fail on NFS without a lock daemon
   (`ENOLCK`) and on some SMB/CIFS mounts. The lock now decides liveness,
   so `run` fails with a clear error that points to `XDG_STATE_HOME`
   instead of going on without it.
2. **A socket path over the limit is an error**, with no fallback to
   `/tmp`. With the hash, only a state root longer than about 70 bytes
   reaches the limit.
3. **No backward compatibility.** Jobs of fe5d747 that are still running
   have no socket and their runners do not hold the lock, so after the
   upgrade they read as `died` and `kill` cannot reach them. Stop them
   before upgrading.
