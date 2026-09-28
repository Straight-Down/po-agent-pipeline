#!/usr/bin/env python3
"""
Stop hook -- runs when Claude Code thinks it has finished and is about to hand
the turn back to you.

This is the hook that replaces "paste the output into Cowork and ask what went
wrong". It runs the suite and, if the change introduced a NEW failure, hands
the failure back to Claude instead of to you.

Baseline handling matters here. A naive "block whenever anything is red" hook
would trap Claude in a loop it can never exit while a pre-existing failure
sits in the suite. So:

  * .claude/known-failures.txt holds the node ids that were already failing.
  * Only failures NOT in that file block the turn.
  * As you fix the backlog, delete lines from that file. It is a ratchet --
    it should only ever shrink.

Regenerate the baseline deliberately, never automatically:
    .venv\\Scripts\\python .claude\\hooks\\verify_stop.py --write-baseline

Contract with Claude Code:
    exit 0  -> let Claude stop
    exit 2  -> stderr is fed back to Claude and it keeps working
    exit 1  -> non-blocking warning shown to Kiko (setup problems)
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SUITE_TIMEOUT = 600
MAX_CHARS = 4000
BASELINE_NAME = "known-failures.txt"

# -rfE puts BOTH failures and errors in the short summary. Using -rf alone
# hides collection/import errors entirely, which makes a suite that cannot
# even load look identical to a suite that is completely green.
FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)

# pytest exit codes
EXIT_OK, EXIT_FAILED, EXIT_INTERRUPTED, EXIT_INTERNAL, EXIT_USAGE, EXIT_NOTESTS = 0, 1, 2, 3, 4, 5


def find_python(project_dir: Path) -> str:
    for candidate in (
        project_dir / ".venv" / "Scripts" / "python.exe",
        project_dir / ".venv" / "bin" / "python",
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


def run_suite(py: str, project_dir: Path):
    """Return (exit_code, output, failing_node_ids). exit_code None on timeout."""
    try:
        p = subprocess.run(
            [py, "-m", "pytest", "-q", "-rfE", "--tb=short", "--no-header", "-p", "no:cacheprovider"],
            cwd=project_dir, capture_output=True, text=True, timeout=SUITE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, "", set()
    out = (p.stdout or "") + (p.stderr or "")
    failing = {m.replace("\\", "/") for m in FAILED_RE.findall(out)}
    return p.returncode, out, failing


def load_baseline(path: Path) -> set:
    if not path.exists():
        return set()
    return {
        line.split("#", 1)[0].strip().replace("\\", "/")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    }


def tail(text: str, n: int = MAX_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= n else text[-n:]


def describe_odd_exit(code: int) -> str:
    return {
        EXIT_NOTESTS: "pytest collected NO TESTS. The suite did not run at all.",
        EXIT_USAGE: "pytest usage error -- bad arguments or config.",
        EXIT_INTERNAL: "pytest internal error.",
        EXIT_INTERRUPTED: "pytest was interrupted, usually a collection/import error.",
    }.get(code, f"pytest exited {code}.")


def write_baseline(py: str, project_dir: Path, baseline_path: Path) -> int:
    if not has_module(py, "pytest"):
        print("pytest is not installed in this project's venv.", file=sys.stderr)
        return 1

    code, out, failing = run_suite(py, project_dir)
    if code is None:
        print("Suite timed out; baseline NOT written.", file=sys.stderr)
        return 1

    # An empty result set is only trustworthy when pytest actually ran and
    # passed. Anything else means the run was broken, and writing an empty
    # baseline would quietly disarm the hook.
    if code not in (EXIT_OK, EXIT_FAILED):
        print(f"{describe_odd_exit(code)}\nBaseline NOT written.\n\n{tail(out)}", file=sys.stderr)
        return 1
    if code == EXIT_FAILED and not failing:
        print(
            "pytest reported failures but none could be parsed from the summary.\n"
            f"Baseline NOT written.\n\n{tail(out)}",
            file=sys.stderr,
        )
        return 1

    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Tests that were already failing when this baseline was taken.\n"
        "# The Stop hook ignores these and blocks only on NEW failures.\n"
        "# This list is a ratchet: delete lines as you fix them, never add.\n"
    )
    body = "\n".join(sorted(failing)) + "\n" if failing else ""
    baseline_path.write_text(header + body, encoding="utf-8")

    # Always show what the run actually did, so an empty baseline is never
    # ambiguous between "all green" and "nothing ran".
    summary = [ln for ln in out.strip().splitlines() if " passed" in ln or " failed" in ln or " error" in ln]
    print(f"Baseline written: {len(failing)} known failures (pytest exit {code}).")
    if summary:
        print("pytest summary: " + summary[-1].strip())
    return 0


def main() -> None:
    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    baseline_path = project_dir / ".claude" / BASELINE_NAME
    py = find_python(project_dir)

    if "--write-baseline" in sys.argv:
        sys.exit(write_baseline(py, project_dir, baseline_path))

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    # Without this guard a permanently-failing test would loop forever.
    if payload.get("stop_hook_active"):
        sys.exit(0)

    if not has_module(py, "pytest"):
        print(
            "pytest is not installed in this project's venv, so none of the "
            "test files in this repo can run. Install it with:\n"
            "    .venv\\Scripts\\python -m pip install -r requirements-dev.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    code, out, failing = run_suite(py, project_dir)

    if code is None:
        print("Test suite timed out; skipping verification.", file=sys.stderr)
        sys.exit(1)
    if code == EXIT_OK:
        sys.exit(0)

    # A broken run is a real problem, but it is Kiko's setup problem, not
    # something for Claude to "fix" by editing code. Warn, do not block.
    if code not in (EXIT_FAILED,):
        print(f"{describe_odd_exit(code)}\n\n{tail(out)}", file=sys.stderr)
        sys.exit(1)

    # Failures reported but nothing parseable -- do not let that pass as green.
    if not failing:
        print(
            "pytest reported failures but none could be parsed from the short "
            f"summary. Treating this as a failure.\n\n{tail(out)}",
            file=sys.stderr,
        )
        sys.exit(2)

    new_failures = failing - load_baseline(baseline_path)
    if not new_failures:
        sys.exit(0)

    listed = "\n".join(f"  - {n}" for n in sorted(new_failures))
    print(
        "This change introduced NEW test failures. Do not finish the turn yet.\n\n"
        f"New failures ({len(new_failures)}):\n{listed}\n\n"
        f"Pytest output:\n{tail(out)}\n\n"
        "Fix the cause. Do not edit a test to make it pass, and do not add it "
        "to .claude/known-failures.txt -- that file is a ratchet for the "
        "pre-existing backlog only. If the test itself encodes the wrong "
        "expectation, stop and say so rather than changing it.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
