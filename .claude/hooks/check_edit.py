#!/usr/bin/env python3
"""
PostToolUse hook -- runs after Claude Code edits a Python file.

The point of this hook is to close the feedback loop that currently runs
through you: instead of you noticing a bug in Cowork and writing a prompt
about it, the error goes straight back to Claude Code, which fixes it in the
same turn.

Contract with Claude Code:
    exit 0  -> quiet, everything fine
    exit 2  -> BLOCKING: stderr is fed back to Claude, which then fixes it
    other   -> non-blocking warning shown to you

Checks, cheapest first, stopping at the first that fails:
    1. py_compile      -- syntax errors. Always available.
    2. ruff            -- lint. Skipped if not installed.
    3. focused pytest  -- test_<name>.py for the file just edited, if it exists.

Cross-platform on purpose: this runs the same under Windows cmd, PowerShell,
Git Bash and WSL, which a .sh hook would not.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Keep fed-back output small. Claude does not need 400 lines of traceback to
# find the bug, and a large hook payload eats the context you are trying to
# conserve.
MAX_CHARS = 3000
FOCUSED_TEST_TIMEOUT = 120


def find_python(project_dir: Path) -> str:
    """Prefer the project venv, so checks see the project's own packages."""
    for candidate in (
        project_dir / ".venv" / "Scripts" / "python.exe",   # Windows
        project_dir / ".venv" / "bin" / "python",           # POSIX / WSL
        project_dir / "venv" / "Scripts" / "python.exe",
        project_dir / "venv" / "bin" / "python",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def has_module(py: str, mod: str) -> bool:
    return subprocess.run(
        [py, "-c", f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('{mod}') else 1)"],
        capture_output=True,
    ).returncode == 0


def run(cmd, cwd, timeout=90):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 0, ""          # a slow check is not a failing check
    except FileNotFoundError:
        return 0, ""


def clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n... (truncated)"


def block(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("path") or ""
    if not raw or not raw.endswith(".py"):
        sys.exit(0)

    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    target = Path(raw)
    if not target.is_absolute():
        target = project_dir / target
    if not target.exists():
        sys.exit(0)

    py = find_python(project_dir)
    rel = os.path.relpath(target, project_dir)

    # 1. Syntax. Nothing downstream is meaningful if this fails.
    code, out = run([py, "-m", "py_compile", str(target)], project_dir, timeout=60)
    if code != 0:
        block(f"SYNTAX ERROR in {rel} -- fix this before doing anything else:\n\n{clip(out)}")

    # 2. Lint.
    if has_module(py, "ruff"):
        code, out = run([py, "-m", "ruff", "check", str(target)], project_dir, timeout=90)
        if code != 0 and out.strip():
            block(
                f"Ruff findings in {rel}:\n\n{clip(out)}\n\n"
                "Fix these now. If a rule is genuinely wrong for this codebase, "
                "add a targeted noqa with a reason rather than silencing the rule globally."
            )

    # 3. The test file that covers what was just edited, if there is one.
    focused = project_dir / f"test_{target.stem}.py"
    if focused.exists() and target.name != focused.name and has_module(py, "pytest"):
        code, out = run(
            [py, "-m", "pytest", focused.name, "-x", "-q", "--tb=short", "--no-header", "-p", "no:cacheprovider"],
            project_dir,
            timeout=FOCUSED_TEST_TIMEOUT,
        )
        if code not in (0, 5):     # 5 == no tests collected
            block(
                f"Editing {rel} broke {focused.name}:\n\n{clip(out)}\n\n"
                "Fix the code. Do not edit the test to make it pass -- if the test "
                "encodes the wrong expectation, say so explicitly and stop."
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
