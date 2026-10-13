import importlib.util
import io
import json
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

    def test_counts_skill_and_each_reference(self):
        with (
            patch.dict(count_tokens.os.environ, {"OPENAI_API_KEY": "key"}),
            patch.object(count_tokens.sys, "argv", ["count_tokens.py"]),
            patch.object(count_tokens, "count_tokens", return_value=42) as count,
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(count_tokens.main(), 0)
        skill = count_tokens.ROOT / "skills" / "multi-agent"
        paths = [skill / "SKILL.md", *sorted((skill / "references").rglob("*.md"))]
        self.assertEqual(count.call_count, len(paths))
        for call, path in zip(count.call_args_list, paths):
            self.assertEqual(
                call.args, (path.read_text(encoding="utf-8"), "gpt-6-luna", "key")
            )
            self.assertIn(
                f"{path.relative_to(count_tokens.ROOT)}: 42 tokens", output.getvalue()
            )
