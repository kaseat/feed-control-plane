#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Set
from urllib.request import Request, urlopen

try:
    import tldextract
except ImportError:  # pragma: no cover
    tldextract = None

USER_AGENT = "node-control-runtime-builder/1.0"

DOMAIN_SOURCES = {
    "antifilter_domains": "https://antifilter.download/list/domains.lst",
    "community_domains": "https://community.antifilter.download/list/domains.lst",
    "refilter_domains": "https://raw.githubusercontent.com/1andrevich/Re-filter-lists/refs/heads/main/domains_all.lst",
}

CRITICAL_DOMAIN_SOURCES = {
    "allow_anime": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Categories/anime.lst",
    "allow_block": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Categories/block.lst",
    "allow_geoblock": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Categories/geoblock.lst",
    "allow_hodca": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Categories/hodca.lst",
    "allow_news": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Categories/news.lst",
    "allow_porn": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Categories/porn.lst",
    "allow_cloudflare": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/cloudflare.lst",
    "allow_cloudfront": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/cloudfront.lst",
    "allow_digitalocean": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/digitalocean.lst",
    "allow_discord": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/discord.lst",
    "allow_google_ai": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/google_ai.lst",
    "allow_google_meet": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/google_meet.lst",
    "allow_hdrezka": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/hdrezka.lst",
    "allow_hetzner": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/hetzner.lst",
    "allow_meta": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/meta.lst",
    "allow_roblox": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/roblox.lst",
    "allow_telegram": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/telegram.lst",
    "allow_tiktok": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/tiktok.lst",
    "allow_twitter": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/twitter.lst",
    "allow_youtube": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Services/youtube.lst",
}

SERVICE_IP_SOURCE_URLS = {
    "cloudflare": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/cloudflare.lst",
    "cloudfront": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/cloudfront.lst",
    "digitalocean": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/digitalocean.lst",
    "discord": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/discord.lst",
    "google_meet": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/google_meet.lst",
    "hetzner": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/hetzner.lst",
    "meta": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/Meta.lst",
    "roblox": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/roblox.lst",
    "telegram": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/telegram.lst",
    "twitter": "https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/Twitter.lst",
}

CRITICAL_DOMAIN_EXCLUDES = {
    "google.com",
    "cloudflare-dns.com",
    "parsec.app",
    "microsoft.com",
}

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-_]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-_]{0,61}[a-z0-9])?)+\.?$")
COMMENT_SPLIT_RE = re.compile(r"\s+#.*$")


