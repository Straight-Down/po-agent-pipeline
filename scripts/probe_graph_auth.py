"""
Standalone Graph auth probe. NOT part of the pipeline.

Answers one question -- "does the credential chain work at all?" -- before
anything is built on top of it, and answers it in four steps that fail
separately:

  a. mint a token with the certificate credential
  b. read the token's `roles` claim
  c. read one message from the target mailbox
  d. attempt a DIFFERENT mailbox and require a 403

## Why this is a throwaway script and not a pipeline component

The mock exists so that a pipeline failure is unambiguous: if the polling job
breaks, it broke in the polling job. Wiring a real Graph client in to answer a
credential question would put two candidate causes behind every future failure.
So this reads `GraphConfig` -- the same loader, the same validation, no second
config path -- and builds its own throwaway MSAL client. It imports nothing from
the pipeline beyond config, and nothing in the pipeline imports it.

**It does not require or modify `GRAPH_CLIENT`.** The switch stays on `mock`;
this forces real-mode *credential validation* for the duration of the probe
without changing what the pipeline would build.

## Why step (d) exists, and why a 200 there is the finding

`Mail.Read` application permission grants read access to **every mailbox in the
tenant** unless an application access policy restricts it. IT confirmed on
2026-09-09 that it is scoped to the single mailbox -- but that is an assurance,
and the pipeline behaves identically either way: it reads one mailbox, every test
passes, and no error or response distinguishes the two cases. Success at step (c)
therefore proves nothing about scope.

Step (d) is the only check here that can fail toward *more* access. A `403` is the
pass. A `200` means the grant is tenant-wide regardless of what was assured, and
that is a finding to escalate, not a passing probe.

## What this never prints

No token, ever -- not truncated, not fingerprinted. No message subjects, senders,
bodies or addresses; step (c) reports a status code and a COUNT. The mailbox
addresses being probed are printed, because you have to know which mailbox a 403
refers to for the result to mean anything.

    python scripts/probe_graph_auth.py
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

# The probe lives in scripts/ but reads the project's own config module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

#: The mailbox step (d) must be REFUSED. A real, populated mailbox in the same
#: tenant -- an address that does not exist would return 404 and prove nothing,
#: since a scoping failure and a typo would look the same.
DENIED_MAILBOX = "kiko.barroso@straightdown.com"

#: The role that has to be present. Its absence at step (b) separates "not
#: granted" from "granted but not admin-consented", which are different fixes
#: and look identical from the outside.
REQUIRED_ROLE = "Mail.Read"


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def load_real_credentials():
    """
    Load config, forcing real-mode credential validation without touching
    `GRAPH_CLIENT`.

    The pipeline's switch stays on `mock`. This asks `GraphConfig` for the same
    validation `real` would trigger -- key parses, thumbprint matches the
    certificate, key and certificate are a genuine pair -- by loading normally and
    then requiring the three identifiers itself. That keeps one loader and one set
    of checks rather than reimplementing them here.
    """
    import config

    cfg = config.GraphConfig.from_env()

    missing = [
        name
        for name, value in (
            ("GRAPH_TENANT_ID", cfg.tenant_id),
            ("GRAPH_CLIENT_ID", cfg.client_id),
            ("GRAPH_MAILBOX", cfg.mailbox),
            ("GRAPH_CERT_PATH", cfg.cert_path),
            ("GRAPH_CERT_PUBLIC_PATH", cfg.cert_public_path),
            ("GRAPH_CERT_THUMBPRINT", cfg.cert_thumbprint),
        )
        if not value
    ]
    if missing:
        raise config.ConfigError(
            "This probe needs the real credentials even though GRAPH_CLIENT is "
            f"'{cfg.client_kind}': missing {', '.join(missing)}.\n"
            "Fill them in .env -- the probe does not require GRAPH_CLIENT=real and "
            "will not change it."
        )
    return cfg


def step_a_mint_token(cfg) -> str | None:
    """Mint a token. Returns it for step (b); never prints it."""
    heading("(a) mint a token -- certificate credential, .default scope")
    import msal

    # Last-4 only. Enough to tell two registrations apart when diagnosing, and
    # not a GUID pasted into whatever captures this output -- the same reason
    # committed docs use placeholders (GRAPH-SETUP.md, "What is secret").
    #
    # Microsoft's error text below is printed VERBATIM and is not redacted. The
    # app id embedded in an AADSTS message is Microsoft's own output, it is the
    # thing that makes 700027 diagnosable at all, and truncating a diagnostic to
    # satisfy a rule about our own printing would be over-correction.
    print(f"    tenant  : ...{cfg.tenant_id[-4:]}")
    print(f"    client  : ...{cfg.client_id[-4:]}")
    print(f"    cert    : {cfg.cert_path.name} (thumbprint {cfg.cert_thumbprint[:8]}...)")
    print(f"    scope   : {GRAPH_SCOPE}")

    app = msal.ConfidentialClientApplication(
        client_id=cfg.client_id,
        authority=f"https://login.microsoftonline.com/{cfg.tenant_id}",
        client_credential={
            "private_key": cfg.cert_path.read_text(encoding="utf-8"),
            "thumbprint": cfg.cert_thumbprint,
            # Sent so Entra can match by certificate rather than thumbprint
            # alone; harmless when it matches, and it makes a mismatched pair
            # fail at the token endpoint instead of on the first API call.
            "public_certificate": cfg.cert_public_path.read_text(encoding="utf-8"),
        },
    )
    result = app.acquire_token_for_client(scopes=[GRAPH_SCOPE])

    if "access_token" in result:
        print(f"\n    RESULT: SUCCESS -- token acquired "
              f"(expires in {result.get('expires_in')}s, type "
              f"{result.get('token_type')})")
        return result["access_token"]

    # The full error text, deliberately: an AADSTS code is the entire diagnosis
    # and truncating it would mean a second round trip to find out what failed.
    print("\n    RESULT: FAILED")
    for key in ("error", "error_description", "correlation_id", "error_codes"):
        if result.get(key):
            print(f"      {key}: {result[key]}")
    return None


def step_b_roles(token: str) -> list[str]:
    """
    Attempt the `roles` claim -- and report honestly when it cannot be read.

    **A Microsoft Graph access token is opaque to the client, so this step cannot
    do what it was originally written to do.** Measured on 2026-09-09: a token
    minted for `graph.microsoft.com` carries a `nonce` in its JWT header, which
    marks Graph's protected token format, and the client-decodable payload holds
    29 claims with **no `roles` and no `scp`** — while the very same token then
    reads the target mailbox successfully. The claim is not absent because the
    permission is missing; it is absent because these tokens are not meant to be
    parsed by anyone but Graph.

    So an empty result here is **not evidence about the permission**, and saying
    otherwise sent one reader off to chase an admin consent that already existed.
    The question this step was for — was `Mail.Read` both granted *and*
    admin-consented, two things that fail separately and look identical from
    outside — is answered instead by **step (c)**: an unconsented application
    permission yields `403` on the call, so a `200` proves both.

    Kept rather than deleted because a future token format, or a non-Graph
    audience, may well carry the claim, and reporting what is actually there is
    cheap. The signature is not verified -- this reads our own freshly-minted
    token, it does not accept one from elsewhere.
    """
    heading("(b) the token's `roles` claim -- informational only")
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        header = json.loads(
            base64.urlsafe_b64decode(
                token.split(".")[0] + "=" * (-len(token.split(".")[0]) % 4))
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    RESULT: could not decode the token payload ({type(exc).__name__})")
        return []

    roles = claims.get("roles") or []
    protected = "nonce" in header
    print(f"    roles claim   : {roles if roles else '(absent)'}")
    print(f"    claims present: {len(claims)}")
    print(f"    Graph protected-token format (nonce in header): {protected}")

    if REQUIRED_ROLE in roles:
        print(f"\n    RESULT: {REQUIRED_ROLE} present in the token.")
    elif protected:
        print("\n    RESULT: INCONCLUSIVE BY DESIGN, and this is expected.")
        print("            A Graph access token is opaque to the client -- the payload")
        print("            we can decode omits `roles` and `scp` entirely. Absence here")
        print("            says NOTHING about the permission.")
        print(f"            Whether {REQUIRED_ROLE} is granted AND admin-consented is")
        print("            settled by step (c): an unconsented application permission")
        print("            returns 403, so a 200 there proves both.")
    else:
        print(f"\n    RESULT: {REQUIRED_ROLE} absent from a token that is NOT in Graph's")
        print("            protected format, so the claim was expected. Check that the")
        print("            permission is added and admin-consented -- but confirm")
        print("            against step (c) before acting on this.")
    return roles


def step_c_read_target(token: str, mailbox: str) -> int | None:
    """Read one message from the target mailbox. Status and COUNT only."""
    heading("(c) read the target mailbox")
    return _get_messages(token, mailbox, label="target")


def step_d_scoping_check(token: str, mailbox: str) -> int | None:
    """
    The scoping check. A 403 is the pass; a 200 is the finding.

    This is the only step whose *failure* means broader access than intended, and
    the only one that turns IT's assurance into an observation.
    """
    heading("(d) THE SCOPING CHECK -- a different mailbox, 403 expected")
    print("    IT confirmed on 2026-09-09 that Mail.Read is scoped to the target")
    print("    mailbox. That is an assurance; this is the observation.")
    status = _get_messages(token, mailbox, label="should be DENIED")

    print()
    if status == 403:
        print("    RESULT: PASS -- 403. Access is genuinely scoped; the application")
        print("            cannot read this mailbox. IT's assurance is confirmed by")
        print("            observation, not just asserted.")
    elif status == 200:
        print("    RESULT: *** FINDING *** -- 200. The application CAN read a mailbox")
        print("            it should not. The grant is tenant-wide despite the")
        print("            assurance. Escalate: an application access policy is")
        print("            missing or does not cover this app registration.")
    elif status == 404:
        print("    RESULT: INCONCLUSIVE -- 404. The mailbox was not found, so this")
        print("            proves nothing about scope. Re-run against a real,")
        print("            populated mailbox in the same tenant.")
    else:
        print(f"    RESULT: INCONCLUSIVE -- unexpected status {status}. Neither a clean")
        print("            refusal nor a successful read; investigate before trusting")
        print("            the scope either way.")
    return status


def _get_messages(token: str, mailbox: str, label: str) -> int | None:
    """
    GET one message. Reports the status code and a COUNT -- never content.

    No subject, sender, body or address from any message is read or printed. The
    probe answers "can it read this mailbox", which a count answers completely.
    """
    import requests

    url = f"{GRAPH_ROOT}/users/{mailbox}/messages"
    print(f"    GET /users/{mailbox}/messages?$top=1   [{label}]")
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"$top": 1, "$select": "id"},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    status: (no response) -- {type(exc).__name__}: {exc}")
        return None

    print(f"    status: {response.status_code}")
    if response.status_code == 200:
        try:
            count = len(response.json().get("value", []))
        except ValueError:
            count = -1
        print(f"    messages returned: {count}")
    else:
        # Error CODE only. An error body can echo the address or a mailbox name,
        # so the message text is deliberately not printed.
        try:
            error = response.json().get("error", {})
            print(f"    error code: {error.get('code')}")
        except ValueError:
            print("    error code: (response was not JSON)")
    return response.status_code


def main() -> int:
    print("=" * 78)
    print("GRAPH AUTH PROBE -- standalone, not part of the pipeline")
    print("=" * 78)
    print("Prints no token, no message content, no addresses from any message.")

    try:
        cfg = load_real_credentials()
    except Exception as exc:  # noqa: BLE001
        print(f"\nCONFIG FAILED: {exc}")
        return 1

    print(f"\n  GRAPH_CLIENT is '{cfg.client_kind}' and stays that way -- this probe")
    print("  forces real-mode credential validation without changing the switch.")
    print(f"  certificate expires: {cfg.cert_not_after}")

    token = step_a_mint_token(cfg)
    if not token:
        print("\nStopping: no token, so steps (b) to (d) cannot run.")
        return 1

    roles = step_b_roles(token)
    target = step_c_read_target(token, cfg.mailbox)
    denied = step_d_scoping_check(token, DENIED_MAILBOX)

    heading("SUMMARY")
    print(f"  (a) token minted            : {'yes' if token else 'NO'}")
    print(f"  (b) {REQUIRED_ROLE} in roles      : "
          f"{'yes' if REQUIRED_ROLE in roles else 'not readable (expected for Graph)'}")
    print(f"  (c) target mailbox read     : {target}")
    print(f"  (d) other mailbox refused   : {denied} "
          f"({'PASS' if denied == 403 else 'FINDING' if denied == 200 else 'inconclusive'})")

    # The verdict deliberately does NOT require the roles claim. It is unreadable
    # in a Graph token by design (step b), so gating on it reports a healthy
    # credential chain as broken -- which it did, on the first clean run.
    clean = bool(token) and target == 200 and denied == 403
    print(f"\n  OVERALL: {'CLEAN' if clean else 'NOT CLEAN -- see above'}")
    if clean and REQUIRED_ROLE not in roles:
        print(f"           ({REQUIRED_ROLE} was not readable from the token, which is")
        print("            expected for Graph. Step (c) is what proves the permission.)")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
