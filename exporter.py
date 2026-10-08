#!/usr/bin/env python3
"""Prometheus exporter for pfSense: firmware, uptime, CPU/RAM/disk, and detailed
DHCP (Kea) service health. Talks to the pfSense REST API package
(jaredhendrickson13/pfSense-pkg-API v2) over HTTP Basic auth - same API used by
the pfsense-mcp server.

Multi-target, blackbox_exporter-style: this process serves any number of
pfSense boxes. Prometheus's scrape config points at the exporter's own
address and passes which pfSense to check via ?target=<url> (see
prometheus.yml's relabel_configs: __address__ from file_sd becomes
__param_target, which becomes this query param). The exporter looks up
credentials for that target from PFSENSE_TARGETS.

Stdlib only, on purpose: the Prometheus hosts this runs on have no pip/venv
available, so no third-party dependency (prometheus_client, httpx) can be
installed. The exposition format is a handful of text lines, not worth a
dependency to render.

Config via environment variables:
  PFSENSE_TARGETS    JSON object mapping target URL -> {"user":..., "pass":...}.
                     The URL must match exactly what's listed as a target in
                     file_sd/pfsense_dhcp_exporter.yml, e.g.:
                     {"https://172.26.1.65/": {"user": "admin", "pass": "secret"}}
  LISTEN_ADDRESS     default 0.0.0.0:9719
  PFSENSE_VERIFY_TLS set to "1" to enforce TLS cert verification (default: off,
                     pfSense typically runs a self-signed cert)

Run:  PFSENSE_TARGETS='{"https://172.26.1.65/":{"user":"admin","pass":"secret"}}' ./exporter.py
Test: curl 'http://localhost:9719/probe?target=https://172.26.1.65/'
"""
import base64
import ipaddress
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGETS = json.loads(os.environ["PFSENSE_TARGETS"])
LISTEN_ADDRESS = os.environ.get("LISTEN_ADDRESS", "0.0.0.0:9719")
SSL_CONTEXT = ssl.create_default_context()
if os.environ.get("PFSENSE_VERIFY_TLS") != "1":
    SSL_CONTEXT.check_hostname = False
    SSL_CONTEXT.verify_mode = ssl.CERT_NONE

UPTIME_RE = re.compile(r"(?:(\d+)\s*Days?)?\s*(?:(\d+)\s*Hours?)?\s*(?:(\d+)\s*Minutes?)?\s*(?:(\d+)\s*Seconds?)?")


def api_get(base_url: str, auth_header: str, path: str, params: dict | None = None) -> dict:
    url = base_url + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers={"Authorization": auth_header})
    with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read())["data"]


def uptime_to_seconds(text: str) -> float:
    d, h, m, s = (int(g) if g else 0 for g in UPTIME_RE.match(text).groups())
    return d * 86400 + h * 3600 + m * 60 + s


def lease_matches_interface(lease: dict, iface: dict) -> bool:
    if lease.get("if"):
        return lease["if"] == iface["id"]
    try:
        ip = int(ipaddress.IPv4Address(lease["ip"]))
        lo = int(ipaddress.IPv4Address(iface["range_from"]))
        hi = int(ipaddress.IPv4Address(iface["range_to"]))
    except (ValueError, KeyError, TypeError):
        return False
    return lo <= ip <= hi


