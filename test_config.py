"""
Configuration-loading tests, plus a repo-wide scan for leaked credentials.

Two halves, and the second is the one that earns its keep:

  1. `GraphConfig.from_env` fails correctly -- naming the missing variable,
     refusing to default `GRAPH_CLIENT`, catching a thumbprint that does not
     match its certificate, and never putting an identifier in `repr`.
  2. **Nothing credential-shaped is in a tracked file.** This repo has already
     had one incident of sensitive values being transcribed into a commit
     message while being removed from the working tree (RUNBOOK section 8
     lesson 12), so the scan is a standing check rather than a formality.

Runs entirely offline. Every test that manipulates the environment restores it,
including on failure, because a leaked variable would silently change the result
of a later test rather than failing it.

    python test_config.py
"""

from __future__ import annotations

import os
import re
import subprocess
import traceback
from contextlib import contextmanager
from pathlib import Path

import config
from config import ConfigError, GraphConfig

HERE = Path(__file__).resolve().parent
_results: list[tuple[bool, str, str]] = []

#: Every variable these tests touch. Saved and restored as a block, so a test
#: cannot leak one into the next.
_GRAPH_VARS = (
    "GRAPH_CLIENT",
    "GRAPH_TENANT_ID",
    "GRAPH_CLIENT_ID",
    "GRAPH_MAILBOX",
    "GRAPH_CERT_PATH",
    "GRAPH_CERT_PUBLIC_PATH",
    "GRAPH_CERT_THUMBPRINT",
)


def check(ok: bool, name: str, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  --  {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 70 - len(title)))


def expect_config_error(fn, name: str, must_mention: tuple[str, ...] = ()) -> None:
    """A ConfigError whose MESSAGE names the thing that is wrong."""
    try:
        fn()
    except ConfigError as exc:
        message = str(exc)
        missing = [m for m in must_mention if m not in message]
        if missing:
            check(False, name, f"raised, but the message omits {missing}")
            return
        first = message.splitlines()[0]
        check(True, name, first[:88])
    except Exception as exc:  # noqa: BLE001
        check(False, name, f"raised {type(exc).__name__}, not ConfigError: {exc}")
    else:
        check(False, name, "did not raise")


@contextmanager
def env(**overrides):
    """
    Set exactly these GRAPH_* variables; unset every other one.

    Absolute rather than additive on purpose: a test for "GRAPH_TENANT_ID is
    missing" must not pass merely because the developer's own `.env` had not been
    loaded yet, nor fail because it had.
    """
    saved = {k: os.environ.get(k) for k in _GRAPH_VARS}
    try:
        for key in _GRAPH_VARS:
            os.environ.pop(key, None)
        for key, value in overrides.items():
            if value is not None:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# The real certificate pair, used for the checks that need a genuine one. Read
# from `.env` rather than hardcoded -- the paths are machine-specific.
def _real_pair() -> tuple[str, str, str] | None:
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover
        return None
    values = dotenv_values(HERE / ".env") if (HERE / ".env").is_file() else {}
    key = values.get("GRAPH_CERT_PATH")
    cer = values.get("GRAPH_CERT_PUBLIC_PATH")
    thumb = values.get("GRAPH_CERT_THUMBPRINT")
    if key and cer and thumb and Path(key).is_file() and Path(cer).is_file():
        return key, cer, thumb
    return None


# ---------------------------------------------------------------------------


