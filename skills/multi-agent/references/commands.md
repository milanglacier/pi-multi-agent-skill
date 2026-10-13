# Commands

Every command runs as `python3 "$SKILL_DIR/scripts/multi_agent.py" <command>`.
A PROMPT or MESSAGE of `-` is read from stdin, without its trailing newlines.
An empty prompt or message is a usage error. `--help` prints a usage summary.

| Command | What it does |
|---|---|
| `run [options] (PROMPT \| -)` | Start a new job. |
| `resume NAME [--background] [--timeout S] (PROMPT \| -)` | Start a new run of a finished job, in the same conversation. |
| `wait NAME [--timeout S]` | Block until the job is no longer running, then print its result. |
| `result NAME` | Print the result of a job that is not running. |
| `steer NAME (MESSAGE \| -)` | Give a running agent a message to read before its next step. |
| `abort NAME` | Ask a running agent to stop, keeping its last message as the result. |
| `kill NAME` / `kill --all` | Stop one job, or every running job of the current project. |
| `status NAME` | Print the job's settings and state. |
| `peek NAME [-n N]` | Print the last N lines (default 20) of the job's progress log. |
| `list [--all]` | List the jobs of the current project. |
| `profiles` | List the available profiles. |
| `rm NAME` / `rm --all` | Delete one job that is not running, or every such job of the current project, with their conversations. |

## Projects and job names

A job belongs to the project of the directory where `run` was called. `run`,
`list`, `kill --all` and `rm --all` work on the current directory's project only.

The other commands look for NAME in the current project first. If the project
has no job of that name, they search every project and use the job when
exactly one project has it, so they also work from another directory. When
several projects have a job of that name, the command fails and lists them;
run it from the job's project.

## run

| Option | Meaning |
|---|---|
| `--profile P` | Profile name, matched case-insensitively. Default `general-purpose`. |
| `--name N` | Job name. Default: the profile name in lowercase, `-`, and 4 hex digits, such as `explore-3f9a`. |
| `--background` | Return as soon as the job has started. |
| `--timeout S` | Wall-clock limit for the run in seconds. Default 3600 for a background run and no limit for a foreground run. `0` means no limit. |
| `--fork` | Start the agent from a copy of the caller's conversation. |
| `--model M` | Model pattern passed to `pi --model`. |
| `--thinking L` | Thinking level passed to `pi --thinking`. |
| `--cwd DIR` | Working directory for the agent, relative to the current directory. Default: the current directory. |

A job name contains letters, digits, `.`, `_` and `-`, starts and ends with a
letter or digit, and has at most 64 characters. `run` refuses a name that
already exists in the project and reports its status and age. Pick another
`--name`. Use `resume` or `rm` only for a job you started or the user asked
about.

`--cwd` changes where the agent works and where project profiles are looked
up (`DIR/.pi/agents/`). The job still belongs to the current directory's
project.

`--fork` copies the conversation in `$PI_SESSION_FILE`, which pi sets for the
commands it runs. It fails when that variable is unset or the file does not
exist. Only the job's first run starts from the copy; later runs continue the
job's own conversation.

When `--model` and `--thinking` are omitted, the profile and then the caller's
settings decide; see `profiles.md`.

A foreground `run` writes `multi-agent: started job NAME` to stderr as soon as
the job starts, then blocks and prints the result when the run ends. If the
command receives SIGINT, SIGTERM or SIGHUP, the job stops with status `killed`
and the command still prints the result. A second signal ends the command at
once.

A background `run` prints the following and exits with code 0. `timeout_s`
is `none` when the run has no limit.

```
job: explore-3f9a
status: running
timeout_s: 3600
```

## resume

`resume` starts a new run of a job that is not running. The agent continues
the job's conversation, with the profile, model, thinking level, tools, system
prompt and working directory that the job was created with. Editing the
profile afterwards does not change an existing job.

Only `--background` and `--timeout` can be given. They apply to the new run
alone and have the same defaults as for `run`, so a resumed run does not
inherit the previous run's timeout. Output is the same as for `run`.

