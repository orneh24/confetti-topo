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

**Pages:** `/` `/devices` `/devices/<id>` `/topology` `/conflicts` `/settings`

**Devices:**
- `GET|POST /api/devices` — POST goes through `identity.ingest`
- `GET|PUT|DELETE /api/devices/<id>` — PUT is an authoritative operator edit (bypasses the ladder, records new
  `serial`/`mgmt_ip`/`hostname` as aliases; a value another device owns comes back in `alias_warnings`)
- `POST /api/devices/<id>/poll` — synchronous, inline on the request thread
- `GET /api/devices/<id>/interfaces`; `GET|PUT /api/devices/<id>/credentials` (GET never returns a secret,
  only `*_set` booleans; PUT is partial, `""` clears one)
- `GET|PUT /api/settings/credentials` — server-wide defaults (the `settings` table; GET shows `*_source`:
  settings/env/none). Order per field: device row > settings > `TOPO_SSH_*` env, empty falls through
- `GET /api/credentials` (every non-node device with `has_override`, `username`, `password_set`, `enable_secret_set`, `updated_at`; no secrets); `POST /api/credentials/bulk`
  `{device_ids, username?, password?, enable_secret?}` (one transaction, an unknown id applies nothing);
  `POST /api/credentials/clear {device_ids}`
- `POST /api/devices/<id>/merge`; `GET /api/conflicts?resolved=0|1`; `POST /api/conflicts/<id>/dismiss`

**Discovery:** `POST /api/discover/{sweep,seedfile,confetti}` return `{device_ids, conflicts}`.
`POST /api/discover/run` runs the scheduled sources now (`TOPO_DISCOVERY_*`); `GET /api/discover/status`.
`GET /api/lldp/unknown`; `POST /api/lldp/adopt {ip, hostname?}` adds one with polling OFF (`enabled=0`).

**Topology:** `GET /api/topology?layer=lldp|bgp|ospf` (vis.js `{nodes, edges}`, default overlays all three),
`GET /api/adjacencies[?device_id=]`, `GET /api/lldp-neighbors[?device_id=]`. The last two return the
**effective** `peer_device_id` / `remote_device_id`.

**Events and metrics:** `GET /api/events` (`device_id`, `kind`, `severity`, `hours`, `limit`); `GET /metrics`
(Prometheus text, hand-written, labels escaped).

**Health:** `GET /api/time` (chrony), `GET /api/health` (services, poller_running, reach_running, discovery,
load/memory/disk/uptime) — both never 500; every check degrades to `null`/`"unknown"`.

## Poller scheduling

`devices.poll_state` (`idle`/`running`) is the per-device lock; `next_poll_at` is the schedule. Each tick
claims due devices, marks them `running`, and runs five tasks per device — `version`, `interfaces`, `lldp`,
`bgp`, `ospf` — over one SSH session, each committing its own transaction. Rows a *successful* task no longer
sees are deleted; a failed task changes nothing. `running` is released in a `finally` and reset at startup.
`role='node'` devices are excluded from the claim query. Success: `fail_count=0`,
`next_poll_at = now + poll_interval_s`. Failure: `fail_count += 1`,
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
- confetti-topo-ui (authoring the pages)
