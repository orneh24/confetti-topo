# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A simplistic server that maps a network from what its devices say about themselves: LLDP neighbors
(cables) and BGP/OSPF adjacencies (sessions), drawn as a topology graph. Infrastructure is ESXi 7.0
with Cisco CSR1000v routers initially; other vendors later.

confetti-topo was split out of `../confetti-butler` on 2026-10-14. Butler keeps inventory, IPAM, config
templates, syslog and alerting; this project owns *what is connected to what*. It is **fully standalone**:
its own device inventory, credentials, discovery, ICMP checker, SSH poller, LLDP crawl, events and graph UI.
It shares no database or API with butler. Two sibling repos stay related:
- `../confetti-traffic` (network connectivity test harness) — confetti-topo can import its node fleet
  (`GET /endpoints`) as `role='node'` devices, and copies its hub UI look by hand.
- `../confetti-butler` — the code in `topo/app/identity.py`, `poller.py`, `reach.py`, `discovery.py`,
  `collectors/` and `parsers/ios.py` started as copies of butler's. They are separate now; a fix in one is
  not automatically a fix in the other (the `confetti-interop-checker` agent looks for drift).

Reference notes for the feature itself: `docs/TOPOLOGY-MAPPING.md` (design, pitfalls, test checklist).

**SSH is read-only.** confetti-topo never writes to a device: only `show` commands. There is no
config-push path and none should be added without being asked for explicitly (regression check R19 fails
on any command that does not start with `show `).

## Architecture

### Server VM (standalone)
- Alpine Linux VM, ESXi 7.0 + vCenter, `open-vm-tools`
- Runs:
  - Flask API served by **waitress**, not the Flask dev server (single-threaded — the poller's own
    requests and the dashboard would queue behind each other)
  - SQLite database (WAL mode) at `/var/lib/confetti-topo/topo.db`
  - Web UI on port 80
  - A background **poller** thread pool (device SSH polling), an **ICMP checker** thread and an optional
    **discovery** timer thread
- SSH server is OpenSSH (`sshd`, with `openssh-sftp-server`), not Dropbear (switched 2026-10-14, same as
  confetti-butler: Dropbear has no SFTP and drops large packets). `/etc/ssh/sshd_config.d/10-confetti.conf` keeps
  root password login on and sets `MaxStartups 100:30:200`; only an ed25519 host key is generated.
  `build-template.sh` `switch_ssh_server()` (called last, above the `--update` exit) moves a VM that still has
  Dropbear over: converts its ed25519 host key, stops Dropbear, starts sshd, and only then removes Dropbear and
  rewrites `dropbear` to `sshd` in `topo.env`'s health list; if sshd does not start Dropbear is started again.
  Rollback does not undo it. R27 checks it.
