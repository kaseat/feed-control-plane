# Node Runtime

This directory contains the client-side runtime for an OpenWrt site-router.

## Minimal OpenWrt setup

Install the packages required for the first sync:

- `curl`
- `ca-bundle` or `ca-certificates`
- `coreutils-sha256sum` or an equivalent `sha256sum`
- `dnsmasq-full`
- `nftables`
- `cron` support, if it is not already present in the image

Create the runtime state directories:

```sh
mkdir -p /etc/node-feeds /var/lib/node-feeds/observed
chmod 700 /etc/node-feeds /var/lib/node-feeds/observed
```

Copy the public template:

```sh
cp /path/to/repo/node/examples/node.conf.example /etc/node-feeds/node.conf
```

Set at least:

- `FEED_BASE_URL=http://<vpn-bind-ip>:18080/node-feeds`
- `FEED_PROFILE=critical`
- `ROUTER_NAME=<your-node-name>`
- `NODE_CLASS=router`
- `STATE_ROOT=/etc/node-feeds`
- `OBSERVED_SPOOL=/var/lib/node-feeds/observed`
- `HUB_PUSH_URL=http://<vpn-bind-ip>:18080/api/observed`
- `HUB_PUSH_TOKEN=<shared-ingest-token>`
- `HUB_HEALTH_URL=http://<vpn-bind-ip>:19090/api/health`
- `TELEMETRY_OBSERVED_URL=http://<vpn-bind-ip>:19090/api/observed`
- `TELEMETRY_PUSH_TOKEN=<shared-telemetry-token>`

`NODE_CLASS` is the control profile for the node:

- `router` for OpenWrt site routers;
- `hub` for the control-plane VPS;
- `vps` for external egress VPS hosts.

The telemetry dashboard uses this field to choose the correct health contract
and display only the relevant controls for each role. In the current contour,
site routers are push-only for telemetry: they upload `node-health.json` and
observed batches, while the hub polls only `hub` and `vps` targets over SSH.

## First sync

Run the update flow manually once:

```sh
/usr/libexec/node-feeds/update.sh /etc/node-feeds/node.conf
```

The first sync should:

- fetch `manifest.txt`;
- validate checksums;
- download the node profile into `staging`;
- atomically move the release into `releases/<version>`;
- switch `current` to the new release.

## Routing contour

The node runtime also prepares the routing contour for `foreign_required` traffic:

- `dnsmasq` loads `dnsmasq-fd4.conf` from the active release via `/etc/dnsmasq.d/node-feeds-fd4.conf`;
- `load-nft-static.sh` materializes `fd4` and `fs4` into `inet fw4`;
- `setup-routing.sh` also installs `lan -> awgde`, `lan -> awgpl`, and `lan -> awgru` firewall forwarding so forwarded LAN traffic can leave through the foreign contour;
- `refresh-foreign-route.sh` installs `foreign_active` and the `fwmark 0x0100` policy rule;
- `setup-routing.sh` runs the full activation flow after each release apply.

## Daily sync

The simplest scheduler on OpenWrt is cron.
Use the example in `node/examples/openwrt-cron.example` and copy it into `/etc/crontabs/root`.

The daily job should call the same update script:

```sh
/usr/libexec/node-feeds/update.sh /etc/node-feeds/node.conf
```

`node-health.sh` should run every few minutes so the hub can see fresh WAN, DNS, tunnel and feed status:

```sh
/usr/libexec/node-feeds/node-health.sh /etc/node-feeds/node.conf
```

## Notes

- `collect-observed.sh` reads dnsmasq query logs and writes hourly NDJSON batches under `OBSERVED_SPOOL`.
- `push-observed.sh` uploads those batches to `hub` and moves successfully sent files into `OBSERVED_SPOOL/sent`.
- `push-health.sh` uploads the current `node-health.json` snapshot to the central telemetry dashboard.
- site-router telemetry is push-only in hub `hybrid` mode; the hub does not need SSH access to routers.
- `push-observed.sh` can optionally duplicate observed batches to the central telemetry dashboard when `TELEMETRY_OBSERVED_URL` and `TELEMETRY_PUSH_TOKEN` are set.
- The central telemetry dashboard is expected at `http://<vpn-bind-ip>:19090/` on the hub.
- `setup-routing.sh` enables `dnsmasq` query logging so the collector has real query events to aggregate.
- `node-health.sh` writes `/var/run/node-health.json`, `/var/run/node-route-policy.state.json` and `/var/lib/node-feeds/status.json`.
- on routers, `node-health.json` should include `wan`, `dns`, `feed`, `tunnels` and `route_tables.foreign_active`; `feed` must expose `current_release`, `current_version`, `profile`, `observed_pending` and `observed_sent`.
- Observed upload format is newline-delimited JSON. Each line is one aggregated record with at least `node`, `window`, `domain`, and `count`.
- The runtime is IPv4-only in the current model.
- If the hub is temporarily unavailable, the node must remain on `current`.
