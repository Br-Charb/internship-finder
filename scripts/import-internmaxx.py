"""Merge Internmaxx's public company registry into sources.toml.

This is a maintainer tool, not Lambda runtime code. It deliberately uses only
the standard library and keeps locally configured sources that are not present
in Internmaxx. Run it manually whenever the upstream registry should be
refreshed, then review and commit the resulting sources.toml.
"""
from __future__ import annotations

import argparse
import json
import re
import tomllib
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = (
    "https://raw.githubusercontent.com/jhuynh06/internmaxx/main/backend/companies.yaml"
)

SECTION_ORDER = (
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "rippling",
    "workday",
    "eightfold",
    "clearcompany",
    "amazon",
    "apple",
    "bloomberg",
    "citadel",
    "deshaw",
    "goldman",
    "google",
    "meta",
    "microsoft",
    "netflix",
    "oracle",
    "shopify",
    "sig",
    "twosigma",
    "uber",
    "list",
    "getro",
)

BOARD_SECTIONS = {"greenhouse", "lever", "ashby", "smartrecruiters", "rippling"}
CUSTOM_SECTIONS = {
    "amazon", "apple", "bloomberg", "citadel", "deshaw", "goldman", "google",
    "meta", "microsoft", "netflix", "shopify", "sig", "twosigma", "uber",
}

# Upstream boards occasionally migrate ATS providers before the registry is
# updated. Keep verified corrections here so a later refresh cannot restore a
# retired endpoint.
SOURCE_OVERRIDES: dict[str, dict[str, Any]] = {
    "fireworks ai": {"ats": "ashby", "slug": "fireworks"},
}


def _split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    start = depth = 0
    quote = ""
    for index, char in enumerate(value):
        if quote:
            if char == quote and value[index - 1:index] != "\\":
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("{") and value.endswith("}"):
        return _parse_inline_map(value[1:-1])
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if re.fullmatch(r"\d+", value):
        return int(value)
    return value