- Installed to `/opt/confetti-topo/`, started by OpenRC service `confetti-topo`
- Entrypoint is `serve.py` — reads `TOPO_PORT` at runtime. Do not move the port into the init script's
  `command_args`: OpenRC expands that at parse time, before `start_pre` sources `topo.env`, so the setting
  would be ignored (same constraint as confetti-traffic's hub).

### The poller — server-initiated pull

confetti-topo dials out to devices over SSH rather than waiting for them to report in. One daemon thread
(`app/poller.py`), started by `serve.py` beside `reach.start()`, idempotent — `start()` returns quietly if
already running.

Each tick (`TOPO_POLL_TICK_S`, default 15s): claim devices with `poll_state='idle'` and
`next_poll_at <= now`, mark them `'running'` (this is the per-device lock — a device whose SSH hangs is never
queued twice), and hand each to a `ThreadPoolExecutor` (`TOPO_POLL_WORKERS`, default 8).

Per device, five tasks run independently, **each committing its own transaction**: `version`, `interfaces`,
`lldp`, `bgp`, `ospf`. Partial failure is the normal case — an LLDP timeout must never discard interface data
collected 200ms earlier. `role='node'` devices (confetti-traffic's imported Alpine fleet) are never polled —
`poller.py`'s device-selection query filters `role != 'node'`.

All five tasks share **one SSH session** per poll (`ssh.run_tasks` is a generator, so each task still commits
before the next command runs). Each apply step deletes that device's rows not seen in this poll — a removed
peer or cable must not stay on the topology forever. But **only a successful task deletes**: a timeout or a
failed login applies nothing (constraint 14). `interfaces` skips the delete on an empty result, since empty
there means a parse failure, not a router with no interfaces; `lldp`, `bgp` and `ospf` do delete on an empty
result ("LLDP is not enabled" is a real answer). The per-device `'running'` lock is always released in a
`finally`, and `poller.start()` resets any `'running'` rows left by a server that stopped mid-poll.

Backoff on failure: `next_poll_at = now + min(interval * 2**fail_count, TOPO_POLL_MAX_BACKOFF_S)`.

Commands and parsers (`app/collectors/ssh.py`, `app/parsers/ios.py`) use **pure regex, not
`use_textfsm=True`** — deliberately, to avoid a hard dependency on the `ntc-templates` package, whose Alpine
availability is unverified (same caution as confetti-traffic CLAUDE.md constraint 14). Confirmed against a
real IOS-XE 17.3 CSR1000v and a 15.4 router; see constraint 3 for command-name gotchas found that way.

`app/platforms.py` maps a device's `platform` to its commands and parsers: `cisco_ios` / `cisco_xe` only. An
unknown platform falls back to the IOS entry. Junos is planned, not started. There is no SNMP collector.

### Topology

`app/topology.py` builds `GET /api/topology`, a vis.js-shaped `{nodes, edges}` graph over LLDP neighbors and
BGP/OSPF adjacencies (`?layer=lldp|bgp|ospf`, or a comma list, limits it; the page's LLDP/BGP/OSPF buttons are independent toggles). A neighbor/peer that does not resolve to a known
device renders as a **ghost node** (`ghost:lldp:<sysname|chassis>`, or `ghost:peer:<ip>` shared by BGP and
OSPF) rather than being dropped — an edge to nowhere reads as a bug, a distinctly styled ghost reads as
"something is out there, unidentified". A cable or BGP/OSPF session both ends report is drawn once (a
session is down if either end says so); ghost links are never de-duplicated (constraint 13). Two real sessions
between the same pair of routers (loopback and link address) stay two edges, paired by position with
Established first — a simple rule, wrong only when the two ends list them in a different order.

The page keeps the map still and readable: physics places only nodes with no saved place, then stops.
Places are stored in `node_positions` (`PUT`/`DELETE /api/topology/positions`, the same map for every viewer;
`GET /api/topology` returns `x`/`y` per node and a `positions` dict). Unknown nodes whose links all go to one
known device are drawn as one "N unknown" node (`group:dev:<id>`, made up by the page, so only `positions`
knows it); double-click expands it. An LLDP link shows its port names only on hover or when selected.
BGP/OSPF labels always show. `/topology?device=<id>` selects and centres on that device once (the Recent changes page links there).

Who is that neighbor? Two steps:
1. **At poll time**, the apply step matches the management IP an LLDP neighbor advertises (`remote_mgmt_ip`),
   or a BGP/OSPF `peer_ip`, against the `mgmt_ip` aliases and stores `remote_device_id` / `peer_device_id`.
   See constraint 3 for why not chassis ID.
2. **At query time**, anything still NULL is looked up in `interfaces.ip`: an address configured on
   **exactly one** device resolves to it (`topology.interface_owners` / `resolve`). This is what lets most
   BGP/OSPF peers resolve, since a peer IP is usually a loopback or point-to-point address, not a management
   IP. An address on two devices (HSRP/VRRP, a reused lab IP) stays a ghost, a device's own address never
   resolves to itself, and the poller's stored match always wins. It is **never written back as an alias**
   (constraint 12).

NULL after both steps is the expected result for peers outside the lab, not a bug.

### Reachability

`app/reach.py` (a daemon thread, `TOPO_PING_*`) pings every enabled device with a `mgmt_ip`, including
`role='node'`, and keeps `devices.reachable` (1 / 0 / NULL = not checked). A device is marked down after
`TOPO_PING_FAILS_TO_DOWN` (2) failures in a row; the first result is a baseline and emits no event.
`_ping` refuses any string that is not an IP address before it reaches a subprocess. Shown on the Devices
and device pages and as a red outline on the topology; BGP/OSPF sessions that are not Established / FULL
are drawn red and dashed.

### Discovery on a timer, and the LLDP crawl

- `app/discovery.py` runs the confetti-traffic and confetti-butler imports, subnet sweeps and the seed file every
  `TOPO_DISCOVERY_INTERVAL_S` seconds (default 0 = never; nothing runs unless configured via
  `TOPO_DISCOVERY_CONFETTI_URL` / `_BUTLER_URL` / `_SWEEP_CIDRS` / `_SEEDFILE`). Each source commits on its own, so one
  failing source never discards another's work. A device that did not exist before gets a
  `device_discovered` event. `POST /api/discover/run` runs it once by hand. There is no vCenter source.
- `app/lldp_crawl.py`: an LLDP neighbor advertising a management IP no device owns is listed by
  `GET /api/lldp/unknown` and added by `POST /api/lldp/adopt` (or automatically with
  `TOPO_LLDP_AUTO_ADOPT=true`, default false). It goes through `identity.ingest`, and the new device is
  created with `enabled=0`: inventory only, never polled until an operator sets credentials and enables it.

### Change events

`app/events.py` `emit()` writes a row to `events` (`TOPO_EVENT_RETENTION_DAYS`, default 30, pruned in the
poller tick). Callers are the poller's apply steps, which hold the old state just before they overwrite it:
interface oper up/down, BGP/OSPF peer state/added/removed, LLDP neighbor added/removed, OS version change, and
`poller._finish` for `device_unreachable` / `device_recovered`. `emit()` never commits — the event shares the
transaction of the change it describes. `events.is_baseline()` is true until a task has an `ok=1` row in
`poll_history`; while true, nothing is emitted, so adding a device doesn't produce one event per interface/peer.
Read via `GET /api/events` (`device_id`, `kind`, `severity`, `hours`, `limit`); shown on the dashboard (last 15)
and each device page. **There is no alerting** (butler has it); events are only displayed.

### Device identity and the merge rule

Four independent ingest paths can all observe the same physical device: manual add, a subnet sweep, a YAML
seed file, and confetti-traffic's `GET /endpoints` (plus the LLDP crawl). `app/identity.py` is the **single
place** that decides whether an observation is a new device, an update to a known one, or an ambiguous
collision — nothing else should `INSERT INTO devices` directly.

`devices.key` is derived from a strength ladder, strongest identifier wins: `serial` > `vmuuid` > `mgmt_ip` >
`hostname`. A key is only ever **promoted** to a stronger identifier, never demoted. Every identifier ever
observed is kept permanently in `device_aliases` (not just current state) — this is what makes cross-source
dedup robust, at the cost of a decommissioned device's IP getting reassigned to a genuinely different device
initially surfacing as a conflict rather than silently merging. That is the intended, safe failure mode.

Two entry points:
- **`identity.ingest(conn, candidates, fields, source, ref)`** — used by every *discovery* path. Ambiguity
  (>1 matched device, or a stronger identifier disagreeing with what's on file) writes a `merge_conflicts`
  row and raises `identity.Conflict` rather than guessing.
- **`identity.observe(conn, device_id, candidates, fields, source, ref)`** — used by the *poller*, which
  already knows the device_id. Only guards against a hardware-identity field (`serial`, `vmuuid`)
  disagreeing with what's on file. `mgmt_ip`/`hostname` drift discovered by polling updates freely.

Ambiguity is **never** auto-resolved. The operator merges via `POST /api/devices/<id>/merge` (see the
`/conflicts` page), which re-points every child table at the survivor and deletes the loser.

### Updating a deployed VM, backups, first boot

The login banner compares the installed `COMMIT` file (written by a no-argument update) with GitHub's newest commit and says whether a newer build exists (3 s timeout, so no internet only prints a notice). `topo-update.sh` with no arguments downloads the main branch tarball from GitHub (`TOPO_UPDATE_URL` overrides) and updates without asking; `topo-update.sh [-y] <topo-dir|tarball>` (also `--rollback`; with a dir/tarball and no `-y` it shows the installed build and asks,
a closed stdin cancels) runs the new tree's `build-template.sh --update` (the build's own install steps, so no
second file list: apk packages, code into `/opt/confetti-topo.new` via `TOPO_STAGE_DIR`, OpenRC/cron/login
scripts, motd; `--update` skips the root password, `topo.env`, host-key/DB/log clean-up and zero-fill). It then
import-checks the staged tree against a throwaway DB *before* stopping anything, backs up the DB with `sqlite3 .backup`
(`/var/lib/confetti-topo/backups`, 0700, newest 5 kept), swaps `/opt/confetti-topo` (old one kept as `.prev`),
restarts, waits for `/api/health` and swaps back by itself if it never comes up. `topo.env` is carried over;
the DB is not restored on rollback (new versions only add tables/columns). Rollback restores the app code
only: packages and the OpenRC/cron scripts are written before the swap and stay new. pip packages are
installed only if missing, so a changed pin is not applied. The build writes `/opt/confetti-topo/BUILD` (a hash
of the code plus the install time); `login-setup.sh` prints it at every interactive login and, on a configured
VM, how to re-run setup and re-configure the static IP. `requirements.txt` holds exact tested versions.

`services/topo-backup.sh` is installed as `/etc/periodic/daily/topo-backup` (no dot in the name — run-parts
skips those; `crond` is enabled by the build): `sqlite3 .backup` nightly, newest `TOPO_BACKUP_KEEP` (7) kept.
`firstboot.initd` applies `topo.ip`/`topo.gateway` (and optional `topo.dns`/`topo.hostname`; the hostname is
re-applied on every boot; `set-static-ip <ip/cidr> <gateway> [dns] [hostname]` and the wizard take the same
two) guestinfo and, once per clone, `topo.ssh_username`,
`ssh_password`, `ssh_secret` and `poll_interval_s` to `topo.env`. Guestinfo is visible to anyone who can see
the VM's advanced settings in vCenter. `topo.env` is 0600.

**Container:** `container/Dockerfile` + `docker-compose.yml` (context `topo/`): same Alpine packages and
pinned pip packages as the VM, `TOPO_HEALTH_SERVICES` emptied (no OpenRC). `container/docker-compose.dev.yml` is the dev setup: `topo/` mounted live (restart the
service after a change, no rebuild), `--profile mock` = seeded fake lab on 8080, `--profile lab` = empty DB on 8081.
There is no Packer template.

## Seeding paths (device discovery)

| Path | Route | Notes |
|---|---|---|
| Manual | `POST /api/devices` | Operator-entered, from the Devices page |
| Subnet sweep | `POST /api/discover/sweep` | TCP connect to port 22 only — coarse on purpose; the poller's own `version` task does real identification next cycle |
| YAML seed file | `POST /api/discover/seedfile` | `seed/devices.yaml.sample` shows the shape |
| confetti-traffic import | `POST /api/discover/confetti` | `GET <hub_url>/endpoints`, imported as `role='node'`, `vendor='alpine'`, `platform='linux'` — never SSH-polled. `site` is the endpoint's `group_name`, which is confetti-traffic's *effective* group: a group set on its dashboard overrides what the node registers, so an override changes `site` on the next import |
| confetti-butler import | `POST /api/discover/butler` | `GET <butler_url>/api/devices`; inventory only (hostname, IP, serial, vendor, platform, model, role, site), no credentials. Merged through `identity.ingest` (source `butler`), so a router already known is not duplicated; butler's role is kept, so a `role='node'` device is still never polled |
| LLDP crawl | `POST /api/lldp/adopt` | A neighbor with an unknown management IP, added inventory-only (polling off) |

## Project Structure
```
topo/
  build-template.sh   — builds the golden template
  serve.py            — production entrypoint (reads TOPO_PORT at runtime)
  run.sh              — foreground launcher for debugging
  app/
    app.py             — Flask routes (single file; splits into its own module once a section outgrows
                          a screenful — see topology.py, identity.py, poller.py)
    config.py          — env-var config
    db.py              — get_db / connect / init_db / sqlite_now / iso / sqlite_ts_arg / like_arg
    identity.py        — device identity ladder, ingest/observe/merge
    poller.py          — background SSH polling scheduler
    topology.py        — the graph, ghost nodes, interface-IP peer resolution
    events.py          — emit() change events, is_baseline() first-poll guard
    platforms.py       — per-platform commands/parsers (IOS only)
    reach.py           — ICMP reachability thread
    discovery.py       — scheduled discovery thread (off by default)
    lldp_crawl.py      — adopt unknown LLDP neighbors as inventory-only devices
    metrics.py         — /metrics Prometheus text
    collectors/        — ssh.py, sweep.py, confetti.py, butler.py, seedfile.py
    parsers/           — ios.py, regex only
  templates/           — base.html (head, header/nav, theme + layout pickers, footer, Retro taskbar)
                         + dashboard, devices, device, changes (Recent changes: events table, banded per device, details pane, right-click menu), topology, conflicts, settings, each `{% extends "base.html" %}`
  static/theme.css     — the ONE place colours live (Dark, Light, Monokai, High Contrast,
                         Terminal green, Confetti Night, Neon Streamers). Pages must not define their own :root colours
  static/layout.css    — Classic, Modern, Retro 95, MS-DOS, Neon Green CRT (MS-DOS also has a keyboard layer:
                         base.html's .dos-menubar / .dos-fkeys / .dos-help, a row cursor, and one keydown IIFE at
                         the end of theme.js that does nothing in any other layout; same block as confetti-butler). Loaded AFTER theme.css
  static/ctxmenu.js    — right-click menus, loaded once by base.html (menu CSS at the end of layout.css); the SAME file as in confetti-butler and confetti-traffic, copy it by hand and keep the three identical. A row carries data-ctx="<type>" and the page calls ctxMenu.register(type, fn(el, ev)); one document-level listener finds the row. Fill data-* attributes with ctxMenu.attr(), never esc(). A destructive item is `danger: true`, goes last and must ask confirm() (R29 fails a page that has one without). Used by changes.html (rows, plus the whole page for the empty-space menu) and the topology map (ctxMenu.open from vis's `oncontext`). R29 checks it
  static/theme.js      — THEMES + LAYOUTS lists and the two header dropdowns (confetti-topo-theme,
                         confetti-topo-layout in localStorage), themeColor() for canvas colours,
                         confettiBlast(), Shuffle, the header health dot
  static/vendor/       — vis-network/ (vendored pinned version, no CDN)
  seed/devices.yaml.sample
  services/ — confetti-topo.initd, firstboot.initd, login-setup.sh, topo-backup.sh
  scripts/  — topo-setup.sh, topo-update.sh, seed_mock_lab.py (fake lab in a new scratch DB),
              capture_readme.py (re-records docs/img/ from that mock lab; run after any UI change)
container/ — Dockerfile + compose
dev/regress.py — the regression gate (see below); dev/samples/ = parser fixtures
docs/ — TOPOLOGY-MAPPING.md, HANDOFF.md, ROADMAP.md, img/
.claude/ — agents: dashboard-ui-checker, lab-validator, leak-scanner, readme-image-capturer;
           skills: 3 reference skills (cisco-ios-patterns, netmiko-ssh-automation, network-bgp-diagnostics) and 6
           confetti-topo-* (api, dev-run, add-platform, lab-validation, release, ui). regression-gate,
           docs-drift-checker, confetti-interop-checker and the alpine-appliance and constraint-regression
           skills are user-level (~/.claude), not in this repo
```

The look (themes, layouts, Shuffle, confettiBlast) is copied by hand from confetti-traffic's hub UI and
confetti-butler. Keep the three in step; `confetti-interop-checker` reports drift.

## Design Decisions
- **Mirror the sibling stack exactly**: Flask + SQLite (WAL) + waitress, vanilla JS with no build step, Alpine
  golden image, OpenRC.
- **Read-only SSH.** No Netmiko config-push path.
- **Credentials**: per field, a device's own `credentials` row, then the Settings page (`settings` table), then
  the env defaults (`TOPO_SSH_*`); an empty value falls through (`ssh.resolve_credentials`, R28). The Settings
  page also applies or removes per-device credentials on several devices at once. Plaintext in v1 —
  `topo.db` must be `0600` (`checkpath --directory --mode 0700` on `/var/lib/confetti-topo` in the OpenRC
  service is the current mitigation).
- **The API is unauthenticated**, same trust model as the siblings; do not expose it. A sweep CIDR or a
  confetti-traffic or confetti-butler URL given to the API makes the server connect there.
- **Keep code simplistic to avoid over-engineering.** A network engineer reading this at 2am should be able
  to follow it without decoding an abstraction.

## Working style
- Keep output minimalistic and use simple English.
- Do not output large code snippets. Point at `file:line` and say what changed; the file itself is the record.
- Do not display code changes in output unless asked to.
- Always restart the dev server when a change needs it to take effect (the app
  reads templates and Python code only at start), and check that the running
  copy serves the change, before telling the user to look at it.

## Non-obvious constraints — do not regress these

1. **Aliases are never deleted, and that's deliberate.** `device_aliases` accumulates every identifier ever
   observed for a device, not current state. The cost: a decommissioned device's IP reassigned to a
   genuinely different device surfaces as a `merge_conflicts` row rather than silently merging two different
   physical boxes. Do not "fix" it by pruning old aliases.
2. **A stronger identifier disagreeing with what's on file is a conflict; a weaker one drifting is not.**
   `identity.ingest`'s `_detect_kind_clash` only flags kinds ranked >= the strongest kind that actually
   produced the match. Get this backwards and either legitimate re-IPs start false-conflicting, or real
   device swaps go unnoticed.
3. **IOS-XE command names are not assumed, they're checked.** `show lldp local-info` is invalid syntax on a
   real IOS-XE 17.3 CSR1000v. `show lldp entry local` is *not* an equivalent: it looks up a neighbor named
   "local" and always returns 0 entries. No IOS-XE 17.3 command shows a device its own LLDP chassis ID, which
   is why LLDP neighbors resolve by advertised management IP, not chassis ID. Older IOS (XE 3.11 / 15.4)
   omits `Local Intf:` from `show lldp neighbors detail`; the collector then fetches the brief
   `show lldp neighbors` table (`ios.fill_lldp_local_if`). `show interfaces` is parsed with pure regex.
4. **Timestamps: everything stores `db.sqlite_now()`'s `'YYYY-MM-DD HH:MM:SS'` form and converts with
   `db.iso()` on the way out.** String-comparing ISO-8601 against `datetime('now', ...)` lets `'T'` sort above
   `' '` and any same-day row passes any window.
5. **`busy_timeout` on every connection, no exceptions.** Several writers share the database (Flask handlers,
   the poller's thread pool, the ICMP checker, the discovery timer). `db.connect()` sets it before
   `journal_mode=WAL`.
6. **Flask templates that embed literal JS containing `{{`/`}}`/`{%`/`%}` must be wrapped in
   `{% raw %}...{% endraw %}`.** `render_template()` runs the whole file through Jinja2. `device.html`'s
   `var deviceId = {{ device_id }};` is the one deliberate server-side substitution.
7. **`identity.observe` (poller path) and `identity.ingest` (discovery paths) are not interchangeable.**
   `observe` skips alias-matching because the caller already knows the device_id; calling `ingest` from the
   poller with only a newly-discovered `serial` would create a duplicate device.
8. **`role='node'` devices are never SSH-polled.** `poller.py`'s device-selection query filters
   `role != 'node'` — confetti-traffic's imported Alpine test VMs would fail every IOS `show` command.
9. **A subnet sweep's ingest passes no `hostname` field**, only the `mgmt_ip` candidate. A known device's real
   hostname must never be overwritten with a raw IP string on re-sweep.
10. **confetti-traffic's imported nodes get explicit `vendor='alpine'`, `platform='linux'`** in
    `collectors/confetti.py` — empty strings would fall through to `identity._create_device`'s
    `cisco`/`cisco_ios` defaults and mislabel every Alpine VM as a Cisco router.
11. **Events are written inside the transaction of the change they describe, and not on a task's first
    poll.** `events.emit()` must never commit. `events.is_baseline()` suppresses events until the task has an
    `ok=1` row in `poll_history`. `device_unreachable` / `device_recovered` fire only on the 0 → failing and
    failing → ok transitions of `fail_count`.
12. **A peer or neighbor resolves by interface IP only when exactly one device owns that address, never to
    itself, and never as a stored alias.** The poller's stored `peer_device_id` / `remote_device_id` wins.
    Storing interface addresses as identity aliases would let a re-addressed interface permanently claim a
    device and turn constraint 1 into false merges.
13. **Unresolved neighbors are ghost nodes, not dropped; a cable or session both ends report is drawn once
    (down if either end says down); ghost links and differently-spelled ports are never de-duplicated.** A duplicate line is safer than a hidden
    parallel link, and an edge to nowhere hides a device the operator should know about.
14. **A failed task deletes nothing.** Only a successful answer — including an empty "LLDP is not enabled" —
    may remove rows. A timeout or a failed login must leave the topology as it was, and the per-device lock
    must be released either way.

## Regression gate

`python dev/regress.py` (stdlib only; `--static` skips the live tier, `-v` shows detail for passes) is the
gate to run before handing back any code change. `R1`..`R14` are constraints 1..14 above; `R15`..`R19` are
cross-file checks (docs vs code task count, shell/Alpine hygiene, poller lock, files the build scripts copy,
platform registry and read-only commands); `R20` starts the real server from a temp copy and drives it over
HTTP; `R21`..`R26` cover the ICMP state machine, `/metrics` format, LLDP crawl, scheduled discovery, the
container files and the BGP `Idle (Admin)` parse found on a real 15.4 router; `R27` checks the OpenSSH
(not Dropbear) setup; `R28` the credential order (device > Settings > env), that no GET returns a secret and
that a bulk apply is all-or-nothing; `R29` the right-click menus (shared `static/ctxmenu.js`, quote-safe `data-*`, scripts parse). Output is one line per check
and a verdict: `CLEAR`, `CLEAR WITH GAPS` (something not run) or `BLOCKED`. The unit tier imports the app, so
it needs the app's own Python packages (else those checks are `NOT RUN`). Each check must be proven red by
reverting its fix in a scratch copy. Adding a constraint means adding its check with the same number; the
`regression-gate` agent runs this script. Doc-only changes need `docs-drift-checker` instead.
