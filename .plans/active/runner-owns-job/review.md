# Review: the runner owns its job (commit c826b58)

Scope: `git diff main...feat/runner-owns-job`, which is the single commit
c826b58: `multi_agent.py`, `tests/test_multi_agent.py`, `tests/fake-pi`,
reference.md, the appendix to `../multi-agent-cli-runner/spec.md`, and this
plan's `plan.md` and `progress.md`. The suite passes: 64 tests in 124 s on
the system Python 3. The findings come from reading the code; none was
reproduced with a stub pi.

Severity:

- **P1**: wrong results or lost control of a job in normal use. Fix before
  merging.
- **P2**: a real failure under conditions that are uncommon but plausible,
  or a guarantee the design claims and does not keep. Fix soon.
- **P3**: a narrow race, a robustness gap, a test gap or stale text.

## Overall

The change does what the plan set out to do. Liveness now comes from one
kernel object, the run-long flock, and only the runner writes a run's state.
The handoff is correct: the starter takes `LOCK_EX`, passes the locked fd and
the bound socket through `pass_fds`, and closes its own copies, so no other
starter can slip in. pi never sees either fd, because the runner's `Popen`
closes every descriptor that is not in `pass_fds`. Binding the socket before
the runner starts gives the listen backlog for free, and the runner answers
late requests with `NOT_RUNNING` while `stop_pi` pumps. The removed code
(pid files, `/proc` and `ps`, the inbox, `kill`'s escalation) was the source
of most findings in the two earlier review rounds, and none of it is left.

The departures from the plan that `progress.md` lists (start order, the
`[_]_runner` pattern, `kill` on a finished job, the runner removing its own
socket) are all improvements.

I found no P1 or P2. The findings are narrow races, robustness gaps, test
gaps and stale plan text.

## Findings

### 1. [P3] A write to pi's stdin has no limit, so a pi that hangs at startup with a large prompt is not cleaned up

`multi_agent.py:824-833` (`send`), `:1086` (the prompt), `:1659-1687`
(`kill_job`).

pi's stdin is an ordinary blocking pipe, and `send()` writes and flushes it
from the runner's only thread. The prompt is written in one call before the
event loop starts. The runner blocks in that `write` only if the prompt is
larger than the pipe buffer (64 KiB on Linux) and pi does not read it. A
normal startup only delays the runner until pi reads. A small prompt never
blocks, however long pi hangs, and the timeout and `kill` then work as
usual. So the case that matters is a large prompt to a pi that hangs at
startup and never recovers, which is a bug in pi that the user would
notice. While the runner is blocked there, it never reaches its loop, so
`--timeout` does not fire, requests on the socket go unanswered, and a
SIGTERM to the runner is recorded but not acted on.

On `main`, `kill` escalated to SIGTERM and then SIGKILL against the runner
and pi. Now `kill` prints the `pkill` hint, which stops the runner and
leaves the hung pi for the user to find and kill. It also means the plan's
"every wait in the runner has a limit" (in "Accepted limits") is not
strictly true.

A deadlock with a healthy pi (the runner blocked writing stdin while pi
waits for its stdout to drain) was considered and is unlikely. Until the
write blocks, the runner reads stdout continuously, so it needs a single
write over 64 KiB (only a very large `steer -`), pi pausing its stdin reads
for another reason at that moment, pi then writing over 64 KiB to stdout,
and pi stopping its stdin reads while stdout is backed up, which is not
confirmed.

Possible fix, if it ever matters: make pi's stdin non-blocking and keep an
outgoing buffer that the selector drains with `EVENT_WRITE`, so every write
goes through the event loop and the deadlines still apply. pi's `rpc.md`
also asks clients to "honor stdin backpressure when writing commands".
Otherwise, correct the "every wait has a limit" sentence in the plan.

### 2. [P3] `result`, `wait` and the foreground command read the result outside the snapshot

`multi_agent.py:525-550` (`job_state`), `:1344`, `:1555-1557`,
`:1569-1574`, `:627-639` (`format_result`).

`job_state` reads `status` under the shared lock, then releases it. Its
callers then read `output.txt`, `started_at`, `ended_at` and `model` through
`format_result`, with no lock. If a `resume` starts in between, it deletes
`output.txt`, `ended_at` and `model` under its exclusive lock. The caller
then prints `(no output)` with the previous `status: done` and exits 0.
`status` has the same gap for `exit_code`, `runs` and the timestamps.

The plan says readers get "a consistent snapshot". That holds only for the
status word. It needs two commands working on one job at the same moment,
so it is rare.

Fix: have `job_state` return the files the callers need, read while it
holds the shared lock, or let callers hold the shared lock while they format
the result.

### 3. [P3] A new job directory exists for a moment without a held lock

`multi_agent.py:1468-1490` (`cmd_run`), `:494-502` (`job_lock`).

`run` creates the job directory with `mkdir`, and only then creates `lock`
and takes it. Between the two steps, a concurrent `job_state` finds either no
lock file or a free one, and no `exit_code`, so it reports `died`. A `list`
or `kill --all` that runs during another `run` can show or skip a brand new
job as `died`. The plan says "a job directory never exists without a lock
file". `test_list_while_a_resume_starts` covers `resume` but not a new
`run`.

Fix: build the job directory under a temporary name in `jobs/`, create and
lock `lock` there, and `rename` it into place. `list` and `find_job` must
then skip the temporary names. Or treat a directory without `runs` as
`running` in `job_state`, since only a starter that is still setting it up
leaves one.

### 4. [P3] `resume` can lock a new file in a directory that `rm` is deleting

`multi_agent.py:494-495` (`job_lock`), `:1830-1843` (`cmd_rm`).

`job_lock` opens `lock` with `O_CREAT`. `rm` holds the lock and then runs
`shutil.rmtree`. If a concurrent `resume` calls `job_lock` after `rmtree`
has unlinked `lock` but before it removes the directory, `O_CREAT` creates
a new file with a new inode. `resume` locks it at once, since `rm` holds the
old inode, and the inode check passes because the path names the new file.
The mutual exclusion is gone. Depending on which files `rmtree` has removed
by then, `resume` either fails with "no readable job.json" and leaves a
directory that holds only `lock` (which `list --all` shows as `died`), or it
starts a run in a directory that is being deleted, whose session file is
already gone.

The window is a few milliseconds and needs `rm` and `resume` on the same
job at once.

Fix: have `rm` first `rename` the job directory to a hidden name in `jobs/`
while it holds the lock, then delete that. A `resume` that opens the old
path afterwards gets `ENOENT`, and one that is already waiting fails the
inode check. Alternatively, only `run` passes `O_CREAT`, and the other
callers treat a missing lock file as "was removed" (hand-made test jobs
would then need a lock file, which `make_job` already creates).

### 5. [P3] The socket path depends on how `XDG_STATE_HOME` is spelled

`multi_agent.py:553-561` (`socket_path`), `:233-235` (`state_root`).

The socket name hashes the job directory as a string, and `state_root` uses
`abspath`, which does not resolve symlinks. A command whose
`XDG_STATE_HOME` names the same directory through another path (a symlink,
or `/tmp` and `/private/tmp` on macOS) finds the same job directory, but
computes a different socket path. `steer`, `abort` and `kill` then see a
held lock with no socket, retry for 5 s, and fail with "does not accept
requests", without the `pkill` hint.

Fix: hash `os.path.realpath(job_dir)`. The job directory exists whenever a
socket path is computed, so `realpath` is well defined.

### 6. [P3] `connect` has no timeout

`multi_agent.py:1588-1590` (`connect_runner`).

`sock.settimeout(ACK_TIMEOUT_S)` is set in `send_request`, after
`connect`. On Linux a blocking `connect` to a Unix socket whose backlog is
full waits until the listener accepts. With a stopped or blocked runner (see
finding 1), the backlog of 64 fills after a few dozen retries of `steer` or
`kill` from a looping agent, and every later command hangs instead of
printing the "not responding" message.

Fix: call `settimeout` before `connect` and turn a timeout there into the
same "not responding" error.

### 7. [P3] Test gaps

- Nothing tests a request that arrives while the runner is in `stop_pi`.
  The plan relies on the runner pumping requests there so they get
  `NOT_RUNNING` instead of hanging. A `steer` sent during
  `FAKE_PI_SLOW_EXIT` would cover it.
- `kill_leftovers` (`tests/test_multi_agent.py:98-108`) reads
  `/proc/PID/cmdline`. Without `/proc` (macOS) the `open` fails, the
  `except OSError` skips the pid, and leftover runners and fake-pis survive
  a failed test. The plan's "Done when" does not name a platform, but the
  script supports macOS (`SUN_PATH_MAX`).
- Findings 1 and 3 have no test.

### 8. [P3] Stale text in the plan

- `plan.md:3-6` says the plan "updates that spec in place rather than
  adding a new one". By the user's later decision, the spec got an
  appendix instead (`progress.md`, "Spec"). Update the sentence, or add a
  note that points to `progress.md`.
- `plan.md:79-80` says `list --all` "scans every project as now". The code
  never did. `progress.md` records this, but the plan still says it.
- `plan.md:184-188` ("every wait in the runner has a limit") is not true
  while finding 1 stands.

## Already decided, not repeated as findings

- An `abort` that reaches pi before the agent starts is ignored, and the run
  ends `aborted` at the 30 s settle deadline (`progress.md`, step 6).
- SIGKILL to a runner orphans its pi for a few seconds, and a `resume` in
  that time puts two writers on one session (plan, "Accepted limits").
- No backward compatibility with jobs started by fe5d747, and no running
  without `flock` (plan, "Decisions").
- The spec's main sections still describe fe5d747; the appendix points to
  this plan.
