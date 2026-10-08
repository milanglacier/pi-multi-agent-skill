# Multi-agent reference

Every command is run as `python3 "$SKILL_DIR/scripts/multi_agent.py" <command>`.
A PROMPT or MESSAGE of `-` is read from stdin.

## Commands

| Command | What it does |
|---|---|
| `run [options] (PROMPT \| -)` | Start a new job. |
| `resume NAME [--background] [--timeout S] (PROMPT \| -)` | Start a new run of a finished job, in the same conversation. |
| `wait NAME [--timeout S]` | Block until the job is no longer running, then print its result. |
| `result NAME` | Print the result of a job that is not running. |
| `steer NAME (MESSAGE \| -)` | Give a running agent a message to read before its next step. |
| `abort NAME` | Stop a running agent and keep its last message as the result. |
| `kill NAME` / `kill --all` | Stop one job, or every running job of the current project. |
| `status NAME` | Print the job's state. |
| `peek NAME [-n N]` | Print the last N lines (default 20) of the job's progress log. |
| `list [--all]` | List the jobs of the current project. |
| `profiles` | List the available profiles. |
| `rm NAME` | Delete a job that is not running, with its conversation. |

### run options

| Option | Meaning |
|---|---|
| `--profile P` | Profile name, matched case-insensitively. Default `general-purpose`. |
| `--name N` | Job name. Default `<profile>-<4 hex digits>`, lowercased. |
| `--background` | Detach the runner and return immediately. |
| `--timeout S` | Wall-clock limit in seconds. Default 3600 for background runs, none for foreground runs. `0` means no limit. |
| `--fork` | Start the agent from a copy of the caller's conversation (`$PI_SESSION_FILE`). Fails if that variable is unset. |
| `--model M` | Model pattern passed to `pi --model`. |
| `--thinking L` | Thinking level passed to `pi --thinking`. |
| `--cwd DIR` | Working directory for the agent. Default: the current directory. |

A job name contains letters, digits, `.`, `_` and `-`, starts and ends with a
letter or digit, and has at most 64 characters. `run` refuses a name that
already exists in the project; use `resume`, another name, or `rm` the old
job.

`resume` reuses the job's profile, model, thinking level and tools. The job
must not be running.

### Output

A foreground `run` writes `multi-agent: started job NAME` to stderr when the
agent starts. `run` (foreground), `wait` and `result` print the agent's final
message and a footer:

```
<final assistant text>

--- multi-agent ---
name: explore-3f9a
status: done
profile: Explore
model: anthropic/claude-sonnet-5
duration_s: 84
```

A background `run` or `resume` prints:

```
job: explore-3f9a
status: running
timeout_s: 3600
```

When `wait --timeout` expires, it prints the job name, `status: running` and
the last progress line, and exits with code 2.

`steer` prints `steer: queued` once the agent has accepted the message, and
`abort` prints `abort: requested`. Both fail with `job is not running; use
resume` when the job has ended.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | The job ended with status `done`, or the command succeeded. |
| 1 | The job ended with another status, or the command failed. |
| 2 | `wait` timed out and the job is still running. |
| 64 | Usage error. |

## Statuses

| Status | Meaning |
|---|---|
| `running` | The agent is working. |
| `done` | The agent finished normally. |
| `failed` | The model returned an error, pi rejected the prompt, or pi exited early. The output holds the error. |
| `aborted` | `abort` stopped the agent. The output is its last message. |
| `timeout` | The run reached its `--timeout`. The output is the agent's last message. |
| `killed` | `kill`, a signal to the runner, or the end of a foreground command stopped the job. |
| `died` | The runner process ended without recording a result, for example after SIGKILL. |

## Profiles

Profiles are Markdown files with frontmatter. They are looked up in three
directories, from highest to lowest priority:

1. `<cwd>/.pi/agents/*.md` (project)
2. `$PI_CODING_AGENT_DIR/agents/*.md`, default `~/.pi/agent/agents/` (user)
3. `profiles/*.md` in this skill's directory (built-in)

When several directories define the same name, the highest file is used as a
whole; fields are never merged across files. To change one field of a
built-in profile, copy the whole file and edit the copy. A profile with
`enabled: false` hides its name, including lower profiles of that name.

| Field | Default | Effect |
|---|---|---|
| `name` | file name without `.md` | Profile name. |
| `description` | `name` | Shown by `profiles`. |
| `model` | the caller's model | `pi --model` |
| `thinking` | the caller's thinking level | `pi --thinking` |
| `tools` | pi's default tool selection | `pi --tools` |
| `disallowed_tools` | none | `pi --exclude-tools` |
| `prompt_mode` | `append` | `append` adds the body to pi's system prompt; `replace` replaces it and skips AGENTS.md and CLAUDE.md files. |
| `enabled` | `true` | `false` hides the profile. |

