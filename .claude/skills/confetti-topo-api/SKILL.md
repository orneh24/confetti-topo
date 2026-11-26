---
name: confetti-topo-api
description: confetti-topo's own HTTP contract — device identity/merge rule, poller scheduling, topology graph and peer resolution, LLDP crawl, events. Read before changing app/app.py, identity.py, poller.py, topology.py, collectors/ssh.py or any schema in db.py.
origin: confetti-topo
---

# confetti-topo API

The HTTP contract, schema and data-flow rules for confetti-topo's server. The reference for its own
routes and internals, not for network devices in general (see `cisco-ios-patterns`,
`network-bgp-diagnostics`, `netmiko-ssh-automation` for that).

## When to Activate

- Adding or changing a route in `app/app.py`
- Changing `identity.py`, `poller.py`, `topology.py`, `collectors/ssh.py` or the schema in `db.py`
- Debugging why a device didn't dedupe, a poll didn't run, or a neighbor shows as a ghost
- Adding a new seeding/discovery source

## Device identity — do not break without understanding this first

`devices.key` comes from a strength ladder: `serial` (4) > `vmuuid` (3) > `mgmt_ip` (2) > `hostname` (1).
Every identifier ever observed lives in `device_aliases` permanently — a history, not current state. A key
only ever promotes upward.

Two entry points, not interchangeable:
- `identity.ingest(conn, candidates, fields, source, ref)` — discovery paths (manual, sweep, seedfile,
  confetti-traffic import, LLDP adopt). Resolves the device by alias-matching from scratch.
- `identity.observe(conn, device_id, candidates, fields, source, ref)` — the poller only. The device_id is
  already known, so it skips alias-matching and only guards `serial`/`vmuuid` disagreeing with what is on
  file. Calling `ingest` from the poller with just a `serial` would create a duplicate device.

Both raise `identity.Conflict` on ambiguity and write a `merge_conflicts` row first. **Nothing auto-merges.**
`POST /api/devices/<survivor_id>/merge` with `{"loser_id": N}` re-points every child table
(`device_aliases`, `device_sources`, `credentials`, `interfaces`, `lldp_neighbors`, `adjacencies`, and the
`remote_device_id` / `peer_device_id` columns) at the survivor and deletes the loser. Deleting a device sets
those two columns to NULL, so its neighbors become ghosts again.

## Routes

**Pages:** `/` `/devices` `/devices/<id>` `/topology` `/conflicts` `/settings` `/syslog` `/changes` `/ipam` `/templates` `/backups` `/find`

**IPAM, templates, config backups (routes in `app/tools_routes.py`):**
- `GET /api/devices/<id>/drift|interface-stats|configs`, `/configs/<vid>`, `/configs/diff` (`?raw=1` unmasks); `GET|PUT /api/devices/<id>/vars`
- `GET /api/ipam/addresses|prefixes|findings`, `POST /api/ipam/analyze`
- `GET|POST /api/templates`, `GET|PUT|DELETE /api/templates/<name>`, `/versions`, `POST /preview`
- `GET /configs/` and `GET /configs/<key>.cfg` — unauthenticated on purpose (constraint 28)
- `POST /api/discover/vcenter`; `GET|PUT /api/settings/flap-display` (`{count, window_min}`, the Recent changes flapping mark)

**Devices:**
- `GET|POST /api/devices` — POST goes through `identity.ingest`
- `GET|PUT|DELETE /api/devices/<id>` — PUT is an authoritative operator edit (bypasses the ladder, records new
  `serial`/`mgmt_ip`/`hostname` as aliases; a value another device owns comes back in `alias_warnings`)
- `POST /api/devices/<id>/poll` — synchronous, inline on the request thread; asks every task, also one marked not supported.
  A result with `unsupported: true` is not a failure (constraint 43)
- Both device GETs add `failed_tasks`; `GET /api/devices/<id>` also adds `tasks` (`task`, `state` ok/failed/unsupported, `error`,
  `at`, `last_ok_at`; table `task_status`)
