"""Claude Code PreToolUse hook: run ruff and pytest before Claude runs `git commit`.

Reads the hook payload (JSON) on stdin. For a Bash/PowerShell command that contains
`git commit`, it runs `ruff check src` and `pytest src/tests -q` from the repo root and
exits 2 (blocking the tool call, stderr goes back to Claude) when either fails.
Commits that touch nothing under src/ or the requirements files skip the checks.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
RUFF = ROOT / ".venv" / "Scripts" / "ruff.exe"
COMMIT_RE = re.compile(r"\bgit\b(?:\s+-\S+(?:\s+\S+)?)*\s+commit(?![-\w])")
TAIL_LINES = 40


def _changed_paths() -> list[str]:
    """Paths changed against HEAD (staged or not) plus untracked files."""
    out: list[str] = []
    for args in (["git", "diff", "HEAD", "--name-only"],
                 ["git", "ls-files", "--others", "--exclude-standard"]):
        res = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        out.extend(line.strip() for line in res.stdout.splitlines() if line.strip())
    return out


def _needs_checks(paths: list[str]) -> bool:
    return any(p.startswith("src/") or p.startswith("requirements") for p in paths)


def _run(label: str, cmd: list[str]) -> str | None:
    """Run one check; return an error report or None when it passed."""
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode == 0:
        return None
    lines = (res.stdout + res.stderr).strip().splitlines()
    return f"[{label}] failed (exit {res.returncode}):\n" + "\n".join(lines[-TAIL_LINES:])


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not COMMIT_RE.search(command):
        return 0
    if not _needs_checks(_changed_paths()):
        return 0

    errors = [e for e in (
        _run("ruff", [str(RUFF), "check", "src"]),
        _run("pytest", [str(PY), "-m", "pytest", "src/tests", "-q", "-p", "no:cacheprovider"]),
    ) if e]
    if errors:
        print("Pre-commit checks failed; the commit was blocked. Fix the code "
              "(change a test only when its expected value legitimately changed) "
              "and commit again.\n\n" + "\n\n".join(errors), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
