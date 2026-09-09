"""
Configuration loading for the pipeline's external credentials.

## Why this module exists, and why it is not a second loader

`netsuite_client.NetSuiteConfig.from_env` already loaded `.env` and raised a
single error naming every missing variable. Adding a parallel Graph loader would
have given the project two places that read `.env`, two error conventions, and
nothing tying them together -- which is exactly the shape of the migration
seeding bug (RUNBOOK section 8 lesson 10): the same information with two sources
of truth and no test comparing them.

So `load_env_file` below is the ONE function that reads `.env`, and
`NetSuiteConfig.from_env` calls it too. `GraphConfig` lives here rather than
inside `netsuite_client` because Graph is not NetSuite, but they share the
loading path and the "name every missing variable, point at .env.example"
convention.

## What is deliberately absent

**No defaults and no silent fallbacks.** A missing variable raises and names
itself. In particular `GRAPH_CLIENT` has no default at all: an empty, absent or
misspelled value raises rather than guessing, and there is no code path by which
it becomes `"real"`. Defaulting to a live client would mean a typo in `.env`
sends real requests to a real mailbox.

**Nothing here logs, and `repr` carries no secrets.** Accidental logging is the
realistic leak path for this kind of object -- far more likely than a deliberate
one -- so `__repr__` reports paths and a redaction marker, never identifiers or
key material, and a test asserts it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

#: How close to expiry the certificate has to be before load warns. The Graph
#: certificate expires 2028-09-08 and will otherwise fail with an opaque auth
#: error on the day, with nothing having said so beforehand.
CERT_EXPIRY_WARN_DAYS = 60

#: The only two accepted values. `mock` first so that reading the tuple never
#: suggests `real` is the fallback.
GRAPH_CLIENTS = ("mock", "real")

#: A well-formed SHA-1 thumbprint once colons and case are normalised.
_THUMBPRINT = re.compile(r"^[0-9A-F]{40}$")

#: Where a reader is sent when something is missing. One string, so the pointer
#: cannot drift between messages.
_EXAMPLE = (
    "Copy .env.example to .env and fill it in -- .env.example documents every "
    "variable the project reads."
)


class ConfigError(Exception):
    """Local configuration is missing or contradictory; nothing was contacted."""


def load_env_file(dotenv_path: Union[str, Path, None] = ".env") -> bool:
    """
    Load `.env` into the process environment. The ONE place that does this.

    Returns whether a file was actually read. Deliberately does NOT override
    variables already set in the environment: an explicit `set GRAPH_CLIENT=mock`
    for a single run should beat the file, and CI should not need to write one.

    Silent no-op when the file is absent or `python-dotenv` is not installed --
    reporting a *missing credential* is the job of the config classes, at the
    point of use, where the message can name the variable.
    """
    if not dotenv_path:
        return False
    path = Path(dotenv_path)
    if not path.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover -- dotenv is a declared dependency
        logger.debug("python-dotenv not installed; not loading %s", path)
        return False
    return bool(load_dotenv(path, override=False))


def _require(name: str, *, reason: str = "") -> str:
    """One required variable, or an error that names it."""
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set" + (f" ({reason})" if reason else "") + f".\n\n{_EXAMPLE}"
        )
    return value


@dataclass(repr=False)
class GraphConfig:
    """
    Everything needed to poll the shipments mailbox, plus which client to build.

    `repr` is suppressed by the dataclass and replaced below: an object holding a
    tenant id, a client id and a mailbox address should never be safe to drop
    into a log line by accident.
    """

    client_kind: str  # "mock" | "real"
    cert_path: Optional[Path] = None
    cert_public_path: Optional[Path] = None
    cert_thumbprint: Optional[str] = None
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    mailbox: Optional[str] = None
    #: Filled in by `from_env` when the certificate was read, so callers can
    #: report expiry without parsing it again.
    cert_not_after: Optional[dt.date] = None

    @property
    def is_mock(self) -> bool:
        return self.client_kind == "mock"

    def __repr__(self) -> str:
        """
        Paths and non-identifying facts only.

        Not a courtesy. A config object reaches a log through an f-string in an
        error path that nobody tested, and by then the tenant id is in a file
        someone else can read. Anything identifying is reported as its presence,
        never its value.
        """
        def presence(value: Optional[str]) -> str:
            return "set" if value else "unset"

        return (
            "GraphConfig("
            f"client_kind={self.client_kind!r}, "
            f"tenant_id=<{presence(self.tenant_id)}>, "
            f"client_id=<{presence(self.client_id)}>, "
            f"mailbox=<{presence(self.mailbox)}>, "
            f"cert_path={str(self.cert_path)!r}, "
            f"cert_thumbprint=<{presence(self.cert_thumbprint)}>, "
            f"cert_not_after={self.cert_not_after!r})"
        )

    __str__ = __repr__

    @classmethod
    def from_env(cls, dotenv_path: Union[str, Path, None] = ".env") -> "GraphConfig":
        """
        Build from `.env` / the environment, validating everything up front.

        **Validation happens at load time, not at first use.** A wrong tenant id
        or a mismatched certificate otherwise surfaces as an opaque `AADSTS`
        failure on the first poll, arbitrarily far from the cause; here it
        surfaces as a message naming the variable before anything is contacted.

        `GRAPH_CLIENT=mock` requires no credentials at all -- the mock needs none
        of them, and requiring them would block every bit of development on IT
        delivering a tenant id. That asymmetry is the point of the switch, not a
        loophole in it.
        """
        load_env_file(dotenv_path)

        # EXACT match, deliberately not case-insensitive. Surrounding whitespace
        # is stripped because it is invisible in an editor and not a typo, but
        # `Real` is rejected rather than folded to `real`: this switch decides
        # whether live requests reach a real mailbox, so it gets no normalisation
        # layer whose behaviour someone later has to reason about. The cost of
        # strictness is one immediately-obvious error message.
        raw = (os.environ.get("GRAPH_CLIENT") or "").strip()
        if raw not in GRAPH_CLIENTS:
            raise ConfigError(
                f"GRAPH_CLIENT must be exactly {' or '.join(repr(c) for c in GRAPH_CLIENTS)} "
                f"(lower case, exact), got {raw!r}.\n"
                "There is no default: an unset, misspelled or differently-cased value is "
                "an error rather than a guess, and it never falls back to 'real' -- a typo "
                f"must not be able to send live requests to a real mailbox.\n\n{_EXAMPLE}"
            )

        config = cls(client_kind=raw)

        # The certificate pair is validated whenever it is configured at all,
        # mock or real. A mock run does not need it, but checking it while it is
        # cheap means the mismatch is found now rather than on the day someone
        # flips the switch.
        cert_path = (os.environ.get("GRAPH_CERT_PATH") or "").strip()
        if raw == "real" or cert_path:
            config.cert_path = _resolve_path(
                _require("GRAPH_CERT_PATH", reason="the private key that signs the "
                         "client assertion"),
                "GRAPH_CERT_PATH",
            )
            config.cert_public_path = _resolve_path(
                _require("GRAPH_CERT_PUBLIC_PATH", reason="the public certificate whose "
                         "thumbprint Entra knows"),
                "GRAPH_CERT_PUBLIC_PATH",
            )
            config.cert_thumbprint = _normalise_thumbprint(
                _require("GRAPH_CERT_THUMBPRINT")
            )
            config.cert_not_after = _validate_cert_pair(
                config.cert_path, config.cert_public_path, config.cert_thumbprint
            )

        # Required only for `real`, but READ either way when present. "Not
        # required" is not the same as "ignored": loading them in mock keeps
        # `repr` honest about what `.env` actually holds, and means flipping the
        # switch changes which checks run, not which variables are consulted.
        for name, attr in (
            ("GRAPH_TENANT_ID", "tenant_id"),
            ("GRAPH_CLIENT_ID", "client_id"),
            ("GRAPH_MAILBOX", "mailbox"),
        ):
            if raw == "real":
                setattr(config, attr, _require(
                    name, reason="required when GRAPH_CLIENT=real"))
            else:
                setattr(config, attr, (os.environ.get(name) or "").strip() or None)

        return config


def _resolve_path(value: str, name: str) -> Path:
    """Expand and check a configured path, naming the variable if it is wrong."""
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_file():
        raise ConfigError(
            f"{name} points at {path}, which is not a readable file.\n\n{_EXAMPLE}"
        )
    return path


def _normalise_thumbprint(value: str) -> str:
    """
    `aa:bb:cc...` and lowercase both accepted; the portal renders it either way.

    Rejects anything that is not 40 hex characters after normalising, because a
    truncated or pasted-with-whitespace thumbprint would otherwise fail the match
    below with a confusing message about the wrong certificate.
    """
    cleaned = value.replace(":", "").replace(" ", "").strip().upper()
    if not _THUMBPRINT.match(cleaned):
        raise ConfigError(
            f"GRAPH_CERT_THUMBPRINT must be 40 hex characters (colons and lowercase "
            f"are fine), got {len(cleaned)} character(s) after normalising.\n"
            "This is the SHA-1 thumbprint from the Entra app registration -- NOT the "
            f"'Certificate ID' / keyId GUID, which is not an auth input.\n\n{_EXAMPLE}"
        )
    return cleaned


def _validate_cert_pair(
    key_path: Path, cert_path: Path, expected_thumbprint: str
) -> Optional[dt.date]:
    """
    Prove the key, the certificate and the configured thumbprint agree.

    Three separate checks, because each catches a different real mistake and the
    first two are commonly mistaken for sufficient:

    1. **The key parses.** Catches a path pointing at the certificate, a
       truncated file, or an encrypted key nobody mentioned.
    2. **The thumbprint matches the certificate.** Catches a mistyped or stale
       thumbprint in `.env`.
    3. **The key and certificate are a PAIR** -- sign with the key, verify with
       the certificate's public key. This is the one that catches "the
       certificate uploaded to Entra is not the one this key belongs to", which
       checks 1 and 2 both pass happily while auth fails much later with
       `AADSTS700027`. A thumbprint match alone only proves the fingerprint was
       typed correctly, not that the local key corresponds to it.

    Returns the certificate's `notAfter` date, and warns if it is inside
    `CERT_EXPIRY_WARN_DAYS`. Nothing here logs or returns key material.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.x509 import load_pem_x509_certificate

    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except Exception as exc:  # noqa: BLE001 -- any parse failure is the same answer
        raise ConfigError(
            f"GRAPH_CERT_PATH ({key_path}) did not parse as an unencrypted PEM private "
            f"key: {type(exc).__name__}. It must be the private key, not the "
            f"certificate, and not passphrase-protected.\n\n{_EXAMPLE}"
        ) from exc

    try:
        cert = load_pem_x509_certificate(cert_path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            f"GRAPH_CERT_PUBLIC_PATH ({cert_path}) did not parse as a PEM certificate: "
            f"{type(exc).__name__}.\n\n{_EXAMPLE}"
        ) from exc

    der = cert.public_bytes(serialization.Encoding.DER)
    actual = hashlib.sha1(der).hexdigest().upper()
    if actual != expected_thumbprint:
        raise ConfigError(
            f"GRAPH_CERT_THUMBPRINT does not match the certificate at {cert_path}.\n"
            f"  configured: {expected_thumbprint}\n"
            f"  actual    : {actual}\n"
            "The thumbprint in .env and the certificate on disk are different "
            f"certificates. Fix whichever is stale before this reaches Entra.\n\n{_EXAMPLE}"
        )

    probe = b"po-agent config pairing probe"
    try:
        signature = key.sign(probe, padding.PKCS1v15(), hashes.SHA256())
        cert.public_key().verify(signature, probe, padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            f"GRAPH_CERT_PATH ({key_path}) is not the private key for the certificate at "
            f"{cert_path} -- they are not a pair, even though the thumbprint matched.\n"
            "That means the certificate uploaded to Entra was generated separately from "
            "this key. Auth would fail later with an opaque AADSTS error.\n\n"
            f"{_EXAMPLE}"
        ) from exc

    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=dt.timezone.utc)
    days = (not_after - dt.datetime.now(dt.timezone.utc)).days
    if days <= CERT_EXPIRY_WARN_DAYS:
        logger.warning(
            "Graph certificate expires in %d day(s), on %s. Generate a replacement and "
            "upload it to the app registration before then -- expiry surfaces as an "
            "authentication failure with nothing having warned first.",
            days, not_after.date(),
        )
    return not_after.date()
