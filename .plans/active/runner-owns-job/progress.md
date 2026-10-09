# Progress: the runner owns its job

## Step 1: spike (2026-10-09, Linux 6.18, Python 3.14.7 and 3.9)

Throwaway snippets in a scratch directory. Every result was the same on
both Pythons.

1. **Lock handoff.** A process took `flock(LOCK_EX)` on a file, started a
   child with the fd in `pass_fds`, and closed its own copy. A
   `LOCK_SH | LOCK_NB` probe from a third descriptor failed while the child
   lived and succeeded once it had exited.
2. **Listen backlog.** A process bound and listened on a Unix socket,
   passed it to a child that called `accept` only after 2 s, and closed
   its own copy. A client's `connect` returned at once, its request was
   queued, and its reply arrived after 2.02 s.
3. **Listener SIGKILLed before `accept`.** The client had connected and
   sent its request. `recv` failed with `ECONNRESET` at once, so the client
   does not hang. A later `connect` to the leftover socket file failed with
   `ECONNREFUSED`, and one to a missing file with `ENOENT`.
4. **Path over the limit.** On Linux a 107-byte path binds; 108 bytes and
   more fail in `bind` and in `connect` with
   `OSError('AF_UNIX path too long')`. That is a clear error, but `run`
   checks the length up front anyway so it can give its own message. The
   limit includes the trailing NUL, so the longest path is 107 bytes on
   Linux (108-byte `sun_path`) and 103 on macOS (104).

Found on the way: this session's scratch directory is about 130 bytes
long, so a socket under it cannot bind at all. Test state directories come
from `tempfile.mkdtemp` under `/tmp`, which keeps their socket paths at
about 80 bytes.

## Steps 2-5: spec, script, tests, docs (2026-10-09)


- **Script.** `job_lock(job_dir, busy_message)` returns the locked fd for
  starters and `rm`; `job_state` replaces `job_running`, `lock_held` and
  `job_status`. `run` takes the lock right after `mkdir`, and its socket
  path is checked before anything is created. `start_runner` passes the
  lock fd and the listening socket (`__runner JOB_DIR LOCK_FD SOCK_FD
  [CALLER_FD]`) and the starter closes its copies. The runner registers the
  listening socket and each connection in its selector, keyed by tag, and
  its loop now blocks until the next event or deadline instead of waking
  every 0.5 s. `steer`, `abort` and `kill` go over the socket. Everything
  in the plan's "What goes" is removed.
- **Spec.** Not updated in place, by the user's decision: the sections
  listed in step 2 still describe fe5d747. A short final section, "Later
  change: the runner owns its job", summarizes the new design and points
  to this plan and this file.
- **reference.md.** `kill` output, the `died` status, "Lifetime", "State",
  and Troubleshooting entries for `died` (wait before `resume`), "the
  runner is not responding", the flock error and the socket path error.
  The `job NAME is busy` entry is gone, since the error no longer happens
  in practice. **SKILL.md** needed no change.
- **Tests.** The 8 tests listed in step 4 are removed. fake-pi records its
  parent pid, which is the runner's, so tests no longer read
  `runner_pid`. Added: `test_socket_path`,
  `test_lock_of_a_job_removed_while_waiting`, `test_socket_path_too_long`,
  `test_list_while_a_resume_starts`, `test_rm`, `test_stuck_runner`, and a
  longer `test_sigkill_to_runner` (leftover socket ignored by `steer`,
  `abort` and `kill`; a `resume` binds over it and takes a steer).
  `test_steer_and_not_running` already sends its steer right after a
  background `run`, so it covers the listen backlog.
- `python3 -m unittest discover tests`: 64 tests pass on Python 3.14.7
  (125 s) and 3.9.25 (119 s). `grep -E 'runner_pid|pi_pid|/proc|\bps\b|inbox'`
  finds nothing in `multi_agent.py`.

### Where the implementation differs from the plan

- **Order in the starter:** check the cwd, bind the socket, then reset the
  run's state. With the plan's order (reset, then bind), a failed bind
  would leave a run with no runner, which reads as `died`. With this
  order, the previous result stays intact.
