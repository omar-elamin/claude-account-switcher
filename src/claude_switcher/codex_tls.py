"""Local certificate and trust management for the Codex HTTPS gateway."""

import os
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


TLS_DIR = (Path.home() / "Library" / "Application Support" / "Claude Switcher"
           / "codex-gateway-tls")
OPENSSL = "/usr/bin/openssl"
SECURITY = "/usr/bin/security"
_CERTIFICATE_LIFETIME_DAYS = 3650
# macOS rejects TLS server certificates valid for more than 825 days, even under a
# user-trusted root, so `security verify-cert` would never see the leaf as trusted.
_LEAF_LIFETIME_DAYS = 825
_MINIMUM_REMAINING_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class CertPaths:
    ca: Path
    cert: Path
    key: Path
    regenerated: bool = False
    previous_fingerprint: str | None = None


def _run(args, message, *, timeout=30):
    try:
        return subprocess.run(
            [str(arg) for arg in args], check=True, capture_output=True,
            text=True, timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(message) from exc


def _fingerprint(ca, openssl):
    try:
        result = _run(
            [openssl, "x509", "-in", ca, "-noout", "-fingerprint", "-sha1"],
            "Could not read the old Codex gateway certificate.",
        )
    except RuntimeError:
        return None
    fingerprint = result.stdout.strip().partition("=")[2].replace(":", "").upper()
    return fingerprint or None


def _existing_certificate_is_valid(paths, openssl):
    if not all(path.is_file() for path in (paths.ca, paths.cert, paths.key)):
        return False
    try:
        _run(
            [openssl, "verify", "-CAfile", paths.ca, paths.cert],
            "The existing Codex gateway certificate is invalid.",
        )
        _run(
            [openssl, "verify", "-check_ss_sig", "-CAfile", paths.ca, paths.ca],
            "The existing Codex gateway CA is invalid.",
        )
        _run(
            [openssl, "x509", "-in", paths.cert, "-noout", "-checkend",
             str(_MINIMUM_REMAINING_SECONDS)],
            "The existing Codex gateway certificate expires soon.",
        )
        _run(
            [openssl, "x509", "-in", paths.ca, "-noout", "-checkend",
             str(_MINIMUM_REMAINING_SECONDS)],
            "The existing Codex gateway CA expires soon.",
        )
        cert_public_key = _run(
            [openssl, "x509", "-in", paths.cert, "-pubkey", "-noout"],
            "Could not read the Codex gateway certificate.",
        ).stdout
        private_public_key = _run(
            [openssl, "pkey", "-in", paths.key, "-pubout"],
            "Could not read the Codex gateway private key.",
        ).stdout
        ca_text = _run(
            [openssl, "x509", "-in", paths.ca, "-noout", "-text"],
            "Could not inspect the Codex gateway CA.",
        ).stdout
        leaf_text = _run(
            [openssl, "x509", "-in", paths.cert, "-noout", "-text"],
            "Could not inspect the Codex gateway certificate.",
        ).stdout
    except RuntimeError:
        return False
    ca_requirements = (
        "Subject: CN=Claude Switcher Local CA (",
        "Public Key Algorithm: id-ecPublicKey",
        "Public-Key: (256 bit)",
        "ASN1 OID: prime256v1",
        "X509v3 Basic Constraints: critical",
        "CA:TRUE, pathlen:0",
        "X509v3 Key Usage: critical",
        "Certificate Sign, CRL Sign",
        "X509v3 Subject Key Identifier:",
        "X509v3 Name Constraints: critical",
        "IP:127.0.0.1/255.255.255.255",
        "Signature Algorithm: ecdsa-with-SHA256",
    )
    leaf_requirements = (
        "Subject: CN=127.0.0.1",
        "Public Key Algorithm: id-ecPublicKey",
        "Public-Key: (256 bit)",
        "ASN1 OID: prime256v1",
        "X509v3 Basic Constraints: critical",
        "CA:FALSE",
        "X509v3 Key Usage: critical",
        "Digital Signature",
        "X509v3 Extended Key Usage:",
        "TLS Web Server Authentication",
        "X509v3 Authority Key Identifier:",
        "X509v3 Subject Key Identifier:",
        "X509v3 Subject Alternative Name:",
        "IP Address:127.0.0.1",
        "Signature Algorithm: ecdsa-with-SHA256",
    )
    return (
        cert_public_key == private_public_key
        and all(value in ca_text for value in ca_requirements)
        and all(value in leaf_text for value in leaf_requirements)
    )


def ensure_certificate(directory=TLS_DIR, openssl=OPENSSL):
    """Return a reusable loopback certificate, replacing invalid sets safely."""
    directory = Path(directory)
    paths = CertPaths(
        directory / "ca.pem", directory / "cert.pem", directory / "key.pem",
        regenerated=False, previous_fingerprint=None,
    )
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise RuntimeError("Could not create the Codex gateway certificate folder.") from exc

    if _existing_certificate_is_valid(paths, openssl):
        try:
            for path in (paths.ca, paths.cert, paths.key):
                os.chmod(path, 0o600)
        except OSError as exc:
            raise RuntimeError("Could not secure the Codex gateway certificate files.") from exc
        return paths

    previous_fingerprint = _fingerprint(paths.ca, openssl) if paths.ca.exists() else None
    temp_dir = Path(tempfile.mkdtemp(prefix=".generate-", dir=directory))
    ca_key = temp_dir / "ca.key"
    generated_ca = temp_dir / "ca.pem"
    leaf_key = temp_dir / "key.pem"
    leaf_csr = temp_dir / "leaf.csr"
    generated_cert = temp_dir / "cert.pem"
    extensions = temp_dir / "leaf.ext"
    try:
        os.chmod(temp_dir, 0o700)
        hostname = socket.gethostname().split(".", 1)[0] or "Mac"
        hostname = "".join(char for char in hostname if char.isalnum() or char in "-_") or "Mac"
        subject = f"/CN=Claude Switcher Local CA ({hostname}-{secrets.token_hex(4)})"

        _run(
            [openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout",
             "-out", ca_key],
            "Could not generate the Codex gateway certificate.",
        )
        os.chmod(ca_key, 0o600)
        _run(
            [openssl, "req", "-x509", "-new", "-sha256", "-key", ca_key, "-days",
             str(_CERTIFICATE_LIFETIME_DAYS), "-subj", subject,
             "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign",
             "-addext", "subjectKeyIdentifier=hash",
             "-addext",
             "nameConstraints=critical,permitted;IP:127.0.0.1/255.255.255.255",
             "-out", generated_ca],
            "Could not generate the Codex gateway certificate.",
        )
        _run(
            [openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout",
             "-out", leaf_key],
            "Could not generate the Codex gateway certificate.",
        )
        os.chmod(leaf_key, 0o600)
        _run(
            [openssl, "req", "-new", "-key", leaf_key, "-subj", "/CN=127.0.0.1",
             "-out", leaf_csr],
            "Could not generate the Codex gateway certificate.",
        )
        extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\n"
            "authorityKeyIdentifier=keyid,issuer\n"
            "subjectKeyIdentifier=hash\n"
            "subjectAltName=IP:127.0.0.1\n",
            encoding="utf-8",
        )
        os.chmod(extensions, 0o600)
        _run(
            [openssl, "x509", "-req", "-sha256", "-in", leaf_csr, "-CA", generated_ca,
             # -CAcreateserial derives its .srl path from the first "." in the CA path,
             # which can land outside the temp dir (e.g. /Users/john.doe).
             "-CAkey", ca_key, "-set_serial", f"0x{secrets.token_hex(16)}", "-days",
             str(_LEAF_LIFETIME_DAYS), "-extfile", extensions,
             "-out", generated_cert],
            "Could not sign the Codex gateway certificate.",
        )
        ca_key.unlink(missing_ok=True)

        _run(
            [openssl, "verify", "-CAfile", generated_ca, generated_cert],
            "Could not verify the new Codex gateway certificate.",
        )
        for path in (generated_ca, generated_cert, leaf_key):
            os.chmod(path, 0o600)
        os.replace(generated_ca, paths.ca)
        os.replace(generated_cert, paths.cert)
        os.replace(leaf_key, paths.key)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("Could not install the Codex gateway certificate.") from exc
    finally:
        try:
            ca_key.unlink(missing_ok=True)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    return CertPaths(
        paths.ca, paths.cert, paths.key, regenerated=True,
        previous_fingerprint=previous_fingerprint,
    )


