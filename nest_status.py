#!/usr/bin/env python3
"""Poll a Google Nest Wifi / Wifi Pro local status API and push it to New Relic.

Stdlib only. Reads GET http://<router>/api/v1/status and sends gauges to the
New Relic Metric API. If the router can't be reached, sends nest.router.up=0.

Also reads WAN byte counters over UPnP (MiniUPnPd on :5000) to report household
throughput, and with --speedtest runs an active download/upload test against
speed.cloudflare.com.

Config via env (see nest-status.env.example):
  NEST_ROUTER      router IP/host           (default 192.168.86.1)
  NR_LICENSE_KEY   New Relic ingest/license key (required unless --dry-run)
  NR_REGION        US or EU                 (default US)
  NEST_HOST_LABEL  value for the 'host' attribute (default: this hostname)
  NEST_STATE_DIR   where the previous counter sample is kept
                   (default $STATE_DIRECTORY from systemd, else ~/.cache/nest-status)
"""
import argparse
import http.client
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

NR_ENDPOINTS = {
    "US": "https://metric-api.newrelic.com/metric/v1",
    "EU": "https://metric-api.eu.newrelic.com/metric/v1",
}

UA = "nest-status/1.0"
UPNP_NS = "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1"
CF = "https://speed.cloudflare.com"
COUNTER_MAX = 2**32 - 1
UPNP_COUNTERS = (
    ("rx", "GetTotalBytesReceived", "NewTotalBytesReceived"),
    ("tx", "GetTotalBytesSent", "NewTotalBytesSent"),
    ("rx_pkts", "GetTotalPacketsReceived", "NewTotalPacketsReceived"),
    ("tx_pkts", "GetTotalPacketsSent", "NewTotalPacketsSent"),
)


def common_attrs(router):
    return {
        "router": router,
        "host": os.environ.get("NEST_HOST_LABEL") or socket.gethostname(),
        "service": "nest-wifi-status",
    }


def gauge(name, value):
    return {"name": name, "type": "gauge", "value": value}


def upnp_call(router, action, timeout):
    body = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{UPNP_NS}"/></s:Body></s:Envelope>'
    ).encode()
    req = urllib.request.Request(
        f"http://{router}:5000/ctl/CmnIfCfg", data=body, method="POST",
        headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{UPNP_NS}#{action}"'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        root = ET.fromstring(r.read())
    return {el.tag.split("}")[-1]: el.text for el in root.iter() if el.tag.split("}")[-1].startswith("New")}


def state_path():
    d = os.environ.get("NEST_STATE_DIR") or os.environ.get("STATE_DIRECTORY") \
        or os.path.expanduser("~/.cache/nest-status")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "counters.json")


def counter_delta(cur, prev):
    """Delta of a 32-bit UPnP counter, or None if it can't be trusted.

    Some routers clamp the counter at 2**32-1 instead of wrapping it, so a value
    at the ceiling means "no information", not "no traffic".
    """
    if cur >= COUNTER_MAX:
        return None
    d = cur - prev
    if d >= 0:
        return d
    if prev <= COUNTER_MAX:
        return d + 2**32
    return None


def throughput_metrics(router, uptime, timeout):
    """WAN rates since the previous run, from UPnP counters.

    Each rate is reported only when its own counter is usable: rx_bps can be
    missing while tx_bps and the packet rates are still sent.
    """
    cur = {}
    for key, action, field in UPNP_COUNTERS:
        cur[key] = int(upnp_call(router, action, timeout)[field])
    now = time.time()
    path = state_path()
    try:
        with open(path) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = None
    with open(path, "w") as f:
        json.dump({"t": now, "uptime": uptime, **cur}, f)

    out = [gauge("nest.wan.rx_bytes_total", cur["rx"]), gauge("nest.wan.tx_bytes_total", cur["tx"]),
           gauge("nest.wan.rx_packets_total", cur["rx_pkts"]), gauge("nest.wan.tx_packets_total", cur["tx_pkts"])]
    out += [gauge("nest.wan.rx_counter_saturated", b(cur["rx"] >= COUNTER_MAX)),
            gauge("nest.wan.tx_counter_saturated", b(cur["tx"] >= COUNTER_MAX))]
    if not prev or uptime < prev.get("uptime", 0):  # first run or router rebooted
        return out
    dt = now - prev["t"]
    if dt <= 0 or dt > 900:
        return out
    for key, name, scale in (("rx", "rx_bps", 8), ("tx", "tx_bps", 8), ("rx_pkts", "rx_pps", 1), ("tx_pkts", "tx_pps", 1)):
        if key not in prev:  # state file from an older version
            continue
        d = counter_delta(cur[key], prev[key])
        if d is not None:
            out.append(gauge(f"nest.wan.{name}", round(d * scale / dt)))
    return out


