#!/usr/bin/env python3
"""Count skill and reference input tokens with the OpenAI Responses API."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://api.openai.com/v1/responses/input_tokens"


def count_tokens(text: str, model: str, api_key: str) -> int:
    request = Request(
        ENDPOINT,
        data=json.dumps({"model": model, "input": text}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        result = json.load(response)
    count = result.get("input_tokens")
    if type(count) is not int or count < 0:
        raise ValueError("The API response has no valid input_tokens count.")
    return count


def read_files(ref: str | None = None) -> dict:
    directory = "skills/multi-agent"
    if ref is None:
        skill = ROOT / directory
        paths = [skill / "SKILL.md", *sorted((skill / "references").rglob("*.md"))]
        return {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in paths
            if path.is_file()
        }

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, encoding="utf-8", check=False
        )
        if result.returncode:
            raise ValueError(result.stderr.strip())
        return result.stdout

    tree = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{tree}}").strip()
    paths = git("ls-tree", "-r", "--name-only", "-z", tree, "--", directory)
    return {
        path: git("show", f"{tree}:{path}")
        for path in paths.split("\0")
        if path == f"{directory}/SKILL.md"
        or (path.startswith(f"{directory}/references/") and path.endswith(".md"))
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", default="gpt-6-luna", help="Model (default: gpt-6-luna)."
    )
    parser.add_argument(
        "--before", default="HEAD", help="Git ref to compare from (default: HEAD)."
    )
    parser.add_argument(
        "--after", help="Git ref to compare to (default: current working tree)."
    )
    args = parser.parse_args()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        parser.error("OPENAI_API_KEY must be set.")

    try:
        before = read_files(args.before)
        after = read_files(args.after)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    print(f"Model: {args.model} (each file is a separate input request)", flush=True)
    print(f"Before: {args.before}; after: {args.after or 'working tree'}", flush=True)
    for path in sorted(before.keys() | after.keys()):
        try:
            before_count = (
                count_tokens(before[path], args.model, api_key) if path in before else 0
            )
            after_count = (
                count_tokens(after[path], args.model, api_key) if path in after else 0
            )
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            print(
                f"{path}: HTTP {error.code}: {detail}",
                file=sys.stderr,
            )
            return 1
        except (OSError, URLError, ValueError) as error:
            print(f"{path}: {error}", file=sys.stderr)
            return 1
        print(
            f"{path}: {before_count} -> {after_count} tokens "
            f"({after_count - before_count:+d})",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
