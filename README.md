# pi-multi-agent-skill

A skill that lets a [pi](https://pi.dev) agent start other pi agents, run them
in parallel or in the background, and wait for, steer, stop or resume them.

Each agent is a separate `pi --mode rpc` process, so it loads the same
extensions, MCP servers, skills and settings as an interactive pi. A Python
script, `multi_agent.py`, starts the process, sends the prompt, answers dialog
requests, records progress and keeps each agent's state as a named job. The
calling agent drives the script with the shell commands described in
`SKILL.md`.

## Install

Link the skill into pi's skills directory:

```bash
ln -s "$PWD/skills/multi-agent" ~/.pi/agent/skills/multi-agent
```

Requirements: pi and Python 3.9 or later. The script uses only the standard
library.

## Usage

The agent drives the skill, but the script is a normal command-line tool:

```bash
multi_agent.py run --profile Explore "Where is the retry policy defined?"
multi_agent.py run --background --name review "Review the error handling in src/server/"
multi_agent.py wait review --timeout 120
multi_agent.py steer review "Skip generated files"
multi_agent.py resume review "Now check the client side"
multi_agent.py list
```

Two profiles ship with the skill: `general-purpose`, which behaves like the
caller's own pi, and `Explore`, a read-only search agent. Profiles in
`~/.pi/agent/agents/` and `<project>/.pi/agents/` replace them or add more.

See [`skills/multi-agent/reference.md`](skills/multi-agent/reference.md) for
every command and option, the profile format, the state layout and
troubleshooting.

## Tests

```bash
python3 -m unittest discover tests
```

The tests run the script against `tests/fake-pi`, an offline stand-in for
`pi --mode rpc`, so they call no model.

## Credits

The `Explore` profile's prompt and description come from
[pi-subagents](https://github.com/tintinweb/pi-subagents) by tintinweb,
used under the MIT license.

## License

MIT