def probe(target: str) -> str:
    creds = TARGETS[target]
    base_url = target.rstrip("/") + "/api/v2"
    auth_header = "Basic " + base64.b64encode(f"{creds['user']}:{creds['pass']}".encode()).decode()

    lines = []

    def gauge(metric: str, value, help_text: str, extra_labels: str = ""):
        labels = f"{{{extra_labels}}}" if extra_labels else ""
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric}{labels} {value}")

    def get(path, params=None):
        return api_get(base_url, auth_header, path, params)

    start = time.monotonic()
    try:
        version = get("/system/version")
        status = get("/status/system")
        services = get("/status/services", {"limit": 0})
        interfaces = [i for i in get("/services/dhcp_servers", {"limit": 0}) if i.get("enable")]
        leases = get("/status/dhcp_server/leases", {"limit": 0})
        iface_descr = {i["id"]: i.get("descr") or i["id"] for i in get("/interfaces")}
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
        gauge("pfsense_up", 0, "Whether the last scrape of this pfSense box succeeded")
        lines.append(f"# scrape failed: {exc}")
        return "\n".join(lines) + "\n"

    gauge("pfsense_up", 1, "Whether the last scrape of this pfSense box succeeded")

    info_labels = ",".join(f'{k}="{version.get(k, "")}"' for k in ("version", "base", "patch", "buildtime"))
    gauge("pfsense_info", 1, "pfSense firmware version, labels carry the details", info_labels)

    gauge("pfsense_uptime_seconds", uptime_to_seconds(status["uptime"]), "Seconds since last reboot")
    gauge("pfsense_cpu_usage_percent", status["cpu_usage"], "Current CPU usage percent")
    gauge("pfsense_cpu_count", status["cpu_count"], "Number of CPU cores")
    load1, load5, load15 = status["cpu_load_avg"]
    gauge("pfsense_cpu_load1", load1, "1 minute load average")
    gauge("pfsense_cpu_load5", load5, "5 minute load average")
    gauge("pfsense_cpu_load15", load15, "15 minute load average")
    gauge("pfsense_mem_usage_percent", status["mem_usage"], "Current memory usage percent")
    gauge("pfsense_swap_usage_percent", status["swap_usage"], "Current swap usage percent")
    gauge("pfsense_disk_usage_percent", status["disk_usage"], "Current disk usage percent")
    if status.get("temp_c") is not None:
        gauge("pfsense_temp_celsius", status["temp_c"], "System temperature in Celsius")

    for svc in services:
        if "dhcp" not in svc["name"].lower():
            continue
        svc_labels = f'service="{svc["name"]}"'
        gauge("pfsense_dhcp_service_up", int(bool(svc["status"])), "Whether the DHCP service process is running", svc_labels)
        gauge("pfsense_dhcp_service_enabled", int(bool(svc["enabled"])), "Whether the DHCP service is enabled", svc_labels)

    for iface in interfaces:
        iface_leases = [l for l in leases if lease_matches_interface(l, iface)]
        static = [l for l in iface_leases if l.get("active_status") == "static"]
        dynamic = [l for l in iface_leases if l.get("active_status") != "static"]
        online = [l for l in iface_leases if str(l.get("online_status")).startswith("active")]

        pool_size = None
        try:
            pool_size = (
                int(ipaddress.IPv4Address(iface["range_to"])) - int(ipaddress.IPv4Address(iface["range_from"])) + 1
            )
        except (ValueError, KeyError, TypeError):
            pass

        descr = iface_descr.get(iface["id"], iface["id"]).replace("\\", "\\\\").replace('"', '\\"')
        il = f'interface="{iface["id"]}",interface_descr="{descr}"'
        if pool_size:
            gauge("pfsense_dhcp_pool_size", pool_size, "Size of the dynamic DHCP pool", il)
            gauge(
                "pfsense_dhcp_pool_utilization_percent",
                round(len(dynamic) / pool_size * 100, 1),
                "Percent of the dynamic pool currently leased",
                il,
            )
        gauge("pfsense_dhcp_leases_total", len(iface_leases), "Total leases (static + dynamic)", il)
        gauge("pfsense_dhcp_leases_static", len(static), "Static (reserved) leases", il)
        gauge("pfsense_dhcp_leases_dynamic", len(dynamic), "Dynamic leases", il)
        gauge("pfsense_dhcp_leases_online", len(online), "Leases currently answering ARP/ping", il)

    gauge("pfsense_scrape_duration_seconds", round(time.monotonic() - start, 3), "How long the scrape took")
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/probe":
            self.send_response(404)
            self.end_headers()
            return

        target = urllib.parse.parse_qs(parsed.query).get("target", [None])[0]
        if not target or target not in TARGETS:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Bad target: missing or unknown ?target= (check PFSENSE_TARGETS)\n")
            return

        body = probe(target).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    host, port = LISTEN_ADDRESS.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"pfsense-dhcp-exporter listening on {LISTEN_ADDRESS}, targets: {list(TARGETS)}")
    server.serve_forever()