The body after the frontmatter is the profile's prompt. `tools` and
`disallowed_tools` accept a comma-separated list or `[a, b]` and are passed to
pi unchanged, so patterns such as `mcp__server__*` and `+name`/`-name` entries
work. Unknown fields are ignored. The parser supports `key: value` lines,
quoted strings, `true`/`false` and lists; it does not support nested YAML.

For model and thinking, the first value found wins: the `run` option, the
profile field, the caller's current model and thinking level, then pi's
configured default.

Every agent also receives a short system-prompt block that tells it its name,
that only its final message is read, and that no person can approve blocked
tool calls. Agents do not load `APPEND_SYSTEM.md` files; put text that
agents need in a profile's body.

## Nesting

An agent can start agents of its own. `MULTI_AGENT_DEPTH` counts the depth: 0
or unset for the user's agent, 1 for an agent it started, and so on. `run` and
`resume` refuse to start when the depth is at least `MULTI_AGENT_MAX_DEPTH`
(default 2). `status` and `list` show the job that started each job.

## Lifetime

- A foreground run is tied to the caller's command. Whatever stops that
  command, SIGKILL included, stops the agent and the commands it was running.
  This also holds for a foreground run started inside another agent, so a
  chain of foreground runs stops together.
- A background run keeps going after the caller ends, until it finishes,
  reaches its timeout, or is stopped with `kill`.
- Each job is run by a separate runner process. Stopping a job through any of
  the paths above lets the agent shut down cleanly. If the runner itself is
  killed with SIGKILL, the job's status becomes `died` and commands the agent
  was running may keep running.

## State

Job directories are in
`$XDG_STATE_HOME/pi-multi-agent/<project>/jobs/<name>/` (default
`~/.local/state`). `<project>` is the current directory with every character
other than a letter or digit replaced by `-`. Conversations are pi session
files in `$PI_CODING_AGENT_DIR/multi-agent-sessions/<project>/`, outside the
list of pi's `/resume`.

| File | Content |
|---|---|
| `job.json` | Launch settings: profile, pi arguments, cwd, parent job, depth, run mode, timeout. |
| `prompt.txt` | Prompt of the latest run. |
| `system-prompt.md` | The profile's prompt when it replaces pi's system prompt. |
| `append-prompt.md` | Text added to pi's system prompt: the profile's prompt in append mode, then the multi-agent block. |
| `status` | Status of the latest run. |
| `runner_pid`, `pi_pid` | Process IDs of the latest run. |
| `started_at`, `ended_at` | UTC timestamps of the latest run. |
| `runs` | Number of runs. |
| `exit_code` | Exit code of the latest run, written last. |
| `output.txt` | Final message of the latest run. |
| `model` | Model that produced the latest message. |
| `stderr.txt` | pi's stderr for the latest run. |
| `progress.log` | One line per tool call, retry, compaction, cancelled dialog and steering message, across runs. |
| `usage.json` | Token and cost totals across runs. |
| `killed` | Marker written when the job is killed. |
| `inbox/` | Requests from `steer` and `abort` and the runner's answers. |
| `runner.log` | Traceback when the runner itself fails. |

To read a finished job's whole conversation in interactive pi, run the
`session_cmd` that `status` prints. Do not open it while the job is running,
because two processes would then write the same session file.

## Troubleshooting

- **`job is not running; use resume`.** The job has ended. `resume` it with a
  new prompt instead of steering it.
- **Status `failed` with `pi exited before the run finished`.** pi stopped
  during start-up or the run. The output holds the end of pi's stderr; the
  whole log is in `stderr.txt`. A model or tool name that pi rejects causes
  this.
- **Status `died`.** The runner was killed without a chance to record a
  result. The agent's pi process exits by itself, but commands it started can
  survive; look for them with `ps` if the agent was running a long command.
  `peek` shows how far it got; `resume` continues the conversation.
- **An agent reports that a tool call needs approval.** Agents cannot ask a
  person. Run that step yourself, or tell the user what was blocked.
- **`cannot start an agent: ... MULTI_AGENT_MAX_DEPTH`.** The caller is too
  deep in a chain of agents. Do the work directly.
- **A job lingers after its caller is gone.** Background jobs outlive their
  caller by design. Stop them with `kill NAME`, or `kill --all` when the user
  asks.
- **`job ... exists in several projects`.** Run the command from the
  directory where the job was started.
