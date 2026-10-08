# Spec: multi-agent skill and CLI runner

This file is the design. The execution plan is `plan.md` in the same
directory.

## Goal

Replace the in-process pi-subagents extension with a skill plus one script.
The script starts other agents, each as a separate `pi --mode rpc` process,
and gives the calling agent a small command set: start, wait, steer, abort,
kill, resume, inspect.

The motivation is to remove the maintenance cost of running agents in process
through the SDK. pi-subagents has to rebuild wiring that the pi CLI already
does (built-in extensions, MCP, tool scoping, model runtime, project trust),
and most recent fixes there were in that wiring. A pi subprocess gets all of
it from the CLI.

## Foreground and background runs

A run is either foreground or background. In both, the runner is a detached
process. The two differ only in what stops it:

- A **foreground** run is tied to the caller's command. That command waits
  for the result, and whatever stops it (the caller's tool abort or timeout,
  the harness exiting, a background tool stopping its task, SIGKILL) stops
  the run.
- A **background** run keeps running after the caller ends. Its timeout,
  `kill NAME` and `kill --all` stop it.

The script does not stop one job because another job ended. Process lifetime
comes from the harness, which already stops the commands it started, and from
two pipes: the one between the foreground command and its runner, and the one
between the runner and pi (see "Lifetime").

## Non-goals

These pi-subagents features are left out on purpose. Each one is either rarely
used or something the agent can do itself.

- Worktree isolation. The agent runs `git worktree` itself when it needs to.
- Persistent agent memory. Another extension can provide it.
- `max_turns`. A wall-clock timeout bounds a run instead.
- Skill preloading and per-profile skill restrictions.
- Extension allowlists (`extensions:`, `exclude_extensions:`, `ext:`
  selectors). Tool names in `tools` and `disallowed_tools` cover the need.
- Scheduling, the event bus, cross-extension RPC, `@mentions`, the widget,
  FleetView and the conversation viewer.
- Stopping the jobs that a job started when that job ends. Each job's
  lifetime follows its own run mode.
- Forwarding permission prompts to the user. An agent's permission prompts
  are always answered with "cancel", which blocks the tool call.

## Repository and layout

The skill lives in `pi-multi-agent-skill/` in the pi-extensions monorepo. It is
a plain directory during development and becomes a submodule later.

```
pi-multi-agent-skill/
  README.md
  LICENSE
  skills/multi-agent/
    SKILL.md
    reference.md
    scripts/multi_agent.py
    profiles/general-purpose.md
    profiles/Explore.md
  tests/
    fake-pi
    test_multi_agent.py
  .plans/active/multi-agent-cli-runner/
    spec.md
    plan.md
```

For testing on pi first, `skills/multi-agent` is symlinked to
`~/.pi/agent/skills/multi-agent`. A later step can link it into
`~/.agents/skills/` so other harnesses see it.

`multi_agent.py` uses only the Python 3.9+ standard library. SKILL.md invokes
it as `python3 "$SKILL_DIR/scripts/multi_agent.py" ...`, as the handoff skill
does, so the exec bit does not matter.

## Concepts

- **Caller**: the agent, or person, that runs `multi_agent.py`.
- **Profile**: a Markdown file with frontmatter that sets the system prompt,
  model, thinking level and tools of an agent. `general-purpose` and
  `Explore` ship with the skill.
- **Job**: one agent conversation, identified by a name. A job has a
  directory of state files and one pi session file. A job can run several
  times: the first `run` and any number of later `resume` calls.
- **Runner**: the Python process that owns one running pi RPC process. It
  sends the prompt, reads events, answers dialog requests, forwards steering
  messages, records status, and writes the final output. The runner is always
  a detached copy of the script in its own session.
- **Foreground command**: the `multi_agent.py run` or `resume` process of a
  foreground run. It starts the runner, waits for it, and prints the result.
  The runner stops the run when this command ends.

## Command-line interface

```
multi_agent.py run      [options] (PROMPT | -)
multi_agent.py resume   NAME [--background] [--timeout S] (PROMPT | -)
multi_agent.py wait     NAME [--timeout S]
multi_agent.py result   NAME
multi_agent.py steer    NAME (MESSAGE | -)
multi_agent.py abort    NAME
multi_agent.py kill     (NAME | --all)
multi_agent.py status   NAME
multi_agent.py peek     NAME [-n N]
multi_agent.py list     [--all]
multi_agent.py profiles
multi_agent.py rm       NAME
```

