#!/usr/bin/env python3
"""Report token usage and cost for multi-agent pi sessions.

The multi-agent skill keeps its pi sessions in a directory of its own, away
from pi's normal session list. This script reads those files, joins the job
metadata next to them when it exists, and prints tables like ccusage.

Sessions and their usage records are the source of truth. Costs are the ones
pi recorded for each request; this script never computes prices itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Tables use a few Unicode characters, so write UTF-8 whatever the locale.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

UTC = timezone.utc  # noqa: UP017 - Python 3.9 has no datetime.UTC.

SESSION_FILE_RE = re.compile(r"^[^_]+_(.+)\.jsonl$")
VIEWS = (
    "overview",
    "daily",
    "weekly",
    "monthly",
    "project",
    "session",
    "model",
    "profile",
)
VIEW_ALIASES = {"summary": "overview", "jobs": "session"}
TIME_VIEWS = {"daily", "weekly", "monthly"}
GROUP_VIEWS = {"project", "session", "model", "profile"}


# --- reading files -----------------------------------------------------------


def pi_agent_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or "~/.pi/agent").expanduser()


def sessions_root() -> Path:
    return pi_agent_dir() / "multi-agent-sessions"


def state_root() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
        / "pi-multi-agent"
    )


def load_jobs(root: Path) -> dict:
    """Map each (project slug, job name) to its job.json content."""
    jobs = {}
    if not root.is_dir():
        return jobs
    for path in sorted(root.glob("*/jobs/*/job.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        name = path.parent.name
        slug = path.parent.parent.parent.name
        jobs[(slug, name)] = data
    return jobs


def parse_timestamp(value):
    """Turn an ISO string or a millisecond epoch number into an aware datetime."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000.0, UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def entry_timestamp(entry: dict, message: dict | None = None):
    timestamp = parse_timestamp(entry.get("timestamp"))
    if timestamp is not None:
        return timestamp
    if message is not None:
        return parse_timestamp(message.get("timestamp"))
    return None


def session_name(path: Path) -> str:
    match = SESSION_FILE_RE.match(path.name)
    return match.group(1) if match else path.stem


