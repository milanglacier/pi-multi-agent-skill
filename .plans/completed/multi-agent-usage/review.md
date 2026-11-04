## Findings

### [P1] Exclude inherited usage from forked jobs

**Location:** `/home/milanglacier/Desktop/personal-projects/pi-extensions/pi-multi-agent-skill/skills/multi-agent/scripts/usage.py:314-315`

`run --fork` copies the caller's session entries, including their usage and timestamps, into the new session. Appending every record without accounting for `parentSession` charges that inherited history again: a parent costing $1 and a fork adding $0.25 report $2.25 instead of $1.25. Forking an ordinary Pi session also imports caller costs into this report. Exclude inherited requests from each fork's usage before aggregation.

### [P2] Include usage recorded on tool results

**Location:** `/home/milanglacier/Desktop/personal-projects/pi-extensions/pi-multi-agent-skill/skills/multi-agent/scripts/usage.py:234-238`

Pi records nested model work, such as codemode classifier and image calls, in tool-result messages' `usage` fields and includes it in session statistics. This assistant-only condition discards those costs and tokens, so an assistant request costing $0.25 followed by tool usage costing $1 reports only $0.25. Include tool-result usage without increasing the assistant-turn count or assigning it to the assistant's model.

### [P2] Prefer the concrete response model when grouping usage

**Location:** `/home/milanglacier/Desktop/personal-projects/pi-extensions/pi-multi-agent-skill/skills/multi-agent/scripts/usage.py:240-242`

Providers can record the requested alias in `model` and the concrete model that answered in `responseModel`. Reading only `model` merges requests answered by different models under the alias, making the model breakdown and session model lists inaccurate. Prefer `responseModel` when present, with `model` as the fallback, as Pi's own cost breakdown does.

### [P2] Keep unattributed summary usage separate from model totals

**Location:** `/home/milanglacier/Desktop/personal-projects/pi-extensions/pi-multi-agent-skill/skills/multi-agent/scripts/usage.py:246-251`

Compaction and branch-summary entries normally contain usage without provider/model metadata. Falling back to the latest model selection invents an attribution: selecting a virtual model such as `router/auto` creates a cost bucket for that virtual model even though a physical model generated the summary, and extension summaries can use another model entirely. Preserve these costs in an unattributed summary bucket unless the entry explicitly records its model.

### [P2] Return empty results for cutoffs before the first record

**Location:** `/home/milanglacier/Desktop/personal-projects/pi-extensions/pi-multi-agent-skill/skills/multi-agent/scripts/usage.py:569-572`

If the first record is dated January 2, `project --until 2026-01-01` derives January 2 as its implicit start and exits with an invalid-range error instead of returning an empty report. The same problem affects other grouped views and `--all`. Handle cutoffs before the dataset begins as empty results rather than treating the implicit dataset boundary as a conflicting user date.

## Overall assessment

**Verdict:** Patch is incorrect.

**Explanation:** The branch was reviewed against the merge base with `main`, `fb7adb043fe37aefc3e7aed5427b6977cfad99a7`. All 87 tests pass on the current Python and Python 3.9, but the report miscounts forked history and tool usage, misattributes some model costs, and rejects valid historical queries.

## User decisions

1. **Forked history:** Use a small timestamp filter, not parent-file comparisons. For a session with `parentSession` and a valid header timestamp, exclude usage dated before that timestamp. Do not read the parent session. The filter assumes pi preserves copied timestamps and that the clock is consistent; it is not a general fork comparison. The original P1 rating was too strong for this project's scope.
2. **Tool-result usage:** Do not fix this finding. Usage recorded on tool results stays outside the report's scope.
3. **Concrete response models:** Fix this finding. Prefer `responseModel`, with the requested model as a fallback.
4. **Summary model attribution:** Keep the current behavior. Attribute summaries to the selected model when the entry does not identify one. Pi's built-in summaries use that selection; routing and extension overrides are accepted limits.
5. **Historical cutoffs:** Fix this finding. Return an empty report when the cutoff is before the first record, including with `--all`. Explicit start dates after end dates remain errors.

## Fix summary

- Forked sessions count usage at or after their header timestamp. Older model-selection entries still supply model information. Ordinary sessions and forks without a valid header timestamp keep their usage records.
- Model breakdowns and session model lists use the concrete response model when pi records one.
- Regression tests cover inherited assistant, standalone and summary usage; a missing parent file outside the session directory; the exact fork timestamp; invalid header timestamps; and concrete-model grouping with a requested-model fallback.
- Cutoffs before the first usage record produce empty reports. The implicit start date does not exceed the cutoff. Regression tests cover every view with and without `--all`, text output, an inclusive cutoff on the first record's date, and invalid explicit date ranges.
- `CHANGELOG.md` records the usage report and these fixes.

**Validation:** `make check-full` passes lint and formatting checks, plus all 93 tests on the current Python and Python 3.9. Findings 1, 3 and 5 are addressed within the agreed scope; findings 2 and 4 are accepted limits.