The job's status, result and duration always describe its latest run. If the
job has no saved conversation, for example because pi failed during start-up,
`resume` prints a note to stderr and the agent starts a new conversation.

## wait and result

`wait` blocks until the job is no longer running and prints its result.
Without `--timeout` it waits as long as the job runs. When `--timeout`
expires first, it prints the following and exits with code 2; the
`last_progress` line is missing when the progress log is empty.

```
job: explore-3f9a
status: running
waited_s: 120
last_progress: 14:02:11 grep retry policy
```

`result` prints the result of a job that is not running and fails with `job
NAME is still running; use wait` otherwise.

`run` in the foreground, `wait` and `result` print the result as the agent's
final message followed by a footer:

```
<final assistant text>

--- multi-agent ---
name: explore-3f9a
status: done
profile: Explore
model: anthropic/claude-sonnet-5
duration_s: 84
```

The text is `(no output)` when the run produced none. For a `failed` run it
ends with an `error:` line. `model` is the model that wrote the latest
message, or the requested model when none did, or `(pi default)` when no
model was requested. `duration_s` covers the latest run. `lifecycle.md`
explains what the text holds for each status.

## steer

`steer` sends a message to the running agent, which reads it before its next
step. It prints `job: NAME` and `steer: queued` once pi has accepted the
message, and the message appears in the progress log. It fails with `job is
not running; use resume` when the run has ended or is ending.

## abort

`abort` asks the agent to stop and prints `job: NAME` and `abort: requested`
without waiting for it. Steering messages that the agent has not read yet are
discarded. The run ends with status `aborted`; use `wait` to get the result.
`abort` fails with `job is not running; use resume` when the run has ended or
is ending.

## kill

`kill NAME` stops the run at once and returns after the job's runner has
exited, which takes at most about 20 seconds. It prints the job name and the
status the run ended with:

```
job: explore-3f9a
status: killed
```

When the run had already ended, or was ending on its own, `kill` changes
nothing, prints that status followed by `(not running)`, and exits with code
0.

`kill --all` stops every running job of the current project in parallel and
prints one such block per job. For a job it could not stop, the block has an
`error:` line instead of the status, and the command exits with code 1. With
no running job it prints `no running jobs in this project`.

## status

`status` prints `key: value` lines: `name`, `status`, `mode` (`foreground` or
`background`), `profile`, `model`, `parent`, `cwd`, `started_at`, `ended_at`,
`runs`, `exit_code`, `output_file`, `session_file` and `session_cmd`. A `-`
marks a missing value. Timestamps are UTC and describe the latest run.

`session_cmd` opens the job's conversation in interactive pi. While the job
runs, `status` adds a `note:` line as a reminder not to open it yet, because
two processes would then write the same conversation file.

## peek

`peek` prints the last N lines of the job's progress log, which spans all of
its runs. Each line starts with the local time. The log records each run's
start and end, every tool call with a short excerpt of its main argument,
retries, compactions, cancelled dialogs (`denied:`), steering messages, abort
requests and timeouts. It does not hold the agent's text; read that with
`wait` or `result`.

## list

`list` prints a table with the columns `NAME`, `STATUS`, `MODE`, `PARENT` and
`AGE`, oldest first. It shows running jobs and jobs that ended in the last 24
hours; `--all` shows every job of the project. `AGE` is the time since the
run started for a running job and since it ended otherwise. `PARENT` is the
job that started this one, or `-`.

## profiles

`profiles` lists each enabled profile with its description and source file.
It looks up project profiles in the current directory.

## rm

`rm NAME` deletes a job that is not running: its state directory and its
conversation file. It prints `removed: NAME` and fails with `job NAME is
running; kill it first` while the job runs.

`rm --all` deletes every job of the current project that is not running,
with its conversation, and leaves running jobs alone.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The job ended with status `done`, or the command succeeded. |
| 1 | The job ended with another status, or the command failed. |
| 2 | `wait` timed out and the job is still running. |
| 64 | Usage error, such as an unknown option or a missing prompt. |

The job's status decides the exit code of a foreground `run` or `resume`, of
`wait` and of `result`. The other commands exit with 0 when they succeed.
