"""Hermetic tests for the Codex gateway's local TLS identity."""

import ssl
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_switcher import codex_tls


OPENSSL = "/usr/bin/openssl"


def _openssl(*args):
    return subprocess.run(
        [OPENSSL, *map(str, args)], check=True, capture_output=True, text=True,
    ).stdout


def _fingerprint(path):
    output = _openssl("x509", "-in", path, "-noout", "-fingerprint", "-sha1")
    return output.strip().partition("=")[2].replace(":", "").upper()


def test_generate_reuse_and_replace_certificate(tmp_path):
    directory = tmp_path / "tls"

    first = codex_tls.ensure_certificate(directory=directory, openssl=OPENSSL)

    assert first.regenerated is True
    assert first.previous_fingerprint is None
    assert (first.ca, first.cert, first.key) == (
        directory / "ca.pem", directory / "cert.pem", directory / "key.pem",
    )
    assert directory.stat().st_mode & 0o777 == 0o700
    assert first.ca.stat().st_mode & 0o777 in (0o600, 0o644)
    assert first.cert.stat().st_mode & 0o777 in (0o600, 0o644)
    assert first.key.stat().st_mode & 0o777 == 0o600
    assert not list(directory.rglob("ca.key"))

    leaf_text = _openssl("x509", "-in", first.cert, "-noout", "-text")
    ca_text = _openssl("x509", "-in", first.ca, "-noout", "-text")
    assert "Basic Constraints: critical" in ca_text
    assert "IP Address:127.0.0.1" in leaf_text
    assert "CA:TRUE, pathlen:0" in ca_text
    assert "Name Constraints: critical" in ca_text
    assert "IP:127.0.0.1/255.255.255.255" in ca_text
    assert "Basic Constraints: critical" in leaf_text
    assert "Key Usage: critical" in leaf_text
    assert "ASN1 OID: prime256v1" in ca_text
    assert "ASN1 OID: prime256v1" in leaf_text
    assert "serverAuth" in leaf_text or "TLS Web Server Authentication" in leaf_text
    assert "cert.pem: OK" in _openssl("verify", "-CAfile", first.ca, first.cert)

    fingerprint = _fingerprint(first.ca)
    original_bytes = tuple(path.read_bytes() for path in (first.ca, first.cert, first.key))
    first.key.chmod(0o644)
    second = codex_tls.ensure_certificate(directory=directory, openssl=OPENSSL)
    assert second.regenerated is False
    assert second.previous_fingerprint is None
    assert second.key.stat().st_mode & 0o777 == 0o600
    assert tuple(path.read_bytes() for path in (second.ca, second.cert, second.key)) == original_bytes
    assert _fingerprint(second.ca) == fingerprint

    second.cert.unlink()
    third = codex_tls.ensure_certificate(directory=directory, openssl=OPENSSL)
    assert third.regenerated is True
    assert third.previous_fingerprint == fingerprint
    assert _fingerprint(third.ca) != fingerprint
    assert not list(directory.rglob("ca.key"))


def test_generation_failure_leaves_no_key_or_partial_install(tmp_path):
    fake = tmp_path / "fake-openssl"
    fake.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = ecparam ]; then\n"
        "  while [ \"$1\" != \"\" ]; do\n"
        "    if [ \"$1\" = -out ]; then shift; printf fake > \"$1\"; exit 0; fi\n"
        "    shift\n"
        "  done\n"
        "fi\n"
        "exit 7\n",
    )
    fake.chmod(0o755)
    directory = tmp_path / "tls"

    with pytest.raises(RuntimeError, match="certificate"):
        codex_tls.ensure_certificate(directory=directory, openssl=str(fake))

    assert not list(directory.rglob("ca.key"))
    assert not (directory / "ca.pem").exists()
    assert not (directory / "cert.pem").exists()
    assert not (directory / "key.pem").exists()


def test_mismatched_leaf_key_regenerates(tmp_path):
    directory = tmp_path / "tls"
    first = codex_tls.ensure_certificate(directory=directory, openssl=OPENSSL)
    fingerprint = _fingerprint(first.ca)
    _openssl(
        "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", first.key,
    )

    replaced = codex_tls.ensure_certificate(directory=directory, openssl=OPENSSL)

    assert replaced.regenerated is True
    assert replaced.previous_fingerprint == fingerprint
    codex_tls.ssl_context(replaced)


def test_unconstrained_rsa_certificate_set_regenerates(tmp_path):
    directory = tmp_path / "tls"
    directory.mkdir()
    ca_key = tmp_path / "rsa-ca.key"
    csr = tmp_path / "rsa-leaf.csr"
    ext = tmp_path / "rsa-leaf.ext"
    _openssl("genrsa", "-out", ca_key, "2048")
    _openssl(
        "req", "-x509", "-new", "-sha256", "-key", ca_key, "-days", "3650",
        "-subj", "/CN=Unconstrained RSA CA", "-addext",
        "basicConstraints=critical,CA:TRUE", "-out", directory / "ca.pem",
    )
    _openssl("genrsa", "-out", directory / "key.pem", "2048")
    _openssl(
        "req", "-new", "-key", directory / "key.pem", "-subj", "/CN=elsewhere",
        "-out", csr,
    )
    ext.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\n",
    )
    _openssl(
        "x509", "-req", "-sha256", "-in", csr, "-CA", directory / "ca.pem",
        "-CAkey", ca_key, "-CAcreateserial", "-days", "3650", "-extfile", ext,
        "-out", directory / "cert.pem",
    )

    replaced = codex_tls.ensure_certificate(directory=directory, openssl=OPENSSL)

    assert replaced.regenerated is True
    leaf_text = _openssl("x509", "-in", replaced.cert, "-noout", "-text")
    ca_text = _openssl("x509", "-in", replaced.ca, "-noout", "-text")
    assert "ASN1 OID: prime256v1" in leaf_text
    assert "IP Address:127.0.0.1" in leaf_text
    assert "Name Constraints: critical" in ca_text


