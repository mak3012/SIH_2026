"""Policy checks on the deployment files: what docs/security.md promises about the network,
TLS and secrets holds in docker-compose.yml. Plus the certificate and .env generators."""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from cryptography import x509

ROOT = Path(__file__).resolve().parents[2]
BASE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = BASE["services"]
# Services allowed a route out, and why (docs/security.md#network).
EGRESS = {"proxy": "published ports", "realtime": "rig WITSML", "edge-sync": "HQ over VSAT"}
sys.path.insert(0, str(ROOT / "infra"))
sys.path.insert(0, str(ROOT / "infra" / "tls"))


def test_only_listed_services_can_leave_the_backend() -> None:
    internal = {n for n, v in BASE["networks"].items() if v and v.get("internal")}
    assert internal == {"backend", "obs", "llm"}
    for name, s in SERVICES.items():
        nets = set(s.get("networks") or [])
        if s.get("network_mode") == "none":
            continue
        assert nets, f"{name} must name its networks"
        outside = nets - internal
        assert not outside or name in EGRESS, f"{name} has a route out via {outside}"
        assert "ports" not in s or name == "proxy", f"{name} publishes ports"


def test_no_dev_auth_in_any_compose_file() -> None:
    for f in ROOT.glob("docker-compose*.yml"):
        assert "DEV_AUTH" not in f.read_text(encoding="utf-8"), f


def test_every_hop_is_tls() -> None:
    env = SERVICES["api"]["environment"]
    assert env["PGSSLMODE"] == "verify-full" and env["S3_ENDPOINT"].startswith("https://")
    assert (
        env["OIDC_JWKS_URL"].startswith("https://")
        and "--ssl-certfile" in SERVICES["api"]["command"]
    )
    assert "ssl=on" in SERVICES["postgres"]["command"]
    hba = (ROOT / "infra/postgres/pg_hba.conf").read_text()
    assert re.search(r"^hostnossl\s+all\s+all\s+0\.0\.0\.0/0\s+reject", hba, re.M)
    assert "sslmode=verify-full" in SERVICES["keycloak"]["environment"]["KC_DB_URL"]
    assert SERVICES["keycloak"]["environment"]["KC_HTTP_ENABLED"] == "false"
    assert "-s3.cert.file=/certs/tls.crt" in SERVICES["s3"]["command"][0]
    assert "-ip.bind=127.0.0.1" in SERVICES["s3"]["command"][0]  # internals on loopback
    assert SERVICES["grafana"]["environment"]["GF_SERVER_PROTOCOL"] == "https"
    # Plain http:// only on loopback (health checks) and the observability / LLM networks.
    allowed = re.compile(r"http://(127\.0\.0\.1|localhost|prometheus|tempo|loki|llm):")
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    plain = [u for u in re.findall(r"http://[^\s'\"]+", text) if not allowed.match(u)]
    assert plain == [], plain
    caddy = (ROOT / "infra/caddy/Caddyfile").read_text()
    assert caddy.count("import tls_up") >= 4 and "http://llm:8080" in caddy


def test_each_service_mounts_only_its_own_key() -> None:
    for name, s in SERVICES.items():
        for v in s.get("volumes") or []:
            if isinstance(v, dict) and v.get("source") == "certs":
                assert v["read_only"] and v["volume"]["subpath"] in (name, "client"), name


def test_secrets_are_required_not_defaulted() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for key in ("POSTGRES_PASSWORD", "S3_ROOT_PASSWORD", "BACKUP_CIPHER_PASS"):
        assert re.search(rf"\${{{key}:\?", text), key
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    from env import SECRETS

    for key in SECRETS:
        assert re.search(rf"^{key}=$", example, re.M), f"{key} must be empty in .env.example"


def test_env_merge_keeps_values_and_fills_secrets() -> None:
    from env import merge

    text, made = merge(
        "A=1\nPOSTGRES_PASSWORD=\nBACKUP_CIPHER_PASS=\n", "POSTGRES_PASSWORD=keep\nOLD=x\n"
    )
    assert "POSTGRES_PASSWORD=keep" in text and made == ["BACKUP_CIPHER_PASS"]
    assert re.search(r"^BACKUP_CIPHER_PASS=\S{32}$", text, re.M) and "OLD=x" in text
    again, made = merge("A=1\nPOSTGRES_PASSWORD=\nBACKUP_CIPHER_PASS=\n", text)
    assert made == [] and again.count("BACKUP_CIPHER_PASS=") == 1


def test_certificates_chain_to_the_ca_and_are_not_reissued(tmp_path: Path) -> None:
    import certs

    run = [sys.executable, str(ROOT / "infra/tls/certs.py"), "--ca", str(tmp_path / "ca")]
    first = subprocess.run([*run, "--out", str(tmp_path / "c")], capture_output=True, text=True)
    assert first.returncode == 0 and first.stdout.count("issued") == len(certs.SERVICES)
    again = subprocess.run([*run, "--out", str(tmp_path / "c")], capture_output=True, text=True)
    assert again.stdout.count("issued") == 0
    ca = x509.load_pem_x509_certificate((tmp_path / "c/ca.crt").read_bytes())
    for svc in certs.SERVICES:
        crt = x509.load_pem_x509_certificate((tmp_path / f"c/{svc}/tls.crt").read_bytes())
        crt.verify_directly_issued_by(ca)
        names = crt.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert svc in names.get_values_for_type(x509.DNSName)
    assert not (tmp_path / "c/ca.key").exists() and not (tmp_path / "c/client/tls.key").exists()


def test_slo_alerts_name_a_runbook_that_exists() -> None:
    rules = yaml.safe_load((ROOT / "infra/prometheus/rules.yml").read_text())
    alerts = {x["alert"]: x for g in rules["groups"] for x in g["rules"] if "alert" in x}
    for name in ("FeedLatencyHigh", "FeedSilent", "AlertEvaluationSlow", "LLMDown"):
        assert alerts[name]["labels"]["runbook"], name
    for a in alerts.values():
        if "runbook" in a["labels"]:
            assert (ROOT / f"docs/operations/runbooks/{a['labels']['runbook']}.md").exists()


@pytest.mark.parametrize(
    ("licence", "allowed"),
    [
        ("MIT", True),
        ("LGPL-3.0-only", True),
        ("MIT OR GPL-3.0-or-later", True),
        ("AGPL-3.0", False),
        ("GPL-2.0-only", False),
        ("SSPL-1.0", False),
        ("UNKNOWN", False),
    ],
)
def test_licence_policy(licence: str, allowed: bool) -> None:
    import licences

    assert (licences.verdict(licences.Pkg("npm", "x", "1", licence)) is None) == allowed
