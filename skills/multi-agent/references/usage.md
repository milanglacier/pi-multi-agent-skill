# Usage report

`usage.py` shows token counts and the cost pi recorded for the multi-agent
sessions on this machine. Run it only when the user asks what the agents used
or cost.

```bash
python3 "$SKILL_DIR/scripts/usage.py" [VIEW] [OPTIONS]
```

The default `overview` view shows the all-time total, the token breakdown,
the top projects and models, and the last seven days. Start there; it answers
most questions.

| View | What it shows |
|---|---|
| `daily`, `weekly`, `monthly` | Totals per period, including empty periods. |
| `project` | Totals per project. |
| `session` | Totals per job, with its profile and models. |
| `model` | Totals per provider and model. |
| `profile` | Totals per profile. |

| Option | Meaning |
|---|---|
| `--since DATE`, `--until DATE` | Inclusive date range for every view. |
| `--days N`, `--weeks N`, `--months N` | Window for the time view; defaults 30, 12 and 12. |
| `--project TEXT` | Only projects whose path contains TEXT; repeatable. |
| `--all` | All time instead of the default window. |
| `--limit N` | Rows in a group view; `0` shows all. |
| `--timezone NAME` | IANA timezone for day boundaries; default local. |
| `--json` | One JSON object instead of tables. |

The report covers every project on the machine, not just the current one. Use
`--project` to narrow it. Costs come from pi; records without a recorded cost
are counted as unpriced. Running jobs appear with the usage recorded so far.

Group views print every row unless `--limit` is given, so pass `--limit 10`
and raise it only when the user asks for more. When you need to compare or
sum numbers, `--json` is easier than a wide table. Report only the numbers
the user asked for, not the whole output.