def _parse_inline_map(body: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for part in _split_top_level(body):
        key, separator, value = part.partition(":")
        if not separator:
            raise ValueError(f"Could not parse registry field: {part!r}")
        result[key.strip()] = _parse_scalar(value)
    return result


def parse_registry(text: str) -> list[dict[str, Any]]:
    companies: list[dict[str, Any]] = []
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line.startswith("- {"):
            continue
        # Registry comments are outside the inline map. Values currently do not
        # contain a literal "} #", so this is intentionally small and strict.
        body = line[2:]
        end = body.rfind("}")
        if end < 0:
            raise ValueError(f"Unclosed registry entry on line {number}")
        companies.append(_parse_inline_map(body[1:end]))
    return companies


def convert_company(company: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    company = {
        **company,
        **SOURCE_OVERRIDES.get(str(company.get("name", "")).casefold(), {}),
    }
    section = str(company["ats"]).lower()
    entry: dict[str, Any] = {
        "name": str(company["name"]),
        "tier": int(company.get("tier", 3)),
        "group": str(company.get("group", "other")),
    }
    slug = str(company.get("slug", ""))
    if section in BOARD_SECTIONS:
        entry["board"] = slug
    elif section == "workday":
        wd = company.get("workday") or {}
        entry["host"] = f"{wd['tenant']}.{wd['instance']}.myworkdayjobs.com"
        entry["site"] = str(wd["site"])
        if wd.get("search"):
            entry["search"] = str(wd["search"])
    elif section == "eightfold":
        ef = company.get("eightfold") or {}
        entry["host"] = str(ef["host"])
        entry["domain"] = str(ef["domain"])
        entry["query"] = str(ef.get("query", ""))
    elif section == "oracle":
        oracle = company.get("orc") or {}
        entry["tenant"] = str(oracle["tenant"])
        entry["site"] = str(oracle["site"])
        entry["slug"] = slug
    elif section in CUSTOM_SECTIONS:
        entry["slug"] = slug
    else:
        raise ValueError(f"Unsupported Internmaxx ATS type: {section}")
    return section, entry


def source_identity(section: str, entry: dict[str, Any]) -> tuple[str, ...]:
    def norm(key: str) -> str:
        return str(entry.get(key, "")).strip().casefold()

    if section in BOARD_SECTIONS:
        return section, norm("board")
    if section == "workday":
        return section, norm("host"), norm("site")
    if section == "eightfold":
        return section, norm("host"), norm("domain")
    if section == "clearcompany":
        return section, norm("site_id")
    if section == "list":
        return section, norm("name")
    if section == "getro":
        return section, str(entry.get("collection", ""))
    return section, norm("slug") or norm("name")


def merge(existing: dict[str, Any], upstream: list[dict[str, Any]]) -> dict[str, Any]:
    overridden_names = set(SOURCE_OVERRIDES)
    result: dict[str, Any] = {
        section: [
            dict(entry)
            for entry in existing.get(section, [])
            if str(entry.get("name", "")).casefold() not in overridden_names
        ]
        for section in SECTION_ORDER
        if isinstance(existing.get(section), list)
    }
    seen = {
        source_identity(section, entry)
        for section, entries in result.items()
        for entry in entries
    }

    for company in upstream:
        section, entry = convert_company(company)
        identity = source_identity(section, entry)
        if identity in seen:
            # Preserve local naming/tiering, but carry the useful upstream group.
            for current in result.get(section, []):
                if source_identity(section, current) == identity:
                    current.setdefault("group", entry["group"])
                    if "query" in entry:
                        current.setdefault("query", entry["query"])
                    break
            continue
        result.setdefault(section, []).append(entry)
        seen.add(identity)

    for section, entries in result.items():
        entries.sort(key=lambda item: (
            int(item.get("tier", 1)),
            str(item.get("group", "")),
            str(item.get("name", "")).casefold(),
        ))

    if "usajobs" in existing:
        result["usajobs"] = dict(existing["usajobs"])
    return result


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def render_toml(config: dict[str, Any], upstream_count: int) -> str:
    lines = [
        "# Master source catalog for the Lambda scraper.",
        f"# Includes {upstream_count} entries from jhuynh06/internmaxx plus local sources.",
        "# Refresh manually with: python scripts/import-internmaxx.py",
        "# tier 1 = every 5 min; tier 2 = every 15 min; tier 3 = hourly.",
        "",
    ]
    preferred_keys = (
        "name", "board", "host", "domain", "site", "site_id", "collection",
        "slug", "search", "urls", "keywords", "tier", "group",
    )
    for section in SECTION_ORDER:
        for entry in config.get(section, []):
            lines.append(f"[[{section}]]")
            keys = [key for key in preferred_keys if key in entry]
            keys.extend(key for key in entry if key not in keys)
            for key in keys:
                lines.append(f"{key} = {_toml_value(entry[key])}")
            lines.append("")

    if "usajobs" in config:
        lines.append("[usajobs]")
        entry = config["usajobs"]
        for key in ("keywords", "tier", "group"):
            if key in entry:
                lines.append(f"{key} = {_toml_value(entry[key])}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-url", default=DEFAULT_REGISTRY)
    parser.add_argument("--sources", type=Path, default=ROOT / "sources.toml")
    parser.add_argument("--check", action="store_true", help="show counts without writing")
    args = parser.parse_args()

    request = urllib.request.Request(args.registry_url, headers={"User-Agent": "internship-finder-importer/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        upstream = parse_registry(response.read().decode("utf-8"))
    existing = tomllib.loads(args.sources.read_text(encoding="utf-8"))
    merged = merge(existing, upstream)
    output = render_toml(merged, len(upstream))

    direct_sections = set(SECTION_ORDER) - {"list", "getro"}
    direct_count = sum(len(merged.get(section, [])) for section in direct_sections)
    print(f"Internmaxx entries: {len(upstream)}; merged direct sources: {direct_count}")
    if not args.check:
        args.sources.write_text(output, encoding="utf-8")
        print(f"Updated {args.sources}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
