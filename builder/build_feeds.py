#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class OutputFile:
    path: Path
    profile: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(output_dir: Path, files: list[OutputFile], runtime_version: int) -> dict:
    now = datetime.now(timezone.utc)
    profiles: dict[str, dict] = {}
    for item in files:
        profiles.setdefault(item.profile, {"files": {}})
        profiles[item.profile]["files"][item.path.name] = {
            "sha256": sha256_file(item.path),
            "size": item.path.stat().st_size,
        }

    return {
        "version": now.strftime("%Y%m%d%H%M%S"),
        "generated_at_utc": now.isoformat(),
        "router_runtime_version": runtime_version,
        "profiles": profiles,
        "notes": [
            "Public repo stores only templates and builder code.",
            "Production observed/candidate/approved data is expected to live on hub.",
            "Site nodes are expected to download only the critical profile from hub.",
            "The critical profile is expected to separate dns-derived fd4 and curated static fs4 layers.",
            "Recommended release file names are crit.domains, dnsmasq-fd4.conf, nft-fs4.txt and ref.domains.",
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
            lines.append(
                "\t".join(
                    [
                        "file",
                        file_name,
                        meta["sha256"],
                        str(meta["size"]),
                    ]
                )
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a profile-aware manifest")
    parser.add_argument("--input-dir", required=True, help="Directory with already prepared profile files")
    parser.add_argument("--output-dir", required=True, help="Directory where manifest.json will be written")
    parser.add_argument("--runtime-version", type=int, default=1)
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    files: list[OutputFile] = []
    for profile_dir in sorted(p for p in input_dir.iterdir() if p.is_dir()):
        for file_path in sorted(p for p in profile_dir.iterdir() if p.is_file()):
            files.append(OutputFile(path=file_path, profile=profile_dir.name))

    manifest = build_manifest(output_dir, files, args.runtime_version)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "manifest.txt").write_text(build_manifest_txt(manifest), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
