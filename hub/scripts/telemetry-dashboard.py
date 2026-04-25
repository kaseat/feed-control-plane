#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_SSH_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "StrictHostKeyChecking=accept-new",
]
DEFAULT_PATHS = {
    "health": "/var/run/node-health.json",
    "route_policy": "/var/run/node-route-policy.state.json",
    "feed_status": "/var/lib/node-feeds/status.json",
    "leases": "/tmp/dhcp.leases",
}
VPS_TUNNELS = ("awgde", "awgpl", "awgru")
DNS_QUERY_RE = re.compile(r"query(?:\[[^]]+\])?\s+(.+?)\s+from\s+([^\s]+)")
DOMAIN_RE = re.compile(r"[^a-z0-9._-]+")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def seconds_since_iso(value: str | None) -> int | None:
    dt = parse_iso_utc(value)
    if not dt:
        return None
    delta = utc_now() - dt
    return max(0, int(delta.total_seconds()))


def json_loads(text: str, default: Any) -> Any:
    payload = text.strip()
    if not payload:
        return default
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return default


def normalize_domain(value: str) -> str | None:
    text = value.strip().lower().rstrip(".")
    text = text.split(" ", 1)[0]
    text = DOMAIN_RE.sub("-", text).strip("-._")
    if not text or "/" in text or ":" in text or text.startswith("@"):
        return None
    return text


def normalize_client(value: str) -> str | None:
    text = value.strip()
    if not text or text in {"127.0.0.1", "::1"}:
        return None
    return text


def parse_dns_log(text: str) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "dnsmasq" not in line or "query" not in line:
            continue
        match = DNS_QUERY_RE.search(line)
        if not match:
            continue
        domain = normalize_domain(match.group(1))
        client = normalize_client(match.group(2))
        if not domain or not client:
            continue
        counts[(domain, client)] += 1
    items = [
        {"domain": domain, "client": client, "count": count}
        for (domain, client), count in counts.items()
    ]
    items.sort(key=lambda item: (-item["count"], item["domain"], item["client"]))
    return items


def parse_dhcp_leases(text: str) -> list[dict[str, Any]]:
    leases: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        expires, mac, ip, hostname, client_id = parts[:5]
        leases.append(
            {
                "expires": expires,
                "mac": mac,
                "ip": ip,
                "hostname": "" if hostname in {"*", "-"} else hostname,
                "client_id": "" if client_id in {"*", "-"} else client_id,
            }
        )
    return leases


def digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def iso_from_epoch(epoch: int | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dig(mapping: Any, *keys: str, default: Any = "") -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def node_class_from_labels(labels: dict[str, str]) -> str:
    role = str(labels.get("role", "")).strip().lower()
    if role == "site-router":
        return "router"
    if role == "hub":
        return "hub"
    if role in {"vps", "egress-vps"}:
        return "vps"
    return "other"


@dataclass(slots=True)
class NodeSpec:
    name: str
    host: str
    user: str = "root"
    port: int | None = None
    jump: str | None = None
    identity_file: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class TelemetryConfig:
    bind: str
    port: int
    source_mode: str
    db_path: Path
    poll_interval_seconds: int
    retention_days: int
    ssh_options: list[str]
    ssh_timeout_seconds: int
    dns_tail_lines: int
    nodes: list[NodeSpec]


def load_config(path: Path) -> TelemetryConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    listen = raw.get("listen", {})
    ssh_cfg = raw.get("ssh", {})
    defaults = {
        "user": ssh_cfg.get("user", "root"),
        "port": ssh_cfg.get("port"),
        "jump": ssh_cfg.get("jump"),
        "identity_file": ssh_cfg.get("identity_file"),
        "paths": {**DEFAULT_PATHS, **raw.get("paths", {})},
    }
    nodes: list[NodeSpec] = []
    for item in raw.get("nodes", []):
        node_paths = {**defaults["paths"], **item.get("paths", {})}
        nodes.append(
            NodeSpec(
                name=item["name"],
                host=item.get("host") or item["name"],
                user=item.get("user", defaults["user"]),
                port=item.get("port", defaults["port"]),
                jump=item.get("jump", defaults["jump"]),
                identity_file=item.get("identity_file", defaults["identity_file"]),
                labels=dict(item.get("labels", {})),
                paths=node_paths,
            )
        )
    bind = os.environ.get("TELEMETRY_BIND_ADDR", str(listen.get("bind", raw.get("bind", "127.0.0.1"))))
    port = int(os.environ.get("TELEMETRY_PORT", str(listen.get("port", raw.get("port", 19090)))))
    source_mode = os.environ.get("TELEMETRY_MODE", str(raw.get("source_mode", "pull"))).strip().lower()
    return TelemetryConfig(
        bind=bind,
        port=port,
        source_mode=source_mode,
        db_path=Path(raw.get("db_path", "./telemetry.sqlite3")).expanduser().resolve(),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 300)),
        retention_days=int(raw.get("retention_days", raw.get("snapshot_retention_days", 14))),
        ssh_options=list(ssh_cfg.get("options", DEFAULT_SSH_OPTIONS)),
        ssh_timeout_seconds=int(ssh_cfg.get("timeout_seconds", raw.get("ssh_timeout_seconds", 12))),
        dns_tail_lines=int(raw.get("dns_tail_lines", 200)),
        nodes=nodes,
    )


class TelemetryStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self.lock, self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS node_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collected_at TEXT NOT NULL,
                    node TEXT NOT NULL,
                    health_json TEXT NOT NULL,
                    route_policy_json TEXT NOT NULL,
                    feed_status_json TEXT NOT NULL,
                    leases_json TEXT NOT NULL,
                    dns_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    health_digest TEXT NOT NULL,
                    route_digest TEXT NOT NULL,
                    feed_digest TEXT NOT NULL,
                    leases_digest TEXT NOT NULL,
                    dns_digest TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS node_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collected_at TEXT NOT NULL,
                    node TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dns_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collected_at TEXT NOT NULL,
                    node TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    client TEXT NOT NULL,
                    count INTEGER NOT NULL
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_node_time ON node_snapshots(node, collected_at DESC)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_node_time ON node_events(node, collected_at DESC)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dns_node_time ON dns_observations(node, collected_at DESC)"
            )

    def latest_snapshot(self, node: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM node_snapshots WHERE node = ? ORDER BY id DESC LIMIT 1",
                (node,),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def list_nodes(self) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT node FROM node_snapshots ORDER BY node"
            ).fetchall()
        return [row["node"] for row in rows]

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM node_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_dns(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM dns_observations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_snapshot(
        self,
        *,
        collected_at: str,
        node: NodeSpec,
        health: dict[str, Any],
        route_policy: dict[str, Any],
        feed_status: dict[str, Any],
        leases: list[dict[str, Any]],
        dns_items: list[dict[str, Any]],
        dns_raw: str,
    ) -> None:
        health_json = json.dumps(health, ensure_ascii=False, sort_keys=True)
        route_json = json.dumps(route_policy, ensure_ascii=False, sort_keys=True)
        feed_json = json.dumps(feed_status, ensure_ascii=False, sort_keys=True)
        leases_json = json.dumps(leases, ensure_ascii=False, sort_keys=True)
        dns_json = json.dumps(dns_items, ensure_ascii=False, sort_keys=True)
        summary = summarize_snapshot(node, health, route_policy, feed_status, leases, dns_items, collected_at)
        summary_json = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        health_digest = digest(health_json)
        route_digest = digest(route_json)
        feed_digest = digest(feed_json)
        leases_digest = digest(leases_json)
        dns_digest = digest(dns_raw)

        previous = self.latest_snapshot(node.name)
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO node_snapshots (
                    collected_at, node, health_json, route_policy_json, feed_status_json,
                    leases_json, dns_json, summary_json, health_digest, route_digest,
                    feed_digest, leases_digest, dns_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    collected_at,
                    node.name,
                    health_json,
                    route_json,
                    feed_json,
                    leases_json,
                    dns_json,
                    summary_json,
                    health_digest,
                    route_digest,
                    feed_digest,
                    leases_digest,
                    dns_digest,
                ),
            )
            if previous:
                prev_summary = json.loads(previous["summary_json"])
                self._maybe_record_change(node.name, collected_at, prev_summary, summary)
            if not previous or previous["dns_digest"] != dns_digest:
                for item in dns_items:
                    self.conn.execute(
                        """
                        INSERT INTO dns_observations (collected_at, node, domain, client, count)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            collected_at,
                            node.name,
                            item["domain"],
                            item["client"],
                            int(item["count"]),
                        ),
                    )

    def _maybe_record_change(
        self,
        node_name: str,
        collected_at: str,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> None:
        keys = [
            ("status", "status"),
            ("wan_status", "wan_status"),
            ("dns_status", "dns_status"),
            ("feed_status", "feed_status"),
            ("foreign_active", "foreign_active"),
        ]
        for key, label in keys:
            before = previous.get(key, "unknown")
            after = current.get(key, "unknown")
            if before != after:
                detail = {"before": before, "after": after, "key": key}
                self.conn.execute(
                    """
                    INSERT INTO node_events (collected_at, node, kind, summary, detail_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        collected_at,
                        node_name,
                        "state-change",
                        f"{label}: {before} -> {after}",
                        json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    ),
                )

    def purge_old(self, retention_days: int) -> None:
        cutoff = (utc_now() - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM node_snapshots WHERE collected_at < ?", (cutoff,))
            self.conn.execute("DELETE FROM node_events WHERE collected_at < ?", (cutoff,))
            self.conn.execute("DELETE FROM dns_observations WHERE collected_at < ?", (cutoff,))


def summarize_snapshot(
    node: NodeSpec,
    health: dict[str, Any],
    route_policy: dict[str, Any],
    feed_status: dict[str, Any],
    leases: list[dict[str, Any]],
    dns_items: list[dict[str, Any]],
    collected_at: str,
) -> dict[str, Any]:
    node_class = node_class_from_labels(node.labels)
    route_tables = dig(health, "route_tables", default={})
    foreign_route = dig(route_tables, "foreign_active", default={})
    if not isinstance(foreign_route, dict):
        foreign_route = {}
    foreign_active = foreign_route.get("active_egress", "unknown")
    foreign_default_dev = foreign_route.get("default_dev", "")
    tunnels = {}
    health_tunnels = dig(health, "tunnels", default={})
    if not isinstance(health_tunnels, dict):
        health_tunnels = {}
    ordered_tunnels = sorted(
        health_tunnels.keys(),
        key=lambda name: (VPS_TUNNELS.index(name) if name in VPS_TUNNELS else len(VPS_TUNNELS), name),
    )
    for tunnel_name in ordered_tunnels:
        tunnel = health_tunnels.get(tunnel_name)
        if not isinstance(tunnel, dict):
            continue
        tunnels[tunnel_name] = {
            "status": tunnel.get("status", "unknown"),
            "handshake_age_seconds": tunnel.get("handshake_age_seconds", 0),
            "rx_bytes": tunnel.get("rx_bytes", 0),
            "tx_bytes": tunnel.get("tx_bytes", 0),
            "probe_https": tunnel.get("probe_https", "unknown"),
            "egress": tunnel.get("egress", ""),
            "active": bool(
                node_class == "router"
                and (
                    (tunnel.get("egress") and tunnel.get("egress") == foreign_active)
                    or tunnel_name == foreign_default_dev
                    or tunnel.get("interface") == foreign_default_dev
                )
            ),
        }
    client_metric = dig(health, "metrics", "clients_count", default=None)
    clients_count: int | None = None
    clients_source = "unknown"
    if isinstance(client_metric, int) and client_metric >= 0:
        clients_count = client_metric
        clients_source = "health"
    if leases:
        clients_count = len(leases)
        clients_source = "leases"
    dnsmasq_status = dig(health, "dns", "dnsmasq", default="unknown")
    query_logging = bool(dig(health, "dns", "query_logging", default=False))
    current_release = dig(health, "feed", "current_release", default="")
    current_profile = dig(health, "feed", "profile", default="")
    current_release_version = ""
    if current_release:
        current_release_version = Path(current_release).parent.name
    current_version = current_release_version or dig(health, "feed", "current_version", default="")
    current_release_mtime = dig(health, "feed", "current_release_mtime", default="")
    summary = {
        "node": node.name,
        "node_class": node_class,
        "control_profile": node_class,
        "labels": node.labels,
        "collected_at": collected_at,
        "status": health.get("status", "unknown"),
        "wan_status": dig(health, "wan", "status", default="unknown"),
        "wan_external_ip": dig(health, "wan", "external_ip", default=""),
        "uplink_status": dig(health, "wan", "status", default="unknown"),
        "uplink_external_ip": dig(health, "wan", "external_ip", default=""),
        "dns_status": dig(health, "dns", "status", default="unknown"),
        "dnsmasq_status": dnsmasq_status,
        "query_logging_enabled": query_logging,
        "dns_observed_pairs_count": len(dns_items),
        "feed_status": dig(health, "feed", "status", default="unknown"),
        "current_version": current_version,
        "current_profile": current_profile,
        "current_release": current_release,
        "current_release_mtime": current_release_mtime,
        "current_release_age_seconds": seconds_since_iso(current_release_mtime) if current_release_mtime else None,
        "observed_pending": dig(health, "feed", "observed_pending", default=0),
        "observed_sent": dig(health, "feed", "observed_sent", default=0),
        "foreign_active": foreign_active,
        "route_policy_mode": dig(route_policy, "classes", "foreign", "mode", default="unknown"),
        "route_policy_last_decision": dig(route_policy, "classes", "foreign", "last_decision", default="unknown"),
        "route_policy_last_reason": dig(route_policy, "classes", "foreign", "last_reason", default=""),
        "leases_count": len(leases),
        "clients_count": clients_count,
        "clients_source": clients_source,
        "dns_pairs_count": len(dns_items),
        "tunnels": tunnels,
        "route_tables": {
            "foreign_active": dig(route_tables, "foreign_active", default={}),
            "corp_active": dig(route_tables, "corp_active", default={}),
            "admin_active": dig(route_tables, "admin_active", default={}),
        },
    }
    return summary


def summarize_dashboard(nodes: list[dict[str, Any]], dns_events: list[dict[str, Any]]) -> dict[str, Any]:
    def count_class(class_name: str) -> tuple[int, int]:
        items = [node for node in nodes if node.get("node_class") == class_name]
        healthy = sum(1 for node in items if node.get("status") == "healthy")
        return healthy, len(items)

    def count_feed_for_class(class_name: str) -> tuple[int, int]:
        items = [node for node in nodes if node.get("node_class") == class_name]
        healthy = sum(1 for node in items if node.get("feed_status") == "healthy")
        return healthy, len(items)

    router_nodes = [node for node in nodes if node.get("node_class") == "router"]
    foreign_counts = defaultdict(int)
    for node in router_nodes:
        foreign_counts[str(node.get("foreign_active") or "unknown")] += 1

    latest_collected_at = None
    latest_age_seconds = None
    for node in nodes:
        age = seconds_since_iso(str(node.get("collected_at") or ""))
        if age is None:
            continue
        if latest_age_seconds is None or age < latest_age_seconds:
            latest_age_seconds = age
            latest_collected_at = node.get("collected_at")

    stale_threshold = 600
    stale_nodes = sum(1 for node in nodes if (seconds_since_iso(str(node.get("collected_at") or "")) or 10**9) > stale_threshold)
    router_feed_healthy, router_feed_total = count_feed_for_class("router")
    router_healthy, router_total = count_class("router")
    hub_healthy, hub_total = count_class("hub")
    vps_healthy, vps_total = count_class("vps")

    feed_versions = sorted({str(node.get("current_version") or "") for node in router_nodes if node.get("current_version")})
    feed_version = feed_versions[0] if len(feed_versions) == 1 else ("mixed" if feed_versions else "unknown")

    return {
        "routers": {"healthy": router_healthy, "total": router_total},
        "hub": {"healthy": hub_healthy, "total": hub_total},
        "vps": {"healthy": vps_healthy, "total": vps_total},
        "router_feed": {"healthy": router_feed_healthy, "total": router_feed_total, "version": feed_version},
        "foreign": {
            "de": foreign_counts.get("de", 0),
            "pl": foreign_counts.get("pl", 0),
            "ru": foreign_counts.get("ru", 0),
            "unknown": foreign_counts.get("unknown", 0),
        },
        "freshness": {
            "latest_collected_at": latest_collected_at or "",
            "latest_age_seconds": latest_age_seconds if latest_age_seconds is not None else 0,
            "stale_nodes": stale_nodes,
            "dns_rows": len(dns_events),
        },
    }


class TelemetryCollector:
    def __init__(self, config: TelemetryConfig, store: TelemetryStore) -> None:
        self.config = config
        self.store = store
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._collector_loop, name="telemetry-collector", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def should_poll_node(self, node: NodeSpec) -> bool:
        node_class = node_class_from_labels(node.labels)
        if self.config.source_mode == "hybrid" and node_class == "router":
            return False
        return True

    def _ssh_base(self, node: NodeSpec) -> list[str]:
        command = ["ssh", *self.config.ssh_options]
        if node.port:
            command.extend(["-p", str(node.port)])
        if node.identity_file:
            command.extend(["-i", node.identity_file])
        if node.jump:
            command.extend(["-J", node.jump])
        command.append(f"{node.user}@{node.host}")
        return command

    def _run(self, node: NodeSpec, remote_args: list[str]) -> subprocess.CompletedProcess[str]:
        # OpenSSH joins remote argv through a shell; quote explicitly so
        # commands like `sh -lc "printf yes"` keep their intended arguments.
        command = self._ssh_base(node) + [shlex.join(remote_args)]
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.config.ssh_timeout_seconds,
        )

    def read_text(self, node: NodeSpec, remote_path: str) -> str:
        result = self._run(node, ["cat", remote_path])
        if result.returncode != 0:
            return ""
        return result.stdout

    def read_shell(self, node: NodeSpec, command: str) -> str:
        result = self._run(node, ["sh", "-lc", command])
        if result.returncode != 0:
            return ""
        return result.stdout

    def collect_linux_fallback(self, node: NodeSpec) -> dict[str, Any]:
        node_class = node_class_from_labels(node.labels)
        shell_ok = self.read_shell(node, "printf yes").strip() == "yes"
        if not shell_ok:
            return {}
        external_ip = self.read_shell(
            node,
            "curl -fsS --connect-timeout 3 --max-time 6 https://1.1.1.1/cdn-cgi/trace 2>/dev/null | awk -F= '/^ip=/{print $2; exit}'",
        ).strip()
        default_route = self.read_shell(
            node,
            "ip route show default 2>/dev/null | awk '$1 == \"default\" { for (i = 1; i <= NF; i++) if ($i == \"dev\") { print $(i + 1); exit } }'",
        ).strip()
        tunnels: dict[str, dict[str, Any]] = {}
        for tunnel_name in VPS_TUNNELS:
            awg_output = self.read_shell(node, f"awg show {shlex.quote(tunnel_name)} 2>/dev/null || wg show {shlex.quote(tunnel_name)} 2>/dev/null || true")
            if not awg_output.strip():
                continue
            handshake = ""
            transfer = ""
            for raw_line in awg_output.splitlines():
                line = raw_line.strip()
                if line.startswith("latest handshake:"):
                    handshake = line.split(": ", 1)[1].strip()
                elif line.startswith("transfer:"):
                    transfer = line.split(": ", 1)[1].strip()
            handshake_seconds = 0
            if handshake:
                handshake_seconds = int(self._parse_human_age(handshake))
            rx_bytes, tx_bytes = self._parse_transfer_bytes(transfer)
            probe_status = "down"
            if self.read_shell(node, f"curl -fsS --connect-timeout 3 --max-time 6 --interface {shlex.quote(tunnel_name)} -k https://1.1.1.1/cdn-cgi/trace >/dev/null && printf ok || printf down").strip() == "ok":
                probe_status = "ok"
            status = "down"
            if handshake_seconds and handshake_seconds < 300:
                status = "healthy"
            elif handshake_seconds and handshake_seconds < 900:
                status = "degraded"
            tunnels[tunnel_name] = {
                "status": status,
                "handshake_age_seconds": handshake_seconds,
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "probe_https": probe_status,
                "egress": tunnel_name.removeprefix("awg"),
                "interface": tunnel_name,
            }
        return {
            "status": "healthy",
            "node_class": node_class,
            "wan": {
                "status": "healthy" if external_ip or default_route else "degraded",
                "external_ip": external_ip,
                "default_route": default_route,
                "probe_https": "ok" if external_ip else "down",
            },
            "dns": {"status": "unknown"},
            "feed": {"status": "unknown"},
            "route_tables": {
                "foreign_active": {"active_egress": "unknown", "status": "unknown"},
                "corp_active": {"active_egress": "unknown", "status": "unknown"},
                "admin_active": {"active_egress": "unknown", "status": "unknown"},
            },
            "tunnels": tunnels,
        }

    @staticmethod
    def _parse_human_age(value: str) -> int:
        value = value.strip()
        if not value:
            return 0
        hours = minutes = seconds = 0
        match = re.search(r"([0-9]+) hour", value)
        if match:
            hours = int(match.group(1))
        match = re.search(r"([0-9]+) minute", value)
        if match:
            minutes = int(match.group(1))
        match = re.search(r"([0-9]+) second", value)
        if match:
            seconds = int(match.group(1))
        return hours * 3600 + minutes * 60 + seconds

    @staticmethod
    def _parse_transfer_bytes(value: str) -> tuple[int, int]:
        if not value:
            return 0, 0
        left, _, right = value.partition(",")
        def _one(side: str) -> int:
            parts = side.strip().split()
            if len(parts) < 2:
                return 0
            number = float(parts[0].replace(",", ""))
            unit = parts[1]
            scale = {
                "B": 1,
                "KiB": 1024,
                "MiB": 1024**2,
                "GiB": 1024**3,
                "TiB": 1024**4,
            }.get(unit, 1)
            return int(number * scale)
        rx_bytes = _one(left.replace("received", "").strip())
        tx_bytes = _one(right.replace("sent", "").strip())
        return rx_bytes, tx_bytes

    def collect_node(self, node: NodeSpec) -> None:
        collected_at = iso_now()
        health = json_loads(self.read_text(node, node.paths["health"]), {})
        route_policy = json_loads(self.read_text(node, node.paths["route_policy"]), {})
        feed_status = json_loads(self.read_text(node, node.paths["feed_status"]), {})
        leases = parse_dhcp_leases(self.read_text(node, node.paths["leases"]))
        dns_text = self.read_shell(node, f"logread -e dnsmasq | tail -n {self.config.dns_tail_lines}")
        dns_items = parse_dns_log(dns_text)
        if not health and not route_policy and not feed_status and not leases and not dns_items:
            fallback = self.collect_linux_fallback(node)
            if fallback:
                health = fallback
                route_policy = {}
                feed_status = {}
                leases = []
                dns_items = []
            else:
                with self.store.lock, self.store.conn:
                    self.store.conn.execute(
                        """
                        INSERT INTO node_events (collected_at, node, kind, summary, detail_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            collected_at,
                            node.name,
                            "collector-skip",
                            "empty snapshot, keeping previous state",
                            json.dumps({"node": node.name}, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                return
        self.store.save_snapshot(
            collected_at=collected_at,
            node=node,
            health=health,
            route_policy=route_policy,
            feed_status=feed_status,
            leases=leases,
            dns_items=dns_items,
            dns_raw=dns_text,
        )

    def _collector_loop(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            for node in self.config.nodes:
                if not self.should_poll_node(node):
                    continue
                try:
                    self.collect_node(node)
                except Exception as exc:  # pragma: no cover - defensive runtime logging
                    with self.store.lock, self.store.conn:
                        self.store.conn.execute(
                            """
                            INSERT INTO node_events (collected_at, node, kind, summary, detail_json)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                iso_now(),
                                node.name,
                                "collector-error",
                                str(exc),
                                json.dumps({"error": str(exc)}, ensure_ascii=False),
                            ),
                        )
            self.store.purge_old(self.config.retention_days)
            elapsed = time.monotonic() - started
            remaining = max(1, self.config.poll_interval_seconds - int(elapsed))
            self.stop_event.wait(timeout=remaining)


