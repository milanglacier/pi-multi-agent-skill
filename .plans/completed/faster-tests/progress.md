# Progress: faster tests

## Steps 1-4 (2026-10-10, Linux 6.18, Python 3.14 and 3.9)

All four steps are done as planned, with one difference:

- **Invalid `MULTI_AGENT_TIME_SCALE`.** The constants are computed when the
  script is imported, before `main` and its usage-error handling run, so an
  invalid value raises `SystemExit` with
  `multi-agent: error: MULTI_AGENT_TIME_SCALE needs a positive number, got: X`
  and exit status 1 instead of 64. Only tests set the variable.

What changed:

- **Script.** `time_scale()` reads the variable; `ACK_TIMEOUT_S`,
  `SETTLE_TIMEOUT_S`, `PI_EXIT_GRACE_S`, `PI_TERM_GRACE_S`,
  `RUNNER_STOP_GRACE_S` (including its 5 s margin) and `LOCK_TIMEOUT_S` are
  multiplied by it, and `WAIT_POLL_S` names `wait`'s 0.5 s interval. With
  the variable unset, the constants are 10, 30, 10, 5, 20, 5 and 0.5, as
  before.
- **fake-pi.** `FAKE_PI_HOLD=<path>` keeps the run from settling until the
  file exists. `FAKE_PI_SLOW_EXIT` accepts fractional seconds and writes an
  event every 0.1 s.
- **Tests.** The module sets the scale to 0.1 before it loads the script;
  `Base.hold()` returns the env entry and a `release()`. The tests listed in
  the plan use the scaled constants or a hold. `test_timeout` and
  `test_timeout_when_pi_does_not_settle` use `--timeout 0.2`; the latter
  samples CPU over 1 s and asserts under 0.2 s.
- **Spec.** `MULTI_AGENT_TIME_SCALE` next to `MULTI_AGENT_PI_BIN`, and the
  "Tests" section.

## Results

| | Before | After |
|---|---|---|
| Whole suite, Python 3.14 | 124 s | 26.5 to 26.9 s (5 consecutive runs, all passing) |
| Whole suite, Python 3.9 | | 21.4 s, passing |

Slowest tests after the change:

| Test | Before | After |
|---|---|---|
| `test_timeout_when_pi_does_not_settle` | 31.2 s | 3.3 s |
| `test_concurrent_resumes_start_one_run` | 6.9 s | 1.9 s |
| `test_kill_escalates_when_pi_ignores_everything` | 15.3 s | 1.8 s |
| `test_kill_waits_for_a_slow_shutdown` | 12.3 s | 1.5 s |
| `test_signals_stop_the_run` | 1.4 s | 1.4 s |
| `test_stuck_runner` | 10.4 s | 1.4 s |
| `test_resume_or_rm_of_running_job_is_refused` | 10.4 s | 1.3 s |
| `test_rm` | 5.7 s | 1.2 s |

Every other test takes under 1 s. Change 3 (parallel runner) was not done;
it stays optional.
