# pi-multi-agent-skill

A minimal, lightweight multi-agent skill and CLI for [pi](https://pi.dev). It
needs no extension and adds no tools. Your agent starts other agents, runs
them in parallel or in the background, and waits for, steers, stops or
resumes them. Any harness or agent that has a shell can use it.

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
- **No hanging on prompts.** Agents never wait on a question nobody can
  answer. A blocked approval comes back in the agent's result, so you can run
  that step yourself.
- **Works outside pi.** Claude Code, Codex or any other agent with a shell
  can run the same commands to delegate work to pi agents.

## Install

Install the skill with the open skills CLI:

```bash
npx skills install milanglacier/pi-multi-agent-skill --global --agent pi
```

The CLI copies the skill to `~/.agents/skills/multi-agent`, which pi reads.
You can also link it into pi's skills directory yourself:

```bash
ln -s "$PWD/skills/multi-agent" ~/.pi/agent/skills/multi-agent
```

Requirements: pi and Python 3.9 or later. Nothing else to install.

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

## License

MIT
