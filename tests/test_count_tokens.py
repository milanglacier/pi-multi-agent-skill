import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "count_tokens", Path(__file__).resolve().parents[1] / "scripts" / "count_tokens.py"
)
count_tokens = importlib.util.module_from_spec(spec)
spec.loader.exec_module(count_tokens)


class CountTokensTests(unittest.TestCase):
    def test_request_and_count(self):
        with patch.object(
            count_tokens, "urlopen", return_value=io.BytesIO(b'{"input_tokens": 42}')
        ) as urlopen:
            self.assertEqual(
                count_tokens.count_tokens("Hello 🌍", "gpt-4.1", "key"), 42
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, count_tokens.ENDPOINT)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer key")
        self.assertEqual(
            json.loads(request.data), {"model": "gpt-4.1", "input": "Hello 🌍"}
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)

    def test_invalid_count(self):
        for result in [{}, {"input_tokens": True}, {"input_tokens": -1}]:
            with (
                self.subTest(result=result),
                patch.object(
                    count_tokens,
                    "urlopen",
                    return_value=io.BytesIO(json.dumps(result).encode()),
                ),
                self.assertRaises(ValueError),
            ):
                count_tokens.count_tokens("Hello", "gpt-4.1", "key")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.patch_root = patch.object(count_tokens, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.skill = self.root / "skills" / "multi-agent"
        (self.skill / "references" / "nested").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("old", encoding="utf-8")
        (self.skill / "references" / "removed.md").write_text("gone", encoding="utf-8")
        (self.skill / "references" / "ignore.txt").write_text("ignored")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial files")
        self.first = self.git("rev-parse", "HEAD").strip()
        self.git("tag", "initial")
        (self.skill / "SKILL.md").write_text("updated", encoding="utf-8")
        (self.skill / "references" / "removed.md").unlink()
        (self.skill / "references" / "nested" / "added.md").write_text(
            "new", encoding="utf-8"
        )

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def run_main(self, *args):
        with (
            patch.dict(count_tokens.os.environ, {"OPENAI_API_KEY": "key"}),
            patch.object(count_tokens.sys, "argv", ["count_tokens.py", *args]),
            patch.object(
                count_tokens, "count_tokens", side_effect=lambda text, *_: len(text)
            ) as count,
            patch("sys.stdout", new_callable=io.StringIO) as output,
            patch("sys.stderr", new_callable=io.StringIO) as error,
        ):
            result = count_tokens.main()
        return result, output.getvalue(), error.getvalue(), count

    def assert_comparison(self, output):
        self.assertIn("skills/multi-agent/SKILL.md: 3 -> 7 tokens (+4)", output)
        self.assertIn("references/nested/added.md: 0 -> 3 tokens (+3)", output)
        self.assertIn("references/removed.md: 4 -> 0 tokens (-4)", output)
        self.assertNotIn("ignore.txt", output)

    def test_defaults_compare_head_with_working_tree(self):
        result, output, error, count = self.run_main()
        self.assertEqual(result, 0, error)
        self.assertIn("Before: HEAD; after: working tree", output)
        self.assert_comparison(output)
        self.assertEqual(count.call_count, 4)
        for call in count.call_args_list:
            self.assertEqual(call.args[1:], ("gpt-6-luna", "key"))

    def test_compare_git_refs(self):
        self.git("add", ".")
        self.git("commit", "-qm", "Updated files")
        (self.skill / "SKILL.md").write_text("uncommitted", encoding="utf-8")
        for ref in ("initial", self.first[:7], "HEAD~1"):
            with self.subTest(ref=ref):
                result, output, error, _ = self.run_main(
                    "--before", ref, "--after", "HEAD"
                )
                self.assertEqual(result, 0, error)
                self.assert_comparison(output)

    def test_invalid_refs_fail_before_api_requests(self):
        for option in ("--before", "--after"):
            with self.subTest(option=option):
                result, _, error, count = self.run_main(option, "missing-ref")
                self.assertEqual(result, 1)
                self.assertTrue(error)
                count.assert_not_called()
