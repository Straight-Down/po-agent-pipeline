"""
Pytest wiring for this project.

Why this file exists
--------------------
`test_parsing.py` was written as a standalone argparse script
(`python test_parsing.py --live`), not as a pytest module. Every one of its 45
test functions takes a `tmp: Path` argument, which its own `main()` supplies by
hand. Pytest has no fixture called `tmp` -- the builtin is `tmp_path` -- so when
pytest collected the file it errored all 45 tests at setup with
"fixture 'tmp' not found".

Those errors were invisible for a long time because nobody ran `pytest` on the
folder; the file was only ever run directly as a script. The result was 4,000+
lines that looked like a test suite, and were treated as one, while contributing
nothing to any automated check.

This file bridges the two: it supplies `tmp`, registers the `--live` flag the
script already understood, and skips the API-calling tests unless you ask for
them. `python test_parsing.py --live` still works exactly as before.
"""

from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="also run tests that make real Anthropic API calls (costs tokens)",
    )


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    """Alias for pytest's tmp_path, matching test_parsing.py's parameter name."""
    return tmp_path


def pytest_collection_modifyitems(config, items):
    """Live tests hit the Anthropic API and cost money. Opt in explicitly."""
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="live API test; pass --live to run")
    for item in items:
        if item.name.startswith("test_live"):
            item.add_marker(skip_live)


# ---------------------------------------------------------------------------
# The gate that makes a pytest run mean something
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. Every suite here records assertions through a `check()` that
# appends to a module-level `_results` and PRINTS -- it never raises:
#
#     def check(ok, name, detail=""):
#         _results.append((bool(ok), name, detail))
#         print(f"  [{'PASS' if ok else 'FAIL'}] {name}" ...)
#
# Each script's `main()` is what reads `_results` and exits non-zero. Under
# pytest `main()` never runs, so a test function that records fifty failing
# checks still returns normally and pytest marks it passed. Before this fixture,
# `87 passed` meant "93 functions ran without raising" -- the 1,189 individual
# assertions could not fail a pytest run at all. A gate that cannot fail is
# worse than no gate: it manufactures confidence instead of withholding it
# (RUNBOOK section 8 lesson 18).
#
# WHY NOT FIX IT IN THE SUITES. Two rejected alternatives, both worse:
#
#   - Make `check()` raise. That would destroy the property that makes these
#     scripts worth running: they collect EVERY failure and report them
#     together. You want to see fifty failures at once, not the first one.
#   - Add an assert at the end of each test function. That is 93 chances to
#     forget, and the 94th test written next month would not have one. A gate
#     you have to remember to attach is a gate that eventually is not attached.
#
# An autouse fixture applies to every test in the session, including tests that
# do not exist yet, which is the only version of this that stays true.


@pytest.fixture(autouse=True)
def _enforce_check_results(request):
    """
    Bracket each test so only the checks IT ran can be held against it.

    Records where the module's `_results` stood at setup; the verdict itself is
    passed in `pytest_runtest_makereport` below. Modules with no `_results` are
    left entirely alone rather than erroring -- a future test file that does not
    use this pattern must not be broken by the fixture that exists to serve the
    ones that do.
    """
    results = getattr(request.module, "_results", None)
    if isinstance(results, list):
        request.node._check_start = len(results)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    Turn recorded-but-unraised check failures into a real test FAILURE.

    **Why not simply fail in the fixture's teardown.** That was the first
    version, and it reports as `ERROR at teardown of test_x` while the test
    itself still counts in the `passed` tally -- `4 passed, 1 error`. The exit
    code is non-zero, so it is not silent, but the headline number says the test
    passed when it did not. Re-introducing a misleading green while removing one
    is not a fix. Judging at the end of the CALL phase, where `_results` is
    already fully populated, makes it an honest `1 failed`.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return

    results = getattr(item.module, "_results", None)
    start = getattr(item, "_check_start", None)
    if not isinstance(results, list) or start is None:
        return

    # A test may clear `_results` itself (the live suites do when re-run in a
    # loop). If the list is shorter than where we started, the bracket is
    # meaningless -- inspect everything present rather than nothing.
    window = results[start:] if len(results) >= start else results
    failed = [entry for entry in window if not entry[0]]
    if not failed:
        return

    lines = [f"{len(failed)} of {len(window)} check(s) recorded FALSE in {item.name}:"]
    for entry in failed:
        name = entry[1] if len(entry) > 1 else "<unnamed>"
        detail = entry[2] if len(entry) > 2 else ""
        lines.append(f"  - {name}" + (f"  --  {detail}" if detail else ""))
    message = "\n".join(lines)

    if report.passed:
        report.outcome = "failed"
        report.longrepr = message
    elif report.failed:
        # The test also raised. Keep the traceback and append what was recorded.
        report.longrepr = f"{report.longrepr}\n\n{message}"
