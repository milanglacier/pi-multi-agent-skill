"""Tests for the multi-agent usage report. Run with `python3 -m unittest discover tests`."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "skills" / "multi-agent" / "scripts" / "usage.py"

_spec = importlib.util.spec_from_file_location("usage_report", SCRIPT)
usage_report = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = usage_report
_spec.loader.exec_module(usage_report)


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.agent_dir = self.tmp / "agent"
        self.state_home = self.tmp / "state"
        self.sessions_dir = self.agent_dir / "multi-agent-sessions"
        self.sessions_dir.mkdir(parents=True)
        self.state_root = self.state_home / "pi-multi-agent"

    def run_report(self, *args):
        environ = {
            "PI_CODING_AGENT_DIR": str(self.agent_dir),
            "XDG_STATE_HOME": str(self.state_home),
        }
        output = io.StringIO()
        error = io.StringIO()
        with (
            mock.patch.dict(os.environ, environ),
            mock.patch.object(usage_report.sys, "stdout", output),
            mock.patch.object(usage_report.sys, "stderr", error),
        ):
            code = usage_report.main(list(args))
        return code, output.getvalue(), error.getvalue()

    def write_session(
        self, slug, name, cwd, entries, stamp=None, start=None, parent_session=None
    ):
        stamp = stamp or "2026-01-01T00-00-00-000Z"
        start = start or "2026-01-01T00:00:00Z"
        header = {
            "type": "session",
            "version": 3,
            "id": name,
            "timestamp": start,
            "cwd": cwd,
        }
        if parent_session is not None:
            header["parentSession"] = parent_session
        lines = [json.dumps(header), *[json.dumps(entry) for entry in entries]]
        directory = self.sessions_dir / slug
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{stamp}_{name}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def write_job(self, slug, name, **data):
        path = self.state_root / slug / "jobs" / name / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        job = {
            "name": name,
            "profile": "general-purpose",
            "cwd": "/tmp/project",
            "parent": None,
            "depth": 1,
            "mode": "foreground",
        }
        job.update(data)
        path.write_text(json.dumps(job), encoding="utf-8")

    def assistant(
        self,
        timestamp,
        input_tokens=10,
        output_tokens=2,
        cache_read=3,
        cache_write=4,
        reasoning=1,
        cost=0.01,
        provider="test",
        model="model-a",
    ):
        usage = {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": cache_read,
            "cacheWrite": cache_write,
            "reasoning": reasoning,
            "totalTokens": input_tokens + output_tokens + cache_read + cache_write,
        }
        if cost is not None:
            usage["cost"] = {"total": cost}
        return {
            "type": "message",
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "provider": provider,
                "model": model,
                "usage": usage,
            },
        }

    def usage_entry(
        self,
        timestamp,
        input_tokens=1,
        output_tokens=1,
        cache_read=2,
        cache_write=3,
        cost=0.005,
        provider="test",
        model="model-a",
    ):
        usage = {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": cache_read,
            "cacheWrite": cache_write,
            "reasoning": 0,
            "totalTokens": input_tokens + output_tokens + cache_read + cache_write,
        }
        if cost is not None:
            usage["cost"] = {"total": cost}
        return {
            "type": "usage",
            "timestamp": timestamp,
            "kind": "cache_warm",
            "provider": provider,
            "model": model,
            "usage": usage,
        }

    def test_counts_assistant_and_usage_entries_without_reasoning(self):
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/alpha",
            [
                self.assistant(
                    "2026-01-02T10:00:00Z",
                    input_tokens=100,
                    output_tokens=20,
                    cache_read=30,
                    cache_write=40,
                    reasoning=7,
                    cost=0.01,
                ),
                self.usage_entry(
                    "2026-01-02T11:00:00Z",
                    input_tokens=1,
                    output_tokens=2,
                    cache_read=3,
                    cache_write=4,
                    cost=0.005,
                ),
            ],
            start="2026-01-02T09:00:00Z",
        )

        code, output, error = self.run_report("project", "--timezone", "UTC", "--json")

        self.assertEqual(code, 0, error)
        data = json.loads(output)
        totals = data["totals"]
        self.assertEqual(totals["turns"], 1)
        self.assertEqual(totals["input"], 101)
        self.assertEqual(totals["output"], 22)
        self.assertEqual(totals["cache_read"], 33)
        self.assertEqual(totals["cache_write"], 44)
        self.assertEqual(totals["total_tokens"], 200)
        self.assertEqual(totals["cost"], 0.015)
        self.assertEqual(totals["unpriced_records"], 0)

    def test_project_and_job_metadata(self):
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/project-a",
            [self.assistant("2026-01-02T10:00:00Z", cost=0.1)],
        )
        self.write_session(
            "slug-b",
            "beta",
            "/tmp/project-b",
            [self.assistant("2026-01-02T11:00:00Z", cost=0.2)],
        )
        self.write_job(
            "slug-a",
            "alpha",
            project="/tmp/root-project",
            cwd="/tmp/project-a",
            profile="Explore",
            depth=2,
            parent="/tmp/root-job",
            mode="background",
        )

        code, output, error = self.run_report(
            "session", "--timezone", "UTC", "--json", "--limit", "0"
        )
        self.assertEqual(code, 0, error)
        rows = {row["label"]: row for row in json.loads(output)["rows"]}
        self.assertEqual(rows["alpha"]["profile"], "Explore")
        self.assertEqual(rows["alpha"]["project"], "/tmp/root-project")
        self.assertEqual(rows["alpha"]["cwd"], "/tmp/project-a")
        self.assertEqual(rows["alpha"]["depth"], 2)
        self.assertEqual(rows["alpha"]["parent"], "/tmp/root-job")
        self.assertEqual(rows["alpha"]["mode"], "background")
        self.assertEqual(rows["beta"]["profile"], "-")
        self.assertEqual(rows["beta"]["project"], "/tmp/project-b")
        self.assertIsNone(rows["beta"]["parent"])

        code, output, error = self.run_report("project", "--timezone", "UTC", "--json")
        self.assertEqual(code, 0, error)
        labels = {row["label"] for row in json.loads(output)["rows"]}
        self.assertEqual(labels, {"/tmp/root-project", "/tmp/project-b"})

    def test_time_views_include_empty_periods(self):
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/alpha",
            [
                self.assistant("2026-01-01T12:00:00Z", cost=0.01),
                self.assistant("2026-01-03T12:00:00Z", cost=0.02),
            ],
        )

        code, output, error = self.run_report(
            "daily",
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-03",
            "--timezone",
            "UTC",
            "--json",
        )
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual(data["totals"]["turns"], 2)
        self.assertEqual(
            [row["label"] for row in data["rows"]],
            ["2026-01-01", "2026-01-02", "2026-01-03"],
        )
        self.assertEqual(data["rows"][1]["total_tokens"], 0)
        self.assertEqual(data["rows"][1]["cost"], 0.0)

        code, output, error = self.run_report(
            "weekly",
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-03",
            "--timezone",
            "UTC",
            "--json",
        )
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual([row["label"] for row in data["rows"]], ["2026-W01"])
        self.assertEqual(data["totals"]["turns"], 2)

        code, output, error = self.run_report(
            "monthly",
            "--since",
            "2026-01-01",
            "--until",
            "2026-02-28",
            "--timezone",
            "UTC",
            "--json",
        )
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual([row["label"] for row in data["rows"]], ["2026-01", "2026-02"])
        self.assertEqual(data["rows"][1]["turns"], 0)
        self.assertEqual(data["totals"]["turns"], 2)

    def test_all_starts_at_first_date_and_filters_projects(self):
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/alpha",
            [self.assistant("2026-01-02T10:00:00Z", cost=0.1)],
        )
        self.write_session(
            "slug-b",
            "beta",
            "/tmp/beta",
            [self.assistant("2026-01-04T10:00:00Z", cost=0.2)],
        )

        code, output, error = self.run_report(
            "daily",
            "--all",
            "--until",
            "2026-01-05",
            "--timezone",
            "UTC",
            "--json",
        )
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual(data["period"]["since"], "2026-01-02")
        self.assertEqual(
            [row["label"] for row in data["rows"]],
            ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"],
        )

        code, output, error = self.run_report(
            "project", "--project", "alpha", "--timezone", "UTC", "--json"
        )
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual(data["totals"]["sessions"], 1)
        self.assertEqual([row["label"] for row in data["rows"]], ["/tmp/alpha"])

    def test_cutoffs_before_first_record_return_empty_reports(self):
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/project",
            [self.assistant("2026-01-02T10:00:00Z", cost=0.1)],
        )
        for view in usage_report.VIEWS:
            for options in ([], ["--all"]):
                with self.subTest(view=view, options=options):
                    code, output, error = self.run_report(
                        view,
                        *options,
                        "--until",
                        "2026-01-01",
                        "--timezone",
                        "UTC",
                        "--json",
                    )
                    self.assertEqual(code, 0, error)
                    data = json.loads(output)
                    self.assertEqual(data["totals"]["sessions"], 0)
                    self.assertEqual(data["totals"]["total_tokens"], 0)
                    self.assertEqual(data["totals"]["cost"], 0.0)
                    self.assertLessEqual(
                        data["period"]["since"], data["period"]["until"]
                    )
                    self.assertTrue(all(row["sessions"] == 0 for row in data["rows"]))

        code, output, error = self.run_report("project", "--until", "2026-01-01")
        self.assertEqual(code, 0, error)
        self.assertIn("No sessions match the filters", output)

    def test_cutoff_on_first_record_date_includes_usage(self):
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/project",
            [self.assistant("2026-01-02T10:00:00Z", cost=0.1)],
        )
        for options in ([], ["--all"]):
            with self.subTest(options=options):
                code, output, error = self.run_report(
                    "project",
                    *options,
                    "--until",
                    "2026-01-02",
                    "--timezone",
                    "UTC",
                    "--json",
                )
                self.assertEqual(code, 0, error)
                data = json.loads(output)
                self.assertEqual(data["totals"]["sessions"], 1)
                self.assertEqual(data["totals"]["cost"], 0.1)

    def test_explicit_start_after_end_is_still_an_error(self):
        with self.assertRaises(SystemExit) as error:
            self.run_report("project", "--since", "2026-01-02", "--until", "2026-01-01")
        self.assertEqual(error.exception.code, 2)

    def test_limit_keeps_totals_and_zero_means_all(self):
        for index in range(3):
            self.write_session(
                f"slug-{index}",
                f"agent-{index}",
                f"/tmp/project-{index}",
                [
                    self.assistant(
                        f"2026-01-0{index + 1}T10:00:00Z", cost=0.1 * (index + 1)
                    )
                ],
            )

        code, output, error = self.run_report(
            "session", "--limit", "2", "--timezone", "UTC", "--json"
        )
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual(data["totals"]["sessions"], 3)
        self.assertAlmostEqual(data["totals"]["cost"], 0.6)

        code, output, error = self.run_report(
            "session", "--limit", "2", "--timezone", "UTC"
        )
        self.assertEqual(code, 0, error)
        self.assertIn("Showing 2 of 3 rows", output)

        code, output, error = self.run_report(
            "session", "--limit", "0", "--timezone", "UTC", "--json"
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(len(json.loads(output)["rows"]), 3)

    def test_partial_last_line_is_ignored(self):
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/alpha",
            [self.assistant("2026-01-02T10:00:00Z", cost=0.1)],
        )
        path = next((self.sessions_dir / "slug-a").glob("*.jsonl"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"message","id":"half-written"')

        code, output, error = self.run_report("project", "--timezone", "UTC", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["totals"]["turns"], 1)
        self.assertEqual(error, "")

    def test_malformed_line_is_skipped_with_a_warning(self):
        path = self.sessions_dir / "slug-a" / "2026-01-01T00-00-00-000Z_alpha.jsonl"
        path.parent.mkdir(parents=True)
        header = {
            "type": "session",
            "version": 3,
            "id": "alpha",
            "timestamp": "2026-01-01T00:00:00Z",
            "cwd": "/tmp/alpha",
        }
        lines = [
            json.dumps(header),
            "not json",
            json.dumps(self.assistant("2026-01-02T10:00:00Z", cost=0.1)),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        code, output, error = self.run_report("project", "--timezone", "UTC", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["totals"]["turns"], 1)
        self.assertIn("skipped 1 malformed line", error)

    def test_session_without_usage_is_still_counted(self):
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/alpha",
            [self.assistant("2026-01-02T10:00:00Z", cost=0.1)],
        )
        self.write_session(
            "slug-a",
            "empty",
            "/tmp/alpha",
            [],
            stamp="2026-01-03T00-00-00-000Z",
            start="2026-01-03T00:00:00Z",
        )

        code, output, error = self.run_report("project", "--timezone", "UTC", "--json")
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual(data["totals"]["sessions"], 2)
        self.assertEqual(data["totals"]["turns"], 1)
        self.assertEqual(data["rows"][0]["sessions"], 2)
        self.assertEqual(data["rows"][0]["total_tokens"], 19)

        code, output, error = self.run_report("session", "--timezone", "UTC", "--json")
        self.assertEqual(code, 0, error)
        rows = {row["label"]: row for row in json.loads(output)["rows"]}
        self.assertEqual(rows["empty"]["total_tokens"], 0)
        self.assertEqual(rows["empty"]["turns"], 0)

    def test_resumed_session_files_are_grouped(self):
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/alpha",
            [self.assistant("2026-01-02T10:00:00Z", cost=0.1)],
            stamp="2026-01-02T00-00-00-000Z",
            start="2026-01-02T00:00:00Z",
        )
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/alpha",
            [self.assistant("2026-01-03T10:00:00Z", cost=0.2)],
            stamp="2026-01-03T00-00-00-000Z",
            start="2026-01-03T00:00:00Z",
        )

        code, output, error = self.run_report("session", "--timezone", "UTC", "--json")
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual(data["totals"]["sessions"], 1)
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["sessions"], 1)
        self.assertEqual(data["rows"][0]["turns"], 2)
        self.assertAlmostEqual(data["rows"][0]["cost"], 0.3)

    def test_fork_excludes_inherited_usage_and_keeps_model_selection(self):
        entries = [
            {
                "type": "model_change",
                "timestamp": "2026-01-01T00:00:00Z",
                "provider": "test",
                "modelId": "model-a",
            }
        ]
        for timestamp, cost in (
            ("2026-01-01T12:00:00Z", 1.0),
            ("2026-01-02T00:00:00Z", 0.25),
        ):
            entries.append(self.assistant(timestamp, cost=cost))
            for kind in ("usage", "compaction", "branch_summary"):
                entry = self.usage_entry(timestamp, cost=cost)
                entry["type"] = kind
                del entry["provider"]
                del entry["model"]
                entries.append(entry)
        self.write_session(
            "slug-a",
            "fork",
            "/tmp/project",
            entries,
            start="2026-01-02T00:00:00Z",
            parent_session=str(self.tmp / "outside" / "missing-parent.jsonl"),
        )

        code, output, error = self.run_report("session", "--timezone", "UTC", "--json")

        self.assertEqual(code, 0, error)
        self.assertEqual(error, "")
        data = json.loads(output)
        self.assertEqual(data["totals"]["sessions"], 1)
        self.assertEqual(data["totals"]["turns"], 1)
        self.assertEqual(data["totals"]["total_tokens"], 40)
        self.assertEqual(data["totals"]["cost"], 1.0)
        self.assertEqual(data["rows"][0]["models"], ["test/model-a"])
        self.assertEqual(data["rows"][0]["started_at"], "2026-01-02T00:00:00+00:00")

    def test_timestamp_filter_requires_a_fork_and_valid_header_time(self):
        for name, parent, start in (
            ("ordinary", None, "2026-01-02T00:00:00Z"),
            ("unknown-start", "/missing-parent.jsonl", "invalid"),
        ):
            with self.subTest(name=name):
                self.write_session(
                    "slug-a",
                    name,
                    "/tmp/project",
                    [self.assistant("2026-01-01T12:00:00Z", cost=1.0)],
                    start=start,
                    parent_session=parent,
                )
                path = next((self.sessions_dir / "slug-a").glob(f"*_{name}.jsonl"))
                _, records, _, _ = usage_report.read_session_file(path)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].cost, 1.0)

    def test_concrete_response_models_are_grouped_separately(self):
        entries = []
        for response_model, cost in (("model-a", 0.1), ("model-b", 0.2), (None, 0.3)):
            entry = self.assistant("2026-01-02T12:00:00Z", model="auto", cost=cost)
            if response_model is not None:
                entry["message"]["responseModel"] = response_model
            entries.append(entry)
        self.write_session("slug-a", "alpha", "/tmp/project", entries)

        code, output, error = self.run_report("model", "--timezone", "UTC", "--json")

        self.assertEqual(code, 0, error)
        data = json.loads(output)
        rows = {row["label"]: row for row in data["rows"]}
        self.assertEqual(set(rows), {"test/model-a", "test/model-b", "test/auto"})
        self.assertEqual(rows["test/model-a"]["cost"], 0.1)
        self.assertEqual(rows["test/model-b"]["cost"], 0.2)
        self.assertEqual(rows["test/auto"]["cost"], 0.3)
        self.assertAlmostEqual(data["totals"]["cost"], 0.6)

        code, output, error = self.run_report("session", "--timezone", "UTC", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(
            json.loads(output)["rows"][0]["models"],
            ["test/auto", "test/model-a", "test/model-b"],
        )

    def test_missing_cost_is_reported_as_unpriced(self):
        self.write_session(
            "slug-a",
            "alpha",
            "/tmp/alpha",
            [self.assistant("2026-01-02T10:00:00Z", cost=None)],
        )

        code, output, error = self.run_report("project", "--timezone", "UTC", "--json")
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual(data["totals"]["total_tokens"], 19)
        self.assertEqual(data["totals"]["cost"], 0.0)
        self.assertEqual(data["totals"]["unpriced_records"], 1)

        code, output, error = self.run_report("project", "--timezone", "UTC")
        self.assertEqual(code, 0, error)
        self.assertIn("usage record(s) had no cost", output)

    def test_project_falls_back_to_the_session_slug(self):
        self.write_session(
            "slug-a",
            "alpha",
            "",
            [self.assistant("2026-01-02T10:00:00Z", cost=0.1)],
        )

        code, output, error = self.run_report("project", "--timezone", "UTC", "--json")
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual(data["totals"]["sessions"], 1)
        self.assertEqual([row["label"] for row in data["rows"]], ["slug-a"])

    def test_no_sessions_prints_a_short_message(self):
        code, output, error = self.run_report("daily")

        self.assertEqual(code, 0, error)
        self.assertIn("No multi-agent sessions found", output)

    def test_no_sessions_json_has_zero_totals(self):
        code, output, error = self.run_report("project", "--timezone", "UTC", "--json")

        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual(data["totals"]["sessions"], 0)
        self.assertEqual(data["totals"]["total_tokens"], 0)
        self.assertEqual(data["rows"], [])


if __name__ == "__main__":
    unittest.main()
