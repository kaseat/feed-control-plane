#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    text = SLUG_RE.sub("-", text).strip("-._")
    return text or fallback


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HubRequestHandler(SimpleHTTPRequestHandler):
    server_version = "node-control-hub/1.0"

    def __init__(self, *args: Any, directory: str | None = None, **kwargs: Any) -> None:
        self.public_root = Path(directory or ".").resolve()
        self.data_root = Path(os.environ["DATA_ROOT"]).resolve()
        self.observed_root = Path(os.environ["OBSERVED_ROOT"]).resolve()
        self.ingest_token = os.environ.get("HUB_INGEST_TOKEN", "")
        self.max_observed_bytes = int(os.environ.get("HUB_MAX_OBSERVED_BYTES", "1048576"))
        super().__init__(*args, directory=str(self.public_root), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        message = format % args
        print(f"{self.log_date_time_string()} {self.client_address[0]} {message}", flush=True)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/api/observed":
            self.send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")
            return
        if self.ingest_token:
            auth = self.headers.get("Authorization", "")
            auth_parts = auth.strip().split()
            if len(auth_parts) != 2 or auth_parts[0].lower() != "bearer" or auth_parts[1] != self.ingest_token.strip():
                self.send_error(HTTPStatus.UNAUTHORIZED, "missing or invalid bearer token")
                return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid content length")
            return
        if length <= 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "empty request body")
            return
        if length > self.max_observed_bytes:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
            return

        body = self.rfile.read(length)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "body must be utf-8")
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

        first = records[0]
        node = slugify(first.get("node") or first.get("router"), "unknown")
        window = slugify(first.get("window") or first.get("hour") or first.get("timestamp"), "unknown")
        ingest_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

        target_dir = self.observed_root / ingest_id[:8] / node
        target_dir.mkdir(parents=True, exist_ok=True)
        payload_path = target_dir / f"{ingest_id}-{window}.jsonl"
        meta_path = payload_path.with_suffix(".meta.json")

        payload_path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {
                    "received_at_utc": iso_utc_now(),
                    "node": node,
                    "window": window,
                    "records": len(records),
                    "remote_addr": self.client_address[0],
                    "path": self.path,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        response = {
            "status": "accepted",
            "node": node,
            "window": window,
            "records": len(records),
            "stored_as": str(payload_path.relative_to(self.data_root)),
        }
        encoded = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(HTTPStatus.ACCEPTED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description="Node Control Hub HTTP server")
    parser.add_argument("--bind", default=os.environ.get("HTTP_BIND_ADDR", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HTTP_PORT", "18080")))
    parser.add_argument("--public-root", default=os.environ.get("PUBLIC_ROOT", "/opt/node-control/runtime/public"))
    args = parser.parse_args()

    public_root = Path(args.public_root).resolve()
    public_root.mkdir(parents=True, exist_ok=True)
    Path(os.environ["OBSERVED_ROOT"]).mkdir(parents=True, exist_ok=True)

    handler = partial(HubRequestHandler, directory=str(public_root))
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"hub server listening on {args.bind}:{args.port}", flush=True)
    print(f"hub public root: {public_root}", flush=True)
    print(f"hub observed root: {Path(os.environ['OBSERVED_ROOT']).resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
