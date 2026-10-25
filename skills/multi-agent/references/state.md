# State

## Locations

Job directories are in
`$XDG_STATE_HOME/pi-multi-agent/<project>/jobs/<name>/` (default
`~/.local/state`). `<project>` is the project's directory with every character
other than a letter or digit replaced by `-` and the leading `-` removed, so
`/home/me/app` becomes `home-me-app`.

A running job's runner listens on a Unix socket in
`$XDG_STATE_HOME/pi-multi-agent/sockets/`, which `steer`, `abort` and `kill`
connect to. Conversations are pi session files in
`$PI_CODING_AGENT_DIR/multi-agent-sessions/<project>/` (default
`~/.pi/agent/`), so they stay out of pi's `/resume` list.

## Job files

| File | Content |
|---|---|
| `job.json` | Launch settings: profile, pi arguments, cwd, parent job, depth, run mode, timeout. |
| `prompt.txt` | Prompt of the latest run. |
| `system-prompt.md` | The profile's prompt, when it replaces pi's system prompt. |
| `append-prompt.md` | Text added to pi's system prompt: the profile's prompt in append mode, then the multi-agent block. |
| `status` | Status of the latest run; final once `exit_code` exists. |
| `lock` | Held by the job's runner for as long as it runs. The job is running exactly while this lock is held. |
| `started_at`, `ended_at` | UTC timestamps of the latest run. |
| `runs` | Number of runs. |
| `exit_code` | Exit code of the latest run, written last. A run without it that is no longer running has `died`. |
| `output.txt` | Result text of the latest run; empty if that run produced none. |
| `model` | Model that wrote the latest message. |
| `stderr.txt` | pi's stderr for the latest run. |
| `progress.log` | The lines `peek` prints, across runs. |
| `usage.json` | Token and cost totals across runs. |
| `runner.log` | Traceback when the runner itself fails. |

Starting a run clears the previous run's result, end time and model, so these
files always describe the latest run.

## Reading a conversation

To read a finished job's whole conversation in interactive pi, run the
`session_cmd` that `status` prints. Do not open it while the job is running,
because two processes would then write the same session file.

## The agent's environment

The script starts the program in `MULTI_AGENT_PI_BIN` (default `pi`). An
agent inherits the environment of the command that started it, plus:

| Variable | Value |
|---|---|
| `MULTI_AGENT_JOB` | The agent's job directory. |
| `MULTI_AGENT_DEPTH` | The agent's nesting depth; see `lifecycle.md`. |
| `PI_MINIMAL_PERMISSION_SYSTEM_NONINTERACTIVE` | `1`, so that extension never waits for an approval. |
| `PI_REQUEST_USER_INPUT_NONINTERACTIVE` | `1`, so that extension never asks the user a question. |
| `PWD` | The agent's working directory. |
