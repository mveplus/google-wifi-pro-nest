# nest-status

Polls the Google Nest Wifi / Wifi Pro local API (`http://<router>/api/v1/status`, no auth, LAN only)
from a Raspberry Pi and pushes gauges to the New Relic Metric API. Python 3 stdlib only.

## Metrics

| metric | meaning |
|---|---|
| `nest.router.up` | 1 = API answered, 0 = unreachable (`error` attribute set) |
| `nest.router.api_latency_ms` | status call round trip |
| `nest.system.uptime_s` | router uptime (drops = reboot) |
| `nest.system.lan0_link`, `nest.system.led_intensity` | |
| `nest.wan.online`, `ethernet_link`, `has_ip`, `captive_portal`, `invalid_credentials` | 0/1 |
| `nest.software.update_required`, `update_progress` | |

Attributes on every point: `router`, `host`, `modelId`, `softwareVersion`, `updateStatus`, `ledAnimation`,
`setupState`, `wanIp`, `wanGateway`, `dnsMode`.

## Test

```sh
python3 nest_status.py --dry-run
NR_LICENSE_KEY=... NR_REGION=EU python3 nest_status.py
```

## Install on the Pi

```sh
# 1 = copy code
sudo install -D -m755 nest_status.py /opt/nest-status/nest_status.py
# 2 = config (fill NR_LICENSE_KEY, NR_REGION)
sudo install -m600 nest-status.env.example /etc/nest-status.env && sudo nano /etc/nest-status.env
# 3 = units
sudo cp systemd/nest-status.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nest-status.timer
# 4 = check
sudo systemctl start nest-status.service; journalctl -u nest-status -n 5
```

## NRQL

```sql
SELECT latest(nest.wan.online), latest(nest.router.up) FROM Metric WHERE service='nest-wifi-status' TIMESERIES
SELECT max(nest.system.uptime_s)/3600 AS 'uptime h' FROM Metric WHERE service='nest-wifi-status' TIMESERIES
SELECT latest(softwareVersion) FROM Metric WHERE service='nest-wifi-status' FACET router
```

Alert: `nest.wan.online < 1` or `nest.router.up < 1` for 3 min. Note: if the WAN is down, the Pi can't reach New
Relic either, so pair it with a **loss-of-signal** alert on `nest.router.up` (no data for 5 min = outage).
