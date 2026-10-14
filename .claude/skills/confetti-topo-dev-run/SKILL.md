---
name: confetti-topo-dev-run
description: Run confetti-topo locally on a Windows dev machine — start the server on a mock lab or a real router, set SSH credentials, poll, look at the topology. Use when asked to start, run, or test the app locally.
origin: confetti-topo
---

# Running confetti-topo locally

For the routes see `confetti-topo-api`. This skill only covers the local dev loop.

## Start the server

Port 80 needs admin rights on Windows, so use 8080. Run in the background:

```sh
cd topo && TOPO_PORT=8080 python serve.py
```

- The database is `topo.db` in the working directory (`TOPO_DB_PATH` overrides). It holds plaintext device
  credentials; it is git-ignored. Check it is up: `curl http://127.0.0.1:8080/api/health`.
  `"rc-service not installed"` for every service is normal on Windows.
- **Stopping on Windows:** stopping the background shell leaves `python serve.py` running and holding the
  port. Find it with `Get-NetTCPConnection -LocalPort 8080 -State Listen` and kill that PID before
  restarting. If an old server is still up, it (not the new code) answers requests.
- Flask caches templates: restart after editing `templates/`.

## A fake lab, no routers needed

```sh
python scripts/seed_mock_lab.py /tmp/mock.db        # refuses an existing file
TOPO_DB_PATH=/tmp/mock.db TOPO_PORT=8080 TOPO_PING_ENABLED=false python serve.py
```

Three routers, LLDP links, BGP/OSPF sessions (some resolve by interface IP, some stay ghosts), an unknown
LLDP neighbor and a few events. `next_poll_at` is far in the future, so nothing dials the fake addresses.
**Set `TOPO_PING_ENABLED=false`**: Windows `ping` exits 0 on some "unreachable" replies, which would flip the
mock devices to "answering ping".

## A real router

1. Add: `POST /api/devices` with `{"mgmt_ip": "...", "vendor": "cisco", "platform": "cisco_ios"}`.
   The key is `mgmt:<ip>` until the first poll finds a serial.
2. Credentials: `PUT /api/devices/<id>/credentials` with username/password/enable_secret. Write the JSON to
   a temp file and send it with `--data @file` (passwords often contain `!` or `$`). Delete the file after.
3. Poll now: `POST /api/devices/<id>/poll` (synchronous, up to about a minute). Returns ok/error per task
   (version, interfaces, lldp, bgp, ospf).
4. Look: `GET /api/devices/<id>`, `/api/lldp-neighbors?device_id=<id>`, `/api/adjacencies?device_id=<id>`,
   then the `/topology` page.

Real lab addresses and hostnames are in the auto-memory file `reference_lab_routers.md` (kept in the
confetti-butler project's memory folder); never commit them. Credentials come from the user in chat, every
session. A router with LLDP off and no BGP/OSPF succeeds with empty results: that is expected, not a bug.
