"""
Generate the RSA keypair + self-signed X.509 certificate that NetSuite's
OAuth 2.0 Client Credentials (M2M) flow requires.

How NetSuite's M2M auth works, so the output of this script makes sense:

  - There is no browser login anywhere in this flow. Instead, our service signs
    a short-lived JWT ("client assertion") with the PRIVATE key generated here,
    and POSTs it to NetSuite's token endpoint. NetSuite verifies that signature
    against the PUBLIC certificate you upload into its UI, and hands back a
    1-hour bearer access token.
  - So: the certificate goes INTO NetSuite (public, safe to email/upload).
    The private key never leaves this machine (secret, never uploaded anywhere).
  - NetSuite assigns the uploaded certificate an ID. That ID becomes the JWT's
    `kid` header, which is how NetSuite knows which certificate to verify with.
    You'll paste it back as NS_CERTIFICATE_ID.

Where the files go (and why not here):

  This project folder lives inside OneDrive. Anything written here gets synced
  to Microsoft's cloud and inherited by anyone the folder is ever shared with,
  which is not an acceptable home for a private key that can authenticate as a
  NetSuite service account. So the default output directory is
  %USERPROFILE%\\.po-agent\\keys, which OneDrive does not sync. Override with
  --out-dir if you have a better location (a secrets manager / Azure Key Vault
  is the right answer once this is hosted -- see the build plan's Phase 4).

Usage:
    .venv\\Scripts\\python.exe generate_m2m_keypair.py
    .venv\\Scripts\\python.exe generate_m2m_keypair.py --out-dir D:\\secrets --days 365

Re-running will refuse to clobber an existing private key unless --force is
passed -- overwriting the key silently would break the mapping NetSuite holds
for the old certificate, with a confusing "invalid_client" error as the only
symptom.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# NetSuite accepts RSA 2048/3072/4096 with SHA-256. 2048 is the documented
# minimum; 4096 is a free upgrade here since we sign at most one JWT per hour.
DEFAULT_KEY_SIZE = 4096

# NetSuite's certificate-to-role mapping expires when the certificate does, and
# the UI caps validity at 2 years. 730 days keeps us at that cap; shorter is
# fine if you'd rather rotate more often (you must re-upload on expiry either
# way -- see the rotation note printed at the end).
DEFAULT_DAYS = 730

DEFAULT_OUT_DIR = Path.home() / ".po-agent" / "keys"

PRIVATE_KEY_NAME = "netsuite_m2m_private.pem"
CERT_NAME = "netsuite_m2m_cert.pem"


def _lock_down_windows_acl(path: Path) -> str:
    """
    Restrict a file to the current user only.

    chmod 600 is meaningless on Windows, so use icacls: strip inherited
    permissions (which would otherwise include Administrators/SYSTEM) and grant
    full control to just the current user. Returns a human-readable result
    string rather than raising -- a failed ACL tightening is worth reporting
    loudly but shouldn't destroy the freshly generated key.
    """
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{_current_user()}:(F)"],
            check=True,
            capture_output=True,
            text=True,
        )
        return f"locked to {_current_user()} only (inherited ACLs removed)"
    except FileNotFoundError:
        return "SKIPPED -- icacls not found; tighten permissions manually"
    except subprocess.CalledProcessError as exc:
        return f"FAILED -- {exc.stderr.strip() or exc}; tighten permissions manually"


def _current_user() -> str:
    import os

    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def generate(out_dir: Path, key_size: int, days: int, force: bool, passphrase: str | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    key_path = out_dir / PRIVATE_KEY_NAME
    cert_path = out_dir / CERT_NAME

    if key_path.exists() and not force:
        sys.exit(
            f"Refusing to overwrite an existing private key:\n  {key_path}\n\n"
            "If you genuinely want a new keypair, pass --force -- but note you will then\n"
            "have to upload the NEW certificate into NetSuite and update NS_CERTIFICATE_ID.\n"
            "Until you do, authentication will fail with 'invalid_client'."
        )

    print(f"Generating {key_size}-bit RSA keypair ...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)

    # Self-signed is correct here: NetSuite is not doing PKI chain validation,
    # it just pins the exact certificate you upload. No CA involved.
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "California"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Straight Down"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "PO Update Automation"),
            x509.NameAttribute(NameOID.COMMON_NAME, "netsuite-m2m-po-update"),
        ]
    )

    now = dt.datetime.now(dt.timezone.utc)
    not_after = now + dt.timedelta(days=days)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))  # small skew allowance
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())  # NetSuite requires a SHA-256 signature
    )

    if passphrase:
        encryption = serialization.BestAvailableEncryption(passphrase.encode())
    else:
        encryption = serialization.NoEncryption()

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    acl_result = _lock_down_windows_acl(key_path)
    fingerprint = cert.fingerprint(hashes.SHA256()).hex(":").upper()

    print()
    print("=" * 78)
    print("KEYPAIR GENERATED")
    print("=" * 78)
    print()
    print("  PRIVATE KEY (secret -- never upload this anywhere, ever):")
    print(f"    {key_path}")
    print(f"    permissions: {acl_result}")
    print(f"    encrypted:   {'yes (passphrase required at runtime)' if passphrase else 'no'}")
    print()
    print("  CERTIFICATE (public -- this is the file you upload to NetSuite):")
    print(f"    {cert_path}")
    print()
    print(f"  SHA-256 fingerprint: {fingerprint}")
    print(f"  Valid until:         {not_after.date().isoformat()}  ({days} days)")
    print()
    print("=" * 78)
    print("NEXT STEPS")
    print("=" * 78)
    print()
    print("  1. Follow NETSUITE-M2M-SETUP.md in this folder. It walks through creating")
    print("     the 'PO Update' role and Integration record, and uploading the")
    print(f"     certificate above at Setup > Integration > OAuth 2.0 Client Credentials")
    print("     (M2M) Setup.")
    print()
    print("  2. NetSuite will show you a Certificate ID after upload. Along with the")
    print("     Account ID and the Integration record's Consumer Key / Client ID, put")
    print("     those into a .env file (copy .env.example) -- they are the three values")
    print("     netsuite_client.py needs.")
    print()
    print(f"  3. Set a calendar reminder for ~{not_after.date().isoformat()}: when this")
    print("     certificate expires, NetSuite auth stops working. Rotation = re-run this")
    print("     script with --force, re-upload, update NS_CERTIFICATE_ID.")
    print()
    print("  Do NOT commit the private key, the .env file, or paste them into chat.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--key-size", type=int, default=DEFAULT_KEY_SIZE, choices=[2048, 3072, 4096])
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"certificate validity (default {DEFAULT_DAYS}; NetSuite caps at 2 years)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing private key")
    parser.add_argument(
        "--passphrase",
        default=None,
        help="encrypt the private key at rest. You must then set NS_PRIVATE_KEY_PASSPHRASE "
        "for the service to run unattended, which puts the secret in the environment "
        "instead of the file -- a real tradeoff, not a strict improvement.",
    )
    args = parser.parse_args()

    if args.days > 730:
        print(f"WARNING: {args.days} days exceeds NetSuite's documented 2-year cap; upload may be rejected.\n")

    generate(args.out_dir, args.key_size, args.days, args.force, args.passphrase)


if __name__ == "__main__":
    main()
