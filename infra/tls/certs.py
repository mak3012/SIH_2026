"""Private CA and one server certificate per service, for TLS on every hop of the stack.

    python infra/tls/certs.py --ca /ca --out /certs        (the compose `init` job runs this)

Writes /certs/<service>/{tls.crt, tls.key, ca.crt}, and /certs/client/ca.crt alone for clients
that serve nothing; each service mounts only its own folder
(compose volume subpath), so a compromised container holds its own key and nothing else. The
CA key stays in /ca, mounted only into this job.

Other CAs the stack must trust (the rig's WITSML store, headquarters' CA on a rig box): put
their PEM files in /ca/trust/; they are appended to every ca.crt bundle.

Idempotent: keeps the CA, and re-issues a service certificate only when it is missing, expires
within 30 days, or its names changed. Names: the service, `localhost`, 127.0.0.1, plus
TLS_EXTRA_SANS (comma-separated, e.g. the rig box's or the site's host name).

Production: replace the CA with the organisation's PKI by dropping its signed tls.crt/tls.key
into each folder (and its root into ca.crt); this script never overwrites a certificate that is
still valid and has the right names.
"""

import argparse
import ipaddress
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# service -> uid that must be able to read its key (the image's user)
SERVICES = {
    "postgres": 1000,  # timescale/timescaledb-ha
    "keycloak": 1000,
    "s3": 0,  # seaweedfs runs as root
    "api": 10001,  # infra/python.Dockerfile
    "realtime": 10001,
    "witsml-sim": 10001,
    "dagster": 10001,
    "edge-sync": 10001,
    "web": 0,  # nginx master reads it as root
    "proxy": 0,  # caddy
    "grafana": 472,
    "prometheus": 65534,
    "tempo": 10001,
    "loki": 10001,
}
CA_DAYS, CERT_DAYS, RENEW_DAYS = 3650, 397, 30


def _name(cn: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Well Intelligence"),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ]
    )


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def ca(dir: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    crt, key = dir / "ca.crt", dir / "ca.key"
    if crt.exists() and key.exists():
        k = serialization.load_pem_private_key(key.read_bytes(), None)
        assert isinstance(k, ec.EllipticCurvePrivateKey)
        return x509.load_pem_x509_certificate(crt.read_bytes()), k
    k = ec.generate_private_key(ec.SECP384R1())
    now = datetime.now(UTC)
    c = (
        x509.CertificateBuilder()
        .subject_name(_name("Well Intelligence private CA"))
        .issuer_name(_name("Well Intelligence private CA"))
        .public_key(k.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), critical=False)
        .sign(k, hashes.SHA384())
    )
    dir.mkdir(parents=True, exist_ok=True)
    key.write_bytes(_pem_key(k))
    key.chmod(0o400)
    crt.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    return c, k


def sans(service: str, extra: list[str]) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = []
    for n in dict.fromkeys([service, "localhost", "127.0.0.1", *extra]):
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            names.append(x509.DNSName(n))
    return names


def needs_issue(crt: Path, want: list[x509.GeneralName]) -> bool:
    if not crt.exists():
        return True
    c = x509.load_pem_x509_certificate(crt.read_bytes())
    if c.not_valid_after_utc - datetime.now(UTC) < timedelta(days=RENEW_DAYS):
        return True
    have = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return set(have) != set(want)


def issue(
    service: str,
    names: list[x509.GeneralName],
    ca_crt: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
) -> tuple[bytes, bytes]:
    k = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    c = (
        x509.CertificateBuilder()
        .subject_name(_name(service))
        .issuer_name(ca_crt.subject)
        .public_key(k.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return c.public_bytes(serialization.Encoding.PEM), _pem_key(k)


def _write(path: Path, data: bytes, uid: int, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(data)
    path.chmod(mode)
    if hasattr(os, "chown") and os.geteuid() == 0:
        os.chown(path, uid, uid)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ca", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    extra = [s.strip() for s in os.environ.get("TLS_EXTRA_SANS", "").split(",") if s.strip()]
    ca_crt, ca_key = ca(a.ca)
    ca_pem = ca_crt.public_bytes(serialization.Encoding.PEM)
    for extra_ca in sorted((a.ca / "trust").glob("*.pem")):
        ca_pem += extra_ca.read_bytes().strip() + b"\n"
        print(f"trusting {extra_ca.name}")
    for service, uid in SERVICES.items():
        d = a.out / service
        names = sans(service, extra)
        if needs_issue(d / "tls.crt", names):
            crt, key = issue(service, names, ca_crt, ca_key)
            _write(d / "tls.key", key, uid, 0o400)
            _write(d / "tls.crt", crt, uid, 0o444)
            print(f"issued {service}: {', '.join(str(n.value) for n in names)}")
        _write(d / "ca.crt", ca_pem, uid, 0o444)
        if hasattr(os, "chown") and os.geteuid() == 0:
            os.chown(d, uid, uid)
    _write(a.out / "client" / "ca.crt", ca_pem, 0, 0o444)
    _write(a.out / "ca.crt", ca_pem, 0, 0o444)  # for `make ca` (trust it on the host)


if __name__ == "__main__":
    main()
