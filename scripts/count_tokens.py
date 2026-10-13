#!/usr/bin/env python3
"""Count skill and reference input tokens with the OpenAI Responses API."""

import argparse
import json
import os
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", default="gpt-6-luna", help="Model (default: gpt-6-luna)."
    )
    args = parser.parse_args()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        parser.error("OPENAI_API_KEY must be set.")

    skill = ROOT / "skills" / "multi-agent"
    paths = [skill / "SKILL.md", *sorted((skill / "references").rglob("*.md"))]
    print(f"Model: {args.model} (each file is a separate input request)", flush=True)
    for path in paths:
        try:
            count = count_tokens(path.read_text(encoding="utf-8"), args.model, api_key)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            print(
                f"{path.relative_to(ROOT)}: HTTP {error.code}: {detail}",
                file=sys.stderr,
            )
            return 1
        except (OSError, URLError, ValueError) as error:
            print(f"{path.relative_to(ROOT)}: {error}", file=sys.stderr)
            return 1
        print(f"{path.relative_to(ROOT)}: {count} tokens", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