- `GET /api/vpn[?device_id=N]` — IPsec tunnels (`vpn_tunnels`); `peer_device_id` / `peer_hostname` are looked up at query time
- `GET /api/find?q=` — IP, prefix, MAC or text over the stored tables (read-only); `GET /api/backups`, `/api/backups/search?q=&regex=1&invert=1` (masked)
- `GET /api/backup` (list), `POST /api/backup/download {name?}`, `GET /api/backup/env` (secrets blanked), `POST /api/backup/restore` (multipart `file`)
- `POST /api/devices/<id>/credentials/test` — log in and run `version` only, stores nothing
- `GET|PUT /api/settings/links` (the Traffic URL), `GET|PUT /api/settings/syslog-pull`, `GET /api/syslog[?after_id=N&origin=local]`, `GET /api/syslog/sources`
- `GET|PUT /api/topology/notes` — map notes per node or link
- `PUT /api/devices/<id>/display-name {name}` — the name the map shows (empty = hostname; node `label`, with `hostname` beside it). A label only (constraint 46)
- `GET /api/routes|vlans|stp|trunks|mac?device_id=N` — one device's routing table (`vrf`, `kind`, `prefix`), VLANs, spanning-tree summary,
  trunk ports, learned MACs (`q` filters; a MAC owned by one other device carries `device`/`interface`)
- `GET /api/topology/snapshots[?limit=]` and `/api/topology/snapshots/<id>` — read-only topology history (`TOPO_SNAPSHOT_*`)
- `TOPO_AUTH_TOKEN` (off by default): pages go to `/login`, `/api/*` and `/metrics` answer 401 without a cookie, `Authorization: Bearer` or `X-Topo-Token`; `/api/health` and `/static` stay open
- `GET /api/devices/<id>/interfaces`; `GET|PUT /api/devices/<id>/credentials` (GET never returns a secret,
  only `*_set` booleans; PUT is partial, `""` clears one)
- `GET|PUT /api/settings/credentials` — server-wide defaults (the `settings` table; GET shows `*_source`:
  settings/env/none). Order per field: device row > settings > `TOPO_SSH_*` env, empty falls through
- `GET|PUT /api/settings/lldp-matching` — `{chassis, name}` booleans (default off; GET adds `source`: settings/env;
  PUT is partial and refuses non-booleans). Optional LLDP match by chassis ID = `devices.mgmt_mac`, or system name = hostname
- `GET /api/credentials` (every non-node device with `has_override`, `username`, `password_set`, `enable_secret_set`, `updated_at`; no secrets); `POST /api/credentials/bulk`
  `{device_ids, username?, password?, enable_secret?}` (one transaction, an unknown id applies nothing);
  `POST /api/credentials/clear {device_ids}`
- `POST /api/devices/<id>/merge`; `GET /api/conflicts?resolved=0|1`; `POST /api/conflicts/<id>/dismiss`

**Discovery:** `POST /api/discover/{sweep,seedfile,confetti,traffic}` return `{device_ids, conflicts}`
(`hub_url` may be a bare IP; `http://` is added; `traffic` takes no body and uses the URL saved on Settings, 400 if none, 502 if the hub fails).
`GET|PUT /api/settings/traffic-import` `{interval_min}` sets the opt-in re-import timer (0 = off, else 5..1440); GET returns `{url, interval_min, status}`.
`POST /api/discover/run` runs the `TOPO_DISCOVERY_*` sources now; `GET /api/discover/status` (neither covers the saved-URL timer).
`GET /api/lldp/unknown`; `POST /api/lldp/adopt {ip, hostname?}` adds one with polling OFF (`enabled=0`).

**Topology:** `GET /api/topology?layer=lldp|bgp|ospf|ipsec|default|connected[,..]` (`topology.LAYERS`; vis.js `{nodes, edges}`; each edge has `details`, one report per end that saw it; no `layer` = all six),
`PUT` / `DELETE /api/topology/positions` (saved node places; nodes carry `x`/`y` when saved, and the response
also has `positions` {node_id: {x, y}}, including the page-only `group:dev:<id>`),
`GET /api/adjacencies[?device_id=]`, `GET /api/lldp-neighbors[?device_id=]`. The last two return the
**effective** `peer_device_id` / `remote_device_id` (`/api/lldp-neighbors` also has `matched_by`: null, `chassis` or `name`).

