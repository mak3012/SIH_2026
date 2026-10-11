"""Create or update .env from .env.example, generating every secret that is empty.

    python infra/env.py [--env .env] [--example .env.example]     (make secrets)

Keys already set in .env are kept (so a rerun never rotates a password a volume depends on);
keys new in .env.example are added. Secrets are the keys listed in SECRETS; each gets 32 random
URL-safe characters. Production: inject these from the secret store instead (docs/security.md).
"""

import argparse
import re
import secrets
from pathlib import Path

SECRETS = (
    "POSTGRES_PASSWORD",
    "S3_ROOT_PASSWORD",
    "KEYCLOAK_ADMIN_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
    "BACKUP_CIPHER_PASS",
    "EDGE_SYNC_CLIENT_SECRET",
)
LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def merge(example: str, current: str) -> tuple[str, list[str]]:
    """The .env text: example's layout, current values where set, generated secrets where
    empty. Returns (text, generated keys)."""
    have = {m[1]: m[2] for line in current.splitlines() if (m := LINE.match(line))}
    out, made = [], []
    for line in example.splitlines():
        m = LINE.match(line)
        if not m:
            out.append(line)
            continue
        key, value = m[1], have.get(m[1], m[2])
        if key in SECRETS and not value:
            value = secrets.token_urlsafe(24)
            made.append(key)
        out.append(f"{key}={value}")
    extra = [f"{k}={v}" for k, v in have.items() if not re.search(rf"^{k}=", example, re.M)]
    if extra:
        out += ["", "# --- kept from the previous .env (not in .env.example) ---", *extra]
    return "\n".join(out) + "\n", made


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--env", type=Path, default=Path(".env"))
    p.add_argument("--example", type=Path, default=Path(".env.example"))
    a = p.parse_args()
    current = a.env.read_text(encoding="utf-8") if a.env.exists() else ""
    text, made = merge(a.example.read_text(encoding="utf-8"), current)
    a.env.write_text(text, encoding="utf-8")
    print(f"{a.env}: generated {', '.join(made) or 'nothing (all secrets set)'}")


if __name__ == "__main__":
    main()