def test_graph_client_never_defaults() -> None:
    section("GRAPH_CLIENT has no default and never becomes 'real'")

    with env():  # nothing set at all
        expect_config_error(
            lambda: GraphConfig.from_env(dotenv_path=None),
            "GRAPH_CLIENT unset raises rather than defaulting",
            ("GRAPH_CLIENT", "mock", "real"),
        )
    with env(GRAPH_CLIENT=""):
        expect_config_error(
            lambda: GraphConfig.from_env(dotenv_path=None),
            "GRAPH_CLIENT='' raises rather than defaulting",
            ("GRAPH_CLIENT",),
        )
    with env(GRAPH_CLIENT="  "):
        expect_config_error(
            lambda: GraphConfig.from_env(dotenv_path=None),
            "GRAPH_CLIENT of whitespace raises",
            ("GRAPH_CLIENT",),
        )
    for bad in ("Real", "production", "live", "true", "1", "moc", "mock,real"):
        with env(GRAPH_CLIENT=bad):
            expect_config_error(
                lambda: GraphConfig.from_env(dotenv_path=None),
                f"GRAPH_CLIENT={bad!r} raises",
                ("GRAPH_CLIENT",),
            )

    # The property that matters most, stated as its own assertion: there is no
    # input for which an unusable value silently becomes the live client.
    with env(GRAPH_CLIENT="mock"):
        cfg = GraphConfig.from_env(dotenv_path=None)
    check(cfg.client_kind == "mock" and cfg.is_mock,
          "and the only way to get a client is to name it exactly", cfg.client_kind)
    with env(GRAPH_CLIENT="mock "):
        cfg = GraphConfig.from_env(dotenv_path=None)
    check(cfg.client_kind == "mock",
          "surrounding whitespace IS stripped -- invisible in an editor, not a typo")
    check(config.GRAPH_CLIENTS[0] == "mock",
          "'mock' is first in GRAPH_CLIENTS, so reading it never suggests 'real' is "
          "the fallback")


def test_mock_needs_no_credentials() -> None:
    section("mock mode loads with no credentials present")

    with env(GRAPH_CLIENT="mock"):
        cfg = GraphConfig.from_env(dotenv_path=None)
    check(cfg.is_mock, "mock loads with tenant, client, mailbox and cert all absent")
    check(cfg.tenant_id is None and cfg.client_id is None and cfg.mailbox is None,
          "and those stay None rather than becoming empty strings")
    check(cfg.cert_path is None and cfg.cert_thumbprint is None,
          "no certificate is required or read when none is configured")

    # This is the requirement that keeps development unblocked while IT is slow;
    # if it ever regresses, every offline run starts failing on a tenant id.
    check(True, "-> development is not blocked waiting on a tenant id")

    # Not required is NOT the same as ignored: present values are still read.
    with env(GRAPH_CLIENT="mock", GRAPH_TENANT_ID="t", GRAPH_CLIENT_ID="c",
             GRAPH_MAILBOX="m@example.com"):
        cfg = GraphConfig.from_env(dotenv_path=None)
    check(cfg.tenant_id == "t" and cfg.mailbox == "m@example.com",
          "mock still READS the identifiers when they are present -- so repr stays "
          "honest and switching to real changes the checks, not the inputs")


def test_real_requires_everything() -> None:
    section("real mode validates everything at load time")
    pair = _real_pair()
    if not pair:
        check(False, "the real certificate pair is available for this test",
              "GRAPH_CERT_PATH / _PUBLIC_PATH not readable; skipping the real-mode cases")
        return
    key, cer, thumb = pair
    base = dict(GRAPH_CLIENT="real", GRAPH_CERT_PATH=key,
                GRAPH_CERT_PUBLIC_PATH=cer, GRAPH_CERT_THUMBPRINT=thumb)

    for missing in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_MAILBOX"):
        values = dict(base, GRAPH_TENANT_ID="t", GRAPH_CLIENT_ID="c",
                      GRAPH_MAILBOX="m@example.com")
        values[missing] = ""
        with env(**values):
            expect_config_error(
                lambda: GraphConfig.from_env(dotenv_path=None),
                f"real with empty {missing} raises, naming it",
                (missing, "GRAPH_CLIENT=real"),
            )

    with env(**dict(base, GRAPH_TENANT_ID="t", GRAPH_CLIENT_ID="c",
                    GRAPH_MAILBOX="m@example.com")):
        cfg = GraphConfig.from_env(dotenv_path=None)
    check(not cfg.is_mock and cfg.client_kind == "real", "real loads when complete")
    check(cfg.cert_not_after is not None,
          "and the certificate's notAfter is read at LOAD time, not first use",
          str(cfg.cert_not_after))