`-` reads the prompt or message from stdin, so long text can come from a
heredoc and avoid shell quoting problems. SKILL.md recommends this form for
any prompt longer than one line.

### run

Options:

| Option | Meaning |
|---|---|
| `--profile P` | Profile name, matched case-insensitively. Default `general-purpose`. |
| `--name N` | Job name. Default `<profile>-<4 hex digits>`, lowercased. |
| `--background` | Detach the runner and return immediately. |
| `--timeout S` | Wall-clock limit in seconds. Default 3600 for background runs, none for foreground runs. |
| `--fork` | Start the agent from a copy of the caller's conversation (`--fork "$PI_SESSION_FILE"`). Fails if `PI_SESSION_FILE` is unset. |
| `--model M` | Model pattern passed to `pi --model`. |
| `--thinking L` | Thinking level passed to `pi --thinking`. |
| `--cwd DIR` | Working directory for the agent. Default: the caller's logical cwd. |

Job names follow pi's session-ID rules: letters, digits, `.`, `_` and `-`;
they start and end with a letter or digit; at most 64 characters. `run` fails
if a job with that name already exists in the project. The error tells the
caller to use `resume`, pick another name, or `rm` the old job.

A foreground `run` prints one line to stderr as soon as the job starts:

```
multi-agent: started job explore-3f9a
```

so a caller running it inside a harness's background tool can find the name
in the task output and steer the job. When the job ends, `run` prints the
result (see "Result format") to stdout.

A background `run` prints:

```
job: explore-3f9a
status: running
timeout_s: 3600
```

### resume

Starts a new run of an existing job on the same pi session, with a new
prompt. The job must not be running. It accepts `--background` and
`--timeout` with the same meaning as `run`. The profile, model, thinking
level and tools come from the job's recorded launch settings.

### wait

Blocks until the job is no longer running, or until `--timeout` seconds pass.
On completion it prints the result. It takes one job name. SKILL.md tells the
agent to wait for one job per tool call with a timeout of about 120 seconds,
because a steering message for the waiting agent is only delivered after its
current tool call returns.

### result

Prints the result of a job that is not running. Fails with exit code 1 and a
message if the job is still running.

### steer

Delivers a steering message to a running job. The message reaches the agent
after its current tool calls finish and before its next model call. The
command waits up to 10 seconds for the runner to acknowledge the message and
prints the acknowledgement:

```
job: explore-3f9a
steer: queued
```

If the job has finished, or finishes before the runner reads the message, the
command fails with `job is not running; use resume`.

### abort

Asks a running job to stop and keep what it has. The runner sends RPC
`abort`, waits for the agent to settle, saves the last assistant text as the
output and ends the job with status `aborted`. Use `kill` when a job does not
respond.

### kill

`kill NAME` stops one job:

1. Write the `killed` marker and send SIGTERM to the runner, which stops pi
   (see "Signals and the end of the foreground command").
2. If the runner is still alive after 10 seconds, send it SIGKILL. pi then
   sees its stdin close and exits, possibly before it stops the agent's
   commands (see "Limits").
3. If pi is still alive 10 seconds later, send SIGKILL to the pid in
   `pi_pid`.

`kill` signals the runner and pi by pid. When the job is a foreground run,
the runner records the status `killed`, and the foreground command prints
the result and exits with code 1.

`kill --all` runs the same procedure on every running job of the current
project, in parallel. It is the user's way to stop all background work at
once. SKILL.md tells the agent to use it only when the user asks.

### status, peek, list, profiles, rm

- `status NAME` prints the job's state: name, status, run mode, profile,
  model, the job that started it, start time, run count, exit code, output
  file, session file, and the command that opens the session in interactive
  pi: `pi --session-dir <dir> --session-id <name>`. Interactive pi must only be
  opened on a job that is not running, because two processes would then write
  the same session file.
- `peek NAME [-n N]` prints the last N lines (default 20) of the job's
  progress log.
