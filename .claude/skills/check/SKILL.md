---
name: check
description: Run ruff lint check and pytest for claude-client. Use when the user wants to verify code quality or run tests.
model: haiku
---

Spawn a quality-checker subagent to run the full pipeline for this project:
- Working directory: /home/guido/projects/claude-client
- Lint command: `uv run ruff check .`
- Test command: `uv run pytest tests/ -v`

Return the subagent's summary directly — don't add commentary.