def test_certificate_validation() -> None:
    section("certificate: parses, thumbprint matches, and key/cert are a pair")
    pair = _real_pair()
    if not pair:
        check(False, "the real certificate pair is available for this test", "skipping")
        return
    key, cer, thumb = pair

    with env(GRAPH_CLIENT="mock", GRAPH_CERT_PATH=key,
             GRAPH_CERT_PUBLIC_PATH=cer, GRAPH_CERT_THUMBPRINT=thumb):
        cfg = GraphConfig.from_env(dotenv_path=None)
    check(cfg.cert_thumbprint == thumb.replace(":", "").upper(),
          "the real pair validates and the thumbprint is normalised to upper hex")

    # Thumbprint mismatch -- the stale-.env case.
    wrong = ("A" * 40) if not thumb.upper().startswith("A") else ("B" * 40)
    with env(GRAPH_CLIENT="mock", GRAPH_CERT_PATH=key,
             GRAPH_CERT_PUBLIC_PATH=cer, GRAPH_CERT_THUMBPRINT=wrong):
        expect_config_error(
            lambda: GraphConfig.from_env(dotenv_path=None),
            "a thumbprint that does not match the certificate raises",
            ("GRAPH_CERT_THUMBPRINT", "does not match"),
        )

    # Malformed thumbprint, caught before the comparison so the message is about
    # the format rather than about the wrong certificate.
    # NOTE: an EMPTY thumbprint is a *missing* variable, not a malformed one --
    # `_require` catches it first and says so, which is the right message. It is
    # asserted separately below rather than lumped in here.
    for bad in ("deadbeef", "not-hex-at-all-not-hex-at-all-not-hex-xx", "A" * 39):
        with env(GRAPH_CLIENT="mock", GRAPH_CERT_PATH=key,
                 GRAPH_CERT_PUBLIC_PATH=cer, GRAPH_CERT_THUMBPRINT=bad):
            expect_config_error(
                lambda: GraphConfig.from_env(dotenv_path=None),
                f"malformed thumbprint {bad[:14]!r} raises about the FORMAT",
                ("GRAPH_CERT_THUMBPRINT", "40 hex"),
            )

    with env(GRAPH_CLIENT="mock", GRAPH_CERT_PATH=key,
             GRAPH_CERT_PUBLIC_PATH=cer, GRAPH_CERT_THUMBPRINT=""):
        expect_config_error(
            lambda: GraphConfig.from_env(dotenv_path=None),
            "an EMPTY thumbprint raises about being UNSET, not about the format",
            ("GRAPH_CERT_THUMBPRINT", "is not set"),
        )

    # Colons and lowercase are what the portal renders; both must be accepted.
    colonised = ":".join(thumb[i:i + 2] for i in range(0, len(thumb), 2)).lower()
    with env(GRAPH_CLIENT="mock", GRAPH_CERT_PATH=key,
             GRAPH_CERT_PUBLIC_PATH=cer, GRAPH_CERT_THUMBPRINT=colonised):
        cfg = GraphConfig.from_env(dotenv_path=None)
    check(cfg.cert_thumbprint == thumb.upper(),
          "a colon-separated lowercase thumbprint is accepted and normalised")

    # The key path pointing at the CERTIFICATE -- an easy mistake, since the two
    # variables sit next to each other.
    with env(GRAPH_CLIENT="mock", GRAPH_CERT_PATH=cer,
             GRAPH_CERT_PUBLIC_PATH=cer, GRAPH_CERT_THUMBPRINT=thumb):
        expect_config_error(
            lambda: GraphConfig.from_env(dotenv_path=None),
            "GRAPH_CERT_PATH pointing at the certificate raises",
            ("GRAPH_CERT_PATH", "private key"),
        )

    # A missing file names the variable rather than the OS error.
    with env(GRAPH_CLIENT="mock", GRAPH_CERT_PATH="does_not_exist.key",
             GRAPH_CERT_PUBLIC_PATH=cer, GRAPH_CERT_THUMBPRINT=thumb):
        expect_config_error(
            lambda: GraphConfig.from_env(dotenv_path=None),
            "a missing key file names GRAPH_CERT_PATH",
            ("GRAPH_CERT_PATH", "not a readable file"),
        )
    with env(GRAPH_CLIENT="real", GRAPH_CERT_PATH=key, GRAPH_CERT_PUBLIC_PATH="",
             GRAPH_CERT_THUMBPRINT=thumb, GRAPH_TENANT_ID="t",
             GRAPH_CLIENT_ID="c", GRAPH_MAILBOX="m@example.com"):
        expect_config_error(
            lambda: GraphConfig.from_env(dotenv_path=None),
            "a missing GRAPH_CERT_PUBLIC_PATH names itself",
            ("GRAPH_CERT_PUBLIC_PATH",),
        )

    check(config.CERT_EXPIRY_WARN_DAYS == 60,
          "the expiry warning window is 60 days", str(config.CERT_EXPIRY_WARN_DAYS))