def speedtest(down_bytes, up_bytes, timeout):
    """Active test against Cloudflare: latency (ms), download/upload (bits/s)."""
    def get(url):
        return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout)

    # latency: small requests on one kept-alive connection (first one warms up TLS)
    conn = http.client.HTTPSConnection(CF.split("//")[1], timeout=timeout)
    lat = []
    for i in range(6):
        t0 = time.monotonic()
        conn.request("GET", "/__down?bytes=0", headers={"User-Agent": UA})
        conn.getresponse().read()
        if i:
            lat.append((time.monotonic() - t0) * 1000)
    conn.close()
    lat.sort()

    t0 = time.monotonic()
    n = 0
    with get(f"{CF}/__down?bytes={down_bytes}") as r:
        while chunk := r.read(65536):
            n += len(chunk)
    down = n * 8 / (time.monotonic() - t0)

    req = urllib.request.Request(f"{CF}/__up", data=os.urandom(up_bytes), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/octet-stream"})
    t0 = time.monotonic()
    urllib.request.urlopen(req, timeout=timeout).read()
    up = up_bytes * 8 / (time.monotonic() - t0)
    return {"latency_ms": round(lat[len(lat) // 2], 1), "jitter_ms": round(lat[-1] - lat[0], 1),
            "download_bps": round(down), "upload_bps": round(up)}


def fetch_status(router, timeout):
    t0 = time.monotonic()
    with urllib.request.urlopen(f"http://{router}/api/v1/status", timeout=timeout) as r:
        body = json.load(r)
    return body, (time.monotonic() - t0) * 1000.0


def b(v):
    return 1 if v else 0


def build_metrics(router, status, latency_ms, error=None):
    now = int(time.time() * 1000)
    common = common_attrs(router)
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
    g = gauge
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


def collect_status(args):
    router = args.router
    try:
        status, latency = fetch_status(router, args.timeout)
        payload = build_metrics(router, status, latency)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return build_metrics(router, None, 0, error=e)
    try:
        uptime = status.get("system", {}).get("uptime", 0)
        payload[0]["metrics"] += throughput_metrics(router, uptime, args.timeout)
    except (urllib.error.URLError, OSError, ValueError, KeyError, ET.ParseError) as e:
        print(f"UPnP counters unavailable: {e}", file=sys.stderr)
    return payload


def collect_speedtest(args):
    attrs = common_attrs(args.router)
    attrs["test"] = "cloudflare"
    try:
        r = speedtest(args.down_mb * 1_000_000, args.up_mb * 1_000_000, 60)
        metrics = [gauge("nest.speedtest.ok", 1)] + [gauge(f"nest.speedtest.{k}", v) for k, v in r.items()]
    except (urllib.error.URLError, OSError) as e:
        attrs["error"] = str(e)[:200]
        metrics = [gauge("nest.speedtest.ok", 0)]
    return [{"common": {"timestamp": int(time.time() * 1000), "attributes": attrs}, "metrics": metrics}]


def run_once(args):
    payload = collect_speedtest(args) if args.speedtest else collect_status(args)

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
    print(f"sent HTTP {code} " + " ".join(f"{k.split('.', 1)[1]}={v}" for k, v in summary.items()))
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--router", default=os.environ.get("NEST_ROUTER", "192.168.86.1"))
    p.add_argument("--region", default=os.environ.get("NR_REGION", "US").upper(), choices=NR_ENDPOINTS)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--interval", type=int, default=0, help="loop every N seconds (0 = run once)")
    p.add_argument("--speedtest", action="store_true", help="run active Cloudflare speed test instead")
    p.add_argument("--down-mb", type=int, default=25, help="speedtest download size (MB)")
    p.add_argument("--up-mb", type=int, default=10, help="speedtest upload size (MB)")
    p.add_argument("--dry-run", action="store_true", help="print payload, don't send")
    args = p.parse_args()

    if args.interval <= 0:
        return run_once(args)
    while True:
        run_once(args)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
