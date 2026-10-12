# pi-multi-agent-skill

A minimal, lightweight multi-agent skill and CLI for [pi](https://pi.dev). It
needs no extension and adds no tools. Your agent starts other agents, runs
them in parallel or in the background, and waits for, steers, stops or
resumes them. Any harness or agent that has a shell can use it.

It aims to be minimal while keeping subagents as useful as an extension and
tool based approach. A subagent extension adds its tool definitions to every
request, while this skill adds only a few dozen tokens of description to your
agent's context until it decides to delegate.

## Install

Install the skill as a pi package from npm:

```bash
pi install npm:pi-multi-agent-skill
```

Or install directly from GitHub:

```bash
pi install git:github.com/milanglacier/pi-multi-agent-skill
```

You can also install the skill with the open skills CLI:

```bash
npx skills install milanglacier/pi-multi-agent-skill --global --agent pi
```

The CLI copies the skill to `~/.agents/skills/multi-agent`, which pi reads.
You can also link it into pi's skills directory yourself:

```bash
ln -s "$PWD/skills/multi-agent" ~/.pi/agent/skills/multi-agent
```

Requirements: pi and Python 3.9 or later. Nothing else to install.

## Features

- **Small token cost.** A subagent extension adds its tool definitions to
  every request, whether or not your agent delegates anything. With this
  skill, your agent's context holds only the skill's description, a few
  dozen tokens, until it decides to delegate. Then it loads SKILL.md, which
  is about 1.2K tokens and is all it needs for a fully working multi-agent
  setup. The details of each command stay in separate references that it
  reads only when needed.
- **Everything a subagent tool does, without the tool.** Your agent gets the
  same controls a dedicated extension would give it: start agents, wait for
  them, `steer` one with a correction, `abort` it and keep what it has so
  far, `resume` a finished job with a follow-up task, and see what each job
  is doing. You can run the same commands yourself from a terminal.
- **Parallel and background agents.** Run a quick question in the
  foreground, or start several jobs at once and collect their results later.
- **Stops when you stop.** A foreground agent ends when the command that
  started it ends: press Esc, cancel the tool call or close the session, and
  the agent and the commands it was running stop with it, at every level of
  nesting. Background jobs keep running until they finish or time out.
- **Your full pi setup.** Each agent loads the same extensions, MCP servers,
  skills and settings as an interactive pi, and uses your model unless its
  profile picks another.
- **Profiles.** They work as in other subagent tools: each one sets an
  agent's system prompt, model, thinking level and tools.
- **Fork your conversation.** `--fork` hands the agent everything you have
  discussed so far, for tasks that need that context.
- **Works outside pi.** Claude Code, Codex or any other agent with a shell
  can run the same commands to delegate work to pi agents.

## Non-goals

These are deliberate non-goals, to keep the skill small and free of bloat.
Each is better as a separate extension or skill that you pick to match your
own workflow. This follows the minimalism of pi itself and the Unix
philosophy: do one thing well and compose.

- **Memory.** Agents do not share or keep memory between jobs. Use a memory
  extension, or have agents write notes to files.
- **Scheduling.** There are no cron-style or recurring agents. Use cron,
  `systemd` timers or a scheduler extension to run the commands.
- **Worktrees.** Agents are not isolated in git worktrees. Just prompt the
  agent to use a worktree.
- **Dynamic workflows.** There is no scripting layer for orchestration such
  as pipelines, parallel fan-out with gates, or phases. Your agent can
  already start, wait for and chain jobs with plain commands. For
  deterministic scripts, use an extension such as
  [pi-dynamic-workflow](https://github.com/milanglacier/pi-dynamic-workflow).

## Usage

Your agent drives the skill on its own, but the commands also work by hand:

```bash
multi_agent.py run --profile Explore "Where is the retry policy defined?"
multi_agent.py run --background --name review "Review the error handling in src/server/"
multi_agent.py wait review --timeout 120
multi_agent.py steer review "Skip generated files"
multi_agent.py resume review "Now check the client side"
multi_agent.py list
```

See [`skills/multi-agent/references/`](skills/multi-agent/references/) for
every command and option, how jobs start and stop, the profile format, where
state is kept and troubleshooting.

## Tests

```bash
python3 -m unittest discover tests
```

The tests use an offline stand-in for pi, so they call no model.

## Releases

Set the version in `package.json`, commit it, and push a matching tag such
as `v0.1.1`. The GitHub workflow runs the tests and publishes to npm.
Prerelease versions use the `next` npm tag; stable versions use `latest`.
The npm trusted publisher is `milanglacier/pi-multi-agent-skill` with the
workflow filename `publish.yml` and permission to publish directly.

## License

MIT