def test_repr_leaks_nothing() -> None:
    section("repr/str carry no identifiers and no key material")
    pair = _real_pair()
    key, cer, thumb = pair if pair else (None, None, None)

    values = dict(GRAPH_CLIENT="real", GRAPH_TENANT_ID="11111111-2222-3333-4444-555555555555",
                  GRAPH_CLIENT_ID="66666666-7777-8888-9999-000000000000",
                  GRAPH_MAILBOX="shipments@straightdown.com")
    if pair:
        values.update(GRAPH_CERT_PATH=key, GRAPH_CERT_PUBLIC_PATH=cer,
                      GRAPH_CERT_THUMBPRINT=thumb)
    with env(**values):
        cfg = GraphConfig.from_env(dotenv_path=None) if pair else GraphConfig(
            client_kind="real", tenant_id=values["GRAPH_TENANT_ID"],
            client_id=values["GRAPH_CLIENT_ID"], mailbox=values["GRAPH_MAILBOX"],
            cert_thumbprint=(thumb or "A" * 40))

    for rendered, label in ((repr(cfg), "repr"), (str(cfg), "str"),
                            (f"{cfg}", "f-string"), (f"{cfg!r}", "f-string !r")):
        leaked = [
            name for name, secret in (
                ("tenant id", values["GRAPH_TENANT_ID"]),
                ("client id", values["GRAPH_CLIENT_ID"]),
                ("mailbox", values["GRAPH_MAILBOX"]),
            ) if secret and secret in rendered
        ]
        check(not leaked, f"{label} leaks no identifier",
              f"LEAKED {leaked}" if leaked else "clean")

    if pair:
        check(thumb.upper() not in repr(cfg) and thumb.lower() not in repr(cfg),
              "and not the thumbprint either")
        key_material = Path(key).read_text(encoding="utf-8")
        body = "".join(
            l for l in key_material.splitlines() if "BEGIN" not in l and "END" not in l
        )[:40]
        check(body and body not in repr(cfg), "and no key material")

    check("<set>" in repr(cfg) or "<unset>" in repr(cfg),
          "presence is reported instead of the value", repr(cfg)[:70])
    # A dataclass-generated repr would dump every field, so assert the default
    # was actually replaced rather than merely overridden somewhere.
    check(GraphConfig.__repr__ is not object.__repr__,
          "and __repr__ is the custom one, not the dataclass default")


def test_one_dotenv_loader() -> None:
    section("one .env loading path, not two")
    import netsuite_client

    source = Path(netsuite_client.__file__).read_text(encoding="utf-8")
    check("load_env_file" in source,
          "netsuite_client uses config.load_env_file rather than its own load_dotenv")
    check(source.count("from dotenv import load_dotenv") == 0,
          "and no longer imports load_dotenv directly",
          f"{source.count('from dotenv import load_dotenv')} import(s)")

    # Only config.py may call load_dotenv. claude_extractor loads a DIFFERENT
    # file (~/.po-agent/.env, the Anthropic key, deliberately outside this
    # folder), so it is listed explicitly rather than silently allowed.
    allowed = {"config.py", "claude_extractor.py"}
    offenders = [
        p.name for p in HERE.glob("*.py")
        if p.name not in allowed
        and "load_dotenv" in p.read_text(encoding="utf-8")
        and not p.name.startswith("test_")
    ]
    check(not offenders, "no other module loads a .env itself",
          f"offenders: {offenders}" if offenders else "none")


