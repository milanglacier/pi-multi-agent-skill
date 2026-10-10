# Profiles

A profile sets an agent's system prompt, model, thinking level and tools. It
is a Markdown file with frontmatter; the body after the frontmatter is the
profile's prompt.

```markdown
---
description: Reviews a diff for correctness bugs.
model: anthropic/claude-sonnet-5
thinking: high
tools: read, grep, find, ls, bash
---

You review code changes. Report each bug with its file, line and a failing
scenario.
```

## Where profiles come from

Profiles are looked up in three directories, from highest to lowest priority:

1. `<cwd>/.pi/agents/*.md` (project), where `<cwd>` is the agent's working
   directory
2. `$PI_CODING_AGENT_DIR/agents/*.md`, default `~/.pi/agent/agents/` (user)
3. `profiles/*.md` in this skill's directory (built-in)

Names are matched case-insensitively. When several directories define the
same name, the file from the highest directory is used as a whole; fields are
never merged across files. To change one field of a built-in profile, copy the
whole file and edit the copy. A profile with `enabled: false` hides its name,
including lower profiles of that name.

A job keeps the settings it was created with. Editing a profile affects new
jobs only, not `resume` of an existing one.

## Fields

| Field | Default | Effect |
|---|---|---|
| `name` | file name without `.md` | Profile name. |
| `description` | `name` | Shown by `profiles`. |
| `model` | the caller's model | Passed to `pi --model`. |
| `thinking` | the caller's thinking level | Passed to `pi --thinking`. |
| `tools` | pi's default tool selection | Passed to `pi --tools`. |
| `disallowed_tools` | none | Passed to `pi --exclude-tools`. |
| `prompt_mode` | `append` | `append` or `replace`; see below. |
| `enabled` | `true` | `false` hides the profile. |

Unknown fields are ignored. The parser reads `key: value` lines, quoted
strings, `true`/`false` and `[a, b]` lists, and skips comments and indented
lines; it does not support nested YAML.

## Model and thinking level

For each of the two, the first value found wins: the `run` option, the profile
field, the caller's current setting, then pi's configured default. The
caller's settings come from the `PI_PROVIDER`, `PI_MODEL` and
`PI_REASONING_LEVEL` variables that pi sets for the commands it runs.

## Tools

`tools` and `disallowed_tools` accept a comma-separated list or `[a, b]`. The
entries are passed to pi verbatim as `--tools` and `--exclude-tools`; see pi's
documentation of those options for the patterns they accept and which tools
they keep.

## System prompt

With `prompt_mode: append`, the profile's body is passed to pi with
`--append-system-prompt`, so it is added to the system prompt pi builds as
usual. With `prompt_mode: replace`, the body is passed with `--system-prompt`
and pi also gets `--no-context-files`. Any other value makes `run` fail.

Every agent also receives a short block that tells it its job name, that only
its final message is read, and that no person can approve blocked tool calls.
This block is passed with `--append-system-prompt` in both modes, so pi does
not load `APPEND_SYSTEM.md` files for agents; put text that agents need in a
profile's body.
