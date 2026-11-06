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
| `nest.wan.rx_bps`, `nest.wan.tx_bps` | household WAN throughput since the last run (UPnP counters, passive) |
| `nest.wan.rx_bytes_total`, `tx_bytes_total` | raw UPnP counters (wrap at 4 GiB, reset on router reboot) |
| `nest.speedtest.download_bps`, `upload_bps` | `--speedtest`: Cloudflare 25 MB down / 10 MB up from the Pi |
| `nest.speedtest.latency_ms`, `jitter_ms`, `ok` | median / spread of 5 requests on a warm HTTPS connection |

Attributes on every point: `router`, `host`, `modelId`, `softwareVersion`, `updateStatus`, `ledAnimation`,
`setupState`, `wanIp`, `wanGateway`, `dnsMode`.

Throughput needs UPnP on (Google Home app → Wi-Fi → Settings → Advanced networking → UPnP). The speed test
measures the Pi's own path, so on Pi Wi-Fi it caps at the Pi's link; use ethernet for line speed.

## Test

```sh
python3 nest_status.py --dry-run
NR_LICENSE_KEY=... NR_REGION=EU python3 nest_status.py
python3 nest_status.py --dry-run; sleep 30; python3 nest_status.py --dry-run   # 2nd run adds rx_bps/tx_bps
python3 nest_status.py --speedtest --dry-run
```

## Install on the Pi

```sh
# 1 = copy code
sudo install -D -m755 nest_status.py /opt/nest-status/nest_status.py
# 2 = config (fill NR_LICENSE_KEY, NR_REGION)
sudo install -m600 nest-status.env.example /etc/nest-status.env && sudo nano /etc/nest-status.env
# 3 = units
sudo cp systemd/nest-*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nest-status.timer nest-speedtest.timer
# 4 = check
sudo systemctl start nest-status.service; journalctl -u nest-status -n 5
```

## NRQL

```sql
SELECT latest(nest.wan.online), latest(nest.router.up) FROM Metric WHERE service='nest-wifi-status' TIMESERIES
SELECT max(nest.system.uptime_s)/3600 AS 'uptime h' FROM Metric WHERE service='nest-wifi-status' TIMESERIES
SELECT average(nest.wan.rx_bps)/1e6 AS 'down Mbps', average(nest.wan.tx_bps)/1e6 AS 'up Mbps' FROM Metric WHERE service='nest-wifi-status' TIMESERIES
SELECT latest(nest.speedtest.download_bps)/1e6, latest(nest.speedtest.upload_bps)/1e6, latest(nest.speedtest.latency_ms) FROM Metric WHERE test='cloudflare' TIMESERIES 1 hour
SELECT latest(softwareVersion) FROM Metric WHERE service='nest-wifi-status' FACET router
```

Alert: `nest.wan.online < 1` or `nest.router.up < 1` for 3 min. Note: if the WAN is down, the Pi can't reach New
Relic either, so pair it with a **loss-of-signal** alert on `nest.router.up` (no data for 5 min = outage).

## License

MIT, see [LICENSE](LICENSE).