def read_request_body(handler: BaseHTTPRequestHandler) -> str:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise ValueError("invalid content length")
    if length <= 0:
        raise ValueError("empty request body")
    body = handler.rfile.read(length)
    return body.decode("utf-8")


class TelemetryHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type[BaseHTTPRequestHandler], controller: "TelemetryController", store: TelemetryStore) -> None:
        self.controller = controller
        self.store = store
        super().__init__(server_address, RequestHandlerClass)


class TelemetryHandler(BaseHTTPRequestHandler):
    server_version = "node-control-telemetry/1.0"

    @property
    def controller(self) -> "TelemetryController":
        return getattr(self.server, "controller")  # type: ignore[no-any-return]

    @property
    def store(self) -> TelemetryStore:
        return getattr(self.server, "store")  # type: ignore[no-any-return]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        message = format % args
        print(f"{self.log_date_time_string()} {self.client_address[0]} {message}", flush=True)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_text(self, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        encoded = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in {"/", "/index.html"}:
            self._send_text(self.render_dashboard(), "text/html; charset=utf-8")
            return
        if path == "/api/summary":
            self._send_json(self.build_summary())
            return
        if path == "/api/nodes":
            self._send_json(self.build_nodes())
            return
        if path == "/api/events":
            params = parse_qs(parsed.query)
            limit = int(params.get("limit", ["50"])[0])
            self._send_json({"events": self.store.recent_events(limit=limit)})
            return
        if path == "/metrics":
            self._send_text(self.render_metrics(), "text/plain; version=0.0.4; charset=utf-8")
            return
        if path.startswith("/api/node/"):
            node_name = path.split("/", 3)[3]
            snapshot = self.store.latest_snapshot(node_name)
            if not snapshot:
                self.send_error(HTTPStatus.NOT_FOUND, "unknown node")
                return
            self._send_json(snapshot)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/health":
            self.handle_health_post()
            return
        if path == "/api/observed":
            self.handle_observed_post()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def handle_health_post(self) -> None:
        try:
            text = read_request_body(self)
        except ValueError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, f"invalid json: {exc.msg}")
            return
        if not isinstance(payload, dict):
            self.send_error(HTTPStatus.BAD_REQUEST, "health payload must be an object")
            return

        node_name = str(payload.get("node") or payload.get("router") or "unknown")
        node_class = str(payload.get("node_class") or payload.get("class") or "").strip().lower()
        labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
        if not isinstance(labels, dict):
            labels = {}
        if "role" not in labels and node_class:
            role_by_class = {
                "router": "site-router",
                "hub": "hub",
                "vps": "egress-vps",
            }
            labels["role"] = role_by_class.get(node_class, node_class)
        node = NodeSpec(name=node_name, host=node_name, labels={str(k): str(v) for k, v in labels.items()})
        health = payload.get("health") if isinstance(payload.get("health"), dict) else payload
        route_policy = payload.get("route_policy") if isinstance(payload.get("route_policy"), dict) else {}
        feed_status = payload.get("feed_status") if isinstance(payload.get("feed_status"), dict) else {}
        collected_at = str(payload.get("collected_at") or payload.get("generated_at") or iso_now())
        leases = payload.get("leases") if isinstance(payload.get("leases"), list) else []
        dns_items = []
        previous = self.store.latest_snapshot(node_name)
        if previous:
            if not route_policy:
                route_policy = json.loads(previous["route_policy_json"])
            if not feed_status:
                feed_status = json.loads(previous["feed_status_json"])
            if not leases:
                leases = json.loads(previous["leases_json"])
            if not dns_items:
                dns_items = json.loads(previous["dns_json"])
        self.store.save_snapshot(
            collected_at=collected_at,
            node=node,
            health=health,
            route_policy=route_policy,
            feed_status=feed_status,
            leases=leases,
            dns_items=dns_items,
            dns_raw="",
        )
        self._send_json({"status": "accepted", "node": node_name, "kind": "health"}, HTTPStatus.ACCEPTED)

    def handle_observed_post(self) -> None:
        try:
            text = read_request_body(self)
        except ValueError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        records: list[dict[str, Any]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, f"invalid json line: {exc.msg}")
                return
            if not isinstance(obj, dict):
                self.send_error(HTTPStatus.BAD_REQUEST, "observed line must be a JSON object")
                return
            records.append(obj)

        if not records:
            self.send_error(HTTPStatus.BAD_REQUEST, "no observed records found")
            return

        collected_at = iso_now()
        with self.store.lock, self.store.conn:
            for record in records:
                node_name = str(record.get("node") or "unknown")
                domain = str(record.get("domain") or "unknown")
                count = int(record.get("count") or 0)
                client_values: list[str] = []
                if isinstance(record.get("client_hashes"), list):
                    client_values = [str(value) for value in record["client_hashes"] if str(value).strip()]
                elif record.get("client"):
                    client_values = [str(record["client"])]
                if not client_values:
                    client_values = ["unknown"]
                for client in client_values:
                    self.store.conn.execute(
                        """
                        INSERT INTO dns_observations (collected_at, node, domain, client, count)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (collected_at, node_name, domain, client, count),
                    )

        self._send_json({"status": "accepted", "records": len(records), "kind": "observed"}, HTTPStatus.ACCEPTED)

    def build_nodes(self) -> dict[str, Any]:
        nodes = []
        for node in self.controller.config.nodes:
            snapshot = self.store.latest_snapshot(node.name)
            node_class = node_class_from_labels(node.labels)
            health: dict[str, Any] = {}
            if snapshot:
                summary = json.loads(snapshot["summary_json"])
                health = json.loads(snapshot["health_json"]) if snapshot.get("health_json") else {}
            else:
                summary = {
                    "node": node.name,
                    "node_class": node_class,
                    "control_profile": node_class,
                    "status": "unknown",
                    "wan_status": "unknown",
                    "dns_status": "unknown",
                    "dnsmasq_status": "unknown",
                    "query_logging_enabled": False,
                    "feed_status": "unknown",
                    "current_version": "",
                    "current_profile": "",
                    "foreign_active": "unknown",
                    "tunnels": {},
                }
            feed = health.get("feed") if isinstance(health, dict) else {}
            if not isinstance(feed, dict):
                feed = {}
            dns = health.get("dns") if isinstance(health, dict) else {}
            if not isinstance(dns, dict):
                dns = {}
            current_release = str(feed.get("current_release") or summary.get("current_release") or "")
            current_profile = str(feed.get("profile") or feed.get("current_profile") or summary.get("current_profile") or "")
            current_version = ""
            if current_release:
                current_version = Path(current_release).parent.name
            else:
                current_version = str(feed.get("current_version") or summary.get("current_version") or "")
            current_release_mtime = str(feed.get("current_release_mtime") or summary.get("current_release_mtime") or "")
            if current_version:
                summary["current_version"] = current_version
            if current_profile:
                summary["current_profile"] = current_profile
            if current_release:
                summary["current_release"] = current_release
            if current_release_mtime:
                summary["current_release_mtime"] = current_release_mtime
                summary["current_release_age_seconds"] = seconds_since_iso(current_release_mtime)
            dnsmasq_status = str(dns.get("dnsmasq") or summary.get("dnsmasq_status") or "unknown")
            query_logging_enabled = bool(dns.get("query_logging")) if "query_logging" in dns else bool(summary.get("query_logging_enabled", False))
            summary["dnsmasq_status"] = dnsmasq_status
            summary["query_logging_enabled"] = query_logging_enabled
            summary["node"] = summary.get("node", node.name)
            summary["labels"] = {**(summary.get("labels") if isinstance(summary.get("labels"), dict) else {}), **node.labels}
            summary["node_class"] = summary.get("node_class") or node_class
            summary["control_profile"] = summary.get("control_profile") or node_class
            summary["wan_status"] = summary.get("wan_status") or summary.get("uplink_status") or "unknown"
            summary["wan_external_ip"] = summary.get("wan_external_ip") or summary.get("uplink_external_ip") or ""
            summary["uplink_status"] = summary.get("uplink_status") or summary["wan_status"]
            summary["uplink_external_ip"] = summary.get("uplink_external_ip") or summary["wan_external_ip"]
            summary["tunnels"] = summary.get("tunnels") or {}
            nodes.append(summary)
        return {
            "generated_at": iso_now(),
            "poll_interval_seconds": self.controller.config.poll_interval_seconds,
            "nodes": nodes,
        }

    def build_summary(self) -> dict[str, Any]:
        nodes = self.build_nodes()["nodes"]
        events = self.store.recent_events(limit=40)
        dns_events = self.store.recent_dns(limit=40)
        dashboard = summarize_dashboard(nodes, dns_events)
        totals = {
            "nodes_total": len(nodes),
            "nodes_healthy": sum(1 for node in nodes if node.get("status") == "healthy"),
            "nodes_degraded": sum(1 for node in nodes if node.get("status") == "degraded"),
            "nodes_down": sum(1 for node in nodes if node.get("status") == "down"),
            "routers_total": sum(1 for node in nodes if node.get("node_class") == "router"),
            "hub_total": sum(1 for node in nodes if node.get("node_class") == "hub"),
            "vps_total": sum(1 for node in nodes if node.get("node_class") == "vps"),
        }
        return {
            "generated_at": iso_now(),
            "poll_interval_seconds": self.controller.config.poll_interval_seconds,
            "totals": totals,
            "dashboard": dashboard,
            "nodes": nodes,
            "events": events,
            "dns_observations": dns_events,
        }

    def render_metrics(self) -> str:
        lines: list[str] = [
            "# HELP node_telemetry_node_status Node health status by name.",
            "# TYPE node_telemetry_node_status gauge",
        ]
        status_to_value = {"healthy": 1, "degraded": 0.5, "down": 0, "unknown": -1}
        for node in self.build_nodes()["nodes"]:
            name = node.get("node", "unknown")
            status = node.get("status", "unknown")
            node_class = node.get("node_class", "other")
            lines.append(f'node_telemetry_node_status{{node="{name}",status="{status}"}} {status_to_value.get(status, -1)}')
            lines.append(f'node_telemetry_node_class{{node="{name}",class="{node_class}"}} 1')
            lines.append(f'node_telemetry_node_wan_up{{node="{name}"}} {1 if node.get("wan_status") == "healthy" else 0}')
            lines.append(f'node_telemetry_node_dns_up{{node="{name}"}} {1 if node.get("dns_status") == "healthy" else 0}')
            lines.append(f'node_telemetry_node_feed_up{{node="{name}"}} {1 if node.get("feed_status") == "healthy" else 0}')
            lines.append(f'node_telemetry_node_foreign_egress{{node="{name}",egress="{node.get("foreign_active", "unknown")}"}} 1')
            for tunnel_name, tunnel in sorted(node.get("tunnels", {}).items()):
                lines.append(f'node_telemetry_tunnel_up{{node="{name}",tunnel="{tunnel_name}"}} {1 if tunnel.get("status") in {"healthy", "degraded"} else 0}')
                lines.append(
                    f'node_telemetry_tunnel_handshake_age_seconds{{node="{name}",tunnel="{tunnel_name}"}} {int(tunnel.get("handshake_age_seconds") or 0)}'
                )
        return "\n".join(lines) + "\n"

    def render_dashboard(self) -> str:
        summary = self.build_summary()
        nodes_json = json.dumps(summary["nodes"], ensure_ascii=False)
        events_json = json.dumps(summary["events"], ensure_ascii=False)
        dns_json = json.dumps(summary["dns_observations"], ensure_ascii=False)
        dashboard_json = json.dumps(summary["dashboard"], ensure_ascii=False)
        generated_at = html.escape(summary["generated_at"])
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Network Telemetry</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f5f7fb;
      --bg-top: #eaf0f7;
      --panel: #ffffff;
      --panel-2: #fbfdff;
      --ink: #0f172a;
      --muted: #64748b;
      --border: #d8e0ea;
      --border-soft: #edf2f7;
      --accent: #0f766e;
      --accent-soft: rgba(15, 118, 110, 0.12);
      --good: #15803d;
      --warn: #b45309;
      --bad: #b91c1c;
      --surface-strong: rgba(255, 255, 255, 0.72);
      --surface-hover: #f8fbff;
      --surface-good: linear-gradient(180deg, #f7fff9 0%, #eefcf4 100%);
      --surface-warn: linear-gradient(180deg, #fffdf4 0%, #fffbeb 100%);
      --surface-bad: linear-gradient(180deg, #fff7f7 0%, #fef2f2 100%);
      --surface-good-border: #bbf7d0;
      --surface-warn-border: #fde68a;
      --surface-bad-border: #fecaca;
      --chip-bg: #eff6ff;
      --chip-text: #1d4ed8;
      --chip-good-bg: #ecfdf5;
      --chip-good-text: var(--good);
      --chip-warn-bg: #fffbeb;
      --chip-warn-text: var(--warn);
      --chip-bad-bg: #fef2f2;
      --chip-bad-text: var(--bad);
      --shadow-card: 0 16px 40px rgba(15, 23, 42, 0.06);
      --shadow-soft: 0 8px 20px rgba(15, 23, 42, 0.04);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0b1020;
        --bg-top: #111827;
        --panel: #111827;
        --panel-2: #0f172a;
        --ink: #e5edf8;
        --muted: #8fa1bb;
        --border: #243044;
        --border-soft: #1f2937;
        --accent: #34d399;
        --accent-soft: rgba(52, 211, 153, 0.16);
        --good: #34d399;
        --warn: #fbbf24;
        --bad: #f87171;
        --surface-strong: rgba(15, 23, 42, 0.72);
        --surface-hover: #132036;
        --surface-good: linear-gradient(180deg, #0f1f1a 0%, #10261d 100%);
        --surface-warn: linear-gradient(180deg, #201a0b 0%, #2a220a 100%);
        --surface-bad: linear-gradient(180deg, #221315 0%, #2a1518 100%);
        --surface-good-border: #1f6f55;
        --surface-warn-border: #5a4210;
        --surface-bad-border: #7f1d1d;
        --chip-bg: #17213a;
        --chip-text: #93c5fd;
        --chip-good-bg: #10261d;
        --chip-good-text: #6ee7b7;
        --chip-warn-bg: #2a220a;
        --chip-warn-text: #fcd34d;
        --chip-bad-bg: #2a1518;
        --chip-bad-text: #fca5a5;
        --shadow-card: 0 16px 40px rgba(0, 0, 0, 0.28);
        --shadow-soft: 0 8px 20px rgba(0, 0, 0, 0.22);
      }}
    }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: linear-gradient(180deg, var(--bg-top) 0%, var(--bg) 180px, var(--bg) 100%);
      color: var(--ink);
    }}
    .shell {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
    .hero {{
      display: grid;
      gap: 12px;
      grid-template-columns: 1.7fr 1fr;
      align-items: stretch;
      margin-bottom: 20px;
    }}
    .title {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 22px 24px;
      box-shadow: var(--shadow-card);
    }}
    .title h1 {{ margin: 0 0 8px; font-size: 30px; line-height: 1.1; }}
    .title p {{ margin: 0; color: var(--muted); }}
    .stats {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px;
      min-height: 96px;
    }}
    .stat .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    .stat .value {{ font-size: 24px; font-weight: 700; margin-top: 8px; line-height: 1.1; }}
    .stat .subvalue {{ margin-top: 6px; color: var(--muted); font-size: 12px; line-height: 1.35; }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 18px;
      margin-top: 18px;
      box-shadow: var(--shadow-card);
    }}
    .section h2 {{ margin: 0 0 12px; font-size: 18px; }}
    .class-grid {{
      display: grid;
      gap: 16px;
    }}
    .class-block {{
      border: 1px solid var(--border-soft);
      border-radius: 16px;
      padding: 14px 14px 8px;
      background: var(--panel-2);
      margin-bottom: 14px;
    }}
    .class-head {{
      display: flex;
      align-items: center;
      gap: 12px;
      justify-content: space-between;
      margin-bottom: 8px;
    }}
    .class-head h3 {{
      margin: 0;
      font-size: 15px;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--border-soft); vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 600;
      background: var(--chip-bg);
      color: var(--chip-text);
    }}
    .chip.good {{ background: var(--chip-good-bg); color: var(--chip-good-text); }}
    .chip.warn {{ background: var(--chip-warn-bg); color: var(--chip-warn-text); }}
    .chip.bad {{ background: var(--chip-bad-bg); color: var(--chip-bad-text); }}
    .resolver-cell {{ min-width: 280px; }}
    .resolver-card {{
      min-width: 156px;
    }}
    .resolver-card .tunnel-top {{ margin-bottom: 6px; }}
    .resolver-card .tunnel-grid {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin-bottom: 0;
    }}
    .resolver-card .tunnel-metric strong {{
      font-size: 12px;
    }}
    .resolver-card .resolver-foot {{
      margin-top: 6px;
    }}
    .links-cell {{ min-width: 320px; }}
    .tunnel-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      max-width: 560px;
    }}
    .tunnel-card {{
      min-width: 156px;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: linear-gradient(180deg, var(--panel) 0%, var(--panel-2) 100%);
      padding: 8px 10px;
      box-shadow: var(--shadow-soft);
    }}
    .tunnel-card.good {{ border-color: var(--surface-good-border); background: var(--surface-good); }}
    .tunnel-card.warn {{ border-color: var(--surface-warn-border); background: var(--surface-warn); }}
    .tunnel-card.bad {{ border-color: var(--surface-bad-border); background: var(--surface-bad); }}
    .tunnel-card.active {{
      border-color: var(--accent);
      box-shadow: 0 0 0 2px var(--accent-soft), 0 10px 24px var(--accent-soft);
    }}
    .tunnel-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 6px;
    }}
    .tunnel-name {{ font-weight: 800; letter-spacing: .04em; }}
    .tunnel-status {{ font-size: 11px; font-weight: 700; color: var(--muted); }}
    .tunnel-status.active {{ color: var(--accent); }}
    .tunnel-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 4px;
    }}
    .tunnel-metric {{
      border-radius: 9px;
      background: var(--surface-strong);
      padding: 4px 5px;
    }}
    .tunnel-metric span {{
      display: block;
      color: var(--muted);
      font-size: 9px;
      text-transform: uppercase;
      letter-spacing: .06em;
    }}
    .tunnel-metric strong {{
      display: block;
      margin-top: 2px;
      font-size: 12px;
      white-space: nowrap;
    }}
    .tunnel-foot {{ margin-top: 6px; color: var(--muted); font-size: 11px; }}
    .grid-2 {{
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 18px;
    }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }}
    .small {{ color: var(--muted); font-size: 12px; }}
    .scroll {{ overflow: auto; max-height: 520px; }}
    .raw-json {{
      white-space: pre-wrap;
      margin: 0;
      padding: 14px;
      border-radius: 14px;
      background: var(--panel-2);
      border: 1px solid var(--border);
      color: var(--ink);
    }}
    @media (max-width: 1100px) {{
      .hero, .grid-2, .stats {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="hero">
      <div class="title">
        <h1>Network Telemetry</h1>
        <p>Read-only dashboard for site routers, hub services, egress VPS links and feed state.</p>
        <p class="small">Last render: <span class="mono">{generated_at}</span></p>
      </div>
      <div class="stats">
        <div class="stat">
          <div class="label">Routers</div>
          <div class="value" id="stat-routers">{summary["dashboard"]["routers"]["healthy"]}/{summary["dashboard"]["routers"]["total"]}</div>
          <div class="subvalue">site routers healthy</div>
        </div>
        <div class="stat">
          <div class="label">Hub</div>
          <div class="value" id="stat-hub">{summary["dashboard"]["hub"]["healthy"]}/{summary["dashboard"]["hub"]["total"]}</div>
          <div class="subvalue">control plane nodes healthy</div>
        </div>
        <div class="stat">
          <div class="label">Egress VPS</div>
          <div class="value" id="stat-vps">{summary["dashboard"]["vps"]["healthy"]}/{summary["dashboard"]["vps"]["total"]}</div>
          <div class="subvalue">egress nodes healthy</div>
        </div>
        <div class="stat">
          <div class="label">Foreign egress</div>
          <div class="value" id="stat-foreign">{summary["dashboard"]["foreign"]["de"]} / {summary["dashboard"]["foreign"]["pl"]} / {summary["dashboard"]["foreign"]["ru"]}</div>
          <div class="subvalue">de / pl / ru router selection</div>
        </div>
        <div class="stat">
          <div class="label">Router feed</div>
          <div class="value" id="stat-feed">{summary["dashboard"]["router_feed"]["healthy"]}/{summary["dashboard"]["router_feed"]["total"]}</div>
          <div class="subvalue" id="stat-feed-version">version {summary["dashboard"]["router_feed"]["version"]}</div>
        </div>
        <div class="stat">
          <div class="label">Freshness</div>
          <div class="value" id="stat-freshness">{summary["dashboard"]["freshness"]["latest_age_seconds"]}s</div>
          <div class="subvalue" id="stat-freshness-detail">{summary["dashboard"]["freshness"]["stale_nodes"]} stale · {summary["dashboard"]["freshness"]["dns_rows"]} dns rows</div>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Topology by role</h2>
      <div id="node-sections" class="class-grid"></div>
    </div>

    <div class="grid-2">
      <div class="section">
        <h2>Recent DNS observations</h2>
        <div class="scroll">
          <table id="dns-table">
            <thead>
              <tr><th>Node</th><th>Domain</th><th>Client</th><th>Count</th><th>Seen</th></tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
      <div class="section">
        <h2>Recent Events</h2>
        <div class="scroll">
          <table id="events-table">
            <thead>
              <tr><th>Node</th><th>Kind</th><th>Summary</th><th>Seen</th></tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Raw Snapshot</h2>
      <p class="small">Useful when you want the exact JSON payload behind the summary.</p>
      <pre id="raw-json" class="mono raw-json"></pre>
    </div>
  </div>

  <script type="application/json" id="seed-nodes">{nodes_json}</script>
  <script type="application/json" id="seed-events">{events_json}</script>
  <script type="application/json" id="seed-dns">{dns_json}</script>
  <script type="application/json" id="seed-dashboard">{dashboard_json}</script>
  <script>
    const renderChip = (value) => {{
      const status = String(value || 'unknown');
      const cls = status === 'healthy' ? 'good' : (status === 'degraded' ? 'warn' : (status === 'down' ? 'bad' : ''));
      return `<span class="chip ${{cls}}">${{status}}</span>`;
    }};

    const humanBytes = (value) => {{
      const bytes = Number(value || 0);
      if (!bytes) return '0 B';
      const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
      let index = 0;
      let current = bytes;
      while (current >= 1024 && index < units.length - 1) {{
        current /= 1024;
        index += 1;
      }}
      return `${{current.toFixed(current >= 10 || index === 0 ? 0 : 1)}} ${{units[index]}}`;
    }};

    const humanAge = (seconds) => {{
      const total = Number(seconds || 0);
      if (!total) return 'n/a';
      if (total < 60) return `${{total}}s`;
      if (total < 3600) return `${{Math.floor(total / 60)}}m ${{total % 60}}s`;
      return `${{Math.floor(total / 3600)}}h ${{Math.floor((total % 3600) / 60)}}m`;
    }};

    const tunnelLabel = (name, tunnel) => {{
      const egress = String(tunnel.egress || '').toUpperCase();
      if (egress) return egress;
      return String(name || '').replace(/^awg/, '').toUpperCase() || 'LINK';
    }};

    const renderResolverCell = (node) => {{
      const dnsStatus = node.dns_status || 'unknown';
      const feedStatus = node.feed_status || 'unknown';
      const overall = dnsStatus === 'healthy' && feedStatus === 'healthy' ? 'healthy' : (dnsStatus === 'down' || feedStatus === 'down' ? 'down' : 'degraded');
      const cls = overall === 'healthy' ? 'good' : (overall === 'degraded' ? 'warn' : (overall === 'down' ? 'bad' : ''));
      const observed = node.query_logging_enabled ? 'on' : 'off';
      const version = node.current_version || 'version unknown';
      const pending = Number(node.observed_pending ?? 0);
      const sent = Number(node.observed_sent ?? 0);
      return `
        <div class="tunnel-card resolver-card ${{cls}}">
          <div class="tunnel-top">
            <span class="tunnel-name">RESOLVER</span>
            <span class="tunnel-status ${{cls}}">${{overall}}</span>
          </div>
          <div class="tunnel-grid">
            <div class="tunnel-metric"><span>Release</span><strong>${{version}}</strong></div>
            <div class="tunnel-metric"><span>Observed</span><strong>${{pending}}/${{sent}}</strong></div>
          </div>
          <div class="tunnel-foot">observed ${{observed}} · dns ${{dnsStatus}} · feed ${{feedStatus}}</div>
        </div>
      `;
    }};

    const tunnelSummary = (tunnels) => {{
      const cards = [];
      for (const [name, tunnel] of Object.entries(tunnels || {{}})) {{
        const status = tunnel.status || 'unknown';
        if (status === 'unknown' && !Number(tunnel.handshake_age_seconds || 0) && !Number(tunnel.rx_bytes || 0) && !Number(tunnel.tx_bytes || 0)) {{
          continue;
        }}
        const active = Boolean(tunnel.active);
        const cls = `${{status === 'healthy' ? 'good' : (status === 'degraded' ? 'warn' : (status === 'down' ? 'bad' : ''))}} ${{active ? 'active' : ''}}`;
        cards.push(`
          <div class="tunnel-card ${{cls}}">
            <div class="tunnel-top">
              <span class="tunnel-name">${{tunnelLabel(name, tunnel)}}</span>
              <span class="tunnel-status ${{active ? 'active' : ''}}">${{active ? 'ACTIVE' : status}}</span>
            </div>
            <div class="tunnel-grid">
              <div class="tunnel-metric"><span>HS</span><strong>${{humanAge(tunnel.handshake_age_seconds)}}</strong></div>
              <div class="tunnel-metric"><span>RX</span><strong>${{humanBytes(tunnel.rx_bytes)}}</strong></div>
              <div class="tunnel-metric"><span>TX</span><strong>${{humanBytes(tunnel.tx_bytes)}}</strong></div>
            </div>
            <div class="tunnel-foot">${{name}} · ${{status}} · probe ${{tunnel.probe_https || 'unknown'}}</div>
          </div>
        `);
      }}
      return cards.length ? `<div class="tunnel-list">${{cards.join('')}}</div>` : '<span class="small">none</span>';
    }};

    const nodeClassLabel = (node) => {{
      switch (node.node_class || 'other') {{
        case 'router':
          return 'site-router';
        case 'hub':
          return 'control-plane';
        case 'vps':
          return 'egress-vps';
        default:
          return 'other';
      }}
    }};

    const renderNodeLabel = (node) => {{
      const labels = (node.labels && Object.entries(node.labels).map(([k, v]) => `${{k}}=${{v}}`).join(' · ')) || '';
      return `<strong>${{node.node}}</strong><div class="small">${{labels}}</div>`;
    }};

    const renderRouterRows = (nodes) => nodes.map((node) => `
      <tr>
        <td>${{renderNodeLabel(node)}}</td>
        <td>${{renderChip(node.status)}}</td>
        <td>${{renderChip(node.wan_status)}}<div class="small mono">${{node.wan_external_ip || ''}}</div></td>
        <td class="resolver-cell">${{renderResolverCell(node)}}</td>
        <td class="links-cell">${{tunnelSummary(node.tunnels)}}</td>
        <td class="mono">${{node.clients_count === null || node.clients_count === undefined ? 'unknown' : node.clients_count}}</td>
        <td class="mono small">${{node.collected_at || ''}}</td>
      </tr>
    `).join('');

    const renderServerRows = (nodes) => nodes.map((node) => `
      <tr>
        <td>${{renderNodeLabel(node)}}</td>
        <td>${{renderChip(node.status)}}</td>
        <td>${{renderChip(node.uplink_status || node.wan_status)}}<div class="small mono">${{node.uplink_external_ip || node.wan_external_ip || ''}}</div></td>
        <td class="links-cell">${{tunnelSummary(node.tunnels)}}</td>
        <td><span class="chip">${{node.node_class === 'hub' ? 'control-plane' : 'egress-vps'}}</span></td>
        <td class="mono small">${{node.collected_at || ''}}</td>
      </tr>
    `).join('');

    const renderNodeSections = (nodes) => {{
      const groups = {{
        router: nodes.filter((node) => (node.node_class || 'other') === 'router'),
        hub: nodes.filter((node) => (node.node_class || 'other') === 'hub'),
        vps: nodes.filter((node) => (node.node_class || 'other') === 'vps'),
        other: nodes.filter((node) => !(node.node_class || 'other') || !['router', 'hub', 'vps'].includes(node.node_class || 'other')),
      }};
      const sections = [];
      if (groups.router.length) {{
        sections.push(`
          <div class="class-block">
            <div class="class-head">
              <div>
                <h3>Site Routers</h3>
                <div class="small">OpenWrt site routers: WAN, DNS, feed, foreign route, tunnels and client leases. Telemetry is push-only.</div>
              </div>
              <span class="chip">${{groups.router.length}}</span>
            </div>
            <div class="scroll">
              <table>
                <thead>
                  <tr><th>Node</th><th>Status</th><th>WAN</th><th>Resolver</th><th>Tunnels</th><th>Clients</th><th>Updated</th></tr>
                </thead>
                <tbody>${{renderRouterRows(groups.router)}}</tbody>
              </table>
            </div>
          </div>
        `);
      }}
      if (groups.hub.length) {{
        sections.push(`
          <div class="class-block">
            <div class="class-head">
              <div>
                <h3>Control Plane</h3>
                <div class="small">Control-plane host: public uplink, overlay links, feed build, ingest and dashboard services.</div>
              </div>
              <span class="chip">${{groups.hub.length}}</span>
            </div>
            <div class="scroll">
              <table>
                <thead>
                  <tr><th>Node</th><th>Status</th><th>Uplink</th><th>Links</th><th>Profile</th><th>Updated</th></tr>
                </thead>
                <tbody>${{renderServerRows(groups.hub)}}</tbody>
              </table>
            </div>
          </div>
        `);
      }}
      if (groups.vps.length) {{
        sections.push(`
          <div class="class-block">
            <div class="class-head">
              <div>
                <h3>Egress VPS</h3>
                <div class="small">External egress hosts: public uplink, overlay links and future corp tunnels.</div>
              </div>
              <span class="chip">${{groups.vps.length}}</span>
            </div>
            <div class="scroll">
              <table>
                <thead>
                  <tr><th>Node</th><th>Status</th><th>Uplink</th><th>Links</th><th>Profile</th><th>Updated</th></tr>
                </thead>
                <tbody>${{renderServerRows(groups.vps)}}</tbody>
              </table>
            </div>
          </div>
        `);
      }}
      if (groups.other.length) {{
        sections.push(`
          <div class="class-block">
            <div class="class-head">
              <div>
                <h3>Other</h3>
                <div class="small">Nodes that do not yet declare a standard control profile.</div>
              </div>
              <span class="chip">${{groups.other.length}}</span>
            </div>
            <div class="scroll">
              <table>
                <thead>
                  <tr><th>Node</th><th>Status</th><th>Uplink</th><th>Links</th><th>Profile</th><th>Updated</th></tr>
                </thead>
                <tbody>${{renderServerRows(groups.other)}}</tbody>
              </table>
            </div>
          </div>
        `);
      }}
      document.getElementById('node-sections').innerHTML = sections.join('') || '<div class="small">No telemetry targets available.</div>';
    }};

    const renderDns = (items) => {{
      const body = document.querySelector('#dns-table tbody');
      body.innerHTML = items.map((item) => `
        <tr>
          <td class="mono">${{item.node}}</td>
          <td>${{item.domain}}</td>
          <td class="mono">${{item.client}}</td>
          <td class="mono">${{item.count}}</td>
          <td class="mono small">${{item.collected_at || ''}}</td>
        </tr>
      `).join('');
    }};

    const renderEvents = (items) => {{
      const body = document.querySelector('#events-table tbody');
      body.innerHTML = items.map((item) => `
        <tr>
          <td class="mono">${{item.node}}</td>
          <td>${{item.kind}}</td>
          <td>${{item.summary}}</td>
          <td class="mono small">${{item.collected_at}}</td>
        </tr>
      `).join('');
    }};

    const renderDashboardStats = (dashboard) => {{
      const routers = dashboard.routers || {{healthy: 0, total: 0}};
      const hub = dashboard.hub || {{healthy: 0, total: 0}};
      const vps = dashboard.vps || {{healthy: 0, total: 0}};
      const foreign = dashboard.foreign || {{de: 0, pl: 0, ru: 0, unknown: 0}};
      const routerFeed = dashboard.router_feed || {{healthy: 0, total: 0, version: 'unknown'}};
      const freshness = dashboard.freshness || {{latest_age_seconds: 0, stale_nodes: 0, dns_rows: 0}};
      document.getElementById('stat-routers').textContent = `${{routers.healthy}}/${{routers.total}}`;
      document.getElementById('stat-hub').textContent = `${{hub.healthy}}/${{hub.total}}`;
      document.getElementById('stat-vps').textContent = `${{vps.healthy}}/${{vps.total}}`;
      document.getElementById('stat-foreign').textContent = `${{foreign.de}} / ${{foreign.pl}} / ${{foreign.ru}}`;
      document.getElementById('stat-feed').textContent = `${{routerFeed.healthy}}/${{routerFeed.total}}`;
      document.getElementById('stat-feed-version').textContent = `version ${{routerFeed.version || 'unknown'}}`;
      document.getElementById('stat-freshness').textContent = `${{freshness.latest_age_seconds || 0}}s`;
      document.getElementById('stat-freshness-detail').textContent = `${{freshness.stale_nodes || 0}} stale · ${{freshness.dns_rows || 0}} dns rows`;
    }};

    const seedNodes = JSON.parse(document.getElementById('seed-nodes').textContent);
    const seedEvents = JSON.parse(document.getElementById('seed-events').textContent);
    const seedDns = JSON.parse(document.getElementById('seed-dns').textContent);
    const seedDashboard = JSON.parse(document.getElementById('seed-dashboard').textContent);
    renderNodeSections(seedNodes);
    renderEvents(seedEvents);
    renderDns(seedDns);
    renderDashboardStats(seedDashboard);
    document.getElementById('raw-json').textContent = JSON.stringify({{nodes: seedNodes, events: seedEvents, dns_observations: seedDns, dashboard: seedDashboard}}, null, 2);

    async function refresh() {{
      try {{
        const response = await fetch('/api/summary', {{cache: 'no-store'}});
        const data = await response.json();
        renderNodeSections(data.nodes || []);
        renderEvents(data.events || []);
        renderDns(data.dns_observations || []);
        renderDashboardStats(data.dashboard || {{}});
        document.getElementById('raw-json').textContent = JSON.stringify(data, null, 2);
      }} catch (error) {{
        console.warn(error);
      }}
    }}
    setInterval(refresh, 10000);
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only network telemetry dashboard")
    parser.add_argument("--config", default=os.environ.get("TELEMETRY_CONFIG", "/etc/node-control/telemetry.nodes.json"))
    parser.add_argument("--once", action="store_true", help="Collect one round of telemetry and exit")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"config not found: {config_path}")
    config = load_config(config_path)
    store = TelemetryStore(config.db_path)
    collector = TelemetryCollector(config, store)

    if args.once:
        for node in config.nodes:
            if not collector.should_poll_node(node):
                continue
            collector.collect_node(node)
        store.purge_old(config.retention_days)
        print("telemetry collection completed")
        return 0

    if config.source_mode in {"pull", "hybrid"}:
        collector.start()
    server = TelemetryHTTPServer((config.bind, config.port), TelemetryHandler, collector, store)
    print(f"telemetry dashboard listening on {config.bind}:{config.port}", flush=True)
    print(f"telemetry db: {config.db_path}", flush=True)
    print(f"telemetry mode: {config.source_mode}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if config.source_mode in {"pull", "hybrid"}:
            collector.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
