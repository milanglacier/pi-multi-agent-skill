# Troubleshooting

## Job status

- **Status `failed` with `pi exited before the run finished`.** pi stopped
  during start-up or the run. The output holds the end of pi's stderr; the
  whole log is in the job's `stderr.txt`. A model or tool name that pi rejects
  causes this.
- **Status `failed` with `the runner failed`.** The script itself hit an
  error. The traceback is in the job's `runner.log`.
- **Status `died`.** The runner was killed without a chance to record a
  result. The agent's pi process exits by itself, usually within a second,
  but commands it started can survive; look for them with `ps` if the agent
  was running a long command. `peek` shows how far it got; `resume`
  continues the conversation. Wait a few seconds before you `resume` a job
  that has just died, so its old pi has exited and does not write the
  conversation at the same time as the new one.
- **An agent reports that a tool call needs approval.** Agents cannot ask a
  person. Run that step yourself, or tell the user what was blocked.
- **A job lingers after its caller is gone.** Background jobs outlive their
  caller by design. Stop them with `kill NAME`, or `kill --all` when the user
  asks.

## Command errors

- **`job is not running; use resume`.** The run has ended or is ending.
  `resume` the job with a new prompt instead of steering it.
- **`job NAME already exists in this project`.** Pick another `--name`.
  The error shows the existing job's status and age. Use `resume` or `rm`
  only for a job you started or the user asked about.
- **`job NAME is running` from `resume` or `rm`.** The job's runner is
  still alive, or another command was starting the job at the same moment.
  `wait` for it, or `kill` it first.
- **`unknown profile: P (available: ...)`.** No enabled profile has that name
  in the agent's working directory; the message lists the ones that exist.
- **`--fork needs PI_SESSION_FILE`.** The command does not run inside pi, so
  there is no conversation to copy. Drop `--fork` and write a self-contained
  prompt.
- **`cannot start an agent: ... MULTI_AGENT_MAX_DEPTH`.** The caller is too
  deep in a chain of agents. Do the work directly.
- **`the runner of job NAME is not responding`.** The runner did not answer
  `kill` within 10 seconds, or did not exit within 20 seconds after it
  answered. The message ends with a `pkill` command that stops that runner;
  run it, then wait a few seconds before you `resume` the job (see `died`).
- **`the job's working directory no longer exists`.** `resume` runs the
  agent in the directory the job was started in. Start a new job instead.
- **`job NAME exists in several projects`.** Run the command from the
  directory where the job was started.

## Environment

- **`cannot find the pi program`.** Set `MULTI_AGENT_PI_BIN` to the command
  that starts pi, such as `pi-bolt`, or ask the user which one they use.
- **`flock does not work on ...`.** The state directory is on a file system
  without working file locks, such as some NFS or SMB mounts. Set
  `XDG_STATE_HOME` to a directory on a local file system.
- **`the socket path for this job is too long`.** The socket path under
  `XDG_STATE_HOME` exceeds the system's limit for Unix sockets (about 100
  bytes). Set `XDG_STATE_HOME` to a shorter path.
