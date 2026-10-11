"""Licence inventory, policy check and SBOM for the Python and npm dependencies.

    uv run python infra/licences.py                     check: exit 1 on a denied licence
    uv run python infra/licences.py --write-docs        also rewrite the table in docs/licences.md
    uv run python infra/licences.py --sbom sbom.cdx.json   CycloneDX 1.5 SBOM

Python: every distribution installed in the environment (CI syncs all packages and extras
first), licence from its metadata (License-Expression, then License, then classifiers).
npm: every package in package-lock.json (the lockfile records each package's licence).
Container images (PostgreSQL, Keycloak, ...) are listed by hand in docs/licences.md and scanned
in CI (anchore/sbom-action).

Policy (docs/licences.md#policy): strong copyleft and source-available licences are denied in
anything we link or ship, unless EXCEPTIONS says why it is acceptable; an unknown licence fails
until someone looks and records it here.
"""

import argparse
import json
import re
import sys
import tomllib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "licences.md"
DENY = re.compile(r"\b(A?GPL|SSPL|BUSL|Commons[- ]Clause|CC-BY-NC|Elastic)", re.I)
ALLOW_EVEN_IF_MATCHED = re.compile(r"\bLGPL|GPL-[23]\.0[-+ ]?(-or-later)? WITH .*exception", re.I)
# package -> (licence as we determined it, why it is acceptable)
EXCEPTIONS: dict[str, tuple[str, str]] = {}
# Packages whose metadata carries no usable licence: read from their LICENSE file by hand.
KNOWN: dict[str, str] = {
    "parse-cache-control": "BSD-3-Clause",  # LICENSE: hapi's 3-clause BSD text
    "webgl-constants": "MIT",  # LICENSE: MIT
}
WORKSPACE = {"api", "core", "pipelines", "datatools", "@repo/ui", "web", "e2e"}


@dataclass
class Pkg:
    eco: str  # pypi | npm
    name: str
    version: str
    licence: str
    dev: bool = False

    @property
    def purl(self) -> str:
        return f"pkg:{self.eco}/{self.name}@{self.version}"


