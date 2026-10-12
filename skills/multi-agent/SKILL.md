---
name: multi-agent
description: Start other pi agents and delegate work to them. Use it when a task benefits from a multi-agent workflow, such as splitting work across agents, running it in parallel or in the background, or keeping a side task out of your own context.
---

# Multi-agent

This skill starts other agents and manages each one as a named job. An agent
does not see your conversation unless you start it with `--fork`, and you see
only its final message.

`SKILL_DIR` below is the directory that contains this SKILL.md. Every command
has the form:

```bash
python3 "$SKILL_DIR/scripts/multi_agent.py" <command> ...
```

## Pick a profile

A profile sets an agent's system prompt, model, thinking level and tools. Run
this once per session to see the available profiles:

```bash
python3 "$SKILL_DIR/scripts/multi_agent.py" profiles
```

There are two built-in profiles. `general-purpose` (the default) has your
tools and settings. `Explore` is a read-only search agent. Pass a profile
with `--profile NAME`.

## Run an agent

For a short single task, run it in the foreground. The command blocks until
the agent finishes and prints its result:

```bash
python3 "$SKILL_DIR/scripts/multi_agent.py" run --profile Explore "Where is the retry policy for HTTP calls defined?"
```

Pass a prompt longer than one line through `-` and a heredoc:

```bash
python3 "$SKILL_DIR/scripts/multi_agent.py" run --name auth-review - <<'EOF'
Read src/auth/ and list every place that ...
EOF
```

Write prompts that are self-contained: give the goal, the relevant paths and
what the answer should contain. When the task builds on your conversation so
far, add `--fork` instead: the agent starts from a copy of it.

For a long task, or several tasks in parallel:

- If your harness has a tool that runs a shell command in the background, run
  the foreground `run` command with it. The job then stops when your session
  ends. The command prints `multi-agent: started job NAME` first; use that
  name for `steer` and `abort`. If the tool can wake you on each output line,
  turn that off and wait for the command to exit. If the tool has its own
  timeout, see "Timeouts" below.
- Otherwise, or when the work must continue after your session ends, add
  `--background`. The command prints the job name and returns at once. A
  background job runs until it finishes or reaches its timeout (one hour by
  default). Tell the user the job's name.
- Never put the command in the background with `&`; use `--background`.

Run only as many jobs at once as the task needs.

## Timeouts

- A foreground `run` or `resume` has no timeout unless you pass
  `--timeout S`. It stops when the agent finishes or when the command ends,
  for example when your tool call is cancelled or your session ends.
- A `--background` job stops after one hour unless you pass `--timeout S`.
  `--timeout 0` removes the limit.
- When a background tool of your harness runs the foreground command and the
  tool has its own timeout, pass a shorter `--timeout`, leaving a grace
  period so the job stops the agent and records its result before the tool
  kills it. If you disable the tool's timeout instead, the job runs until
  the agent finishes or your session ends.

## Wait for a result

```bash
python3 "$SKILL_DIR/scripts/multi_agent.py" wait NAME --timeout 120
```

Exit code 2 means the job is still running; run the same command again in a
new tool call. Wait for one job per tool call and do not chain several `wait`
commands in one call: a message that steers you arrives only after your
current tool call returns.

The output is the agent's final message followed by a `--- multi-agent ---`
footer. The footer is for you. Report the agent's findings to the user in your
own words.

Exit code 0 means the job ended with status `done`; 1 means it ended another
way (`failed`, `aborted`, `timeout`, `killed`, `died`), and the output says
why.

## Correct or continue a job

```bash
python3 "$SKILL_DIR/scripts/multi_agent.py" steer NAME "Skip the tests directory"
python3 "$SKILL_DIR/scripts/multi_agent.py" abort NAME
python3 "$SKILL_DIR/scripts/multi_agent.py" kill NAME
python3 "$SKILL_DIR/scripts/multi_agent.py" resume NAME "Now check the callers too"
```

- `steer` adds a message that the agent reads before its next step.
- `abort` stops the agent and keeps its last message as the result.
- `kill` stops a job that does not respond to `abort`.
- `resume` continues a finished job's conversation with a new prompt.

`kill --all` stops every running job of the current project. Use it only when
the user asks.

## Inspect jobs

```bash
python3 "$SKILL_DIR/scripts/multi_agent.py" list
python3 "$SKILL_DIR/scripts/multi_agent.py" status NAME
python3 "$SKILL_DIR/scripts/multi_agent.py" peek NAME
```

`peek` shows the job's recent tool calls.

## References

The `references/` directory next to this file has the details. Read a file
only when you need it:

- `commands.md`: every command and option, output formats and exit codes.
- `lifecycle.md`: statuses, how a run stops, foreground and background
  lifetime, timeouts, dialogs and nesting.
- `profiles.md`: writing a profile, where profiles are looked up, and how the
  model, tools and system prompt are chosen.
- `state.md`: where jobs and conversations are stored, and the agent's
  environment.
- `troubleshooting.md`: error messages and what to do about them.
