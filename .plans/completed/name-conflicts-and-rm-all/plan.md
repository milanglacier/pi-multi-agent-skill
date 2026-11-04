# Plan: name conflicts and `rm --all`

Status: draft (2026-10-12).

## Why

Jobs are never deleted unless someone runs `rm NAME`. Job names belong to a
project, not to a session, so a name stays taken until its job is removed.

Default names (`explore-3f9a`) never conflict: `run` retries with a new
random suffix. Conflicts happen only when the agent passes `--name`. Agents
like descriptive names such as `auth-review` or `tests`, so the same name
can come back from a session last week, or from another session that is
running now.

When the name is taken, `run` fails before it starts anything, so the cost is
one tool call. The problem is what the error tells the agent:

```
job auth-review already exists in this project; use `resume auth-review` to
continue it, pick another --name, or `rm auth-review` first
```

It lists three choices as equal and puts `resume` first. The agent does not
know where the old job came from, so it cannot choose well:

- **Pick another name.** This is the right choice almost every time.
- **`resume`.** This continues another job's conversation with a prompt
  meant for a fresh agent. The agent works from context it did not write, and
  nothing reports an error.
- **`rm`.** This deletes a result and conversation that the user or another
  session may still want. Another session may be about to `wait` on that job.

The error also does not say anything about the old job. `list` hides jobs
that ended more than 24 hours ago, so an agent can run `list`, not see
`auth-review`, and still get the conflict.

There is also no quick way to clear a project's jobs. `kill --all` stops
every running job, but `rm` takes exactly one name.

## Scope

1. The conflict error names the old job's status and age, recommends a new
   name, and limits `resume` and `rm` to jobs the agent started or the user
   named.
2. `rm --all` removes every job of the current project that is not running,
   the way `kill --all` stops every job that is running.

SKILL.md does not change. Automatic cleanup of old jobs is left for later
(see "Deferred").

## The changes

### 1. The conflict error

`cmd_run` reads the existing job's status with `job_state` and its age the
same way `list` does: time since the run started for a running job, and time
since it ended otherwise. The error becomes:

```
job auth-review already exists in this project (done, ended 3d ago); pick
another --name. Run `resume auth-review` or `rm auth-review` only for a job
you started or the user asked about.
```

For a running job, the part in parentheses reads `(running, started 5m ago)`.
When the job has no timestamps, it shows only the status, such as `(died)`.

The age logic moves out of `cmd_list` into a small helper, for example
`job_since(job_dir, status)`, which returns the start or end time or `None`.
`list` and `run` both use it, so the two always show the same age.

If two `run` commands race for the same name, the loser can see the winner's
directory before the winner has created its lock file. `job_state` then
reports `died`. This window is a few microseconds and harmless: `resume` and
`rm` take the job's lock and fail cleanly if the job is still being set up.
The plan does not handle it specially.

### 2. `rm --all`

`rm` accepts `--all` with the same rules as `kill --all`:

- It works on the current directory's project only. `rm NAME` still looks in
  other projects when the current one has no such job.
- `rm --all` together with a name is a usage error: `rm takes a job name or
  --all, not both`.
- It removes every job whose `job_state` is not `running`, and leaves running
  jobs alone without listing them, as `kill --all` leaves finished jobs alone.
- It prints `removed: NAME` for each job, in name order, the same line that
  `rm NAME` prints.
- When a job starts between the status check and the removal, its lock is
  held, so removing it fails with the usual `job NAME is running; kill it
  first`. `rm --all` prints `error: <message>` for that job, goes on with the
  others and exits with code 1. This matches how `kill --all` reports a job it
  could not stop.
- With nothing to remove it prints `no jobs to remove in this project` and
  exits with code 0.

The jobs are removed one after another. Removing a job takes a few
milliseconds, so running them in parallel like `kill --all` gains nothing.

The body of `cmd_rm` after `find_job` moves into a helper, for example
`remove_job(job_dir)`, which both forms call.

### 3. Docs

- `USAGE` in the script: `rm (NAME | --all)`.
- `references/commands.md`: the table row for `rm`, the sentence about
  existing names in the `run` section, and the `rm` section, which gets a
  paragraph about `--all` like the one for `kill --all`.
- `references/troubleshooting.md`: the `job NAME already exists in this
  project` entry says to pick another name, and to `resume` or `rm` the job
  only when the agent started it or the user asked.

SKILL.md stays as it is. The guidance an agent needs at a name conflict is
in the error message, which it sees exactly when it matters. A note in
SKILL.md would repeat the message and cost tokens every time the skill loads.
SKILL.md also does not mention `rm` today, and `kill --all` is enough for an
agent to guess that `rm --all` exists if a user asks for it.

### 4. Tests

- `test_existing_name_is_refused`: the error names the status and age of a
  `done` job and of a running job, and says `pick another --name`.
- `test_rm_all`: with a done job, a failed job and a running job (held with
  `self.hold()`), plus a job of the same name in another project, `rm --all`
  removes the done and failed jobs with their session files, leaves the
  running job and the other project's job alone, and exits with code 0. A
  second `rm --all` while the held job still runs prints `no jobs to remove in
  this project`. `rm --all NAME` is a usage error with exit code 64.
- `list` keeps its output; the existing `list` tests cover the shared age
  helper.

## Steps

1. The age helper and the new conflict error, with its test.
2. `remove_job` and `rm --all`, with their test.
3. `USAGE` and the references.
4. `uvx ruff check .`, `uvx ruff format --check .`, and the suite on the
   current Python and on Python 3.9.

## Done when

- A `run --name` that conflicts prints the old job's status and age and
  recommends a new name.
- `rm --all` removes every job of the current project that is not running,
  and nothing else.
- The checks and both test runs in step 4 pass.

## Alternatives considered

### Rejected

- **Add a suffix when the name is taken** (`auth-review` → `auth-review-2`).
  The agent remembers the name it passed, not the one printed. A later
  `wait auth-review` would read the old job's result and report it as the new
  one. An error is much better than a wrong answer that looks right.
- **Always add a random suffix to `--name`.** This removes conflicts, but the
  agent must read the printed name and can no longer choose it. Agents that
  pass `--name` want a name they can type back, so this takes away the reason
  to use the option.
- **One namespace per session instead of per project.** This removes
  conflicts between sessions, but background jobs exist to outlive their
  session and be found by name later, from another session or by the user.
  It also changes the state layout and how `find_job` looks jobs up, which is
  far more than the problem is worth.
- **`run --replace` to delete the old job and reuse its name.** Agents would
  pass it by habit to avoid the error, and it would delete other sessions'
  jobs just as `rm` does today, but without a moment to think first.
- **A SKILL.md note about name conflicts.** It would repeat the error
  message. The message reaches the agent at the moment it matters; the note
  would cost tokens on every load.
- **Keep the current message.** It lists `resume` and `rm` as equal to a new
  name and says nothing about the old job, which leads agents to the two
  choices that do harm.

### Deferred

- **Delete finished jobs automatically after some days**, for example jobs
  that ended more than 7 days ago, checked at the start of `run` for the
  current project. Results are normally read within the session that started
  the job, and the conversations sit outside pi's `/resume` list, so little of
  value is lost. Cleanup would stop disk use from growing and free most old
  names. The job lock makes it safe: cleanup skips every job whose lock is
  held. It is left for a later change so this one stays small; `rm --all` and
  the better error cover the immediate need. Conflicts with a job from the
  same week would remain, so the error message change is needed either way.
- **`rm --older-than DAYS`.** A manual form of the same cleanup. If automatic
  cleanup is added, this flag is not needed; if it is not, this is the
  smaller alternative.
