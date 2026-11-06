#!/usr/bin/env python3
"""Poll a Google Nest Wifi / Wifi Pro local status API and push it to New Relic.

Stdlib only. Reads GET http://<router>/api/v1/status and sends gauges to the
New Relic Metric API. If the router can't be reached, sends nest.router.up=0.

Config via env (see nest-status.env.example):
  NEST_ROUTER      router IP/host           (default 192.168.78.1)
  NR_LICENSE_KEY   New Relic ingest/license key (required unless --dry-run)
  NR_REGION        US or EU                 (default US)
  NEST_HOST_LABEL  value for the 'host' attribute (default: this hostname)
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

NR_ENDPOINTS = {
    "US": "https://metric-api.newrelic.com/metric/v1",
    "EU": "https://metric-api.eu.newrelic.com/metric/v1",
}


def fetch_status(router, timeout):
    t0 = time.monotonic()
    with urllib.request.urlopen(f"http://{router}/api/v1/status", timeout=timeout) as r:
        body = json.load(r)
    return body, (time.monotonic() - t0) * 1000.0


def b(v):
    return 1 if v else 0


def build_metrics(router, status, latency_ms, error=None):
    now = int(time.time() * 1000)
    common = {
        "router": router,
        "host": os.environ.get("NEST_HOST_LABEL") or socket.gethostname(),
        "service": "nest-wifi-status",
    }
    if status is None:
        common["error"] = str(error)[:200]
        return [{"common": {"timestamp": now, "attributes": common},
                 "metrics": [{"name": "nest.router.up", "type": "gauge", "value": 0}]}]

    sw = status.get("software", {})
    sysi = status.get("system", {})
    wan = status.get("wan", {})
    common.update({
        "modelId": sysi.get("modelId", ""),
        "hardwareId": sysi.get("hardwareId", ""),
        "softwareVersion": sw.get("softwareVersion", ""),
        "updateChannel": sw.get("updateChannel", ""),
        "updateStatus": sw.get("updateStatus", ""),
        "setupState": status.get("setupState", ""),
        "ledAnimation": sysi.get("ledAnimation", ""),
        "groupRole": sysi.get("groupRole", ""),
        "wanIp": wan.get("localIpAddress", ""),
        "wanGateway": wan.get("gatewayIpAddress", ""),
        "dnsMode": status.get("dns", {}).get("mode", ""),
    })
    g = lambda n, v: {"name": n, "type": "gauge", "value": v}
    metrics = [
        g("nest.router.up", 1),
        g("nest.router.api_latency_ms", round(latency_ms, 1)),
        g("nest.system.uptime_s", sysi.get("uptime", 0)),
        g("nest.system.lan0_link", b(sysi.get("lan0Link"))),
        g("nest.system.led_intensity", sysi.get("ledIntensity", 0)),
        g("nest.wan.online", b(wan.get("online"))),
        g("nest.wan.ethernet_link", b(wan.get("ethernetLink"))),
        g("nest.wan.has_ip", b(wan.get("ipAddress"))),
        g("nest.wan.captive_portal", b(wan.get("captivePortal"))),
        g("nest.wan.invalid_credentials", b(wan.get("invalidCredentials"))),
        g("nest.software.update_required", b(sw.get("updateRequired"))),
        g("nest.software.update_progress", sw.get("updateProgress", 0.0)),
    ]
    return [{"common": {"timestamp": now, "attributes": common}, "metrics": metrics}]


def send(payload, key, region, timeout):
    req = urllib.request.Request(
        NR_ENDPOINTS[region],
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Api-Key": key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode()


def run_once(args):
    router = args.router
    try:
        status, latency = fetch_status(router, args.timeout)
        payload = build_metrics(router, status, latency)
    except (urllib.error.URLError, OSError, ValueError) as e:
        payload = build_metrics(router, None, 0, error=e)

    attrs = payload[0]["common"]["attributes"]
    summary = {m["name"]: m["value"] for m in payload[0]["metrics"]}
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0
    key = os.environ.get("NR_LICENSE_KEY")
    if not key:
        print("NR_LICENSE_KEY not set", file=sys.stderr)
        return 2
    try:
        code, _ = send(payload, key, args.region, args.timeout)
    except urllib.error.HTTPError as e:
        print(f"New Relic rejected payload: HTTP {e.code} {e.read().decode()[:200]}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"New Relic send failed: {e}", file=sys.stderr)
        return 1
    print(f"sent HTTP {code} up={summary.get('nest.router.up')} "
          f"online={summary.get('nest.wan.online')} sw={attrs.get('softwareVersion', '-')}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--router", default=os.environ.get("NEST_ROUTER", "192.168.78.1"))
    p.add_argument("--region", default=os.environ.get("NR_REGION", "US").upper(), choices=NR_ENDPOINTS)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--interval", type=int, default=0, help="loop every N seconds (0 = run once)")
    p.add_argument("--dry-run", action="store_true", help="print payload, don't send")
    args = p.parse_args()

    if args.interval <= 0:
        return run_once(args)
    while True:
        run_once(args)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