- `list` prints the jobs of the current project with status, run mode, the
  job that started each one, and age. Without `--all` it shows running jobs
  and jobs that ended in the last 24 hours.
- `profiles` prints each available profile with its description and source
  file.
- `rm NAME` deletes a job that is not running: its job directory and its
  session file.

### Name lookup

Commands look up a name in the current project first. If it is not found
there, they search all projects and use the match if exactly one exists.
`kill --all` and `list` cover the current project only.

### Result format

`run` (foreground), `wait` and `result` print the agent's final assistant
text, then a footer for the caller:

```
<final assistant text>

--- multi-agent ---
name: explore-3f9a
status: done
profile: Explore
model: anthropic/claude-sonnet-5
duration_s: 84
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | The job ended with status `done`. |
| 1 | The job ended with another status (`failed`, `aborted`, `timeout`, `killed`, `died`), or the command failed. |
| 2 | `wait` timed out and the job is still running. |
| 64 | Usage error. |

## Profiles

### Discovery

Profiles are looked up in three directories, from highest to lowest
priority:

1. `<cwd>/.pi/agents/*.md` (project)
2. `$PI_CODING_AGENT_DIR/agents/*.md`, default `~/.pi/agent/agents/` (user)
3. `profiles/*.md` in the skill directory (built-in)

A profile's name is its `name` field, or the file name without `.md`. When
several directories define the same name, the profile from the highest one
is used as a whole and the others are ignored. Fields are never merged
across files: a project `Explore.md` that sets only `model` is a complete
profile with that model, default values for every other field, and its own
body as the prompt. To change one field of a built-in profile, copy the
whole file and edit the copy.

A profile with `enabled: false` hides that name, including any lower
profile of the same name.

### Fields

| Field | Default | pi arguments |
|---|---|---|
| `name` | file name without `.md` | |
| `description` | `name` | shown by `profiles` |
| `model` | inherited (see below) | `--model` |
| `thinking` | inherited (see below) | `--thinking` |
| `tools` | pi's default tool selection | `--tools <value>` |
| `disallowed_tools` | none | `-xt <value>` |
| `prompt_mode` | `append` | `append`: the body goes into the `--append-system-prompt` file (see "Starting pi"); `replace`: `--system-prompt <body file> -nc` |
| `enabled` | `true` | `false` hides the profile |

The Markdown body after the frontmatter is the profile's prompt. Unknown
fields are ignored, so pi-subagents profiles that use other fields still
load.

`tools` and `disallowed_tools` are passed to pi unchanged; a `[a, b]` list
is joined with commas first. The script does not interpret or check tool
names, so every form pi accepts works: a plain allowlist such as
`read,bash,grep`, patterns such as `mcp__radius__*`, and `+name`/`-name`
entries that change the default selection, such as `+codemode,-write`. A
value pi rejects makes pi exit, and the job ends with status `failed` and
pi's error in its output. When `tools` is omitted, pi's own default
selection applies, including the user's `defaultTools` setting.

The frontmatter parser supports `key: value` lines, quoted strings,
`true`/`false`, comma-separated lists, and `[a, b]` lists. It does not
support nested YAML.

### Model and thinking precedence

For each of model and thinking, the first value found wins:

1. The `--model` or `--thinking` option of `run`.
2. The profile's `model` or `thinking` field.
3. The caller's current values: `$PI_PROVIDER/$PI_MODEL` and
   `$PI_REASONING_LEVEL`. Pi sets these for commands run by its `bash` tool.
4. Nothing, so pi uses its configured default.

An explicit option on the command line is the clearest statement of intent,
so it wins over the profile.

### Built-in profiles

- `general-purpose`: default `prompt_mode` (`append`), no `tools`, empty
  body. It behaves like the caller's own pi, plus the multi-agent block below.
- `Explore`: `prompt_mode: replace`, `tools: read, bash, grep, find, ls`,
  the read-only search prompt from pi-subagents.

Neither pins a model. A user who wants a cheaper model for `Explore` copies
the file to `~/.pi/agent/agents/Explore.md` and adds `model`. The `Explore`
prompt comes from pi-subagents (MIT license); README.md credits it.

### Multi-agent block

Every run also adds this text to the agent's system prompt, after the profile's body in `append` mode:

```
<multi-agent>
You are an agent named <name>, started by another agent to work on one
task. Whoever reads your result sees only your final message, so make it a
complete, self-contained answer to the task. No person is available to
answer questions. If a tool call is blocked because it needs approval, do
not retry it; say in your final message what you could not do and why.
</multi-agent>
```

## Runner

### Starting pi

The runner starts:

```
pi --mode rpc --session-dir <session dir> <session args> <profile args> \
   --append-system-prompt <append-prompt.md>
```

- `<session args>` is `--session-id <name>` for a new job and for `resume`,
  and `--fork "$PI_SESSION_FILE" --session-id <name>` for `run --fork`.
  `--session <name>` is not used, because it matches session IDs by prefix:
  `--session explore` can open the session of `explore-2`. `--session-id`
  matches exactly and reopens the existing session.
- `append-prompt.md` holds the profile's body in `append` mode followed by
  the multi-agent block.
- Agents do not get `APPEND_SYSTEM.md`. pi loads that file (project or user)
  only when no `--append-system-prompt` is given, and the runner always
  passes one (real-pi check 1). This is accepted: the file is rarely used,
  and the alternatives cost more. Copying pi's lookup of the file into the
  script would break quietly when pi changes it, and adding the text from
  an extension inside the agent needs an extension the script would have to
  ship and load into every agent.
- System-prompt text is always written to files in the job directory and
  passed by path, because pi reads a `--system-prompt` or
  `--append-system-prompt` value that names an existing file as that file.
  The task prompt itself goes through the RPC `prompt` command.
- pi starts in its own session (`setsid`), so no signal for the runner's
  process group reaches pi. The runner stops pi through its stdin. See
  "Lifetime".
- `MULTI_AGENT_PI_BIN` overrides the `pi` executable. Tests use it to run
  `tests/fake-pi`.

The child environment is the runner's environment with these changes:

- `MULTI_AGENT_JOB` is set to the absolute path of this job's directory. A
  job started from inside it records that path as the job that started it.
- `MULTI_AGENT_DEPTH` is set to the caller's depth plus 1.
- `HERDR_ENV`, `HERDR_PANE_ID` and `HERDR_SOCKET_PATH` are removed, so the
  herdr extension inside the agent does not report state for the caller's
  pane.
- `PI_MINIMAL_PERMISSION_SYSTEM_NONINTERACTIVE` is set to `1`, so
  pi-minimal-permission-system treats the agent as a session without UI
  (see "Dialog requests").

pi's stdin and stdout are pipes held by the runner. pi's stderr goes to
`stderr.txt` in the job directory.

### Protocol handling

The runner sends `{"id": "p1", "type": "prompt", "message": <prompt>}`. If
the response has `success: false`, the job ends with status `failed`.

It reads stdout in binary mode and splits records on `\n` only, as pi's RPC
documentation requires. It reads continuously; a reader that stops can stall
pi. It handles these records and ignores the rest:

| Record | Action |
|---|---|
| `tool_execution_start` | Append `HH:MM:SS <toolName> <short args>` to `progress.log`. |
| `message_end` with an assistant message | Remember its `stopReason` and `errorMessage`; add its usage to the job's token totals. |
| `auto_retry_start`, `compaction_start` | Append a line to `progress.log`. |
| `extension_ui_request` with method `select`, `confirm`, `input` or `editor` | Reply `{"type": "extension_ui_response", "id": <id>, "cancelled": true}` and append `denied: <title>` to `progress.log`. |
| `agent_settled` | Finish the run (below). |
| `response` | Match it to the pending command by `id`. |

### Dialog requests

In RPC mode pi always binds a UI context, so `ctx.hasUI` is true for every
extension and pi 1.1.0 has no option to turn it off. An extension that opens
a dialog waits for the reply, often without a timeout, so the runner must
answer every dialog request. It answers with `cancelled: true`, which makes
`confirm` return `false` and `select`, `input` and `editor` return
`undefined`.

pi-minimal-permission-system reports a cancelled `confirm` to the model as
"User denied ...", which is wrong for these agents: no person saw the
request. The runner therefore sets
`PI_MINIMAL_PERMISSION_SYSTEM_NONINTERACTIVE=1` in the child environment.
With that variable the extension takes its no-UI path and blocks an `ask`
rule with "requires approval, but no interactive UI is available" without
opening a dialog. When the extension is not loaded, the variable has no
effect.

The runner does not decide on the variable from `ctx.mode`. pi-tau-web-server
is also an RPC host, and it forwards dialogs to a person in the browser.

### Finishing a run

On `agent_settled` the runner:

1. Sends `get_last_assistant_text` and writes the text to `output.txt`.
2. Sets the status: `aborted` if an abort was requested or the last
   `stopReason` is `aborted`; `failed` if the last `stopReason` is `error`;
   `done` otherwise.
3. Closes pi's stdin, which makes pi shut down. If pi has not exited after 10
   seconds, the runner sends SIGTERM, and SIGKILL 5 seconds later.
4. Writes `exit_code` and `status`, then exits.

If pi's stdout reaches end of file before `agent_settled`, the job ends with
status `failed`, and the last lines of `stderr.txt` are copied into
`output.txt`.

### Control inbox

`steer` and `abort` are separate, short-lived processes. They cannot write to
the pipe that the runner holds, so they hand their requests to the runner
through `inbox/` in the job directory:

1. The command writes `inbox/<timestamp>-<pid>.json` through a temporary file
   and a rename, so the runner never reads a partial file. The content is
   `{"type": "steer", "message": ...}` or `{"type": "abort"}`.
2. The runner checks `inbox/` every 0.5 seconds. For `steer` it sends RPC
   `steer`; for `abort` it sends RPC `clear_queue` and then RPC `abort`, and
   records that an abort was requested. Without `clear_queue`, pi continues
   with queued steering messages after an abort.
3. The runner writes `inbox/<same name>.ack` with
   `{"ok": true, "disposition": ...}` from pi's `steer` response, or
   `{"ok": false, "error": ...}`, and deletes the request file. An abort is
   acknowledged at once with `{"ok": true, "disposition": "requested"}`,
   because pi answers RPC `abort` only after the agent has settled, when the
   job is already ending.
4. The command waits for the `.ack` file, prints it, and deletes it.

After `agent_settled`, the runner answers any remaining request with
`{"ok": false, "error": "job is not running; use resume"}`. A command that
finds no runner alive answers itself with the same error.

The inbox is a directory, not a FIFO or a Unix socket. A FIFO has blocking
open semantics and does not keep writes larger than 4 KB atomic, and a Unix
socket path under `~/.local/state` can exceed the `sun_path` length limit. A
0.5 second delay does not matter, because pi delivers a steering message only
after the current tool calls finish.

### Timeout

When the deadline passes, the runner sends RPC `abort` and waits up to 30
seconds for `agent_settled`. The job ends with status `timeout`, and the last
assistant text is its output. If pi does not settle, the runner closes pi's
stdin and stops pi as in step 3 of "Finishing a run".

### Signals and the end of the foreground command

The runner handles SIGTERM, SIGHUP and SIGINT, and the end of its
foreground command (see "Lifetime"), the same way:

1. Write the `killed` marker.
2. Send `get_last_assistant_text`; if pi answers within 2 seconds, write the
   text to `output.txt`.
3. Close pi's stdin and stop pi as in step 3 of "Finishing a run".
4. Write `exit_code` and the status `killed`, then exit.

The foreground command handles SIGTERM, SIGHUP and SIGINT by closing its end
of the pipe to the runner, waiting for the runner to record the result, and
printing it. A second signal ends the command at once. A harness that follows
SIGTERM with SIGKILL ends the command early, which changes nothing for the
runner.

## Lifetime

### The runner and the foreground command

Every runner starts in its own session (`setsid`) with stdin, stdout and
stderr on `/dev/null`, so no signal for the caller's process group reaches
it, and the caller's command never waits on a pipe the runner holds.

- A background `run` starts the runner and exits. Its timeout, `kill NAME`
  and `kill --all` stop the runner.
- A foreground `run` or `resume` stays as the foreground command. Before it
  starts the runner, it creates a pipe, passes the read end to the runner,
  and keeps the only write end. The runner watches the read end in its event
  loop. When the foreground command ends for any reason, SIGKILL included,
  the kernel closes the write end, the runner reads end of file and stops the
  run as in "Signals and the end of the foreground command". The foreground
  command waits for the runner process and prints the result.

The foreground command is what the harness stops. pi's `bash` tool runs each
command in its own process group and sends SIGKILL to that group when the
tool call is aborted, times out, or pi shuts down. A background tool such as
`bg_task` sends SIGTERM to its task's group and SIGKILL when pi exits.
Either way the foreground command ends, and the runner stops the run.

### The runner stops pi

pi runs in its own session too. Its stdin is a pipe whose only writer is the
runner. The runner stops pi by closing that pipe while it keeps reading pi's
stdout until pi exits. pi in RPC mode treats end of input as a shutdown
request: it emits `session_shutdown`, where extensions such as
pi-background-tasks stop their tasks, and disposes the session, which aborts
the running agent and kills the process tree of a running `bash` tool call.

Because the runner is still reading, pi's writes during shutdown succeed and
the shutdown completes. This matters because pi 1.1.0's RPC writer calls
`process.exit(1)` on the first write to stdout that fails. If nothing reads
pi's stdout, the first event of the shutdown fails with EPIPE and pi exits
before it stops anything, so the commands and background tasks of the agent
keep running (real-pi check 7).

### Nested foreground runs

A job started from inside an agent with a foreground run is a command of
that agent's `bash` tool or background tool. When the outer job stops, its pi
shuts down and stops that command, which is the inner job's foreground
command, so the inner runner stops the inner job in turn. For a chain such
as main pi → A → B, aborting main's `bash` call stops A, then B, and every
command they were running. Each level adds up to the time pi takes to shut
down. A background job started from inside an agent keeps running, like any
background job.

### Why the foreground command is a separate process

The runner must outlive every way the caller can stop a foreground run, so
that pi is never left writing to a pipe nobody reads. An earlier design made
the foreground `run` process the runner itself. A `bash` tool abort then sent
SIGKILL to the runner, pi saw its stdin close while its stdout had no reader,
and pi exited on EPIPE before its shutdown. Every command the agent was
running survived, and in a nested chain the surviving command was the inner
job's runner, so the inner agent kept working, spending tokens and changing
files, with nobody to read its result.

Other fixes were considered:

- **An extension loaded into every agent** that defers pi's `process.exit(1)`
  until the session is disposed. It worked, but it replaces `process.exit` and
  relies on internals (that `pi.getActiveTools()` throws after dispose), so a
  pi update could break it silently and bring the leak back.
- **pi's stdout written to a file** that the runner tails. Writes to a file
  never fail, but the runner would have to poll and handle partial lines, and
  the file grows with every streamed update event.
- **Background runs only.** This avoids the crash only because nothing stops
  the job: Esc and the end of the caller's session no longer stop agents,
  `kill` does not reach the jobs a job started, and the caller needs at least
  two tool calls per job.

The foreground command costs a second process per foreground run, and the
runner notices the end of the command through its event loop, which wakes at
once on the pipe's end of file.

### Limits

- If the runner itself is killed with SIGKILL (by `kill -9` or the OOM
  killer, not by any harness path), pi sees its stdin close while its stdout
  has no reader, exits on EPIPE, and the commands and background tasks of the
  agent keep running. `status` reports `died`. This is accepted: nothing in
  the normal flow sends SIGKILL to a runner, and `kill NAME` sends SIGTERM
  first and waits 10 seconds.
- If the caller's pi is killed with SIGKILL, it cannot stop its commands. A
  foreground command started by its `bash` tool keeps running, and so does
  the job, until the agent finishes.
- A process that calls `setsid` itself leaves pi's process tree. A
  background job is such a process by design.

## Nesting

Every depth uses the same rules. An agent can start other agents in the
foreground, in the background, or through its harness's background tool,
and the script respects that choice.

`MULTI_AGENT_DEPTH` counts the depth: unset or 0 for the user's own agent, 1
for an agent started by it, and so on. `run` and `resume` refuse to start
when the current depth is at least `MULTI_AGENT_MAX_DEPTH` (default 2). With
the default, the user's agent can start agents and those agents can start one
more level.

## State

### Locations

- Job directories: `$XDG_STATE_HOME/pi-multi-agent/<project slug>/jobs/<name>/`,
  with `XDG_STATE_HOME` defaulting to `~/.local/state`.
- Session files: `$PI_CODING_AGENT_DIR/multi-agent-sessions/<project slug>/`,
  with `PI_CODING_AGENT_DIR` defaulting to `~/.pi/agent`. They stay out of
  pi's `/resume` list.

The project slug is the logical cwd with every character other than letters
and digits replaced by `-`, as in the handoff skill. The logical cwd is
`$PWD` when it refers to the current directory, so symlinked paths keep their
name.

### Job directory

| File | Content |
|---|---|
| `job.json` | Launch settings: name, profile, resolved pi arguments, cwd, the job that started it, depth, timeout, run mode. `resume` reads it. |
| `prompt.txt` | Prompt of the latest run. |
| `system-prompt.md` | The profile's body in `replace` mode, passed with `--system-prompt`. |
| `append-prompt.md` | Text passed with `--append-system-prompt`: the profile's body in `append` mode, then the multi-agent block. |
| `status` | `running`, `done`, `failed`, `aborted`, `timeout`, `killed` or `died`. |
| `runner_pid`, `pi_pid` | Process IDs of the current run. |
| `started_at`, `ended_at` | UTC timestamps of the current run. |
| `runs` | Number of runs. |
| `exit_code` | Exit code of the current run, written last. |
| `output.txt` | Final assistant text of the current run. |
| `model` | `provider/model` of the latest assistant message, for the result footer. |
| `stderr.txt` | pi's stderr. |
| `progress.log` | One line per tool call, retry, compaction, denied dialog, steering message, and run start and end, across runs. |
| `usage.json` | Token totals across runs. |
| `killed` | Marker written by `kill` and by the runner's signal handler. |
| `inbox/` | Control requests and acknowledgements. |
| `runner.log` | Traceback when the runner itself fails. |

State files are written through a temporary file and a rename.

A job whose status is `running` but whose runner pid is gone, and which has
no `exit_code` for the current run, is reported as `died`. `status`, `wait`
and `list` apply this check.

## SKILL.md contents

The skill is named `multi-agent`: SKILL.md's frontmatter has
`name: multi-agent`, matching its directory.

SKILL.md tells the agent:

1. When to delegate: independent research, broad searches, and parallel work
   whose intermediate output should stay out of the caller's context. Not for
   small tasks it can do directly.
2. How to pick a profile: run `profiles` once per session.
3. How to run:
   - Foreground for short single tasks.
   - For long or parallel tasks, run the foreground command with the
     harness's built-in tool for background commands when there is one. The
     job then stops when the caller's session ends. If the tool can wake the
     agent on each output line, turn that off and wait for the exit. If the
     tool has its own timeout, disable it or make it longer than the job's
     `--timeout`, so the script's timeout decides when the job stops and the
     job can record its status.
   - Use `--background` when there is no such tool, or when the work should
     continue after the caller ends. A background job keeps running until it
     finishes or reaches its timeout. Tell the user its name.
   - Do not put the command in the background with `&`; use `--background`.
   - Pass prompts longer than one line through `-` and a heredoc.
   - Write prompts that are self-contained; the agent does not see the
     caller's conversation unless `--fork` is used.
4. How to wait: one `wait NAME --timeout 120` per tool call, repeated while
   the exit code is 2. Do not chain several `wait` commands in one tool call.
5. How to correct a running job: `steer`, `abort`, `kill`. How to continue a
   finished one: `resume`. Use `kill --all` only when the user asks.
6. Keep at most 4 jobs running at once unless the user asks for more.
7. The result footer is for the caller; report the agent's findings to the
   user in the caller's own words.

`reference.md` holds the full option tables, status meanings, state layout
and troubleshooting.

SKILL.md and reference.md name no particular extension or harness tool.
They describe background tools in general terms, so the skill works in any
harness that can run a shell command.

They also do not mention permission modes such as YOLO. The user decides
the permission mode; the agent has no say in it, so the skill gives it no
option or instruction about it.

## Interaction with other extensions

The skill and the runner depend on no particular extension. The extensions
below change how an agent behaves, so the design accounts for them. Without
the extension, the skill still works.

### pi-minimal-permission-system: approval prompts

No person can answer an approval prompt in these agents (see "Dialog
requests"). The runner sets `PI_MINIMAL_PERMISSION_SYSTEM_NONINTERACTIVE=1`,
and the extension reads it at `session_start`. It then blocks `ask` rules
with "requires approval, but no interactive UI is available" instead of
opening a dialog, so the agent reports the block correctly.

### pi-minimal-permission-system: YOLO

The skill and the runner do nothing about YOLO. The extension publishes the
session's YOLO state in `PI_MINIMAL_PERMISSION_SYSTEM_YOLO`, and an agent
inherits it through the environment like any other variable, so it runs in
the same mode as its caller at the time it started. A later `/yolo` toggle in
the caller does not reach an agent that is already running.

### pi-background-tasks: session variables and lifetime

An agent can run a foreground `multi_agent.py run` through `bg_task` or
`/bg run`. Both remove `PI_SESSION_ID`, `PI_SESSION_FILE`, `PI_PROVIDER`,
`PI_MODEL` and `PI_REASONING_LEVEL` from the inherited environment and set
them from the current session when the task starts, as pi's `bash` tool
does. The job therefore gets the caller's model and thinking level, and
`--fork` copies the caller's conversation. This matters inside a started
agent too: pi's own `process.env` there holds the values of the agent that
started it, and those values do not reach the task.

On `session_shutdown` pi-background-tasks sends SIGTERM to each task's
process group, and SIGKILL when pi exits. A foreground command started this
way therefore ends with the caller's session, and its runner stops the job
(see "Lifetime").

## Tests

`tests/fake-pi` is an offline stand-in for `pi --mode rpc`. Its behavior is
set by environment variables in each test: reply text, delay before
settling, stop reason, a dialog request to emit, exiting early, ignoring
SIGTERM. It records the arguments and environment it was started with, its
process group, and whether it exited because its stdin closed.

`tests/test_multi_agent.py` covers:

- Profile discovery order, whole-file replacement without field merging,
  `enabled: false` hiding a name, frontmatter parsing, and the resulting pi
  arguments for each field.
- Model and thinking precedence.
- The child environment: `MULTI_AGENT_JOB`, `MULTI_AGENT_DEPTH`, the no-UI
  variable, and no `HERDR_*` variables.
- pi starts in its own session, and the runner in another one, apart from
  the foreground command.
- Foreground run: result format, exit codes, and the stderr start line.
- SIGKILL to the foreground command's process group: the runner stops pi
  and records `killed`.
- Two nested foreground runs, with fake-pi running the inner command as a
  tool call whose process group it kills when its stdin closes: SIGKILL to
  the outer command's group stops both jobs and every process.
- Background run: detaching, surviving the caller's exit, `wait` with and
  without timeout, `result`.
- `steer` and `abort` through the inbox, including the acknowledgement and
  the "not running" error.
- Dialog requests answered with `cancelled`.
- Timeout handling.
- Signal handling, `kill NAME` (also on a foreground run) and `kill --all`.
- SIGKILL to the runner: fake-pi sees its stdin close and exits, and the job
  is reported as `died`.
- The depth limit.
- Records containing U+2028, to check that framing splits on `\n` only.

## Checks with real pi before implementation is final

1. `--system-prompt <file> -nc`: which parts of pi's prompt remain (skill
   list, tool descriptions, `APPEND_SYSTEM.md`).
2. `--fork "$PI_SESSION_FILE"` while the caller's own `bash` tool call is
   still running: how pi handles the unfinished tool call at the end of the
   copied session.
3. `--session-dir <dir> --session <name>` finds the session for `resume`.
4. pi-minimal-permission-system in an agent: an `ask` rule blocks the tool
   call with the no-UI reason, no dialog request reaches the runner, and the
   agent continues and reports the block.
5. With `HERDR_*` removed from the agent's environment, the caller's herdr
   pane state stays correct while agents run and finish.
6. Start-up time and memory of one agent process, and of four running in
   parallel.
7. While an agent runs a long `bash` command and has a `bg_task` task: SIGKILL
   to the foreground command's process group stops pi, the `bash` command's
   process tree and the task. Repeat with the caller's `bash` tool call
   aborted from the TUI, and with two nested foreground runs. SIGKILL to the
   runner itself shows the accepted gap in "Limits".
