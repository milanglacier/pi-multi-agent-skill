# Plan: faster tests

Status: done (2026-10-10), on branch `faster-tests`; see `progress.md`.

## Why

`python3 -m unittest discover tests` takes about 124 s for 64 tests. The
script itself is fast. Measured against `tests/fake-pi`, a whole foreground
`run` (the command, its runner and fake-pi) takes about 0.13 s, and `list`,
`status`, `result`, `steer` and `rm` take about 0.04 s each. Almost all of
the 124 s is waiting.

### Where the time goes

Measured on 2026-10-10 by timing each test and every command the tests ran
(a shim around the script logged each command's wall time).

Seven tests take 92 s, because each waits out one of the script's fixed
timeouts at its production value:

| Test | Time | What it waits for |
|---|---|---|
| `test_timeout_when_pi_does_not_settle` | 31 s | The 1 s `--timeout`, then `SETTLE_TIMEOUT_S` (30 s) for pi to settle after the abort |
| `test_kill_escalates_when_pi_ignores_everything` | 15 s | `PI_EXIT_GRACE_S` (10 s), then `PI_TERM_GRACE_S` (5 s) before SIGKILL |
| `test_kill_waits_for_a_slow_shutdown` | 12 s | `FAKE_PI_SLOW_EXIT=12`, chosen to outlast `PI_EXIT_GRACE_S` |
| `test_stuck_runner` | 10 s | `ACK_TIMEOUT_S` (10 s): the stopped runner's socket accepts the connection, but no reply comes |
| `test_resume_or_rm_of_running_job_is_refused` | 10 s | `LOCK_TIMEOUT_S` (5 s), once for `resume` and once for `rm` |
| `test_concurrent_resumes_start_one_run` | 7 s | `LOCK_TIMEOUT_S` for the losing `resume`, plus a 1 s sleep |
| `test_rm` | 6 s | `LOCK_TIMEOUT_S` for `rm` of a running job |

The other 57 tests take 32 s, of which 27.5 s is spent in commands:

| Cost | Time | Cause |
|---|---|---|
| `wait` | 16.4 s over 18 calls | Fixed `FAKE_PI_DELAY`s of 2 to 3 s that keep a run alive while the test steers it, detaches from it or kills its runner, plus `wait`'s 0.5 s poll interval: a `wait` for a run that ends after 0.05 s still takes 0.54 s |
| `run` | 8.3 s over 55 calls | Real work, about 0.15 s each, three Python processes per run; plus `test_runner_is_detached_from_the_caller` (2 s delay) and `test_timeout` (1 s `--timeout`) |
| Other commands | 2.8 s over ~45 calls | Real work, about 0.04 s each |
| Test code | 4 s | `wait_until` polling, `time.sleep` in tests, process start-up |

## Scope

The script's behavior stays as it is. The only changes to `multi_agent.py`
are a test-only environment variable that scales its timing constants, and
naming the `wait` poll interval as a constant so the scale covers it. With
the variable unset, every constant keeps its current value. Everything else
happens in `tests/fake-pi` and `tests/test_multi_agent.py`.

## The changes

### 1. Shorter timeouts in tests

The script reads `MULTI_AGENT_TIME_SCALE` (a positive number, default 1) and
multiplies these constants by it at import time:

- `ACK_TIMEOUT_S`
- `SETTLE_TIMEOUT_S`
- `PI_EXIT_GRACE_S`
- `PI_TERM_GRACE_S`
- `RUNNER_STOP_GRACE_S` (derived from the two above, so it follows)
- `LOCK_TIMEOUT_S`
- `WAIT_POLL_S`, a new constant for the 0.5 s that `cmd_wait` now writes as
  a literal

`BACKGROUND_TIMEOUT_S` stays as it is; tests that need a run timeout pass
`--timeout`. An invalid value makes every command fail with an error.

The variable is for tests, like `MULTI_AGENT_PI_BIN`. It is documented in the
spec next to `MULTI_AGENT_PI_BIN`, not in SKILL.md or the references.

The tests set it to 0.1:

- The test module sets `os.environ["MULTI_AGENT_TIME_SCALE"]` before it
  loads the script, so the in-process `ma` module has the scaled constants,
  and the tests compute their expectations from them (for example
  `ma.PI_EXIT_GRACE_S`) instead of hard-coding seconds.
- `Base.setUp` drops every `MULTI_AGENT_` variable from the environment it
  passes to commands, so it adds `MULTI_AGENT_TIME_SCALE` back explicitly.

With 0.1 the constants become: lock 0.5 s, ack 1 s, settle 3 s, pi exit
grace 1 s, SIGTERM grace 0.5 s, runner stop grace 2 s, wait poll 0.05 s.

Test changes that follow:

- `test_timeout_when_pi_does_not_settle`: `--timeout 0.2`. Sample the
  runner's CPU time over 1 s instead of 3 s and assert under 0.2 s; a busy
  loop would burn about 1 s. The sample must end before the 3 s settle
  deadline, or the runner may have exited and `/proc/<pid>/stat` is gone.
- `test_kill_waits_for_a_slow_shutdown`: fake-pi's `FAKE_PI_SLOW_EXIT`
  accepts fractional seconds and writes one event every 0.1 s instead of
  every second. The test sets it to `PI_EXIT_GRACE_S + PI_TERM_GRACE_S / 2`
  (1.25 s): longer than the exit grace, so `kill` must keep reading pi's
  stdout past it, and shorter than the exit grace plus the SIGTERM grace,
  because pi ignores SIGTERM and must finish before SIGKILL. It asserts
  that `kill` took longer than `PI_EXIT_GRACE_S`.
- `test_stuck_runner` and `test_rm`: compute their "fails within" bounds
  from the scaled constants.
- `test_timeout`: `--timeout 0.2`, and expect "timeout after 0.2s" in the
  progress log.

The margins are 0.25 s at their tightest (the slow-shutdown window). That is
far above the scheduling jitter of an idle machine. If the test is flaky on
a loaded one, raise the scale to 0.2 rather than special-casing the test.

`test_list_while_a_resume_starts` reads `job_state` in a tight loop while a
`resume` takes the lock, and the starter now retries for 0.5 s instead of
5 s. Each read holds the shared lock only for a moment and the starter
retries every 0.05 s, so it still gets the lock, but this is the test to
watch for flakiness.

Saves about 75 s: about 80 s in the seven slow tests, less what they still
wait at the shorter values, and about 4 s of `wait` polling elsewhere.

### 2. Release runs explicitly instead of sleeping

Several tests start a run with a `FAKE_PI_DELAY` of 0.5 to 3 s only to keep
it alive while they act on it. The delay must be long enough on a slow
machine, so it is mostly idle time on a fast one.

fake-pi gets `FAKE_PI_HOLD=<path>`: the run does not settle until that file
exists. It checks every 0.02 s and handles `steer`, `abort` and other
commands meanwhile, like the existing delay loop. A test helper returns the
env entry and a `release()` that creates the file. Each held run gets its own
path, so releasing one run does not release later ones in the same test.

Tests that switch from a delay to a hold:

| Test | Delay now | Released after |
|---|---|---|
| `test_runner_is_detached_from_the_caller` | 2 s | the session checks |
| `test_detach_wait_and_result` | 2 s | the `wait --timeout 0.2` check |
| `test_survives_the_callers_exit` | 2 s | the runner-alive check |
| `test_steer_and_not_running` | 3 s | the steer is acknowledged |
| `test_sigkill_to_runner` (the resume) | 3 s | the steer is acknowledged |
| `test_list_while_a_resume_starts` | 0.5 s per turn | the starter has exited and `list` shows the job |

Tests that use a 60 s or 120 s delay as "runs until killed" keep it; they
end through `kill` or a signal and never wait out the delay.

Saves about 13 s.

### 3. Optional: run the tests in parallel

Each test has its own temporary state, project and record directories, so
the tests are independent. Running them in parallel (for example with
`uv run --with pytest --with pytest-xdist pytest -n auto tests`) would bring
the wall time down to roughly the longest test plus start-up. This adds a
dependency to the test command, and parallel load eats into the timing
margins of change 1, so it stays optional and out of the default command in
the README.

## Expected result

| After | Seven slow tests | Other 57 tests | Total |
|---|---|---|---|
| Now | 92 s | 32 s | 124 s |
| Change 1 | about 12 s | about 28 s | about 40 s |
| Changes 1 and 2 | about 12 s | about 14 s | about 26 s |
| With change 3 | | | about 5 to 8 s |

The remaining sequential time is about 11 s of real work (starting three
Python processes per `run`, two per other command), the waits that the
slowest tests exist to exercise at their scaled values (3 s settle, 1.5 s
kill escalation, 1 s ack, 0.5 s per lock refusal), and smaller sleeps such
as the 1 s check in `test_concurrent_resumes_start_one_run` and the 0.3 s
pauses in `test_signals_stop_the_run`.

## Steps

Each step ends with the full suite passing.

1. **Time scale** (change 1): `MULTI_AGENT_TIME_SCALE` and `WAIT_POLL_S` in
   the script, the test-module setup, fake-pi's fractional
   `FAKE_PI_SLOW_EXIT`, and the test changes listed above.
2. **Holds** (change 2): `FAKE_PI_HOLD` in fake-pi, documented in its
   docstring; the helper; the tests listed above.
3. **Spec**: in `../multi-agent-cli-runner/spec.md`, `MULTI_AGENT_TIME_SCALE`
   next to `MULTI_AGENT_PI_BIN`, and the "Tests" section. The user-facing
   timeouts in `references/lifecycle.md` and `references/troubleshooting.md`
   do not change.
4. **Measure** again with per-test timings and record the numbers in
   `progress.md`.

## Done when

- `python3 -m unittest discover tests` passes on Python 3.9 and the current
  Python, in under 30 s.
- Five consecutive runs pass, to catch timing flakiness from the shorter
  timeouts.
- The diff of `multi_agent.py` contains only the time scale and the
  `WAIT_POLL_S` constant.
- No test sleeps or sets `FAKE_PI_DELAY` only to keep a run alive while it
  acts on it.

## Out of scope

Two script changes were considered and left out, because they change the
script's behavior and add state handling to it, and the time scale already
recovers almost all of their test-time savings (about 2 s in total):

- `job_lock` could refuse at once when a runner holds the lock exclusively,
  instead of retrying for `LOCK_TIMEOUT_S`. Users wait 5 s for `resume` or
  `rm` of a running job to fail.
- `wait` could block on the job's lock instead of polling, and return as
  soon as the runner exits. Users wait up to 0.5 s after a job ends.

## Open questions

1. **Time scale: one factor or one variable per timeout?** Recommended: one
   factor. Per-timeout variables would let the settle test run in under 1 s,
   but add seven test-only knobs for about 2 s.
2. **Parallel test runner (change 3): add it now, document it as optional,
   or skip it?** Recommended: document it as optional after changes 1 and
   2, once the sequential run is fast and stable.