def _key(name: str) -> str:
    """PEP 503 normalised name (prometheus_client == prometheus-client)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _norm(s: str) -> str:
    s = " ".join(s.split())
    aliases = {
        "MIT License": "MIT",
        "Apache Software License": "Apache-2.0",
        "Apache License 2.0": "Apache-2.0",
        "Apache 2.0": "Apache-2.0",
        "Apache License, Version 2.0": "Apache-2.0",
        "BSD License": "BSD",
        "Python Software Foundation License": "PSF-2.0",
        "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
        "ISC License (ISCL)": "ISC",
        "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
        "GNU Library or Lesser General Public License (LGPL)": "LGPL",
        "The Unlicense (Unlicense)": "Unlicense",
    }
    return aliases.get(s, s)


def python_packages() -> list[Pkg]:
    out = {}
    for d in metadata.distributions():
        name = d.metadata["Name"]
        if not name or _key(name) in WORKSPACE:
            continue
        lic = d.metadata.get("License-Expression") or ""
        raw = d.metadata.get("License") or ""
        if not lic and raw and len(raw) < 80 and "\n" not in raw.strip():
            lic = raw
        if not lic or lic.upper() in ("UNKNOWN", "DUAL LICENSE"):
            cls = [
                c.split(" :: ")[-1]
                for c in d.metadata.get_all("Classifier") or []
                if c.startswith("License ::") and c != "License :: OSI Approved"
            ]
            lic = " OR ".join(_norm(c) for c in cls)
        lic = KNOWN.get(_key(name), _norm(lic) or "UNKNOWN")
        out[_key(name)] = Pkg("pypi", name, d.version, lic)
    return sorted(out.values(), key=lambda p: p.name.lower())


def npm_packages() -> list[Pkg]:
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    out = {}
    for path, p in lock["packages"].items():
        if not path.startswith("node_modules/") and "/node_modules/" not in path or p.get("link"):
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        lic = p.get("license") or "UNKNOWN"
        if isinstance(lic, dict):
            lic = lic.get("type", "UNKNOWN")
        key = f"{name}@{p.get('version')}"
        out[key] = Pkg(
            "npm", name, p.get("version", ""), KNOWN.get(name, str(lic)), bool(p.get("dev"))
        )
    return sorted(out.values(), key=lambda p: p.name)


def verdict(p: Pkg) -> str | None:
    """Why `p` is not allowed, or None."""
    if _key(p.name) in EXCEPTIONS:
        return None
    if p.licence == "UNKNOWN":
        return "licence unknown: check it and record it in KNOWN or EXCEPTIONS"
    if DENY.search(p.licence) and not ALLOW_EVEN_IF_MATCHED.search(p.licence):
        # "X OR Y": fine if any alternative is acceptable
        alts = [a.strip(" ()") for a in re.split(r"\bOR\b", p.licence)]
        if any(not DENY.search(a) or ALLOW_EVEN_IF_MATCHED.search(a) for a in alts):
            return None
        return f"{p.licence} is not allowed in code we link or ship"
    return None


# ---------------------------------------------------------------- direct dependencies
def _req_name(r: str) -> str:
    return _key(re.split(r"[\s\[<>=!~;]", r, maxsplit=1)[0])


def direct() -> list[tuple[str, str, str]]:
    """(ecosystem, package, use) for every dependency a manifest names."""
    rows = []
    py = {
        "apps/api": "API, real-time service, assistant",
        "packages/core": "shared domain code",
        "pipelines": "ingestion (Dagster)",
        "data": "synthetic data and seeding",
        ".": "development",
    }
    for d, what in py.items():
        t = tomllib.loads((ROOT / d / "pyproject.toml").read_text(encoding="utf-8"))
        for r in t.get("project", {}).get("dependencies", []):
            rows.append(("pypi", _req_name(r), what))
        for extra, reqs in t.get("project", {}).get("optional-dependencies", {}).items():
            rows += [("pypi", _req_name(r), f"{what} (extra {extra})") for r in reqs]
        for group, reqs in t.get("dependency-groups", {}).items():
            rows += [("pypi", _req_name(r), f"{what} ({group})") for r in reqs]
    js = {"apps/web": "web app", "packages/ui": "design system", "tests/e2e": "browser tests"}
    for d, what in js.items():
        t = json.loads((ROOT / d / "package.json").read_text(encoding="utf-8"))
        for kind, label in (("dependencies", what), ("devDependencies", f"{what} (build/dev)")):
            rows += [("npm", n, label) for n in t.get(kind, {}) if not n.startswith("@repo/")]
    rows = [r for r in rows if r[1] not in WORKSPACE]
    seen: dict[tuple[str, str], list[str]] = {}
    for eco, name, use in rows:
        seen.setdefault((eco, name), []).append(use)
    return [(e, n, "; ".join(dict.fromkeys(u))) for (e, n), u in sorted(seen.items())]


def table(pkgs: list[Pkg]) -> str:
    by = {(p.eco, _key(p.name) if p.eco == "pypi" else p.name): p for p in pkgs}
    lines = ["| Component | Version | Licence | Use |", "|---|---|---|---|"]
    for eco, name, use in direct():
        p = by.get((eco, name))
        lic, ver = (p.licence, p.version) if p else ("not installed here", "")
        lines.append(f"| `{name}` ({eco}) | {ver} | {lic} | {use} |")
    counts: dict[str, int] = {}
    for p in pkgs:
        counts[p.licence] = counts.get(p.licence, 0) + 1
    lines += [
        "",
        f"All {len(pkgs)} packages, direct and transitive, by licence:",
        "",
        "| Licence | Packages |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines)


def write_docs(pkgs: list[Pkg]) -> None:
    text = DOCS.read_text(encoding="utf-8")
    start, end = "<!-- generated: licences -->", "<!-- end generated -->"
    i, j = text.index(start) + len(start), text.index(end)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    DOCS.write_text(
        text[:i]
        + f"\n\n_Generated by `make licences` on {stamp}._\n\n"
        + table(pkgs)
        + "\n\n"
        + text[j:],
        encoding="utf-8",
    )


def sbom(pkgs: list[Pkg]) -> dict[str, object]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": {"type": "application", "name": "well-intelligence"},
        },
        "components": [
            {
                "type": "library",
                "name": p.name,
                "version": p.version,
                "purl": p.purl,
                "scope": "excluded" if p.dev else "required",
                "licenses": [{"expression": p.licence}] if p.licence != "UNKNOWN" else [],
            }
            for p in pkgs
        ],
    }


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("--write-docs", action="store_true")
    a.add_argument("--sbom", type=Path)
    args = a.parse_args()
    pkgs = python_packages() + npm_packages()
    bad = [(p, why) for p in pkgs if (why := verdict(p))]
    if args.write_docs:
        write_docs(pkgs)
    if args.sbom:
        args.sbom.write_text(json.dumps(sbom(pkgs), indent=1), encoding="utf-8")
    print(f"{len(pkgs)} packages checked; {len(bad)} not allowed")
    for p, why in bad:
        print(f"  {p.purl}: {why}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