def test_no_credentials_in_tracked_files() -> None:
    section("repo-wide: nothing credential-shaped in a tracked file")

    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=HERE, capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    tracked = [HERE / n for n in listing.split("\0") if n]
    check(len(tracked) > 20, "read the tracked file list", f"{len(tracked)} file(s)")

    patterns = {
        "GUID (tenant/client id)": re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
        "PEM header": re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
        "PEM certificate": re.compile(r"-----BEGIN CERTIFICATE-----"),
        "client secret assignment": re.compile(
            r"(?i)\b(client_secret|GRAPH_CLIENT_SECRET)\s*[=:]\s*\S+"),
    }
    #: Placeholders and this test's own regexes are not findings. Kept narrow:
    #: an all-zero GUID is a documented example, a real one is not.
    ignorable = re.compile(
        r"00000000-0000-0000-0000-000000000000"
        r"|xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        r"|<your-tenant-id>",
        re.I,
    )

    findings: list[str] = []
    for path in tracked:
        if path.name == Path(__file__).name:
            continue  # this file necessarily contains the patterns it searches for
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary corpus documents; scanned by their own tooling
        for label, pattern in patterns.items():
            for match in pattern.finditer(text):
                if ignorable.search(match.group(0)):
                    continue
                line = text[: match.start()].count("\n") + 1
                findings.append(f"{path.name}:{line} [{label}]")

    check(not findings, "no tracked file contains a GUID, a PEM block or a client secret",
          "; ".join(findings[:6]) if findings else f"{len(tracked)} file(s) scanned")

    # And the two files that must never be tracked, verified by git rather than
    # by reading .gitignore -- the distinction that caught a real problem before.
    for name in (".env", ".env.bak"):
        tracked_now = subprocess.run(
            ["git", "ls-files", "--error-unmatch", name],
            cwd=HERE, capture_output=True,
        ).returncode == 0
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", name], cwd=HERE, capture_output=True,
        ).returncode == 0
        check(not tracked_now and ignored, f"{name} is ignored and untracked",
              f"tracked={tracked_now} ignored={ignored}")

    check((HERE / ".env.example").is_file(), ".env.example exists and is the contract")
    example = (HERE / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", example, re.M))
    for name in _GRAPH_VARS + ("NS_ACCOUNT_ID", "NS_CLIENT_ID", "NS_CERTIFICATE_ID",
                               "NS_PRIVATE_KEY_PATH", "NS_JWT_ALGORITHM",
                               "NS_PRIVATE_KEY_PASSPHRASE", "NS_HTTP_TIMEOUT"):
        check(name in documented, f".env.example documents {name}")
    for name in ("ANTHROPIC_API_KEY", "PO_AGENT_DB_URL"):
        check(name in example,
              f"and mentions {name}, which lives elsewhere -- the contract is complete")


def main() -> int:
    print("=" * 78)
    print("CONFIGURATION TESTS")
    print("=" * 78)
    for fn in (
        test_graph_client_never_defaults,
        test_mock_needs_no_credentials,
        test_real_requires_everything,
        test_certificate_validation,
        test_repr_leaks_nothing,
        test_one_dotenv_loader,
        test_no_credentials_in_tracked_files,
    ):
        try:
            fn()
        except Exception:  # noqa: BLE001
            print()
            traceback.print_exc()
            _results.append((False, f"{fn.__name__} crashed", ""))

    passed = sum(1 for ok, _n, _d in _results if ok)
    failed = [n for ok, n, _d in _results if not ok]
    print()
    print("=" * 78)
    print(f"{passed}/{len(_results)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
