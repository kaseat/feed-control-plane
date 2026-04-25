#!/usr/bin/env python3
"""
Hub-side candidate processor for observed aggregates.

Policy summary:
- accept only domains that are already covered by the reference corpus;
- reject anything in deny-list;
- send noisy domains to exception queue;
- defer low-volume domains that have not yet met thresholds;
- append accepted domains to the private approved delta file.

The script is public-safe as code, but it is meant to run on hub over private
operational data only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-_]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-_]{0,61}[a-z0-9])?)+\.?$")
COMMENT_SPLIT_RE = re.compile(r"\s+#.*$")
SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    text = SLUG_RE.sub("-", text).strip("-._")
    return text or fallback


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return default.copy() if default is not None else {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def write_lines(path: Path, lines: Iterable[str]) -> None:
    ensure_dir(path.parent)
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")
    tmp_path.replace(path)


def clean_line(line: str) -> str:
    return COMMENT_SPLIT_RE.sub("", line.strip()).strip()


def normalize_domain(raw: Any) -> str | None:
    line = clean_line(str(raw or ""))
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


def normalize_pattern(raw: Any) -> str | None:
    line = clean_line(str(raw or "")).lower().strip().lstrip(".")
    if line.endswith("."):
        line = line[:-1]
    if not line or "/" in line or ":" in line or line.startswith("@"):
        return None
    return line


def domain_matches(domain: str, pattern: str) -> bool:
    return domain == pattern or domain.endswith("." + pattern)


def load_pattern_config(path: Path) -> dict[str, set[str] | list[str]]:
    raw = read_json(path, default={"exact": [], "suffix": [], "contains": []})
    exact = {normalize_pattern(item) for item in raw.get("exact", [])}
    suffix = {normalize_pattern(item) for item in raw.get("suffix", [])}
    contains = {str(item).strip().lower() for item in raw.get("contains", []) if str(item).strip()}
    return {
        "exact": {item for item in exact if item},
        "suffix": {item for item in suffix if item},
        "contains": sorted(contains),
    }


def pattern_matches(domain: str, patterns: dict[str, set[str] | list[str]]) -> str | None:
    exact = patterns["exact"]
    suffix = patterns["suffix"]
    contains = patterns["contains"]
    if domain in exact:
        return "exact"
    for pattern in suffix:
        if domain_matches(domain, pattern):
            return f"suffix:{pattern}"
    for fragment in contains:
        if fragment in domain:
            return f"contains:{fragment}"
    return None


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"processed_batches": {}}
    data = read_json(path, default={"processed_batches": {}})
    if "processed_batches" not in data or not isinstance(data["processed_batches"], dict):
        data["processed_batches"] = {}
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at_utc"] = iso_utc_now()
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def discover_batches(observed_root: Path) -> list[Path]:
    if not observed_root.exists():
        return []
    return sorted(path for path in observed_root.rglob("*.jsonl") if path.is_file() and not path.name.endswith(".meta.json"))


def load_observed_records(batch_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_line in batch_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError("observed line must be a JSON object")
        records.append(obj)
    return records


def record_clients(record: dict[str, Any], fallback_client: str) -> set[str]:
    clients: set[str] = set()
    for key in ("client", "client_id", "client_hash", "source"):
        value = record.get(key)
        if value:
            clients.add(slugify(value))
    hashes = record.get("client_hashes")
    if isinstance(hashes, list):
        for value in hashes:
            if value:
                clients.add(slugify(value))
    if not clients:
        clients.add(slugify(fallback_client))
    return clients


def load_domain_set(path: Path) -> set[str]:
    return {domain for domain in (normalize_domain(line) for line in read_lines(path)) if domain}


def set_matches(domain: str, patterns: set[str]) -> str | None:
    if domain in patterns:
        return domain
    for pattern in patterns:
        if domain_matches(domain, pattern):
            return pattern
    return None


def append_unique(existing: list[str], additions: Iterable[str]) -> list[str]:
    seen = set(existing)
    for item in additions:
        if item and item not in seen:
            existing.append(item)
            seen.add(item)
    return existing


def main() -> int:
    parser = argparse.ArgumentParser(description="Build candidate/approved outputs from observed aggregates")
    parser.add_argument("--repo-root", required=True, help="Repository root")
    parser.add_argument("--data-root", required=True, help="Hub data root")
    parser.add_argument("--runtime-root", required=True, help="Hub runtime root")
    parser.add_argument("--observed-root", required=True, help="Observed spool root")
    parser.add_argument("--candidate-root", required=True, help="Where candidate runs are written")
    parser.add_argument("--approved-file", required=True, help="Private approved delta file")
    parser.add_argument("--state-file", required=True, help="Processing state file")
    parser.add_argument("--thresholds-config", required=True, help="Thresholds JSON config")
    parser.add_argument("--noise-config", required=True, help="Noise filters JSON config")
    parser.add_argument("--deny-config", required=True, help="Deny filters JSON config")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    data_root = Path(args.data_root).resolve()
    runtime_root = Path(args.runtime_root).resolve()
    observed_root = Path(args.observed_root).resolve()
    candidate_root = Path(args.candidate_root).resolve()
    approved_file = Path(args.approved_file).resolve()
    state_file = Path(args.state_file).resolve()

    thresholds_cfg = read_json(Path(args.thresholds_config), default={})
    noise_cfg = load_pattern_config(Path(args.noise_config))
    deny_cfg = load_pattern_config(Path(args.deny_config))

    candidate_thresholds = thresholds_cfg.get("candidate_thresholds", {})
    count_min = int(candidate_thresholds.get("count_min", 3))
    windows_min = int(candidate_thresholds.get("windows_min", 2))
    clients_min = int(candidate_thresholds.get("clients_min", 1))

    current_release = runtime_root / "current"
    critical_file = current_release / "crit.domains"
    reference_file = current_release / "ref.domains"

    critical_domains = load_domain_set(critical_file)
    reference_domains = load_domain_set(reference_file)

    state = load_state(state_file)
    processed_batches: dict[str, Any] = state["processed_batches"]

    batches = discover_batches(observed_root)
    pending_batches: list[Path] = []
    for batch_path in batches:
        rel_path = str(batch_path.relative_to(observed_root))
        batch_sha = sha256_file(batch_path)
        prev = processed_batches.get(rel_path)
        if isinstance(prev, dict) and prev.get("sha256") == batch_sha and prev.get("status") in {"processed", "invalid", "empty"}:
            continue
        pending_batches.append(batch_path)

    aggregated: dict[str, dict[str, Any]] = {}
    invalid_batches: list[dict[str, Any]] = []

    for batch_path in pending_batches:
        rel_path = str(batch_path.relative_to(observed_root))
        batch_sha = sha256_file(batch_path)
        try:
            records = load_observed_records(batch_path)
        except Exception as exc:  # noqa: BLE001
            processed_batches[rel_path] = {
                "sha256": batch_sha,
                "status": "invalid",
                "error": str(exc),
                "processed_at_utc": iso_utc_now(),
            }
            invalid_batches.append(
                {
                    "batch": rel_path,
                    "sha256": batch_sha,
                    "error": str(exc),
                }
            )
            continue

        if not records:
            processed_batches[rel_path] = {
                "sha256": batch_sha,
                "status": "empty",
                "processed_at_utc": iso_utc_now(),
            }
            continue

        node = slugify(records[0].get("node") or records[0].get("router"), "unknown")
        window_fallback = slugify(records[0].get("window") or records[0].get("hour") or records[0].get("timestamp"), "unknown")

        for record in records:
            domain = normalize_domain(record.get("domain"))
            if not domain:
                continue
            try:
                count = int(record.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if count < 1:
                continue
            window = slugify(record.get("window") or record.get("hour") or record.get("timestamp"), window_fallback)
            clients = record_clients(record, node)
            bucket = aggregated.setdefault(
                domain,
                {
                    "domain": domain,
                    "count": 0,
                    "windows": set(),
                    "nodes": set(),
                    "clients": set(),
                    "batches": set(),
                },
            )
            bucket["count"] += count
            bucket["windows"].add(window)
            bucket["nodes"].add(node)
            bucket["clients"].update(clients)
            bucket["batches"].add(rel_path)

        processed_batches[rel_path] = {
            "sha256": batch_sha,
            "status": "processed",
            "record_count": len(records),
            "processed_at_utc": iso_utc_now(),
        }

    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    run_root = candidate_root / run_stamp[:8] / run_stamp
    ensure_dir(run_root)

    accepted: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []

    for domain in sorted(aggregated):
        bucket = aggregated[domain]
        total_count = int(bucket["count"])
        window_count = len(bucket["windows"])
        client_count = len(bucket["clients"])
        critical_match = set_matches(domain, critical_domains)
        deny_match = pattern_matches(domain, deny_cfg)
        noise_match = pattern_matches(domain, noise_cfg)
        reference_match = set_matches(domain, reference_domains)
        meets_thresholds = total_count >= count_min and window_count >= windows_min and client_count >= clients_min
        record = {
            "domain": domain,
            "count": total_count,
            "windows": sorted(bucket["windows"]),
            "nodes": sorted(bucket["nodes"]),
            "clients": sorted(bucket["clients"]),
            "batches": sorted(bucket["batches"]),
        }

        if deny_match:
            record["status"] = "rejected"
            record["reason"] = f"deny:{deny_match}"
            rejected.append(record)
            continue

        if noise_match:
            record["status"] = "exception"
            record["reason"] = f"noise:{noise_match}"
            exceptions.append(record)
            continue

        if critical_match:
            record["status"] = "rejected"
            record["reason"] = f"already-critical:{critical_match}"
            rejected.append(record)
            continue

        if not reference_match:
            record["status"] = "unknown"
            record["reason"] = "not-in-reference"
            unknown.append(record)
            continue

        if meets_thresholds:
            record["status"] = "accepted"
            record["reason"] = "thresholds-met"
            accepted.append(record)
            continue

        record["status"] = "deferred"
        record["reason"] = "below-threshold"
        deferred.append(record)

    write_lines(run_root / "accepted.domains", [item["domain"] for item in accepted])
    write_lines(run_root / "deferred.domains", [item["domain"] for item in deferred])
    write_lines(run_root / "unknown_observed.domains", [item["domain"] for item in unknown])
    write_lines(run_root / "rejected.domains", [item["domain"] for item in rejected])

    with (run_root / "exception.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for item in exceptions:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    summary = {
        "run_stamp": run_stamp,
        "repo_root": str(repo_root),
        "data_root": str(data_root),
        "observed_root": str(observed_root),
        "candidate_root": str(candidate_root),
        "runtime_root": str(runtime_root),
        "pending_batches": len(pending_batches),
        "invalid_batches": invalid_batches,
        "accepted": len(accepted),
        "deferred": len(deferred),
        "exceptions": len(exceptions),
        "rejected": len(rejected),
        "unknown": len(unknown),
        "thresholds": {
            "count_min": count_min,
            "windows_min": windows_min,
            "clients_min": clients_min,
        },
    }
    atomic_write_text(run_root / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")

    if approved_file:
        existing = read_lines(approved_file)
        merged = append_unique(existing, [item["domain"] for item in accepted])
        write_lines(approved_file, merged)

    save_state(state_file, state)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
