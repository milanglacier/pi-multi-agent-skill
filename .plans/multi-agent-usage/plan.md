# Plan: multi-agent usage report

Status: implemented (2026-11-04).

## Goal

Add a standalone `skills/multi-agent/scripts/usage.py` that reads the pi
sessions the multi-agent skill keeps outside pi's normal session directory
and prints a ccusage-style report. It shows token counts and the cost pi
recorded for each request. It supports views by time, project, job, model and
profile.

The script is independent. `multi_agent.py`, `SKILL.md` and `package.json` do
not change.

## Data sources

Pi keeps these sessions in
`$PI_CODING_AGENT_DIR/multi-agent-sessions/<project-slug>/*.jsonl` (default
`~/.pi/agent`). A session header has the agent's working directory in `cwd`.
The filename ends with the job name, and the session header has the same job
name as its `id`.

Job metadata is in
`$XDG_STATE_HOME/pi-multi-agent/<project-slug>/jobs/<name>/job.json` (default
`~/.local/state`). The script joins it by `(project-slug, job-name)` to get
the project, working directory, profile, parent job and depth. The job's
`project` is the project the job belongs to, which can differ from its
working directory after `--cwd`. The report groups by that project when the
metadata exists, and falls back to the header's `cwd`. The session files
remain the only required source; missing or removed job directories do not
stop the report.

Each usage record has an ISO timestamp, a provider, a model, input, output,
cache read, cache write, total tokens, reasoning and a cost object. The cost
comes from pi, so the script never recomputes prices from a model table.

The script counts the same records pi counts in a session total:

- assistant messages with `usage`;
- `usage` entries such as cache warming;
- `usage` on compaction and branch-summary entries.

`reasoning` is already part of output, so it is not added again. A record
without a cost is counted as unpriced and the report says so. Counting
standalone `usage` entries can make a total slightly higher than the job's
`usage.json`, because the runner only records assistant messages. This is the
session's full recorded cost, which is the number the report should show.

Multiple files can hold the same job after a resume. The script groups files
by `(project-slug, job-name)` and adds their usage together.

## Command line

```
usage.py                      overview
usage.py daily   [--days N]   last 30 days
usage.py weekly  [--weeks N]  last 12 weeks
usage.py monthly [--months N] last 12 months
usage.py project              totals per project
usage.py session [--limit N]  totals per job
usage.py model   [--limit N]  totals per provider/model
usage.py profile [--limit N]  totals per profile
```

Aliases: `summary` for `overview`, `jobs` for `session`.

Shared options:

| Option | Meaning |
|---|---|
| `--since YYYY-MM-DD` | First local date to include. |
| `--until YYYY-MM-DD` | Last local date to include. |
| `--project TEXT` | Keep projects whose path contains TEXT. Repeatable. |
| `--all` | Remove the default day, week or month limit. |
| `--timezone NAME` | IANA timezone; default is the local timezone. |
| `--json` | Print one JSON object instead of tables. |
| `--no-color` | Disable ANSI color. `NO_COLOR` also works. |

`daily`, `weekly` and `monthly` include empty periods inside the requested
range, so gaps in activity are visible. `--all` starts at the first usage
record and ends at today. `--since` and `--until` are inclusive and apply to
every view. A `--since` later than `--until` is an error.

## Output

Tables are aligned with a light separator. They use ANSI color only when
stdout is a TTY and color is not disabled. Token counts use thousands
separators; the overview uses short forms such as `12.3M` to stay narrow.
Long project paths are shortened with `~` and a middle ellipsis to fit the
terminal width. A totals row ends every table.

`overview` prints the all-time total cost, the token breakdown, the number of
sessions and turns, the top projects, the top models, and the last seven
days. It ends with the list of available views:

```
Views: overview daily weekly monthly project session model profile
```

`daily`, `weekly` and `monthly` show these columns:

```
Period  Sessions  Turns  Input  Output  Cache R  Cache W  Total tokens  Cost
```

`project` shows project path, sessions, turns, total tokens, cost and last
active time. `session` shows job, project, profile, models, turns, total
tokens, cost and last active time. `model` and `profile` show their label,
sessions, turns, total tokens, cost and share of total cost. The session view
keeps depth and parent in JSON only, so the table stays narrow.

With `--json`, every view returns:

```json
{
  "generated_at": "2026-11-04T12:00:00+08:00",
  "view": "project",
  "timezone": "Asia/Shanghai",
  "period": {"since": "...", "until": "..."},
  "totals": {
    "sessions": 0,
    "turns": 0,
    "input": 0,
    "output": 0,
    "cache_read": 0,
    "cache_write": 0,
    "total_tokens": 0,
    "cost": 0.0
  },
  "rows": []
}
```

Each row has its key and label plus the same counters. The `session` rows also
have `job`, `project`, `profile`, `models`, `depth`, `parent`, `mode`,
`started_at` and `last_active_at`. JSON always uses exact integers and full
cost values.

## Errors and edge cases

- No session directory or no session files prints a short message and exits
  zero, because an empty report is not an error.
- A malformed or partial JSONL line is skipped. The final line of a running
  session can be incomplete, so this must not abort the whole report.
- A session without `cwd` falls back to its directory slug as the project
  label. A session without job metadata shows `-` as its profile.
- A session with no usage records is still counted as a session with zero
  tokens.
- Unknown views and bad dates are argparse errors with exit code 2.

## Tests

Add `tests/test_usage.py` with temporary session and state directories. It
sets `PI_CODING_AGENT_DIR` and `XDG_STATE_HOME`, so no real data is read. The
tests cover:

- assistant usage and standalone `usage` entries, with reasoning not counted
  twice;
- cost read from the record, not recomputed;
- project grouping by the job project, then `cwd`, then the slug;
- job metadata join, including a missing `job.json` and a job whose project differs from its working directory;
- daily, weekly and monthly boundaries in a fixed timezone, including empty
  periods;
- `--since`, `--until`, `--project`, `--all` and `--limit`;
- `--json` totals and row fields;
- a partial final line and a session with no usage;
- empty session directory output.

Run `make check` and `make check-full` after the script and tests are in
place.

## Out of scope

- Reading pi's normal session directory.
- An interactive TUI.
- Recomputing prices that pi did not record.
- Changing `multi_agent.py`, `SKILL.md` or the package manifest.
- A team/tree view that attributes child jobs to their root job. Depth and
  parent are present in JSON, so this can be added later if it is useful.
