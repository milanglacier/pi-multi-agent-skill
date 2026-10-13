## Writing

Write in plain, simple language everywhere: code comments, docs, SKILL.md,
the references, commit messages and explanations to the user. Use complete
sentences, and say what a thing does instead of coining a name for it.
Commit messages use Conventional Commits (`feat:`, `fix:`, `docs:`, ...).

### Code, Comments & Docstrings

The text in the repo must read as the final, clean version. It should carry
no trace of the back and forth it took to get there, as if it had never been
written any other way. Describe only the current state:

- Describe only current behavior in code, docstrings, inline comments,
  SKILL.md and the references. Do not mention what changed or contrast it
  with earlier behavior.
- The only place where contrasting new behavior with earlier behavior is
  acceptable is **commit messages and plans**, where readers have the
  relevant context.

## Designing features

This project is a minimal CLI wrapper around `pi --mode rpc`. pi already
loads extensions, MCP servers, skills, settings, models and sessions; the
script starts pi, passes it a prompt, relays steer and abort, and keeps each
job's result under a name. Keep new features at that size:

- Reuse what pi already does. If pi has a flag or an RPC command for it, use
  that.
- Prefer one small mechanism the kernel enforces (a lock, a pipe, a socket)
  over state that several processes reconstruct from files and pids.
- Leave out what an agent can do itself, such as worktrees or scheduling.
- Stay harness agnostic: SKILL.md and the references name no specific
  extension, harness tool or permission mode.
- KEEP SKILL.md AND `references/` CONCISE. State only what an agent needs
  to use the skill. DO NOT explain familiar syntax, implementation details,
  or repeat information. Put necessary details in references, not SKILL.md,
  and update them with the behavior they describe.
- Stdlib only, Python 3.9 or later.


After changing SKILL.md or the references, run
`python3 scripts/count_tokens.py` to check token counts.

After changing Python code, including tests, run these commands from the
repo root and fix any failures:

```sh
uvx ruff check .
uvx ruff format --check .
python3 -m unittest discover tests
```

# Notes and Rabbit Holes

Knowledge the code does not show. When adding a note, describe the problem
and the outcome to preserve, not a step-by-step patch.

- **The plans are history.** `.plans/completed/` explains past decisions,
  but parts are out of date. The script and the references are current.
- **pi must keep a reader on its stdout until it exits.** pi exits on the
  first failed stdout write, before its shutdown runs, and the agent's
  commands keep running. That is why the foreground command and the runner
  are separate processes, and why nothing should SIGKILL a runner.
- **Use `--session-id`, never `--session`.** `--session` matches by prefix,
  so `resume explore` could open `explore-2`'s session.
- **Every dialog must be answered.** Extensions often wait on a dialog with
  no timeout, so an unanswered one hangs the agent.
- **Agents do not get `APPEND_SYSTEM.md`, by decision.**
  `--append-system-prompt` makes pi skip it; working around that costs more
  than the file is worth.
- **Do not bring pid checks back into liveness.** Pid, `/proc` and `ps`
  checks caused most past bugs; the runner's lock is the only signal.
- **Accepted limits.** SIGKILL to a runner orphans its pi for a few seconds.
  A stuck runner is not killed automatically. An `abort` that reaches pi
  before the agent starts is ignored until the settle deadline.
- **Tests.** Run the suite on Python 3.9 too (`uv run --no-project --python
  3.9 python -m unittest discover tests`), one suite at a time: parallel
  suites can make the signal tests fail.