**Events and metrics:** `GET /api/events` (`device_id`, `kind`, `severity`, `hours`, `limit`; each row also has
`type`, `change`, `item`, `before`, `after`, derived at read time by `events.describe()`, no schema change); `GET /metrics`
(Prometheus text, hand-written, labels escaped).
**BGP flapping:** `/api/adjacencies` rows and `/api/topology` edges carry `flapping` (bool); events `bgp_reset`,
`bgp_flapping`, `bgp_flap_stopped` (`app/flap.py`, `TOPO_FLAP_COUNT`, `TOPO_FLAP_WINDOW_S`, table `bgp_changes`).

**Health:** `GET /api/time` (chrony), `GET /api/health` (services, poller_running, reach_running, discovery,
load/memory/disk/uptime) — both never 500; every check degrades to `null`/`"unknown"`.

## Poller scheduling

`devices.poll_state` (`idle`/`running`) is the per-device lock; `next_poll_at` is the schedule. Each tick
claims due devices, marks them `running`, and runs the platform's tasks, each committing its own transaction.
IOS over one SSH session: `version`, `interfaces`, `lldp`, `bgp`, `ospf`, `routes`, `vpn`, `config`, and on
switches `vlans`, `trunks`, `stp`, `mac`. PAN-OS over the XML API: the first seven. SNMP: `version`,
`interfaces`. A `confetti_node` (`hub_api`, asked through its hub): `version`, `interfaces`, `lldp`. Rows a
*successful* task no longer sees are deleted; a failed task changes nothing. IOS `% Invalid input` to `vpn` is
`unsupported`: left out of scheduled polls for `TOPO_UNSUPPORTED_RECHECK_H` hours (24, 0 = never left out). `running` is released in a
`finally` and reset at startup. The claim query takes `role != 'node' OR platform = 'confetti_node'`. Success:
`fail_count=0`, `next_poll_at = now + poll_interval_s`. Failure: `fail_count += 1`,
`next_poll_at = now + min(interval * 2**fail_count, POLL_MAX_BACKOFF_S)`.

## Topology and peer resolution

`topology.build(conn, layer)`. Poll-time match (alias table, by advertised management IP or peer IP) is stored
in `remote_device_id` / `peer_device_id`. Anything NULL gets one more lookup at query time in
`topology.interface_owners` (an address on exactly one device's interface). Ambiguous addresses, a device's own
address and unknown peers stay ghosts. **Never write the interface lookup back as an alias.** A cable or
session both ends report is one edge, down if either end says so; ghost links are not de-duplicated.

## Anti-Patterns

```python
# BAD: a new discovery source writing devices directly
conn.execute("INSERT INTO devices (...) VALUES (...)")
# GOOD: identity.ingest

# BAD: the poller calling identity.ingest -> duplicate device. Use identity.observe.

# BAD: storing a timestamp as ISO-8601 (datetime.now().isoformat())
# GOOD: db.sqlite_now() in, db.iso() out (CLAUDE.md constraint 4)

# BAD: applying a failed task's empty result (deletes the topology on a timeout)
# GOOD: only ok=True tasks reach ssh.apply_result (constraint 14)

# BAD: storing an interface IP as a device alias to make a peer resolve
# GOOD: leave it a query-time lookup (constraint 12)

# BAD: an inline <script> in a Flask template with literal {{ }} -> 500. Wrap in {% raw %}.
```

## Related Skills

- netmiko-ssh-automation (the collector's connection and error handling)
- network-bgp-diagnostics (BGP state names, `Idle (Admin)`)
- cisco-ios-patterns (command syntax)
- ipsec-vpn (the `vpn` task, both platforms)
- panos-xml-api
- confetti-topo-ui (authoring the pages)