def test_ca_expiring_within_30_days_regenerates(tmp_path):
    directory = tmp_path / "tls"
    directory.mkdir()
    ca_key = tmp_path / "short-ca.key"
    csr = tmp_path / "leaf.csr"
    ext = tmp_path / "leaf.ext"
    _openssl(
        "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", ca_key,
    )
    _openssl(
        "req", "-x509", "-new", "-sha256", "-key", ca_key, "-days", "1",
        "-subj", "/CN=Claude Switcher Local CA (test-short)",
        "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "subjectKeyIdentifier=hash",
        "-addext", "nameConstraints=critical,permitted;IP:127.0.0.1/255.255.255.255",
        "-out", directory / "ca.pem",
    )
    _openssl(
        "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out",
        directory / "key.pem",
    )
    _openssl(
        "req", "-new", "-key", directory / "key.pem", "-subj", "/CN=127.0.0.1",
        "-out", csr,
    )
    ext.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        "subjectKeyIdentifier=hash\n"
        "subjectAltName=IP:127.0.0.1\n",
    )
    _openssl(
        "x509", "-req", "-sha256", "-in", csr, "-CA", directory / "ca.pem",
        "-CAkey", ca_key, "-CAcreateserial", "-days", "365", "-extfile", ext,
        "-out", directory / "cert.pem",
    )
    fingerprint = _fingerprint(directory / "ca.pem")

    replaced = codex_tls.ensure_certificate(directory=directory, openssl=OPENSSL)

    assert replaced.regenerated is True
    assert replaced.previous_fingerprint == fingerprint


def test_ssl_context_loads_generated_identity(tmp_path):
    paths = codex_tls.ensure_certificate(directory=tmp_path / "tls", openssl=OPENSSL)
    context = codex_tls.ssl_context(paths)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def _runner_with_trust(trusted, calls, add_fails=False, delete_fails=False):
    def run(args, message, *, timeout=30):
        calls.append(list(map(str, args)))
        command = args[1]
        if command == "verify-cert":
            if trusted:
                return SimpleNamespace(stdout="", stderr="")
            raise RuntimeError(message)
        if command == "login-keychain":
            return SimpleNamespace(stdout='"/tmp/login.keychain-db"\n', stderr="")
        if command == "add-trusted-cert" and add_fails:
            raise RuntimeError(message)
        if command == "delete-certificate" and delete_fails:
            raise RuntimeError(message)
        return SimpleNamespace(stdout="", stderr="")
    return run


def _paths(tmp_path):
    return codex_tls.CertPaths(
        tmp_path / "ca.pem", tmp_path / "cert.pem", tmp_path / "key.pem",
        regenerated=False, previous_fingerprint=None,
    )


def test_ensure_trusted_skips_add_when_keychain_already_trusts_leaf(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(codex_tls, "_run", _runner_with_trust(True, calls))
    codex_tls.ensure_trusted(_paths(tmp_path))
    assert [call[1] for call in calls] == ["verify-cert"]
    assert "-r" not in calls[0]
    assert calls[0][-6:] == ["-p", "ssl", "-s", "127.0.0.1", "-L", "-q"]


def test_ensure_trusted_adds_ca_to_login_keychain(tmp_path, monkeypatch):
    calls = []
    paths = _paths(tmp_path)
    monkeypatch.setattr(codex_tls, "_run", _runner_with_trust(False, calls))
    codex_tls.ensure_trusted(paths)
    add = next(call for call in calls if call[1] == "add-trusted-cert")
    assert add == [
        "/usr/bin/security", "add-trusted-cert", "-r", "trustRoot", "-p", "ssl",
        "-k", "/tmp/login.keychain-db", str(paths.ca),
    ]


def test_ensure_trusted_maps_add_failure_to_user_error(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(codex_tls, "_run", _runner_with_trust(False, calls, add_fails=True))
    with pytest.raises(RuntimeError, match="trust.*certificate"):
        codex_tls.ensure_trusted(_paths(tmp_path))


def test_previous_ca_delete_is_best_effort(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        codex_tls, "_run", _runner_with_trust(False, calls, delete_fails=True),
    )
    codex_tls.ensure_trusted(_paths(tmp_path), previous_fingerprint="A1B2")
    delete = next(call for call in calls if call[1] == "delete-certificate")
    assert delete == [
        "/usr/bin/security", "delete-certificate", "-Z", "A1B2", "-t",
        "/tmp/login.keychain-db",
    ]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS trust policy")
def test_macos_ssl_policy_accepts_generated_leaf_under_its_ca(tmp_path):
    # Passing the CA as an explicit anchor stands in for keychain trust without
    # touching the keychain. macOS rejects leaves valid for more than 825 days.
    paths = codex_tls.ensure_certificate(directory=tmp_path / "tls", openssl=OPENSSL)
    result = subprocess.run(
        ["/usr/bin/security", "verify-cert", "-c", str(paths.cert), "-r", str(paths.ca),
         "-p", "ssl", "-s", "127.0.0.1", "-L", "-q"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dotted_parent_path_generates_without_stray_serial_file(tmp_path):
    directory = tmp_path / "john.doe" / "tls"
    paths = codex_tls.ensure_certificate(directory=directory, openssl=OPENSSL)
    assert paths.regenerated is True
    assert not list(tmp_path.rglob("*.srl"))
    assert not list(tmp_path.rglob("*.key")) or list(tmp_path.rglob("*.key")) == [paths.key]
