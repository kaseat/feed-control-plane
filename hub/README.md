# Hub Runtime

`hub` is the control-plane node that builds a release bundle and serves it over HTTP for site nodes.

## What it does

On startup:
1. loads `/etc/node-control/hub.conf`;
2. serves the last known `runtime/current` release over HTTP;
3. publishes `manifest.txt`, `manifest.json` and `releases/<version>/...` under `runtime/public/node-feeds`;
4. accepts `POST /api/observed` with NDJSON aggregated observations;
5. starts an HTTP server on `HTTP_PORT` (default `18080`).

This is a runnable bootstrap skeleton, not the final private-data pipeline.

Build is intentionally separate from HTTP serving:
- `build-runtime.sh` builds a new release into `runtime/releases/<version>`;
- `publish-runtime.sh` exports the active `current` release to the HTTP tree;
- `bootstrap.sh` only serves the already published runtime.
- `sync-repo.sh` checks whether the upstream repo head changed and only then runs build + publish.

Observed ingest:
- `POST /api/observed`
- `Content-Type: application/x-ndjson`
- each line is one JSON object
- request body is stored under `data/observed`
- if `HUB_INGEST_TOKEN` is set, requests must use `Authorization: Bearer <token>`

Observed processing:
- `process-observed.sh` turns new batches into candidate / deferred / exception / rejected outputs;
- accepted domains are appended to the private approved delta file under `data/approved/private_critical.domains`;
- the next runtime build merges that approved delta into `critical`;
- `count_min` and `windows_min` are enforced on aggregated totals;
- `clients_min` only becomes meaningful once the observed schema carries distinct client ids or hashes.

Telemetry and dashboard:
- `telemetry-dashboard.py` runs in `pull` or `hybrid` mode;
- in `hybrid` mode, `site-router` targets are push-only and are not polled over SSH;
- hub and egress VPS targets are polled over SSH for uplink/link checks;
- pushed site-router snapshots carry `node-health`, route-policy state and feed state, including `current_version`, `observed_pending` and `observed_sent`;
- it stores snapshots and recent DNS observations in SQLite;
- it serves a read-only dashboard, JSON API and Prometheus-style `/metrics` endpoint;
- the example node list is [telemetry.nodes.example.json](./examples/telemetry.nodes.example.json);
- the host service unit is [node-control-telemetry.service](./systemd/node-control-telemetry.service).

## Minimal local run

Example config:

```sh
REPO_ROOT=/opt/node-control/repo
DATA_ROOT=/opt/node-control/data
RUNTIME_ROOT=/opt/node-control/runtime
HTTP_PORT=18080
PUBLIC_ROOT=/opt/node-control/runtime/public/node-feeds
REGISTRY_FILE=/opt/node-control/data/registry/nodes.json
OBSERVED_ROOT=/opt/node-control/data/observed
APPROVED_CRITICAL_FILE=/opt/node-control/data/approved/private_critical.domains
CANDIDATE_ROOT=/opt/node-control/data/candidate
PROCESS_STATE_FILE=/opt/node-control/data/state/observed-processed.json
NOISE_FILTERS_CONFIG=$REPO_ROOT/seeds/noise.example.json
DENY_FILTERS_CONFIG=$REPO_ROOT/seeds/deny.example.json
PUBLIC_SOURCES_CONFIG=$REPO_ROOT/seeds/public_sources.example.json
ITDOG_PROFILE_CONFIG=$REPO_ROOT/seeds/itdog_profile.example.json
THRESHOLDS_CONFIG=$REPO_ROOT/seeds/thresholds.example.json
```

With Docker Compose:

```sh
docker compose -f hub/docker-compose.yml up --build
```

The bundle is then available at:

- `http://localhost:18080/node-feeds/manifest.txt`
- `http://localhost:18080/node-feeds/releases/<version>/...`

Healthcheck:
- Compose probes `http://127.0.0.1:18080/node-feeds/manifest.txt`
- if it fails, the container is marked unhealthy, but the current release is still the source of truth

## Local deploy on a Linux VPS without docker networks

If you want the container to run without a Docker bridge network, use host networking:

```sh
docker compose -f hub/docker-compose.host.yml run --rm hub /bin/sh /opt/node-control/hub/scripts/build-runtime.sh /etc/node-control/hub.conf
docker compose -f hub/docker-compose.host.yml run --rm hub /bin/sh /opt/node-control/hub/scripts/publish-runtime.sh /etc/node-control/hub.conf
docker compose -f hub/docker-compose.host.yml up -d
```

This setup:
- does not create a Docker bridge network;
- binds the HTTP server directly on the VPS host network;
- keeps `runtime/current` on persistent Docker volumes;
- is the preferred deployment style for a single-hub VPS.

The bundle will then be available at:

- `http://<vpn-bind-ip>:18080/node-feeds/manifest.txt` over the VPN when the host is bound to a VPN-facing address
- `http://127.0.0.1:18080/node-feeds/manifest.txt` locally on the host when the bind is loopback-only

Set `HTTP_BIND_ADDR` in a local override or environment file on the VPS to choose the VPN-facing bind address.
The public template for that file is [hub.env.example](./examples/hub.env.example); on the VPS, place the real file at `/etc/node-control/hub.env` and pass it via `--env-file`.
This keeps the bind address out of git while still making the deployment reproducible.

### Telemetry dashboard

The read-only telemetry dashboard runs as a separate host service.
In `hybrid` mode, site routers push health/observed data to the hub, while the hub can poll itself and egress VPS targets over SSH.
It stores snapshots and recent DNS observations in SQLite.
The dashboard groups targets by role so site routers, hub and egress VPS hosts each show their own relevant health fields.

Example local run:

```sh
python3 hub/scripts/telemetry-dashboard.py --config /etc/node-control/telemetry.nodes.json
```

Example config lives at [telemetry.nodes.example.json](./examples/telemetry.nodes.example.json).
On the hub VPS, use [telemetry.env.example](./examples/telemetry.env.example) to set the VPN-facing bind address and port through `EnvironmentFile=/etc/node-control/telemetry.env`.
The host service unit lives at [node-control-telemetry.service](./systemd/node-control-telemetry.service).
In hub mode the dashboard runs with `TELEMETRY_MODE=hybrid` and accepts site-router snapshots and observed batches from routers over VPN while polling only SSH-reachable non-router targets.
The default collector cadence is 300 seconds and stale targets are marked after 600 seconds without a fresh snapshot.
The central dashboard is available at `http://<vpn-bind-ip>:19090/` when the hub VPS is up.

### Daily sync on the VPS

The `hub` does not need to pull and rebuild on every check. The sync job first fetches the repo head, compares it with the current local commit, and only pulls when the repo changed. It still runs the runtime build/publish cycle on the daily timer so upstream sources can refresh even when the GitHub repo is unchanged.
The host-level timer can use the public-safe example config from the repo, so no separate host config file is required for the sync job.
For the runtime bind address and ingest token, use a separate host-local env file such as `/etc/node-control/hub.env`.
The sync job also processes any new observed batches before the build step so the private approved delta is folded into the next release.
Observed processing happens inside the `hub` container so it sees the same runtime and data volumes as build/publish.

Public examples for a host-level timer live in:
- [node-control-hub-sync.service](./systemd/node-control-hub-sync.service)
- [node-control-hub-sync.timer](./systemd/node-control-hub-sync.timer)

The telemetry dashboard is a separate long-running host service and does not depend on the hub rebuild timer.