def as_number(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def usage_cost(usage: dict):
    cost = usage.get("cost")
    if isinstance(cost, dict):
        total = cost.get("total")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            return float(total)
    return None


@dataclass(frozen=True)
class UsageRecord:
    timestamp: datetime
    provider: str
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int
    cost: float | None
    turn: bool

    @property
    def model_label(self) -> str:
        if self.provider and self.model:
            return f"{self.provider}/{self.model}"
        return self.model or self.provider or "(unknown)"


def make_record(
    timestamp: datetime | None,
    usage: dict,
    provider: str,
    model: str,
    turn: bool,
):
    if timestamp is None:
        return None
    input_tokens = as_number(usage.get("input"))
    output_tokens = as_number(usage.get("output"))
    cache_read = as_number(usage.get("cacheRead"))
    cache_write = as_number(usage.get("cacheWrite"))
    if isinstance(usage.get("totalTokens"), (int, float)) and not isinstance(
        usage.get("totalTokens"), bool
    ):
        total_tokens = as_number(usage.get("totalTokens"))
    else:
        total_tokens = input_tokens + output_tokens + cache_read + cache_write
    return UsageRecord(
        timestamp=timestamp,
        provider=provider,
        model=model,
        input=input_tokens,
        output=output_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        total_tokens=total_tokens,
        cost=usage_cost(usage),
        turn=turn,
    )


def read_session_file(path: Path):
    """Return the header, usage records, bad line count and header timestamp."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise OSError(str(exc)) from exc
    lines = text.split("\n")
    header = {}
    header_time = None
    records = []
    bad_lines = 0
    current_provider = ""
    current_model = ""
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # The last line of a running session may still be half written.
            if index == len(lines) - 1 and not text.endswith(("\n", "\r")):
                continue
            bad_lines += 1
            continue
        if not isinstance(entry, dict):
            bad_lines += 1
            continue
        kind = entry.get("type")
        message = (
            entry.get("message") if isinstance(entry.get("message"), dict) else None
        )
        timestamp = entry_timestamp(entry, message)
        if kind == "session":
            if not header:
                header = entry
                header_time = timestamp
            continue
        if kind == "model_change":
            provider = entry.get("provider")
            model = entry.get("modelId") or entry.get("model")
            if provider or model:
                current_provider = str(provider or "")
                current_model = str(model or "")
            continue
        if kind == "message":
            if message is None or message.get("role") != "assistant":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            provider = str(message.get("provider") or current_provider or "")
            model = str(message.get("model") or current_model or "")
            record = make_record(timestamp or header_time, usage, provider, model, True)
            if record is not None:
                records.append(record)
            continue
        if kind in ("usage", "compaction", "branch_summary"):
            usage = entry.get("usage")
            if not isinstance(usage, dict):
                continue
            provider = str(entry.get("provider") or current_provider or "")
            model = str(entry.get("model") or current_model or "")
            record = make_record(
                timestamp or header_time, usage, provider, model, False
            )
            if record is not None:
                records.append(record)
    return header, records, bad_lines, header_time


@dataclass
class Session:
    slug: str
    name: str
    cwd: str = ""
    job_project: str = ""
    profile: str = "-"
    parent: str | None = None
    depth: int = 0
    mode: str = ""
    start: datetime | None = None
    last: datetime | None = None
    records: list = field(default_factory=list)
    files: int = 0

    @property
    def key(self):
        return (self.slug, self.name)

    @property
    def project(self) -> str:
        return self.job_project or self.cwd or self.slug


def load_sessions(root: Path, jobs: dict):
    sessions = {}
    warnings = []
    if not root.is_dir():
        return [], warnings
    for path in sorted(root.glob("*/*.jsonl")):
        slug = path.parent.name
        name = session_name(path)
        try:
            header, records, bad_lines, header_time = read_session_file(path)
        except OSError as exc:
            warnings.append(f"warning: cannot read {path}: {exc}")
            continue
        if bad_lines:
            warnings.append(f"warning: skipped {bad_lines} malformed line(s) in {path}")
        key = (slug, name)
        session = sessions.get(key)
        if session is None:
            session = Session(slug=slug, name=name)
            sessions[key] = session
        session.files += 1
        cwd = header.get("cwd")
        if not session.cwd and isinstance(cwd, str) and cwd:
            session.cwd = cwd
        if header_time is not None:
            session.start = (
                header_time
                if session.start is None
                else min(session.start, header_time)
            )
        for record in records:
            session.records.append(record)
            session.last = (
                record.timestamp
                if session.last is None
                else max(session.last, record.timestamp)
            )
    for key, session in sessions.items():
        job = jobs.get(key)
        if job:
            project = job.get("project")
            if isinstance(project, str) and project:
                session.job_project = project
            if not session.cwd:
                cwd = job.get("cwd") or project
                if isinstance(cwd, str):
                    session.cwd = cwd
            profile = job.get("profile")
            session.profile = profile if isinstance(profile, str) and profile else "-"
            parent = job.get("parent")
            session.parent = parent if isinstance(parent, str) else None
            depth = job.get("depth")
            if isinstance(depth, int) and not isinstance(depth, bool):
                session.depth = depth
            mode = job.get("mode")
            session.mode = mode if isinstance(mode, str) else ""
        if session.records:
            first = min(record.timestamp for record in session.records)
            last = max(record.timestamp for record in session.records)
            session.start = (
                first if session.start is None else min(session.start, first)
            )
            session.last = last if session.last is None else max(session.last, last)
        if session.start is None:
            session.start = session.last
        if session.last is None:
            session.last = session.start
    return list(sessions.values()), warnings


# --- dates and periods -------------------------------------------------------


def local_datetime(moment: datetime, tz: ZoneInfo | None) -> datetime:
    return moment.astimezone(tz) if tz is not None else moment.astimezone()


def local_date(moment: datetime, tz: ZoneInfo | None) -> date:
    return local_datetime(moment, tz).date()


def current_date(tz: ZoneInfo | None) -> date:
    return local_datetime(datetime.now(UTC), tz).date()


def timezone_name(tz: ZoneInfo | None) -> str:
    if tz is None:
        return "local"
    return getattr(tz, "key", str(tz))


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def month_start(day: date) -> date:
    return day.replace(day=1)


def add_months(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: date
    end: date


def make_periods(view: str, start: date, end: date) -> list:
    periods = []
    if view == "daily":
        cursor = start
        while cursor <= end:
            label = cursor.isoformat()
            periods.append(Period(label, label, cursor, cursor))
            cursor += timedelta(days=1)
        return periods
    if view == "weekly":
        cursor = week_start(start)
        last = week_start(end)
        while cursor <= last:
            iso = cursor.isocalendar()
            label = f"{iso[0]:04d}-W{iso[1]:02d}"
            periods.append(
                Period(cursor.isoformat(), label, cursor, cursor + timedelta(days=6))
            )
            cursor += timedelta(days=7)
        return periods
    cursor = month_start(start)
    last = month_start(end)
    while cursor <= last:
        label = f"{cursor.year:04d}-{cursor.month:02d}"
        periods.append(
            Period(label, label, cursor, add_months(cursor, 1) - timedelta(days=1))
        )
        cursor = add_months(cursor, 1)
    return periods


def period_for(periods: list, day: date) -> str | None:
    for period in periods:
        if period.start <= day <= period.end:
            return period.key
    return None


# --- grouping ----------------------------------------------------------------


@dataclass
class Bucket:
    sessions: set = field(default_factory=set)
    models: set = field(default_factory=set)
    turns: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    unpriced: int = 0
    last: datetime | None = None

    def add_session(self, key, moment: datetime | None = None):
        self.sessions.add(key)
        if moment is not None and (self.last is None or moment > self.last):
            self.last = moment

    def add(self, key, record: UsageRecord):
        self.add_session(key, record.timestamp)
        self.models.add(record.model_label)
        if record.turn:
            self.turns += 1
        self.input += record.input
        self.output += record.output
        self.cache_read += record.cache_read
        self.cache_write += record.cache_write
        self.total_tokens += record.total_tokens
        if record.cost is None:
            self.unpriced += 1
        else:
            self.cost += record.cost


@dataclass
class Row:
    key: str
    label: str
    bucket: Bucket
    extra: dict = field(default_factory=dict)


def bucket_data(bucket: Bucket) -> dict:
    return {
        "sessions": len(bucket.sessions),
        "turns": bucket.turns,
        "input": bucket.input,
        "output": bucket.output,
        "cache_read": bucket.cache_read,
        "cache_write": bucket.cache_write,
        "total_tokens": bucket.total_tokens,
        "cost": bucket.cost,
        "unpriced_records": bucket.unpriced,
    }


def row_data(row: Row) -> dict:
    data = {"key": row.key, "label": row.label}
    data.update(bucket_data(row.bucket))
    if row.bucket.last is not None:
        data["last_active_at"] = row.bucket.last.isoformat()
    data.update(row.extra)
    return data


# --- report ------------------------------------------------------------------


def project_matches(project: str, filters: list) -> bool:
    if not filters:
        return True
    text = project.casefold()
    return any(item.casefold() in text for item in filters)


def dataset_bounds(sessions: list, tz: ZoneInfo | None):
    dates = []
    for session in sessions:
        for record in session.records:
            dates.append(local_date(record.timestamp, tz))
        if not session.records and session.start is not None:
            dates.append(local_date(session.start, tz))
    return (min(dates), max(dates)) if dates else (None, None)


def session_in_range(
    session: Session,
    start: date,
    end: date,
    tz: ZoneInfo | None,
    require_date: bool,
) -> bool:
    if session.records:
        return any(
            start <= local_date(record.timestamp, tz) <= end
            for record in session.records
        )
    if session.start is not None:
        return start <= local_date(session.start, tz) <= end
    return not require_date


def records_in_range(
    session: Session, start: date, end: date, tz: ZoneInfo | None
) -> list:
    return [
        record
        for record in session.records
        if start <= local_date(record.timestamp, tz) <= end
    ]


def determine_range(view: str, args, sessions: list, tz: ZoneInfo | None):
    first, last = dataset_bounds(sessions, tz)
    today = current_date(tz)
    end = args.until or today
    if args.all:
        if args.until is None and last is not None:
            end = max(end, last)
        start = args.since or (first if first is not None else end)
        return start, end, view in TIME_VIEWS or bool(args.since or args.until)
    if view in TIME_VIEWS:
        if args.since is not None:
            start = args.since
        elif view == "daily":
            start = end - timedelta(days=args.days - 1)
        elif view == "weekly":
            start = week_start(end) - timedelta(weeks=args.weeks - 1)
        else:
            start = add_months(month_start(end), -(args.months - 1))
        return start, end, True
    start = args.since or (first if first is not None else end)
    if args.until is None and last is not None:
        end = max(end, last)
    return start, end, bool(args.since or args.until)


def project_sessions(sessions: list, args, tz: ZoneInfo | None):
    filtered = [s for s in sessions if project_matches(s.project, args.project)]
    start, end, require_date = determine_range(args.view, args, filtered, tz)
    filtered = [
        s for s in filtered if session_in_range(s, start, end, tz, require_date)
    ]
    return filtered, start, end


def total_bucket(sessions: list, start: date, end: date, tz: ZoneInfo | None) -> Bucket:
    total = Bucket()
    for session in sessions:
        total.add_session(session.key, session.last)
        for record in records_in_range(session, start, end, tz):
            total.add(session.key, record)
    return total


def time_rows(
    view: str,
    sessions: list,
    start: date,
    end: date,
    tz: ZoneInfo | None,
) -> list:
    periods = make_periods(view, start, end)
    buckets = {period.key: Bucket() for period in periods}
    for session in sessions:
        records = records_in_range(session, start, end, tz)
        if records:
            for record in records:
                key = period_for(periods, local_date(record.timestamp, tz))
                if key is not None:
                    buckets[key].add(session.key, record)
        elif session.start is not None:
            day = local_date(session.start, tz)
            if start <= day <= end:
                key = period_for(periods, day)
                if key is not None:
                    buckets[key].add_session(session.key, session.last)
    return [Row(period.key, period.label, buckets[period.key]) for period in periods]


def project_rows(sessions: list, start: date, end: date, tz: ZoneInfo | None) -> list:
    buckets = {}
    for session in sessions:
        key = session.project
        bucket = buckets.setdefault(key, Bucket())
        bucket.add_session(session.key, session.last)
        for record in records_in_range(session, start, end, tz):
            bucket.add(session.key, record)
    return [
        Row(key, key, bucket)
        for key, bucket in sorted(
            buckets.items(), key=lambda item: (-item[1].cost, item[0])
        )
    ]


def session_rows(sessions: list, start: date, end: date, tz: ZoneInfo | None) -> list:
    rows = []
    for session in sessions:
        bucket = Bucket()
        bucket.add_session(session.key, session.last)
        for record in records_in_range(session, start, end, tz):
            bucket.add(session.key, record)
        extra = {
            "job": session.name,
            "project": session.project,
            "cwd": session.cwd or None,
            "profile": session.profile,
            "models": sorted(bucket.models),
            "files": session.files,
            "depth": session.depth,
            "parent": session.parent,
            "mode": session.mode,
            "started_at": session.start.isoformat() if session.start else None,
        }
        rows.append(
            Row(
                f"{session.slug}/{session.name}",
                session.name,
                bucket,
                extra,
            )
        )
    rows.sort(key=lambda row: (-row.bucket.cost, row.label))
    return rows


def model_rows(sessions: list, start: date, end: date, tz: ZoneInfo | None) -> list:
    buckets = {}
    for session in sessions:
        for record in records_in_range(session, start, end, tz):
            key = record.model_label
            bucket = buckets.setdefault(key, Bucket())
            bucket.add(session.key, record)
    return [
        Row(key, key, bucket)
        for key, bucket in sorted(
            buckets.items(), key=lambda item: (-item[1].cost, item[0])
        )
    ]


def profile_rows(sessions: list, start: date, end: date, tz: ZoneInfo | None) -> list:
    buckets = {}
    for session in sessions:
        key = session.profile
        bucket = buckets.setdefault(key, Bucket())
        bucket.add_session(session.key, session.last)
        for record in records_in_range(session, start, end, tz):
            bucket.add(session.key, record)
    return [
        Row(key, key, bucket)
        for key, bucket in sorted(
            buckets.items(), key=lambda item: (-item[1].cost, item[0])
        )
    ]


def make_rows(view: str, sessions: list, start: date, end: date, tz) -> list:
    if view in TIME_VIEWS:
        return time_rows(view, sessions, start, end, tz)
    if view == "project":
        return project_rows(sessions, start, end, tz)
    if view == "session":
        return session_rows(sessions, start, end, tz)
    if view == "model":
        return model_rows(sessions, start, end, tz)
    return profile_rows(sessions, start, end, tz)


@dataclass
class Report:
    data: dict
    rows: list
    total_rows: int
    sessions: list
    warnings: list
    tz: ZoneInfo | None


def recent_rows(
    sessions: list, args, tz: ZoneInfo | None, today: date, days: int = 7
) -> list:
    end = args.until or today
    start = end - timedelta(days=days - 1)
    if args.since is not None and args.since > start:
        start = args.since
    start = min(start, end)
    selected = [
        session
        for session in sessions
        if session_in_range(session, start, end, tz, True)
    ]
    return time_rows("daily", selected, start, end, tz)


def generate_report(sessions: list, args, tz: ZoneInfo | None) -> Report:
    warnings = []
    selected, start, end = project_sessions(sessions, args, tz)
    if start > end:
        raise ValueError(
            f"the start date {start.isoformat()} is after the end date {end.isoformat()}"
        )
    totals = total_bucket(selected, start, end, tz)
    if args.view == "overview":
        top_projects = project_rows(selected, start, end, tz)[:5]
        top_models = model_rows(selected, start, end, tz)[:5]
        all_rows = top_projects
    else:
        top_projects = []
        top_models = []
        all_rows = make_rows(args.view, selected, start, end, tz)
    total_rows = len(all_rows)
    rows = all_rows
    if args.limit is not None and args.limit > 0 and args.view in GROUP_VIEWS:
        rows = all_rows[: args.limit]
    model_count = len(
        {
            record.model_label
            for session in selected
            for record in records_in_range(session, start, end, tz)
        }
    )
    data = {
        "generated_at": local_datetime(datetime.now(UTC), tz).isoformat(
            timespec="seconds"
        ),
        "view": args.view,
        "timezone": timezone_name(tz),
        "period": {"since": start.isoformat(), "until": end.isoformat()},
        "filters": {"projects": args.project or []},
        "totals": bucket_data(totals),
        "project_count": len({session.project for session in selected}),
        "model_count": model_count,
        "row_count": len(rows),
        "total_rows": total_rows,
        "rows": [row_data(row) for row in rows],
    }
    if args.view == "overview":
        data["top_projects"] = [row_data(row) for row in top_projects]
        data["top_models"] = [row_data(row) for row in top_models]
        data["last_7_days"] = [
            row_data(row) for row in recent_rows(selected, args, tz, current_date(tz))
        ]
    return Report(data, rows, total_rows, selected, warnings, tz)


# --- output ------------------------------------------------------------------


class Palette:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)


def pad(text: str, width: int, align: str) -> str:
    return text.rjust(width) if align == "right" else text.ljust(width)


def render_table(
    headers: list,
    rows: list,
    aligns: list,
    palette: Palette,
    bold_last: bool = True,
) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    header_line = "  ".join(
        palette.bold(pad(str(header), widths[index], aligns[index]))
        for index, header in enumerate(headers)
    )
    rule = "  ".join("-" * width for width in widths)
    lines = [header_line, rule]
    for row_index, row in enumerate(rows):
        line = "  ".join(
            pad(str(cell), widths[index], aligns[index])
            for index, cell in enumerate(row)
        )
        if bold_last and row_index == len(rows) - 1:
            line = palette.bold(line)
        lines.append(line)
    return "\n".join(lines)


def format_tokens(value: int) -> str:
    return f"{value:,}"


def format_compact(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def format_cost(value: float) -> str:
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    if abs(value) < 1:
        return f"${value:.3f}"
    return f"${value:,.2f}"


def format_share(part: float, total: float) -> str:
    if total == 0:
        return "-"
    return f"{part / total * 100:.1f}%"


def format_date(moment: datetime | None, tz: ZoneInfo | None) -> str:
    if moment is None:
        return "-"
    return local_date(moment, tz).isoformat()


def display_path(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def short_project(path: str) -> str:
    parts = Path(path).parts
    if len(parts) >= 2:
        return os.path.join(*parts[-2:])
    return path


def middle(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 1) // 2
    right = limit - 1 - left
    return text[:left] + "…" + text[-right:]


def models_text(models: set) -> str:
    ordered = sorted(models)
    if not ordered:
        return "-"
    if len(ordered) == 1:
        return middle(ordered[0], 28)
    first = middle(ordered[0], 13)
    second = middle(ordered[1], 13)
    suffix = "" if len(ordered) == 2 else f" +{len(ordered) - 2}"
    return f"{first}, {second}{suffix}"


def total_line(totals: dict) -> list:
    return [
        "Total",
        format_tokens(totals["sessions"]),
        format_tokens(totals["turns"]),
        format_tokens(totals["input"]),
        format_tokens(totals["output"]),
        format_tokens(totals["cache_read"]),
        format_tokens(totals["cache_write"]),
        format_tokens(totals["total_tokens"]),
        format_cost(totals["cost"]),
    ]


def render_time_table(report: Report, palette: Palette) -> str:
    headers = [
        "Period",
        "Sessions",
        "Turns",
        "Input",
        "Output",
        "Cache R",
        "Cache W",
        "Total tokens",
        "Cost",
    ]
    aligns = ["left"] + ["right"] * 8
    rows = []
    for row in report.rows:
        bucket = row.bucket
        rows.append(
            [
                row.label,
                format_tokens(len(bucket.sessions)),
                format_tokens(bucket.turns),
                format_tokens(bucket.input),
                format_tokens(bucket.output),
                format_tokens(bucket.cache_read),
                format_tokens(bucket.cache_write),
                format_tokens(bucket.total_tokens),
                format_cost(bucket.cost),
            ]
        )
    rows.append(total_line(report.data["totals"]))
    return render_table(headers, rows, aligns, palette)


def render_project_table(report: Report, palette: Palette) -> str:
    headers = ["Project", "Sessions", "Turns", "Total tokens", "Cost", "Last active"]
    aligns = ["left", "right", "right", "right", "right", "left"]
    rows = []
    for row in report.rows:
        bucket = row.bucket
        rows.append(
            [
                middle(display_path(row.label), 48),
                format_tokens(len(bucket.sessions)),
                format_tokens(bucket.turns),
                format_tokens(bucket.total_tokens),
                format_cost(bucket.cost),
                format_date(bucket.last, report.tz),
            ]
        )
    totals = report.data["totals"]
    rows.append(
        [
            "Total",
            format_tokens(totals["sessions"]),
            format_tokens(totals["turns"]),
            format_tokens(totals["total_tokens"]),
            format_cost(totals["cost"]),
            "-",
        ]
    )
    return render_table(headers, rows, aligns, palette)


def render_session_table(report: Report, palette: Palette) -> str:
    headers = [
        "Agent",
        "Project",
        "Profile",
        "Models",
        "Turns",
        "Total tokens",
        "Cost",
        "Last active",
    ]
    aligns = ["left", "left", "left", "left", "right", "right", "right", "left"]
    rows = []
    for row in report.rows:
        bucket = row.bucket
        rows.append(
            [
                middle(row.label, 24),
                middle(short_project(row.extra["project"]), 28),
                middle(row.extra["profile"], 18),
                models_text(bucket.models),
                format_tokens(bucket.turns),
                format_tokens(bucket.total_tokens),
                format_cost(bucket.cost),
                format_date(bucket.last, report.tz),
            ]
        )
    totals = report.data["totals"]
    rows.append(
        [
            "Total",
            "-",
            "-",
            "-",
            format_tokens(totals["turns"]),
            format_tokens(totals["total_tokens"]),
            format_cost(totals["cost"]),
            "-",
        ]
    )
    return render_table(headers, rows, aligns, palette)


def render_ranking_table(report: Report, label: str, palette: Palette) -> str:
    headers = [label, "Sessions", "Turns", "Total tokens", "Cost", "Share"]
    aligns = ["left", "right", "right", "right", "right", "right"]
    rows = []
    total_cost = report.data["totals"]["cost"]
    for row in report.rows:
        bucket = row.bucket
        rows.append(
            [
                middle(row.label, 36),
                format_tokens(len(bucket.sessions)),
                format_tokens(bucket.turns),
                format_tokens(bucket.total_tokens),
                format_cost(bucket.cost),
                format_share(bucket.cost, total_cost),
            ]
        )
    totals = report.data["totals"]
    rows.append(
        [
            "Total",
            format_tokens(totals["sessions"]),
            format_tokens(totals["turns"]),
            format_tokens(totals["total_tokens"]),
            format_cost(totals["cost"]),
            format_share(totals["cost"], total_cost),
        ]
    )
    return render_table(headers, rows, aligns, palette)


def render_overview(report: Report, palette: Palette) -> str:
    totals = report.data["totals"]
    lines = []
    period = "{} to {}".format(
        report.data["period"]["since"], report.data["period"]["until"]
    )
    lines.append(palette.bold("pi multi-agent usage") + "  " + period)
    lines.append(
        "{} sessions · {} projects · {} turns · {} models".format(
            format_tokens(totals["sessions"]),
            format_tokens(report.data["project_count"]),
            format_tokens(totals["turns"]),
            format_tokens(report.data["model_count"]),
        )
    )
    lines.append("")
    lines.append("Total cost   " + palette.bold(format_cost(totals["cost"])))
    lines.append(
        "Tokens       {}   input {} · output {} · cache read {} · cache write {}".format(
            format_compact(totals["total_tokens"]),
            format_compact(totals["input"]),
            format_compact(totals["output"]),
            format_compact(totals["cache_read"]),
            format_compact(totals["cache_write"]),
        )
    )
    if totals["unpriced_records"]:
        lines.append(
            "Unpriced     {} usage record(s) had no cost".format(
                totals["unpriced_records"]
            )
        )

    lines.append("")
    lines.append(palette.bold("Top projects"))
    lines.append(
        render_ranking_subset(
            report.data["top_projects"], "Project", palette, full_path=True
        )
    )
    lines.append("")
    lines.append(palette.bold("Top models"))
    lines.append(render_ranking_subset(report.data["top_models"], "Model", palette))
    lines.append("")
    lines.append(palette.bold("Last 7 days"))
    lines.append(render_recent_subset(report.data["last_7_days"], palette))
    lines.append("")
    lines.append("Views: " + "  ".join(VIEWS))
    return "\n".join(lines)


def render_ranking_subset(
    rows: list, label: str, palette: Palette, full_path: bool = False
) -> str:
    headers = [label, "Sessions", "Turns", "Tokens", "Cost"]
    aligns = ["left", "right", "right", "right", "right"]
    data = []
    for row in rows:
        name = display_path(row["label"]) if full_path else row["label"]
        data.append(
            [
                middle(name, 48),
                format_tokens(row["sessions"]),
                format_tokens(row["turns"]),
                format_compact(row["total_tokens"]),
                format_cost(row["cost"]),
            ]
        )
    return render_table(headers, data, aligns, palette, bold_last=False)


def render_recent_subset(rows: list, palette: Palette) -> str:
    headers = ["Date", "Sessions", "Turns", "Tokens", "Cost"]
    aligns = ["left", "right", "right", "right", "right"]
    data = []
    for row in rows:
        data.append(
            [
                row["label"],
                format_tokens(row["sessions"]),
                format_tokens(row["turns"]),
                format_compact(row["total_tokens"]),
                format_cost(row["cost"]),
            ]
        )
    return render_table(headers, data, aligns, palette, bold_last=False)


def render_text(report: Report, palette: Palette) -> str:
    if report.data["view"] == "overview":
        return render_overview(report, palette)
    if report.data["view"] in TIME_VIEWS:
        return render_time_table(report, palette)
    if report.data["view"] == "project":
        return render_project_table(report, palette)
    if report.data["view"] == "session":
        return render_session_table(report, palette)
    if report.data["view"] == "model":
        return render_ranking_table(report, "Model", palette)
    return render_ranking_table(report, "Profile", palette)


def color_enabled(args) -> bool:
    return not args.no_color and "NO_COLOR" not in os.environ and sys.stdout.isatty()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Show token usage and cost from the pi sessions the multi-agent "
            "skill stores under its own session directory."
        ),
        epilog=(
            "Examples:\n"
            "  usage.py daily --days 14\n"
            "  usage.py project --since 2026-10-01\n"
            "  usage.py session --json\n"
            "  usage.py weekly --all\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "view",
        nargs="?",
        default="overview",
        choices=VIEWS + tuple(VIEW_ALIASES),
        help="report view (default: overview)",
    )
    parser.add_argument(
        "--days", type=positive_int, default=30, help="days in daily view (default: 30)"
    )
    parser.add_argument(
        "--weeks",
        type=positive_int,
        default=12,
        help="weeks in weekly view (default: 12)",
    )
    parser.add_argument(
        "--months",
        type=positive_int,
        default=12,
        help="months in monthly view (default: 12)",
    )
    parser.add_argument("--since", type=date_arg, metavar="YYYY-MM-DD")
    parser.add_argument("--until", type=date_arg, metavar="YYYY-MM-DD")
    parser.add_argument(
        "--project",
        action="append",
        metavar="TEXT",
        help="only projects whose path contains TEXT; repeatable",
    )
    parser.add_argument(
        "--all", action="store_true", help="show all time instead of the default window"
    )
    parser.add_argument(
        "--timezone",
        type=timezone_arg,
        metavar="NAME",
        help="IANA timezone for day boundaries (default: local)",
    )
    parser.add_argument(
        "--limit",
        type=nonnegative_int,
        metavar="N",
        help="limit rows in group views; 0 means all",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")
    return parser


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive whole number")
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive whole number")
    return number


def nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a whole number")
    if number < 0:
        raise argparse.ArgumentTypeError("expected a whole number")
    return number


def date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a date such as 2026-10-01")


def timezone_arg(value: str) -> ZoneInfo | None:
    if value.lower() in ("local", ""):
        return None
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise argparse.ArgumentTypeError(f"unknown timezone: {value}")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.view = VIEW_ALIASES.get(args.view, args.view)
    if args.since is not None and args.until is not None and args.since > args.until:
        parser.error("--since must not be after --until")
    root = sessions_root()
    jobs = load_jobs(state_root())
    sessions, warnings = load_sessions(root, jobs)
    try:
        report = generate_report(sessions, args, args.timezone)
    except ValueError as exc:
        print(f"usage.py: error: {exc}", file=sys.stderr)
        return 2
    report.warnings.extend(warnings)
    for warning in report.warnings:
        print(warning, file=sys.stderr)

    if args.json:
        print(json.dumps(report.data, indent=2, ensure_ascii=False))
        return 0

    if not sessions:
        print(f"No multi-agent sessions found in {root}.")
        return 0
    if not report.sessions:
        print("No sessions match the filters.")
        return 0

    palette = Palette(color_enabled(args))
    print(render_text(report, palette))
    totals = report.data["totals"]
    if totals["unpriced_records"] and report.data["view"] != "overview":
        print(
            palette.dim(
                "{} usage record(s) had no cost.".format(totals["unpriced_records"])
            )
        )
    if report.total_rows > len(report.rows):
        print(
            palette.dim(
                f"Showing {len(report.rows)} of {report.total_rows} rows; use --limit 0 for all."
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
