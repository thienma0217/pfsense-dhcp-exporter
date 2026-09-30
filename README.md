# pfsense-dhcp-exporter

Prometheus exporter for [pfSense](https://www.pfsense.org/) boxes running the
community [pfSense-pkg-API](https://github.com/jaredhendrickson13/pfsense-api)
(v2). Exposes firmware version, uptime, CPU/RAM/disk usage, and detailed DHCP
(Kea) service health — including per-interface pool size, utilization, and
lease counts, which the official
[pfrest/pfsense_exporter](https://github.com/pfrest/pfsense_exporter) does not
cover.

Built to complement, not replace, `pfrest/pfsense_exporter` — run both side by
side if you also want interface traffic, gateway, and firewall-state metrics.

## Why stdlib only

No third-party dependency (`prometheus_client`, `httpx`, ...). The Prometheus
hosts this typically runs on have no `pip`/`venv` available, and the
Prometheus text exposition format is a handful of lines — not worth a
dependency to render. `exporter.py` is a single file with zero external
requirements beyond Python 3.10+.

## Architecture

Multi-target, [blackbox_exporter](https://github.com/prometheus/blackbox_exporter)-style:
one exporter process can serve any number of pfSense boxes. Prometheus's
scrape config points at the exporter's own `ip:port`, and passes **which**
pfSense to check via `?target=<url>` through relabeling — the exporter looks
up credentials for that target from `PFSENSE_TARGETS` and probes it live on
every scrape.

```
Prometheus --scrape--> exporter:9719/probe?target=https://pfsense-host/
                              |
                              +-- looks up credentials in PFSENSE_TARGETS
                              +-- calls pfSense REST API (HTTP Basic auth)
                              +-- returns metrics for that one target
```

## Metrics

| Metric | Labels | Description |
|---|---|---|
| `pfsense_up` | | Whether the last scrape of this pfSense box succeeded |
| `pfsense_info` | `version`, `base`, `patch`, `buildtime` | Firmware version (details in labels, value always `1`) |
| `pfsense_uptime_seconds` | | Seconds since last reboot |
| `pfsense_cpu_usage_percent` | | Current CPU usage percent |
| `pfsense_cpu_count` | | Number of CPU cores |
| `pfsense_cpu_load1` / `load5` / `load15` | | Load averages |
| `pfsense_mem_usage_percent` | | Current memory usage percent |
| `pfsense_swap_usage_percent` | | Current swap usage percent |
| `pfsense_disk_usage_percent` | | Current disk usage percent |
| `pfsense_temp_celsius` | | System temperature (omitted if the box reports none) |
| `pfsense_dhcp_service_up` / `_enabled` | `service` | Whether the DHCP service process is running / enabled (e.g. `kea-dhcp4`) |
| `pfsense_dhcp_pool_size` | `interface`, `interface_descr` | Size of the dynamic DHCP pool |
| `pfsense_dhcp_pool_utilization_percent` | `interface`, `interface_descr` | Percent of the dynamic pool currently leased |
| `pfsense_dhcp_leases_total` / `_static` / `_dynamic` / `_online` | `interface`, `interface_descr` | Lease counts per interface |
| `pfsense_scrape_duration_seconds` | | How long the scrape took |

`interface_descr` is the human-readable interface name (e.g. `VLAN2101`),
looked up from `/api/v2/interfaces` and joined with `/services/dhcp_servers`.

Dynamic (non-reserved) leases aren't tagged with an interface by the pfSense
API, so they're matched to a DHCP interface by checking whether the lease IP
falls inside that interface's pool range.

## Configuration

Environment variables (see `.env.example`):

| Variable | Required | Description |
|---|---|---|
| `PFSENSE_TARGETS` | yes | JSON object mapping target URL → `{"user": ..., "pass": ...}`. The URL must match exactly what's listed as a target in Prometheus's file_sd file. |
| `LISTEN_ADDRESS` | no | Default `0.0.0.0:9719` |
| `PFSENSE_VERIFY_TLS` | no | Set to `1` to enforce TLS cert verification (default: off, pfSense typically runs a self-signed cert) |

```bash
PFSENSE_TARGETS='{"https://10.0.0.1/": {"user": "admin", "pass": "secret"}}'
```

One exporter instance can serve multiple pfSense boxes — just add more
entries to the JSON object.

## Running

```bash
cp .env.example .env   # fill in PFSENSE_TARGETS
docker compose up -d --build
```

Or without Docker (any Python 3.10+, no dependencies to install):

```bash
export PFSENSE_TARGETS='{"https://10.0.0.1/": {"user": "admin", "pass": "secret"}}'
python3 exporter.py
```

Test a probe directly:

```bash
curl 'http://localhost:9719/probe?target=https://10.0.0.1/'
```

Unknown or missing `target` returns `400 Bad target`.

## Prometheus setup

Mirrors `blackbox_exporter`'s well-known pattern: the scrape target is the
pfSense URL, and `relabel_configs` redirects the actual HTTP request to the
exporter, passing the pfSense URL along as `?target=`.

Add this job to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: pfsense_dhcp_exporter
    metrics_path: /probe
    scrape_interval: 30s
    scrape_timeout: 15s
    static_configs:
      - targets:
          - https://10.0.0.1/
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: localhost:9719   # exporter's own address
```

Add one more entry under `targets:` (and to `PFSENSE_TARGETS`) per additional
pfSense box.
