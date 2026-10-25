# Job lifecycle

A job is a named agent and its conversation. `run` creates the job and starts
its first run; each `resume` starts another run in the same conversation. A
job has at most one run at a time, and its status describes the latest run.

## Statuses

| Status | Meaning | Result text |
|---|---|---|
| `running` | The agent is working. | — |
| `done` | The agent finished its turn. | Its final message. |
| `failed` | The run could not finish; see below. | The agent's last message, if any, then an `error:` line. |
| `aborted` | `abort` stopped the agent, or something else aborted its turn. | Its last message. |
| `timeout` | The run reached its `--timeout`. | Its last message. |
| `killed` | `kill`, a signal to the job's runner, or the end of a foreground command stopped the run. | Its last message. |
| `died` | The job's runner ended without recording a result, for example after a SIGKILL. | None; the result reads `(no output)`. |

A run ends as `failed` when the model returns an error after pi's own retries,
pi rejects the prompt, pi cannot start, or pi exits during the run. In the
last case the result holds the end of pi's stderr. A model or tool name that
pi rejects is a common cause.

"Last message" is the text of the agent's latest message in this run. It is
empty when that message had only tool calls, or when the run stopped before
the agent wrote anything; the result then reads `(no output)`. A message cut
short by an abort keeps the text written before it.

A prompt that pi handles by itself without starting the agent, such as an
extension command, ends the run at once as `done` with no output.

## How a run stops

- **The agent finishes.** The status is `done`, or `failed` if its last
  message was an error.
- **`abort`.** Steering messages that the agent has not read are discarded and
  pi is asked to abort the current turn. The agent gets 30 seconds to wind
  down; after that the run is stopped anyway. The status is `aborted`.
- **Timeout.** The limit counts from the start of the run. When it is
  reached, the run is aborted as above and ends as `timeout`.
- **`kill`, a signal, or the end of a foreground command.** The run stops at
  once without the 30-second wind-down. The status is `killed`.

In every case pi is shut down cleanly: it gets 10 seconds to exit, then
SIGTERM, then SIGKILL 5 seconds later. A run that hits its timeout can
therefore take up to 45 seconds to end. `steer` and `abort` fail with `job is
not running; use resume` as soon as the run has started to end.

## Foreground and background runs

Every run is driven by its own runner process, detached from the caller's
terminal and process group.

- A foreground run is tied to the command that started it. Whatever ends that
  command, SIGKILL included, stops the agent and the commands it was running.
  On SIGINT, SIGTERM or SIGHUP the command waits for the run to stop and
  prints its result; a second signal ends it at once. A foreground run started
  by another agent is tied to that agent's command in the same way, so a chain
  of foreground runs stops together.
- A background run keeps going after its caller ends, until it finishes,
  reaches its timeout, or is stopped with `kill`.
- If a runner is killed with SIGKILL, its job's status becomes `died` at once.
  The agent's pi process usually exits within a second, but commands the agent
  was running may keep running.

## Timeouts

A timeout applies to one run. `run` and `resume` default to 3600 seconds in
the background and to no limit in the foreground; `--timeout 0` removes the
limit, and fractional seconds are allowed. A timeout does not carry over to
the next run.

## Dialogs and approvals

No person watches an agent. When an extension inside the agent opens a dialog
(select, confirm, input or editor), it is cancelled at once and logged in the
progress log as `denied:`. Extensions that have a non-interactive mode run in
it (see `state.md`), so tool calls that need a person fail without opening a
dialog. The agent is told not to retry them and to report what it could not
do in its final message.

## Nesting

An agent can start agents of its own. `PI_MULTI_AGENT_DEPTH` counts the depth:
0 or unset for the user's agent, 1 for an agent it started, and so on. `run` and
`resume` refuse to start when the caller's depth is at least
`PI_MULTI_AGENT_MAX_DEPTH` (default 2), so by default the user's agent and its
agents can delegate, and their agents cannot. Agents inherit
`PI_MULTI_AGENT_MAX_DEPTH` from the environment of the user's agent.

`PI_MULTI_AGENT_JOB` holds the job directory of the agent that is running.
`status` and `list` use it to show the job that started each job.