def is_trusted(paths):
    """Check the leaf against the current macOS trust stores only."""
    try:
        _run(
            [SECURITY, "verify-cert", "-c", paths.cert, "-p", "ssl", "-s",
             "127.0.0.1", "-L", "-q"],
            "The Codex gateway certificate is not trusted.",
        )
    except RuntimeError:
        return False
    return True


def _login_keychain():
    fallback = Path.home() / "Library" / "Keychains" / "login.keychain-db"
    try:
        result = _run(
            [SECURITY, "login-keychain"],
            "Could not find the login keychain.",
        )
    except RuntimeError:
        return fallback
    value = result.stdout.strip().strip('"')
    return Path(value).expanduser() if value else fallback


def ensure_trusted(paths, previous_fingerprint=None):
    """Add the generated CA to the user's SSL trust settings when needed."""
    keychain = None
    if not is_trusted(paths):
        keychain = _login_keychain()
        try:
            _run(
                [SECURITY, "add-trusted-cert", "-r", "trustRoot", "-p", "ssl",
                 "-k", keychain, paths.ca],
                "Could not trust the Codex gateway certificate.",
                timeout=120,
            )
        except RuntimeError as exc:
            raise RuntimeError("Could not trust the Codex gateway certificate.") from exc

    if previous_fingerprint:
        keychain = keychain or _login_keychain()
        try:
            _run(
                [SECURITY, "delete-certificate", "-Z", previous_fingerprint, "-t",
                 keychain],
                "Could not remove the old Codex gateway certificate.",
            )
        except RuntimeError:
            pass


def ssl_context(paths):
    """Build the TLS server context for the loopback gateway."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=paths.cert, keyfile=paths.key)
    return context