def fetch_text(url: str, timeout: int = 45) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data.decode("utf-8", errors="replace")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_lines(path: Path, lines: Iterable[str]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")


def read_local_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def clean_line(line: str) -> str:
    return COMMENT_SPLIT_RE.sub("", line.strip()).strip()


def normalize_domain(raw: str) -> str | None:
    line = clean_line(raw)
    if not line:
        return None

    for prefix in ("domain:", "full:", "regexp:", "keyword:", "include:"):
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
            if prefix in ("regexp:", "keyword:", "include:"):
                return None
            break

    parts = line.split()
    if len(parts) >= 2 and parts[0] in {"0.0.0.0", "127.0.0.1", "::1"}:
        line = parts[1]

    line = line.lower().strip().lstrip(".")
    if line.startswith("*."):
        line = line[2:]
    if line.endswith("."):
        line = line[:-1]

    if not line or "/" in line or ":" in line or line.startswith("@"):
        return None
    return line if DOMAIN_RE.match(line) else None


def normalize_ip_or_cidr(raw: str) -> str | None:
    line = clean_line(raw)
    if not line:
        return None
    parts = line.split()
    candidate = parts[-1] if len(parts) > 1 else line
    candidate = candidate.split(";")[0].strip()
    if not candidate:
        return None
    try:
        if "/" in candidate:
            return str(ipaddress.ip_network(candidate, strict=False))
        ip = ipaddress.ip_address(candidate)
        return f"{ip}/32" if ip.version == 4 else f"{ip}/128"
    except ValueError:
        return None


def is_public_suffix(domain: str) -> bool:
    if tldextract is None:
        return domain.count(".") < 1
    extracted = tldextract.extract(domain)
    return not extracted.domain and bool(extracted.suffix)


def collapse_subdomains(domains: list[str]) -> list[str]:
    domain_set = set(domains)
    kept: Set[str] = set()

    def sort_key(domain: str) -> tuple[int, int, str]:
        return (domain.count("."), len(domain), domain)

    for domain in sorted(domain_set, key=sort_key):
        ancestor = domain
        redundant = False
        while "." in ancestor:
            ancestor = ancestor.split(".", 1)[1]
            if ancestor in kept and not is_public_suffix(ancestor):
                redundant = True
                break
        if not redundant:
            kept.add(domain)
    return sorted(kept)


def build_domain_set(source_texts: dict[str, str], manual_include: list[str], manual_exclude: list[str]) -> list[str]:
    domains: Set[str] = set()
    for text in source_texts.values():
        for line in text.splitlines():
            domain = normalize_domain(line)
            if domain:
                domains.add(domain)
    for line in manual_include:
        domain = normalize_domain(line)
        if domain:
            domains.add(domain)
    for line in manual_exclude:
        domain = normalize_domain(line)
        if domain and domain in domains:
            domains.remove(domain)
    return sorted(domains)


def build_ip_set(source_texts: dict[str, str], manual_include: list[str], manual_exclude: list[str]) -> list[str]:
    items: Set[str] = set()
    for text in source_texts.values():
        for line in text.splitlines():
            item = normalize_ip_or_cidr(line)
            if item:
                items.add(item)
    for line in manual_include:
        item = normalize_ip_or_cidr(line)
        if item:
            items.add(item)
    for line in manual_exclude:
        item = normalize_ip_or_cidr(line)
        if item and item in items:
            items.remove(item)

    def sort_key(item: str) -> tuple[int, int, int]:
        net = ipaddress.ip_network(item, strict=False)
        return (net.version, int(net.network_address), net.prefixlen)

    return [str(ipaddress.ip_network(item, strict=False)) for item in sorted(items, key=sort_key)]


def make_dnsmasq_nftset(domains: list[str], family: str = "inet", table: str = "fw4", set_v4: str = "fd4") -> list[str]:
    return [f"nftset=/{domain}/4#{family}#{table}#{set_v4}" for domain in domains]


def build_manifest(version: str, generated_at_utc: str, files: list[Path]) -> dict:
    profiles = {
        "critical": {"files": {}},
        "reference": {"files": {}},
    }
    for file_path in files:
        profile = "critical" if file_path.name in {"crit.domains", "dnsmasq-fd4.conf", "nft-fs4.txt"} else "reference"
        profiles[profile]["files"][file_path.name] = {
            "sha256": sha256_file(file_path),
            "size": file_path.stat().st_size,
        }
    return {
        "version": version,
        "generated_at_utc": generated_at_utc,
        "router_runtime_version": 1,
        "profiles": profiles,
        "notes": [
            "Critical runtime is built from curated allow-domains domain sources plus itdog service subnet sources and optional manual overrides.",
            "Private approved delta from hub observations is merged into the critical domain layer when APPROVED_CRITICAL_FILE is set.",
            "Reference runtime is built from public domain corpuses and is not loaded directly by nodes.",
            "Runtime remains IPv4-only in this bundle.",
        ],
    }


def build_manifest_txt(manifest: dict) -> str:
    lines: list[str] = [
        f"version\t{manifest['version']}",
        f"generated_at_utc\t{manifest['generated_at_utc']}",
        f"router_runtime_version\t{manifest['router_runtime_version']}",
    ]
    for profile_name, profile_data in manifest["profiles"].items():
        lines.append(f"profile\t{profile_name}")
        for file_name, meta in profile_data["files"].items():
            lines.append("\t".join(["file", file_name, meta["sha256"], str(meta["size"])]))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a real runtime bundle from public sources")
    parser.add_argument("--repo-root", required=True, help="Repository root with seeds and optional manual overrides")
    parser.add_argument("--output-dir", required=True, help="Directory where bundle will be written")
    parser.add_argument("--version", help="Explicit release version to use in manifest and filenames")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    ensure_dir(output_dir)

    public_sources_cfg = json.loads((repo_root / "seeds/public_sources.example.json").read_text(encoding="utf-8"))
    itdog_profile_cfg = json.loads((repo_root / "seeds/itdog_profile.example.json").read_text(encoding="utf-8"))

    manual_dir = repo_root / "manual"
    manual_include_domains = read_local_lines(manual_dir / "manual_include.domains")
    manual_exclude_domains = read_local_lines(manual_dir / "manual_exclude.domains")
    manual_critical_domains = read_local_lines(manual_dir / "manual_critical.domains")
    approved_critical_file = os.environ.get("APPROVED_CRITICAL_FILE", "")
    approved_critical_domains = read_local_lines(Path(approved_critical_file)) if approved_critical_file else []
    manual_include_ip = read_local_lines(manual_dir / "manual_include_ip.cidr")
    manual_exclude_ip = read_local_lines(manual_dir / "manual_exclude_ip.cidr")

    domain_texts = {name: fetch_text(url) for name, url in public_sources_cfg["domain_sources"].items()}
    service_ip_groups = itdog_profile_cfg.get("service_ip_seed_groups", [])
    service_ip_sources_cfg = public_sources_cfg.get("service_ip_sources", {})
    service_ip_texts: dict[str, str] = {}
    for group in service_ip_groups:
        url = service_ip_sources_cfg.get(group) or SERVICE_IP_SOURCE_URLS.get(group)
        if not url:
            raise SystemExit(f"missing service IP source URL for group: {group}")
        service_ip_texts[group] = fetch_text(url)

    critical_source_texts = {
        name: fetch_text(url)
        for name, url in {
            key: value
            for key, value in {
                **CRITICAL_DOMAIN_SOURCES,
            }.items()
            if key in {
                "allow_anime",
                "allow_block",
                "allow_geoblock",
                "allow_hodca",
                "allow_news",
                "allow_porn",
                "allow_cloudflare",
                "allow_cloudfront",
                "allow_digitalocean",
                "allow_discord",
                "allow_google_ai",
                "allow_google_meet",
                "allow_hdrezka",
                "allow_hetzner",
                "allow_meta",
                "allow_roblox",
                "allow_telegram",
                "allow_tiktok",
                "allow_twitter",
                "allow_youtube",
            }
        }.items()
    }

    critical_domains = build_domain_set(
        critical_source_texts,
        manual_critical_domains + approved_critical_domains,
        list(CRITICAL_DOMAIN_EXCLUDES),
    )
    reference_domains = build_domain_set(domain_texts, manual_include_domains + critical_domains, manual_exclude_domains)
    cidrs = build_ip_set(service_ip_texts, manual_include_ip, manual_exclude_ip)

    crit_path = output_dir / "crit.domains"
    dnsmasq_path = output_dir / "dnsmasq-fd4.conf"
    nft_path = output_dir / "nft-fs4.txt"
    ref_path = output_dir / "ref.domains"

    write_lines(crit_path, critical_domains)
    write_lines(dnsmasq_path, make_dnsmasq_nftset(critical_domains))
    write_lines(nft_path, cidrs)
    write_lines(ref_path, reference_domains)

    build_now = datetime.now(timezone.utc)
    version = args.version or build_now.strftime("%Y%m%d%H%M%S")
    generated_at_utc = build_now.isoformat()
    manifest = build_manifest(version, generated_at_utc, [crit_path, dnsmasq_path, nft_path, ref_path])
    manifest_json = output_dir / "manifest.json"
    manifest_txt = output_dir / "manifest.txt"
    manifest_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_txt.write_text(build_manifest_txt(manifest), encoding="utf-8")

    for file_path in [crit_path, dnsmasq_path, nft_path, ref_path, manifest_json, manifest_txt]:
        (output_dir / f"{file_path.name}.sha256").write_text(f"{sha256_file(file_path)}  {file_path.name}\n", encoding="utf-8")

    print("runtime bundle built:")
    print(f"  version: {version}")
    print(f"  critical domains: {len(critical_domains)}")
    print(f"  reference domains: {len(reference_domains)}")
    print(f"  cidrs: {len(cidrs)}")
    print(f"  output: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