- **The `pkill` hint is `pkill -KILL -f '[_]_runner <job dir> '`**, with
  regex characters in the path escaped. The plan's literal `__runner ...`
  also matches the command line of a shell that runs `pkill`. That
  happens with `sh -c` and with an agent's `bash` tool, so `pkill` would
  kill the shell. `test_stuck_runner` runs the printed command through
  `/bin/sh -c` and checks that it stops the runner.
- **`kill` on a job that is not running** still prints `status: X (not
  running)` and exits 0, as before. It does not fail with "job is not
  running; use resume", which the plan's test list suggests. The plan
  also says the command output stays the same, `test_kill_name` checks
  this behavior, and "use resume" is wrong advice for `kill`.
- **`kill --all` exits 1** if any job could not be stopped (before, it
  always exited 0). Only a stuck runner causes that now.
- **The runner removes its socket file** when it exits normally, after it
  answers every request still open. It holds the lock until then, so no
  new run can have bound that path. So a socket file is left only after
  SIGKILL.
- **A held lock with no socket.** `connect_runner` retries for 5 s, then
  fails with "the runner of job NAME does not accept requests". This
  happens only if someone deletes a live runner's socket file.
- **A job directory without a lock file** (only hand-made ones) is read
  from its files.

### Notes

- A `kill` to a stuck (SIGSTOPped) runner times out, but its request stays
  queued in the socket. If the runner continues, it reads the request and
  stops the run. That is what the caller asked for, so it is left as is.
- `list --all` covers the current project only, as before. The plan says
  it "scans every project as now"; the code never did that, and this
  change does not either.

## Step 6: real pi (2026-10-09, pi 1.1.0)

Model `opencode-go/deepseek-v4.1-flash`, thinking off, state in a scratch
`XDG_STATE_HOME`. Processes were found with `pgrep -f` on the runner's
`__runner <job dir> ` and pi's `--append-system-prompt <job dir>/...`.

- **Background `run`, `steer` sent at once, `wait`:** the steer was
  answered `queued` while the runner was still starting, the answer had
  both parts, and the job ended `done` after 3 s. No socket file was left.
- **`abort`** while the agent ran `bash` `sleep 6013`: answered at once;
  `wait` returned `aborted` 0.6 s after the abort; the sleep was gone.
- **`kill`** while the agent ran `bash` `sleep 6014`: returned `status:
  killed` after 0.27 s; the runner and the sleep were gone; the progress
  log ends with `stopped by kill`.
- **Foreground `resume`** of the first job: the agent quoted its earlier
  answer; `runs: 2`.
- **Nested foreground runs**, the outer agent running the inner `run`
  with `bash`, the inner agent running `sleep 6016`. SIGKILL to the outer
  command's process group: within 0.25 s both runners, both pis, the inner
  command's shell and the sleep were gone. Both jobs were `killed`, each
  with `stopped: the caller's command ended`.
- **SIGKILL to a runner** while its agent ran `bash` `sleep 6017`:
  `status` 0.07 s later said `died`, and pi had already exited. The sleep
  survived, which is the accepted limit; the check killed it. The socket
  file was left. `steer` and `abort` failed with "job is not running; use
  resume", and `kill` printed `status: died (not running)`. A foreground
  `resume` bound over the leftover file, the agent still knew the code
  word from its first run, and the socket file was gone afterwards.
- A final `pgrep` found no runner, pi or sleep left. The check jobs and
  their session directory were removed.

Found on the way, not changed: an `abort` that reaches pi right after the
`prompt`, before pi has started the agent, is ignored. pi then runs the
task, and the runner stops it at the 30 s settle deadline with status
`aborted`. This happened because the check script's first `pgrep` matched
its own shell and sent the abort too early. The inbox design had the same
race: its runner checked the inbox right after sending the prompt.

Not fixed, by user decision as of now. Sending an `abort` to an agent
that has only just started is rare in practice, and the cost is small:
the task runs until the settle deadline and still ends `aborted`. Watch
for it in real use; if it shows up, a possible fix is for the runner to
hold an abort until pi has answered the `prompt` command.

## Done when

- [x] `python3 -m unittest discover tests` passes on Python 3.9 and the
  current Python.
- [x] `grep` finds no `runner_pid`, `pi_pid`, `/proc`, `ps` or `inbox` in
  `multi_agent.py`.
- [x] The real-pi checks in step 6 pass.
