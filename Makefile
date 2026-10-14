.PHONY: check check-full token-count

check:
	uvx ruff check .
	uvx ruff format --check .
	python3 -m unittest discover tests

check-full: check
	uv run --no-project --python 3.9 python -m unittest discover tests

token-count:
	python3 scripts/count_tokens.py
