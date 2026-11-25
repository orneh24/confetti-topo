# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A simplistic server that maps a network from what its devices say about themselves: LLDP neighbors
(cables) and BGP/OSPF adjacencies (sessions), drawn as a topology graph, and that keeps the inventory
around it: device inventory, IPAM, config templates and their pull endpoint, running-config backups
with drift, syslog and change events. Infrastructure is ESXi 7.0 with Cisco CSR1000v routers
initially; other vendors later.

confetti-topo was split out of the former confetti-butler project on 2026-10-14 and **merged back on 2026-11-21**: this
project now holds everything butler did (IPAM, templates, config backups, drift, SNMP,
interface error history, the vCenter import) plus the topology. The name, service (`confetti-topo`),
install path and `TOPO_*` environment variables did not change. It is **fully standalone**: its own
device inventory, credentials, discovery, ICMP checker, SSH poller, LLDP crawl, events and graph UI. One
sibling repo stays related:
- `../confetti-traffic` (network connectivity test harness) — confetti-topo can import its node fleet
  (`GET /endpoints`) as `role='node'` devices, and copies its hub UI look by hand. The
  shared syslog files stay byte-identical with confetti-traffic (constraint 24). The `confetti-interop-checker` agent looks for drift.

The former confetti-butler project is retired: its code was ported into this repo and nothing here talks to it any more
(no import, no Settings URL, no syslog pull; removed 2026-11-22).

Reference notes for the feature itself: `docs/TOPOLOGY-MAPPING.md` (design, pitfalls, test checklist).

**Device access is read-only.** confetti-topo never writes to a device: SSH `show` commands (including
`show running-config` for the backups), SNMP get/walk, and for PAN-OS `<show>` operations over the XML API.
Configs reach a device only by the device pulling them (`copy http://<server>/configs/<key>.cfg running-config`).
There is no config-push path and none should be added without being asked
for explicitly (regression check R19 fails on any command that is not a `show`; R30 on any XML API request that
is not a `<show>` op).

## Architecture

### Server VM (standalone)
- Alpine Linux VM, ESXi 7.0 + vCenter, `open-vm-tools`
- Runs:
  - Flask API served by **waitress**, not the Flask dev server (single-threaded — the poller's own
    requests and the dashboard would queue behind each other)
  - SQLite database (WAL mode) at `/var/lib/confetti-topo/topo.db`
  - Web UI on port 80
  - A background **poller** thread pool (device polling, SSH or XML API), an **ICMP checker** thread, an optional
    **discovery** timer thread and a UDP **syslog** listener (port 514, `TOPO_SYSLOG_*`)
- SSH server is OpenSSH (`sshd`, with `openssh-sftp-server`), not Dropbear (switched 2026-10-14, same as
  the former confetti-butler: Dropbear has no SFTP and drops large packets). `/etc/ssh/sshd_config.d/10-confetti.conf` keeps
  root password login on and sets `MaxStartups 100:30:200`; only an ed25519 host key is generated.
  `build-topo.sh` `switch_ssh_server()` (called last, above the `--update` exit) moves a VM that still has
  Dropbear over: converts its ed25519 host key, stops Dropbear, starts sshd, and only then removes Dropbear and
  rewrites `dropbear` to `sshd` in `topo.env`'s health list; if sshd does not start Dropbear is started again.
  Rollback does not undo it. R27 checks it.
- Installed to `/opt/confetti-topo/`, started by OpenRC service `confetti-topo`
- Entrypoint is `serve.py` — reads `TOPO_PORT` at runtime. Do not move the port into the init script's
  `command_args`: OpenRC expands that at parse time, before `start_pre` sources `topo.env`, so the setting
  would be ignored (same constraint as confetti-traffic's hub).

### The poller — server-initiated pull

confetti-topo dials out to devices over SSH (or the PAN-OS XML API) rather than waiting for them to report in. One daemon thread
(`app/poller.py`), started by `serve.py` beside `reach.start()`, idempotent — `start()` returns quietly if
already running.

Each tick (`TOPO_POLL_TICK_S`, default 15s): claim devices with `poll_state='idle'` and
`next_poll_at <= now`, mark them `'running'` (this is the per-device lock — a device whose SSH hangs is never
queued twice), and hand each to a `ThreadPoolExecutor` (`TOPO_POLL_WORKERS`, default 8).

Per device, eleven tasks run independently, **each committing its own transaction**: `version`, `interfaces`,
`lldp`, `bgp`, `ospf`, `routes`, `config` (IOS only, the running-config backup, last so a slow read delays nothing) and `vlans`, `trunks`, `stp` and `mac` (IOS **switches** only: `ssh.run_tasks` leaves it out for a router, which answers
`Invalid input`, and PAN-OS has none; a new switch gets it from its second poll, once its model is known). Partial failure is the normal case — an LLDP timeout must never discard interface data
collected 200ms earlier. `role='node'` devices (confetti-traffic's imported Alpine fleet) are polled only when their platform is
`confetti_node`, and then through the hub, not by SSH (`collectors/hub.py`, constraint 39); any other node is never polled —
`poller.py`'s device-selection query is `role != 'node' OR platform = 'confetti_node'`.

All eleven tasks share **one SSH session** per poll (PAN-OS: one keygen per poll) (`ssh.run_tasks` is a generator, so each task still commits
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

`app/platforms.py` maps a device's `platform` to its transport, commands and parsers: `cisco_ios` / `cisco_xe`
(SSH), `paloalto_panos` (XML API) and `snmp` (net-snmp `snmpget` / `snmpwalk`, v1 and v2c, `version` and `interfaces`
only, no CLI; `collectors/snmp.py`). An unknown platform falls back to the IOS entry. Junos is planned, not
started.

**PAN-OS** (`collectors/panos.py`, `parsers/panos.py`, skill `panos-xml-api`): the poller picks the collector from the
platform's `transport` (`xml_api` or `ssh`) and records it in `poll_history.transport`; the six tasks, the apply
steps and the events are shared. The collector logs in once per poll (`keygen`, with the username and password from
the usual credential order), then sends one `<show>` operation per task; BGP takes two (the peer list, and the
summary that holds the router's own AS). Its parsers use XML (ElementTree), the one place the parsers are not regex.
The management certificate is not checked unless `TOPO_PANOS_VERIFY_TLS=true`; the request timeout is
`TOPO_PANOS_TIMEOUT_S` (default 20 s). Fixtures in `dev/samples/panos_*.xml`
were captured from a real PA-VM (PAN-OS 11.1); other versions and Panorama are untested.

### Topology

`app/topology.py` builds `GET /api/topology`, a vis.js-shaped `{nodes, edges}` graph over LLDP neighbors and
BGP/OSPF adjacencies (`?layer=lldp|bgp|ospf`, or a comma list, limits it; the page's LLDP/BGP/OSPF buttons are independent toggles; a Lines dropdown sets the connector thickness Small / Medium / Large, `confetti-topo-line-width`, per browser, `lineScale()` multiplies the widths by 0.5 / 1 / 2). A neighbor/peer that does not resolve to a known
device renders as a **ghost node** (`ghost:lldp:<sysname|chassis>`, or `ghost:peer:<ip>` shared by BGP and
OSPF) rather than being dropped — an edge to nowhere reads as a bug, a distinctly styled ghost reads as
"something is out there, unidentified". A cable or BGP/OSPF session both ends report is drawn once (a
session is down if either end says so); ghost links are never de-duplicated (constraint 13). Two real sessions
between the same pair of routers (loopback and link address) stay two edges, paired by position with
Established first — a simple rule, wrong only when the two ends list them in a different order.

The page keeps the map still and readable: physics places only nodes with no saved place, then stops.
Places are stored in `node_positions` (`PUT`/`DELETE /api/topology/positions`, the same map for every viewer;
`GET /api/topology` returns `x`/`y` per node and a `positions` dict). An unknown node (or "N unknown" node) whose links all go to one known device is kept close to it and out of the way: `tuckUnknown` in topology.html puts one that is more than `UNKNOWN_NEAR` (400 px) away, or that touches a link or another node, or whose own AS area would meet the area of another AS (an unknown BGP peer has the AS it reports), on a ring around that device at the angle and distance with the most free room, after the layout settles and on load, and saves the new places. Devices and links never move for it: the real map has precedence over unknown nodes (a drag that is near and clear stays). Unknown nodes whose links all go to one
known device are drawn as one "N unknown" node (`group:dev:<id>`, made up by the page, so only `positions`
knows it); double-click expands it. An LLDP link shows its port names only on hover or when selected, and so does a
default-route or subnet link; a second link of the same kind between the same two nodes bends further out. Below the
Devices list, an Unknown list names each unknown node and the device(s) that see it (browser only, nothing stored).
BGP/OSPF labels always show. A BGP session is typed by the AS at its two ends (`adjacencies.local_as` is the router's own
AS, from `show bgp summary`): `bgp_type` `ibgp` = thin yellow line, `ebgp` = thick purple line, and the **AS areas** toggle
draws a box labelled with the AS behind its nodes (`nodes[].asn`). `/topology?device=<id>` selects and centres on that device once (the Recent changes page links there).
Right-click **Focus on device** makes one device the master: a tree layout from it, one row per hop over the shown links
(`treePositions` in topology.html). It is per browser (`confetti-topo-master`), and the tree is never saved to `node_positions`
(`savePositions` returns while `treeOn`).

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
3. **Optional, off by default, LLDP only** (Settings page, `GET|PUT /api/settings/lldp-matching`, env
   `TOPO_LLDP_MATCH_CHASSIS` / `_NAME`): a neighbor still unresolved is matched by its chassis ID to a device's
   management MAC (`devices.mgmt_mac`, filled by the PAN-OS version task; IOS cannot report its own, constraint 3),
   then by its system name to a hostname (case and domain suffix ignored). `topology.resolve_lldp`; same rules as
   step 2 and never stored (constraint 16). The link card says `identified by`.

NULL after these steps is the expected result for peers outside the lab, not a bug.

**Default and Connected layers** (`topology.build`, `_connected`; buttons Default / Connected, `?layer=default|connected`): built at
query time from stored `routes` and `interfaces`, never stored. *Default* = one green arrow per RIB `0.0.0.0/0` next hop, to the
unique owner of that address (constraint 12), else a `ghost:peer:<ip>` shared with BGP/OSPF; not de-duplicated. *Connected* = devices
with an address in the same subnet (orange): two devices get one line, three or more meet at a `subnet:<cidr>` node (`device_type`
`subnet`, not a ghost, `device_id` null); a subnet holding a device's `mgmt_ip`, a /32 and a subnet with one device draw nothing;
a port reported not up draws the line red and dashed. Constraint 23.
The **Layout** dropdown (Free / Hierarchical, `confetti-topo-layout-mode`, per browser) lays the map out as the focus tree does
(`treePositions`), from the Focus device if one is set, else from the shown device with the most links (`autoRoot`); places are
never saved (`treeOn`). **By site** (`site`) is different: `sitePositions` puts each `devices.site` (nodes carry `site`) in its own block, laid out
with `treePositions` from the site's `autoRoot`, blocks packed in rows; the places ARE saved and can be dragged, a new device goes
right of its site's last row (`siteSlot`), and the page draws a solid box per site under the AS areas (`drawSiteAreas`). On a
map with more than `BIG_MAP` (20) devices, a browser with no saved choice starts with only LLDP + BGP shown (no FILTERED marker,
`layersBig`) and, with no saved places, in By site. A pair of nodes with one link draws it straight; parallel links still curve.
Zoomed out, vis leaves out small labels (`scaling.label.drawThreshold`) and below 60 % zoom the vendor mark is dropped (`zoomDetail`).
The **Labels** dropdown (Default / Hover only / Off, `confetti-topo-label-mode`, per browser) sets when link labels show (`edgeLabel`).
The toolbar is three groups, Show (layers, unknown nodes) / Look / Actions (Fit, Tidy AS areas, Re-arrange, History, an Export menu); a switched-on toggle is filled. The **Fit** button is `fitMap`; it leaves room for the Devices list and an open legend. An open node or link card takes the legend's place at full height (`fitNodePanel`). "Tidy AS areas" puts each node with no AS in the free slot nearest the mean x of the nodes it links to. On this full-height page the footer's "Is it DNS..?" / "Make it rain!" are in the map's right-click menu instead. Selecting a device fades every node and link that is not it or one link away (`dimMap`, `selNear`,
`edgeOpacity`; combined with the search fade, nothing saved).

### Routing table

The `routes` task stores a device's IPv4 routing table in `routes` (one row per next hop, `vrf`, `kind` = `rib` or
`fib`); `GET /api/routes?device_id=N` (`vrf`, `kind`, `prefix`) reads it, the device page shows it (its VRF picker lists `default`, the global table, first and selects it), and the map's
right-click menu links to it. IOS runs `show ip route vrf *` (the global table, then each VRF; an IOS that answers
`%` gets `show ip route`). PAN-OS sends the RIB (`<show><routing><route/>`, the virtual router is the vrf) and the
FIB (`<show><routing><fib/>`); a FIB that cannot be read keeps the RIB. Under `is subnetted` an IOS route line has no
mask and takes the header's. `emit()` writes one `routes_changed` event per poll (`+N -M` on (vrf, prefix) of the RIB),
never one per prefix. Fixtures: `dev/samples/ios_route_*.txt`, `panos_route.xml`, `panos_fib.xml`. Not handled: IPv6,
a cap on a very large table (a full BGP table would be one row per route).

### VLANs, trunks and spanning tree (switches)

The `stp` task runs `show spanning-tree summary` and keeps one row per switch in `stp` (mode, the VLANs it is root bridge for,
the VLANs with a blocking port; `GET /api/stp?device_id=N`, a Spanning tree box on the device page). A change in the root
VLANs or the blocking VLANs is one `stp_changed` warning event each (not on the first poll). Fixture
`dev/samples/ios_stp_summary.txt`. Per-port STP roles are not handled.

The `mac` task runs `show mac address-table` and keeps the **dynamic** (learned) entries in `mac_table` (VLAN, MAC, port; at most
5000 per switch; `GET /api/mac?device_id=N&q=`, a MAC addresses box with a filter on the device page). The switch's own static
rows are always in the answer, so an answer with no rows is a parse failure and deletes nothing. No events (addresses come and go
all day). Fixture `dev/samples/ios_mac_table.txt` (real format, MACs replaced). `interfaces` now keeps each interface's hardware MAC (`interfaces.mac`, IOS and PAN-OS); `/api/mac` names a learned MAC's device when exactly
one other device owns it (query time only, never stored, never an alias; same rule as constraint 12).

The `trunks` task runs `show interfaces trunk` the same way (`trunks` table, `GET /api/trunks?device_id=N`, a Trunks table on
the device page). A switch may have no trunks, so an empty answer clears the table; a `%` answer fails the task and
deletes nothing (both switch tasks). Fixture `dev/samples/ios_trunk.txt`.

The `vlans` task runs `show vlan brief` on a device the topology draws as a switch (`ssh._is_switch`, from role, model and
hostname) and stores one row per VLAN in `vlans` (`GET /api/vlans?device_id=N`, shown on the device page). A switch always
has VLAN 1, so an answer with no rows deletes nothing; a real list replaces the old one. No events. Fixture
`dev/samples/ios_vlan_brief.txt` (real format, Catalyst 3560).

### Features that came from the former confetti-butler

These came over on 2026-11-21 as the same code, `BUTLER_*` renamed to `TOPO_*`.

- **Config templates and the pull endpoint** (`templates` page, `/api/templates*`, `rendering.py`): Jinja2 in a sandbox with
  `StrictUndefined` (constraint 27), rendered against a device's polled facts plus its own `template_vars`. `GET /configs/<key>.cfg`
  serves the result as plain text for the device to pull; it is unauthenticated on purpose, also with `TOPO_AUTH_TOKEN` set
  (constraint 28), and `check_hardcoded_secrets` warns on save instead. Every save appends a `template_versions` row. A device gets a
  template through `devices.template_name` (the Edit form on its page).
- **Running-config backups** (`config` task, IOS only, `ssh._apply_config`, `config_versions`): `show running-config` with the
  volatile lines removed, stored only when complete (a `version` line and a closing `end`) and only when the SHA-256 differs from the latest
  version (constraint 29). `TOPO_CONFIG_VERSIONS_KEEP` (0 = all). The API (`/api/devices/<id>/configs`, `/configs/<vid>`, `/configs/diff`)
  masks secrets (`rendering.redact_secrets`) unless `?raw=1`. A change is a `config_changed` event.
- **Drift** (`drift.py`, `devices.drift_status`, a Template drift box and the Template column): the rendered template must be contained in
  the latest backup, line by line under the same parent; secret lines are not compared (constraint 32). `config_drift` (warning) and
  `config_compliant` events on a change between the two, none on the first evaluation. Saving a template re-evaluates its devices.
- **IPAM** (`ipam.py`, `/ipam`, `/api/ipam/*`): discovery-first, built from `interfaces`; `POST /api/ipam/analyze` rebuilds
  `ipam_findings` (overlapping prefixes, duplicate addresses). `/api/ipam/prefixes` adds `used`, `size` and `free` (`ipam.usable_hosts`);
  the page has one search box over both tables (an address also matches the prefix that holds it). A **Devices** picker (`confetti-topo-ipam-devices`, tick any number of devices, All / None, a find box) limits both tables and the findings to the ticked devices; a prefix's Used and Free still count every device. **Prefixes in use** is one row per (VRF, network): the VRF is not read from the device (`interfaces.vrf` is never filled) but is the VRF of the interface's connected route (`ipam.VRF_SQL`, global table when there is none), so the analysis ignores overlaps and duplicates across VRFs; a row has sites, use %, up / partly down / down, a duplicate mark (live) and an overlap mark (from the last analysis), a **Site** filter (`confetti-topo-ipam-site`) and an Export CSV of the rows shown. Not handled: free blocks, hierarchy, reserved ranges (Used and Free count only polled devices). Checked by R57.
- **Interface error history** (`interface_stats`, `interfaces.input_errors` / `crc_errors`): a sample when the counters change plus one an
  hour, pruned after `TOPO_STATS_RETENTION_DAYS` (7); a counter that goes down (reload) adds nothing to the 24 h growth
  (`ssh.growth`, constraint 33). Rising CRC errors are an `interface_errors` warning event.
- **Interface traffic** (added 2026-11-22, constraint 35): `show interfaces` also gives the device's own 5-minute (or `load-interval`) rate,
  stored as `interfaces.in_bps` / `out_bps`, and the byte counters, sampled into `interface_stats.in_bytes` / `out_bytes` by the same
  on-change + hourly rule. `ssh.rates()` turns two samples into bits per second; a counter that went down gives no rate (a gap), never a
  negative. The device page shows the rate per interface and a "graph" link opens an SVG chart (`/api/devices/<id>/interface-stats`,
  6 h / 24 h / 7 days). IOS only (PAN-OS and SNMP leave them empty). No new device command.

### Added 2026-11-22

- **Find** (`app/find.py`, `/find`, `GET /api/find?q=`, the header search box on every page): an IP (the interface that holds it, the
  longest matching route per device and VRF), a prefix, a MAC (`interfaces.mac` owner and the switch ports that learned it,
  access port vs trunk) or text (devices, interfaces, LLDP names). Read-only, query time only, never an alias (constraint 37).
- **Backups page** (`/backups`, `tools_routes.backups_page`, `GET /api/backups`, `GET /api/backups/search?q=&regex=1&invert=1`): the latest
  running-config backup per device, and a search over all of them on the masked text (up to 20 lines per device, with the parent line).
  Not under `/configs/` on purpose (that prefix is unauthenticated, constraint 28).
- **Database backup and restore** (`app/backup.py`, Settings "Backup and restore", `scripts/topo-db.sh`, constraint 36): `GET /api/backup`
  lists stored files, `POST /api/backup/download` (`{name?}`; none = a new backup now) sends one, `GET /api/backup/env` the `TOPO_*`
  settings with secret-looking keys blanked, `POST /api/backup/restore` (multipart `file`) restores. Files live in `<db folder>/backups`
  (0700, files 0600). A restore checks the upload (`quick_check`, a `devices` table), saves the current database as
  `topo-pre-restore-<time>.db`, copies the upload in with SQLite's backup API (no restart), runs `init_db` and resets `running` rows.
  `topo-db.sh backup|list|env|restore [-y]` does the same on the VM (restore stops and starts the service).
- **Test login** (`POST /api/devices/<id>/credentials/test`, a button on the device page and per row in Settings' Device credentials): logs
  in with the resolved credentials and runs the `version` task only; returns ok/error, what it found, the transport and the time. Stores
  nothing (constraint 37); refused for `role='node'`.
- **Map notes** (`map_notes`, `GET|PUT /api/topology/notes`): free text (2000 characters) on a node or a link (`edge:<edgeKey>`), edited in
  the node and link cards, included in the JSON export. An empty note deletes it.
- **Device page status strip**: ping, poll, sessions, flapping, template drift, interface errors 24 h and ports down, each a link to its box.
- **Recent changes, flapping and column widths**: a peer, interface or port (device + type + item) that changed `count` times or more within
  `window_min` minutes gets a yellow "flapping" pill in Notes and a line in the details pane (`computeFlaps`, display only, from the events
  loaded; not the BGP flap rule of constraint 22, so a peer cycling Idle/Active counts here). The rule is set on Settings ("Flapping on Recent
  changes", `GET|PUT /api/settings/flap-display` `{count 2-100, window_min 1-1440}`, settings keys `flap_display_count` / `_window_min`,
  default 3 within 60). Columns hug their content, Notes takes the rest; a header's right edge drags its width (fixed layout from then on,
  text cut with "..."), double-click it to reset; kept in `confetti-topo-changes-columns`. A "Hide flapping" checkbox leaves the flapping rows out, and the table header stays on screen while the page scrolls (`table.sticky-head`, also on Syslog). Since 2026-11-23 every table works this way (`static/colresize.js`), and a
  right-click on any table offers **Resize (compact)**: each column as narrow as its widest cell (at most 360 px) and the table only as wide as its columns.
- **Device page layout** works like the dashboard (dashgrid.js with `page: 'device'`, one layout for every device page in
  `confetti-topo-device-layout`; every box has a `data-box`, so it also folds): drag, resize, Reset, Auto sort, Lock. The grid has an even
  number of columns (2 / 4 / 6 by width) so Events and Syslog start half a row each (`.half`). Events, Syslog, LLDP, BGP / OSPF, Interfaces and Routes
  show their first 10 rows (`tbody.capped`, so after a sort too; the rule is in layout.css) with a "Show all (N more)" button (`toggleCap`). The dashboard's Devices and
  Unknown LLDP widgets work the same way. IPAM caps its two tables at 25 (`capped25`).
- **Big lists (added 2026-11-23)**, all per browser: the **Devices** page has chips (All, Down, Poll failing, Not polled yet, Template drift, one per role, each with a count)
  and a "Group by site" box (a heading row per site, `tr.grp`, click folds it; on by default above 20 devices; `tablesort.js` sorts inside a group); its Poll now / Delete
  buttons are only in the right-click menu. The **dashboard** Devices widget has "Problems only" (down, poll failing or drifted; on by default above 20 devices, `confetti-topo-dashboard-problems`).
  **Recent changes** has "Hide flapping" and "Fold flapping" (on): the changes of one flapping item are one row, and its details pane lists them all. **Syslog** has 7 and 14 day
  windows, a count per severity and "Load older messages" (the server answers 2000 rows at most; the 15 s refresh waits while older rows are shown). Backups and the Settings
  credentials table have a filter box (`static/tablefilter.js`, `data-filter-table`; a bulk apply only touches the rows shown). On the topology, By site has "Bundle site links":
  the links between two sites are one dashed line with a count between their best linked devices (`bundleEdge`); a session that is down, the selected device's links and a
  bundle clicked open stay drawn; it is `confetti-topo-bundle-links`. AS areas start off in By site, and a list of more than 20 devices is grouped by site.
- **Page help**: an empty Syslog page shows the IOS `logging host` lines for this server; Templates has "+ New from example". The Devices page puts the table first (sortable, Columns chooser) and folds "Add or discover devices"
  and "Unknown LLDP neighbors".
- **Alerting was removed on 2026-11-23** (`alerts.py`, the Alerts page, `/api/alert-rules*`, `TOPO_ALERTS_*`, `TOPO_SMTP_*`). An older
  database keeps its unused `alert_rules` table and `events.alerted` column (constraint 34: nothing is dropped).
- **SNMP** (`collectors/snmp.py`): see Architecture. Device and Settings credentials carry `snmp_community` and `snmp_version`
  (same order as the SSH ones; only the Settings page shows its own saved community, as plain text, constraint 31).
- **vCenter import** (`POST /api/discover/vcenter`, `collectors/vcenter.py`): inventory only, through `identity.ingest`. Never tested
  against a real vCenter; it has no page, only the API.

Menu order (one list in `base.html`, `all_pages`): Dashboard, Topology, Devices, Conflicts (see below), Recent changes, Syslog, IPAM, Templates,
Backups, Settings (last). On the **Classic** layout IPAM, Templates and Backups are a second row labelled Tools (`.nav-row-tools`, shown only while no
`data-layout` is set); the other layouts list them with the rest. The MS-DOS Go menu hotkeys are 1-9 for the first nine, 0 for the last
(Settings) and the label's first letter for the rest. Conflicts moves with the open count (`app._open_conflicts`, a context processor): with open conflicts it is
"Conflicts (N)", highlighted yellow (`.nav-attn`, every layout) in its place after Devices; with none it is the last entry, after Settings
(then it has hotkey 0). R6 checks both.

### Topology history

`app/history.py`: the poller tick (every `TOPO_SNAPSHOT_INTERVAL_S`, default 300; 0 = off) builds the whole map and stores it in
`topology_snapshots` only when its shape (nodes, links, link up/down, reachable) differs from the newest snapshot; snapshots older
than `TOPO_SNAPSHOT_RETENTION_DAYS` (14) are pruned. `GET /api/topology/snapshots` (list, newest first) and
`GET /api/topology/snapshots/<id>` are read-only. The topology page's History dropdown shows one with a banner and "Back to live".
A snapshot is the `/api/topology` answer, so it holds nothing the page does not already show.

**Compare** (its own toolbar row; `buildCompare` in topology.html, browser only, no API of its own, nothing stored): From (Off or a
snapshot) and To (Live or a snapshot), always read older → newer; `/topology?compare=<id>,<id|live>` opens one. The newer map is drawn
with what is gone added back. A link is `added`, `gone`, `changed` (up/down, label, flapping) or `same`; BGP/OSPF links are matched by
ends and protocol, because their label holds the state (`matchEdges`). A device is `changed` when its ping state or **any of its links**
changed. Only shown layers count. Changed items are green / red / orange (`--orange`) with a `+` / `−` / `~` mark; unchanged ones are faded,
their links grey. Three per-browser toggles (`confetti-topo-cmp-group`, `-cmp-only-dev`, `-cmp-only-edge`): **Group changed / unchanged**
(changed on top in their places, unchanged packed below, a box around each), **Hide unchanged devices**, **Hide unchanged links** (also
drops unchanged unknown and subnet nodes left without a link). A changed unknown node is never folded into "N unknown"; bundles are off.
Compare and History exclude each other. While either is on (`readOnlyView`), no node place is saved and Re-arrange does nothing.

### Syslog

`app/syslog_server.py` (a daemon thread, `TOPO_SYSLOG_ENABLED` default true, UDP `TOPO_SYSLOG_PORT` 514) stores what devices say in `syslog`,
through `syslog_core.py` (constraint 24, the same parser and insert as confetti-traffic). The receiver's rules: no `allow_reuse_address`, `busy_timeout` before `journal_mode`, `MAX_RAW` 4096, a row cap (`TOPO_SYSLOG_MAX_ROWS`) and an age limit
(`TOPO_SYSLOG_RETENTION_DAYS`, 14, pruned in the poller tick). `/syslog` is the page (filters, right-click menu, a "via" column that names
the server a row was pulled from), `GET /api/syslog` and `/api/syslog/sources` read it, the device page has a Syslog box, `/metrics` has
`topo_syslog_*`, `/api/health` has `syslog_listening`. A device is matched to a row at query time by its `mgmt_ip` or hostname alias, never
stored.

**Pulling from the other servers** (`app/syslog_pull.py`, `pull_once` in `syslog_core.py`, constraint 26; the same two files as in
confetti-traffic): a device logs to one Confetti server only. The Settings page has a switch per other server (`GET|PUT /api/settings/syslog-pull`, off by
default, needs its URL) and `TOPO_SYSLOG_PULL_INTERVAL_S` (15) sets the round. A pulled message is stored with `origin` = that server and goes through the same
dedup and the poll trigger as a received one. `/api/syslog?after_id=N&origin=local` is what a puller reads from here (`X-Syslog-Max-Id` header).

**The poll trigger** (`app/syslog_trigger.py`, constraint 25): a stored message whose `%FAC-SEV-MNEMONIC` is in `TOPOLOGY_MNEMONICS` (today
`LINEPROTO-UPDOWN`, `LINK-UPDOWN`, `BGP-ADJCHANGE`, `BGP-NOTIFICATION`, `OSPF-ADJCHG`; extend it from messages seen on real routers) sets
`devices.poll_requested_at` on the device that sent it (a `mgmt_ip` alias, else an interface address held by exactly one device). The trigger
never polls and never touches `poll_state` or `next_poll_at`: `poller._tick` also claims an idle device with a request (and `fail_count = 0`)
and clears the request on claim, so the per-device lock and the backoff hold, and a spoofed packet costs at most one extra poll. At most one
request per device per `TOPO_SYSLOG_TRIGGER_MIN_S` (30). Only the five mnemonics above are known to the code; they have not yet been checked
against a real router's output.

### Reachability

`app/reach.py` (a daemon thread, `TOPO_PING_*`) pings every enabled device with a `mgmt_ip`, including
`role='node'`, and keeps `devices.reachable` (1 / 0 / NULL = not checked). A device is marked down after
`TOPO_PING_FAILS_TO_DOWN` (2) failures in a row; the first result is a baseline and emits no event.
`_ping` refuses any string that is not an IP address before it reaches a subprocess. Shown on the Devices
and device pages and as a red outline on the topology; BGP/OSPF sessions that are not Established / FULL
are drawn red and dashed, and so is an LLDP cable when a device reports its local port not up (`topology.port_key`
name match; a port that matches nothing is unknown, not down). The topology page's Devices list has a search box that
fades non-matching devices on the map; link details carry `local_port_state`.

### Discovery on a timer, and the LLDP crawl

- `app/discovery.py` runs the confetti-traffic import, subnet sweeps and the seed file every
  `TOPO_DISCOVERY_INTERVAL_S` seconds (default 0 = never; nothing runs unless configured via
  `TOPO_DISCOVERY_CONFETTI_URL` / `_SWEEP_CIDRS` / `_SEEDFILE`). Each source commits on its own, so one
  failing source never discards another's work. A device that did not exist before gets a
  `device_discovered` event. `POST /api/discover/run` runs it once by hand. There is no vCenter source.
- A second, opt-in timer imports the node fleet from the hub URL saved on Settings every `traffic_import_interval_min` minutes (0 = off, else 5-1440;
  `GET|PUT /api/settings/traffic-import`, `discovery.import_traffic`, `POST /api/discover/traffic` by hand). It runs even with `TOPO_DISCOVERY_INTERVAL_S=0`,
  is skipped when `TOPO_DISCOVERY_CONFETTI_URL` is the same hub, and commits on its own. `discovery.start()` is idempotent and is called again when the interval is saved.
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
and each device page.

### BGP flapping

`app/flap.py`: polls come every few minutes, so a BGP peer that drops and returns between two polls looks unchanged. A peer
change is (a) the state going to or from Established between two polls, or (b) the state Established at both polls but the
Up/Down timer smaller than before (`flap.uptime_seconds`; the timer only grows, so a smaller value is a reset, event
`bgp_reset`). Each change is a row in `bgp_changes`. A peer with `TOPO_FLAP_COUNT` (3) changes inside `TOPO_FLAP_WINDOW_S`
(3600) is flapping: `adjacencies.flapping=1`, one `bgp_flapping` warning when it starts, one `bgp_flap_stopped` info when it
ends. Idle/Active/Connect cycling of a peer that never comes up is not counted. BGP only (OSPF has no timer to read), nothing
on a first poll, nothing from a failed task, no device command. The map draws a flapping session thick, yellow and dotted
(`edges[].flapping`), the device page and the dashboard's Sessions down / flapping widget show a `flapping` pill.

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

### Updating a deployed VM, backups

The login banner compares the installed `COMMIT` file (written by a no-argument update) with GitHub's newest commit and says whether a newer build exists (3 s timeout, so no internet only prints a notice). `topo-update.sh` with no arguments downloads the main branch tarball from GitHub (`TOPO_UPDATE_URL` overrides) and updates without asking; `topo-update.sh [-y] <topo-dir|tarball>` (also `--rollback`; with a dir/tarball and no `-y` it shows the installed build and asks,
a closed stdin cancels) runs the new tree's `build-topo.sh --update` (the build's own install steps, so no
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
The network is set with `set-static-ip <ip/cidr> <gateway> [dns] [hostname]` or the login wizard; defaults for the
poller go in `topo.env` (0600).

**Container:** `container/Dockerfile` + `docker-compose.yml` (context `topo/`): same Alpine packages and
pinned pip packages as the VM, `TOPO_HEALTH_SERVICES` emptied (no OpenRC). `container/docker-compose.dev.yml` is the dev setup: `topo/` mounted live (restart the
service after a change, no rebuild), `--profile mock` = seeded fake lab on 8080, `--profile lab` = empty DB on 8081.
There is no Packer template: topo is installed once per environment, so the build does not prepare a VM for cloning (it keeps host keys, DB and machine-id).

## Seeding paths (device discovery)

| Path | Route | Notes |
|---|---|---|
| Manual | `POST /api/devices` | Operator-entered, from the Devices page |
| Subnet sweep | `POST /api/discover/sweep` | TCP connect to port 22 only (a firewall with SSH off must be added by hand or seed file) — coarse on purpose; the poller's own `version` task does real identification next cycle |
| YAML seed file | `POST /api/discover/seedfile` | `seed/devices.yaml.sample` shows the shape |
| confetti-traffic import | `POST /api/discover/confetti` (typed URL) or `POST /api/discover/traffic` (the URL saved in Settings; Settings can also re-import on a timer, `GET\|PUT /api/settings/traffic-import`) | `GET <hub_url>/endpoints`, imported as `role='node'`, `vendor='alpine'`, `platform='confetti_node'` — managed through the hub, never SSH-polled. `site` is the endpoint's `group_name`, which is confetti-traffic's *effective* group: a group set on its dashboard overrides what the node registers, so an override changes `site` on the next import |
| LLDP crawl | `POST /api/lldp/adopt` | A neighbor with an unknown management IP, added inventory-only (polling off) |

## Project Structure
```
topo/
  build-topo.sh   — installs the app on a fresh Alpine VM (one install per environment, no template)
  serve.py            — production entrypoint (reads TOPO_PORT at runtime)
  run.sh              — foreground launcher for debugging
  app/
    app.py             — Flask routes (single file; splits into its own module once a section outgrows
                          a screenful — see topology.py, identity.py, poller.py)
    config.py          — env-var config
    db.py              — get_db / connect / init_db / sqlite_now / iso / sqlite_ts_arg / like_arg
    identity.py        — device identity ladder, ingest/observe/merge
    poller.py          — background polling scheduler (SSH or XML API)
    topology.py        — the graph, ghost nodes, interface-IP peer resolution
    events.py          — emit() change events, is_baseline() first-poll guard
    platforms.py       — per-platform transport/commands/parsers (IOS over SSH, PAN-OS over the XML API)
    reach.py           — ICMP reachability thread
    discovery.py       — scheduled discovery thread (off by default)
    lldp_crawl.py      — adopt unknown LLDP neighbors as inventory-only devices
    metrics.py         — /metrics Prometheus text
    history.py         — topology snapshots on change (TOPO_SNAPSHOT_*)
    rendering.py       — sandboxed Jinja2 for config templates, secret masking (constraint 27)
    drift.py           — template vs latest running-config backup (constraint 32)
    ipam.py            — overlap and duplicate-address findings
    tools_routes.py   — the Flask Blueprint `tools` with the IPAM, templates, /configs, backups, drift, vCenter import
    syslog_core.py     — syslog parser + the one insert path; the SAME file in confetti-traffic (constraint 24)
    syslog_server.py   — UDP syslog listener, stores through syslog_core
    syslog_trigger.py  — a topology message asks for an early poll of its device (constraint 25)
    syslog_pull.py     — pulls syslog from the other Confetti servers on a timer; the SAME file in confetti-traffic (constraint 26)
    auth.py            — optional access token (TOPO_AUTH_TOKEN): /login, cookie or bearer; off by default
    find.py            — the header search: IP, prefix, MAC or text over stored tables (constraint 37)
    backup.py          — database backup, download, restore and the topo.env export (constraint 36)
    collectors/        — ssh.py, panos.py (XML API), snmp.py, vcenter.py, sweep.py, confetti.py, seedfile.py
    parsers/           — ios.py (regex only), panos.py (XML)
  templates/           — base.html (head, header/nav, theme + layout pickers, footer, Retro taskbar)
                         + dashboard (widgets: system, services, activity, devices, events, sessions down, open conflicts, unknown LLDP neighbors, map changes; the Devices widget has click-to-sort headings and a Columns chooser, kept in `confetti-topo-dashboard-devices`; every widget has a Collapse / Expand button (fold.js), and dashgrid.js re-runs Auto sort when one is folded or opened), devices, device, changes (Recent changes: filter bar, summary tiles, sortable events table with type/change/before/after from `events.describe()`, banded per device, details pane, right-click menu), topology, syslog, conflicts, settings, ipam, alerts, templates_editor (CodeMirror in static/vendor/codemirror), find, backups, each `{% extends "base.html" %}`
  static/theme.css     — the ONE place colours live (Dark, Light, Monokai,
                         Terminal green, Rainbow). Pages must not define their own :root colours
  static/layout.css    — Classic, Modern, Retro 95, MS-DOS (MS-DOS has two colour variants, DOS Blue and Neon Green: `data-variant="green"` on `:root`, a header dropdown shown only in MS-DOS, saved in `confetti-topo-dos-variant`; it also has a keyboard layer:
                         base.html's .dos-menubar / .dos-fkeys / .dos-help, a row cursor, and one keydown IIFE at
                         the end of theme.js that does nothing in any other layout; same block as in confetti-traffic). Loaded AFTER theme.css
  static/confetti-links.js — the header shortcut buttons to the other Confetti projects (URLs from the Settings page, `GET /api/settings/links`); opens in a new tab; the SAME file as in confetti-traffic, copy it by hand and keep the two identical
  static/dashgrid.js   — the dashboard's (and device page's, `page: 'device'`; the saved key is `<prefix>-<page>-layout`) movable/resizable widgets (`data-box` is the attribute name, kept): drag a `.section[data-box]` by its header (pointer events on `document`, not pointer capture: moving the node in the DOM drops the capture), drag the corner grip to resize (width snaps to grid columns, height free); a widget dropped on the middle band of another (outlined `dg-join`) shares that row with it and the widgets already on it, the columns split between them (`joinRow`, the first is pinned to column 1 via `state.row`); while dragging, a `.dg-ghost` clone follows the pointer and the original stays in the grid as a grid-pattern drop place (`.dg-drag`); a drop in a row's empty space (`emptySpace`: right of its last widget or left of its first) puts the widget beside that row's widgets; "Reset layout" button and, when the page passes `autoSort: true`, an "Auto sort layout" button (`autoSort()`: drops saved sizes, puts the non-full-width widgets in rows of similar height, widens a last short row to fill it, gives each widget at least the columns its content needs (`needColumns`, no sideways scroll), stacks widgets beside the tallest one when that leaves less empty space (it spans their grid rows: `state.rows`, `stackPlan`), lets up to three short `.wide` widgets share a row and stretches every widget to its row height (`state.fill`); best effort; a widget hidden at the time (display none) is skipped and keeps its default size); a "Lock layout" button (`state.locked`, saved with the layout) stops moving, resizing, Reset, Auto sort and the re-sort on fold until "Unlock layout"; saved per browser in `confetti-topo-dashboard-layout`. Injects its own CSS; the SAME file as in confetti-traffic, copy it by hand and keep the two identical
  static/ctxmenu.js    — right-click menus, loaded once by base.html (menu CSS at the end of layout.css); the SAME file as in confetti-traffic, copy it by hand and keep the two identical. A row carries data-ctx="<type>" and the page calls ctxMenu.register(type, fn(el, ev)); `ctxMenu.extend(fn(target, ev))` adds items to every menu (colresize.js); one document-level listener finds the row. Fill data-* attributes with ctxMenu.attr(), never esc(). A destructive item is `danger: true`, goes last and must ask confirm() (R29 fails a page that has one without). Used by every page: rows and a `page` menu for empty space (`document.documentElement.setAttribute('data-ctx', 'page')`; header/nav/footer keep the browser menu) on the dashboard, devices, device (one `row` menu per table body), conflicts, settings and changes pages, and the topology map (ctxMenu.open from vis's `oncontext`). Rows are looked up by id when the menu opens, because the tables are rebuilt on every refresh; a destructive item reuses the page's own confirming function. R29 checks it
  static/fold.js       — Collapse / Expand for `.section[data-fold]` (`data-fold-default="closed"`) and every dashboard widget; the
                         choice per page and box in `confetti-topo-folded`; `fold.open(id)`. topo only (not shared)
  static/tablefilter.js — a text filter for a long table (`<input data-filter-table="id">`; hides rows with `filt-off`, applied again after every redraw). topo only (not shared)
  static/colresize.js  — column widths for every table with a header row (drag a header edge, double-click to reset, right-click "Resize (compact)"
                         through `ctxMenu.extend`); per table in `confetti-topo-cols-<page>-<table id>` (device pages share one; a table can set
                         `data-col-key`, opt out with `data-no-resize`); also picks up tables built later. topo only (not shared)
  static/colpick.js    — the Columns chooser (`colPick.attach(table, btn, key, hidden)`), used by the Devices page
                         (`confetti-topo-devices-columns`); the dashboard's Devices widget has its own chooser
  static/theme.js      — THEMES + LAYOUTS lists and the two header dropdowns (confetti-topo-theme,
                         confetti-topo-layout in localStorage), themeColor() for canvas colours,
                         confettiBlast(), Shuffle, the header health dot
  static/vendor/       — vis-network/ (vendored pinned version, no CDN), codemirror/ (the template editor)
  seed/devices.yaml.sample
  services/ — confetti-topo.initd, login-setup.sh, topo-backup.sh
  scripts/  — topo-setup.sh, topo-update.sh, topo-db.sh (database backup/list/env/restore on the VM), seed_mock_lab.py (fake lab in a new scratch DB),
              capture_readme.py (re-records docs/img/ from that mock lab; run after any UI change)
container/ — Dockerfile + compose
dev/regress.py — the regression gate (see below); dev/samples/ = parser fixtures;
dev/requirements-dev.txt — ruff (dev machine only); ruff.toml — F rules only (repo root)
docs/ — TOPOLOGY-MAPPING.md, HANDOFF.md, ROADMAP.md, img/
.claude/ — agents: check-red-prover, dashboard-ui-checker, lab-validator, leak-scanner, readme-image-capturer;
           skills: 6 reference skills (cisco-ios-patterns, netmiko-ssh-automation, network-bgp-diagnostics,
           network-config-validation, network-interface-health, panos-xml-api) and 7
           confetti-topo-* (api, dev-run, add-platform, add-task, lab-validation, release, ui). regression-gate,
           docs-drift-checker, confetti-interop-checker and the alpine-appliance and constraint-regression
           skills are user-level (~/.claude), not in this repo
```

The look (themes, layouts, Shuffle, confettiBlast) is copied by hand from confetti-traffic's hub UI.
Keep the two in step; `confetti-interop-checker` reports drift.

## Design Decisions
- **Mirror the sibling stack exactly**: Flask + SQLite (WAL) + waitress, vanilla JS with no build step, Alpine
  golden image, OpenRC.
- **Read-only device access.** No Netmiko config-push path, no XML API config or commit.
- **Credentials**: per field, a device's own `credentials` row, then the Settings page (`settings` table), then
  the env defaults (`TOPO_SSH_*`); an empty value falls through (`ssh.resolve_credentials`, R28). The Settings
  page also applies or removes per-device credentials on several devices at once. Plaintext in v1 —
  `topo.db` must be `0600` (`checkpath --directory --mode 0700` on `/var/lib/confetti-topo` in the OpenRC
  service is the current mitigation).
- **The API is unauthenticated unless `TOPO_AUTH_TOKEN` is set** (`app/auth.py`, constraint 18), same trust model as
  the siblings; do not expose it either way. A sweep CIDR or a
  confetti-traffic URL given to the API makes the server connect there.
- **Keep code simplistic to avoid over-engineering.** A network engineer reading this at 2am should be able
  to follow it without decoding an abstraction.

## Working style
- Keep output minimalistic and use simple English.
- Do not output large code snippets. Point at `file:line` and say what changed; the file itself is the record.
- Do not display code changes in output unless asked to.
- Always check a UI change in a real browser (Claude in Chrome, or the `dashboard-ui-checker` agent) before saying it is done:
  load the page, use the changed control, and read the console for errors. A regression run or a code read is not enough.
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
8. **`role='node'` devices are never SSH-polled.** `poller.py`'s device-selection query is
   `role != 'node' OR platform = 'confetti_node'` — confetti-traffic's imported Alpine test VMs would fail every IOS `show` command; a
   managed node (platform `confetti_node`) is asked through its hub instead (constraint 39), and `POST /api/devices/<id>/poll` refuses any other node.
9. **A subnet sweep's ingest passes no `hostname` field**, only the `mgmt_ip` candidate. A known device's real
   hostname must never be overwritten with a raw IP string on re-sweep.
10. **confetti-traffic's imported nodes get explicit `vendor='alpine'`, `platform='confetti_node'`** in
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
15. **PAN-OS is read-only and keeps its secrets out of URLs.** The XML API collector sends only `keygen` and
    `op` requests, and the `op` command must start with `<show>`; the password travels in the POST body, the API key
    in the `X-PAN-KEY` header, never in a URL, a stored output or an error text. A failed login fails every task
    and deletes nothing (constraint 14). Checked by R30 (R15 was already taken by a cross-file check).
16. **LLDP chassis/name matching is optional, unique-owner only, query-time only.** Both switches
    (`lldp_match_chassis`, `lldp_match_name`) default to off, so constraints 3 and 13 hold unless an operator turns
    one on. A match applies only when exactly one device has the MAC or name, never to the reporting device itself,
    never over the poller's stored match or an interface-IP match, and is **never stored as an alias**
    (`devices.mgmt_mac` is not an identity; constraints 1 and 12). Checked by R31.
17. **A routes poll that parses to nothing deletes nothing, and only the kinds it holds delete.** A router always has
    connected routes, so an empty table means a parse failure; a FIB that could not be read must not wipe the stored
    FIB. Route changes are one `routes_changed` event per poll, and none on a device's first `routes` poll
    (constraint 11). Checked by R32.
18. **The access token is optional, off by default, and never changes the poller.** With `TOPO_AUTH_TOKEN` empty nothing is
    checked. With it set, pages redirect to `/login`, `/api/*` and `/metrics` answer 401, and `/api/health`, `/static`, `/login`
    and `/logout` stay open. The cookie is HttpOnly, SameSite=Strict and holds a hash, never the token; `next` may only be a
    path on this server. Checked by R33.
19. **VLANs, trunks and spanning tree are asked of switches only, and a failed answer deletes nothing.** A router answers `show vlan brief` with
    `Invalid input`, so `ssh.run_tasks` drops the `vlans` task unless `_is_switch`; PAN-OS has none. A switch always has VLAN 1,
    so an empty VLAN parse is a failure, not a real answer (constraint 14); a `%` answer fails all four tasks (the `mac` task also keeps nothing on an empty parse). Checked by R34.
20. **Topology history is written only when the map changed, pruned by age, read-only over the API, and holds no secrets.** A snapshot
    is the `/api/topology` data and nothing else (no credentials, no device output). An unchanged map adds no row, and
    `TOPO_SNAPSHOT_INTERVAL_S=0` stores nothing. A history failure must never stop the poller tick. Checked by R35.
21. **The URL of the other Confetti project is only a link target: http(s) with a host, validated on write, fetched only by the syslog pull.** The Settings page
    keeps the Traffic URL (`settings` key `url_traffic`, `GET|PUT /api/settings/links`); a value that is not
    http:// or https:// with a host, or has a user:password, whitespace or markup characters, is a 400 and changes nothing (all or nothing); an empty
    value clears. `static/confetti-links.js` (the same file as in confetti-traffic) turns it into a small header button that
    open in a new tab with `noopener`, re-checks http(s) and never builds HTML from the URL. The server connects to one only for the syslog pull (constraint 26, opt-in per server) and the node import (`POST /api/discover/traffic`, a click, or the opt-in `traffic_import_interval_min` timer, 0 = off, `discovery.import_traffic`), and never otherwise. Checked by R36.
22. **BGP flap detection works only from what a poll already read, counts only changes to or from Established or a timer reset of an
    Established peer, and is silent on a first poll.** A peer cycling Idle/Active/Connect never flaps. Flapping = `TOPO_FLAP_COUNT`
    changes inside `TOPO_FLAP_WINDOW_S`; one `bgp_flapping` event at the start and one `bgp_flap_stopped` at the end, never one per
    poll. BGP only, no device command, `bgp_changes` is age-pruned in the poller tick. Checked by R37.
23. **Default and Connected are drawn from stored routes and interfaces at query time and store nothing.** A default route's next hop
    resolves only to the unique owner of that address, never to the device itself, else it is a ghost; a RIB row only (not FIB).
    A Connected subnet skips any subnet holding a device's `mgmt_ip`, /32s and subnets with one device; three or more devices
    meet at a `subnet:` node. Never an alias (constraints 1, 12). No new device command. Checked by R38.
24. **Syslog is parsed and stored by one shared file, `app/syslog_core.py`, the same in confetti-traffic and confetti-topo.**
    Copy it by hand and keep the two byte-identical, with `dev/samples/syslog_cases.json` (a line in, the fields out; also identical).
    `parse()` never raises and never drops a line (RFC 3164 and Cisco, plus RFC 5424). `insert()` is the only way a row is stored, and it
    never commits. It skips a message when its `(origin, origin_id)` is already stored, or when a copy was pulled from another server within `dedup_s` (`syslog.origin`, `origin_id`,
    `msg_hash`); two copies this server received itself are both kept (a link that flaps twice is two messages). Pulling from another
    server is constraint 26. Checked by R40 and R40-sync.
25. **A syslog message may only ask for an early poll; it never polls and never changes the topology.** `syslog_trigger.maybe_request` only
    sets `devices.poll_requested_at`, and only for a topology mnemonic from a device that is enabled, not `role='node'` and not failing
    (`fail_count = 0`), at most once per `TOPO_SYSLOG_TRIGGER_MIN_S`. The source address resolves to a `mgmt_ip` alias or to an interface address
    held by exactly one device, and is never stored as an alias (constraints 1, 12). `poller._tick` claims a requested device through the
    normal `idle` check, sets `'running'` and clears the request, and skips a failing device (constraint 14's lock and backoff hold). The
    listener never sets `allow_reuse_address`. Checked by R41 and the live R20 (a real UDP datagram is stored and parsed).
26. **Syslog may be pulled from the other Confetti servers, opt-in per server, and only their own messages.** `GET|PUT /api/settings/syslog-pull` has a switch per server
    (`syslog_pull_<name>`, off by default; the URL from constraint 21 must be set first). Switching on starts from the other server's newest message, never its history
    (`syslog_cursor_<name>`; a different URL or an id that goes backwards resets it). The request is `GET <url>/api/syslog?after_id=N&origin=local` only, http(s) only, no
    redirect followed, a server that does not answer with `X-Syslog-Max-Id` is refused, every field is length-limited text and `msg_hash` is recomputed here. `origin=local`
    is what stops a message travelling around a loop. Rows go through `syslog_core.insert` (constraint 24). A failed request stores nothing and keeps the cursor; one server
    failing never stops the other. The token for a server that has one is write-only: sent in an `Authorization` header, never in a URL, never returned by a GET (traffic
    has none, so topo sends none). Checked by R42.

27. **Config templates render in a sandbox with `StrictUndefined`.** `rendering._ENV` is a `SandboxedEnvironment`; an unset variable
    raises `RenderError` instead of printing nothing, and an attribute escape (`__class__`, `__init__.__globals__`) is refused. A template is
    operator text that a device will pull and apply. Checked by R43.
28. **`GET /configs/<key>.cfg` and `/configs/` stay unauthenticated, also with `TOPO_AUTH_TOKEN` set.** A device cannot send a token with
    `copy http://...`. Everything else (the templates page and API, backups, drift) still needs the token. Do not add a login check to the
    pull route, and do not widen `auth.OPEN_PREFIXES` beyond `/static/` and `/configs/`. Templates carry no real secrets
    (`check_hardcoded_secrets`). Checked by R44.
29. **A running-config is stored only when complete and only when it changed, and leaves the API masked.** `parse_running_config` returns ""
    for an error text or a read without `end`, and the `config` task then fails and stores nothing (constraint 14); volatile lines
    (`Building configuration`, last-change stamps, `ntp clock-period`) are dropped so an unchanged config is one version. Secrets are masked by
    `rendering.redact_secrets` in every API answer unless `?raw=1`. Checked by R45.
30. *(Removed 2026-11-23 with the alerting feature; the number is kept so the others keep theirs. R46 and R47 are retired.)*
31. **SNMP is read-only and refuses what it cannot do safely.** `snmpget` / `snmpwalk` only, v1 and v2c (v3 fails with a clear message), the host
    must be an IP address before it reaches a subprocess, a missing tool names the apk package. The community is a credential: per device, then Settings,
    then `TOPO_SNMP_COMMUNITY`. It is returned by one GET only: `/api/settings/credentials` gives the community saved on the Settings page (a plain text field, not a password field, by the operator's choice), never an environment one, never a device's own. Checked by R48 (and R28 for the order).
32. **Drift is containment by parent chain, not text equality.** Every line the rendered template produces must be in the latest backup under the
    same parent; extra lines in the running config are not drift, secret lines are skipped, and a template `no X` is met when `X` is absent.
    `config_drift` / `config_compliant` are events only on a change between the two, not on the first evaluation. Checked by R49.
33. **Interface error history is sampled on change, and a counter reset adds nothing.** `interface_stats` gets a row when the counters change plus an
    hourly heartbeat; `ssh.growth` ignores a decrease (a reload). Checked by R50.
34. **A deployed database upgrades in place.** `db.init_db` runs on every start over the existing file: `CREATE TABLE IF NOT EXISTS` never adds a
    column to an existing table, so every new column needs its own `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` (with a default if `NOT NULL`).
    Nothing may drop or rewrite rows, and a second run changes nothing. Checked by R51 (the oldest `db.py` in git builds the old schema).
35. **Traffic rates come from the device's own counters, and a counter reset gives a gap, not a number.** The rate column is what IOS reports;
    the graph's rates are computed from two byte samples (`ssh.rates`), and a decrease (reload, `clear counters`) gives `None`. Byte samples follow
    constraint 33's on-change + hourly rule. No new device command. Checked by R52 (fixture `ios_interfaces_counters.txt`).
36. **A database backup is only handed out on a POST, only by a stored file name, and a restore saves the current database first.** The backup
    holds the credentials (plaintext, like `topo.db`): the folder is 0700, `backups/` is git-ignored, `path_of` accepts a bare `topo-*-<time>.db`
    name only. A restore refuses a file that fails `quick_check` or has no `devices` table and changes nothing then; otherwise it saves
    `topo-pre-restore-<time>.db` before copying in. The `topo.env` export blanks every key with PASS, SECRET, TOKEN, COMMUNITY or KEY. Checked by R53.
37. **Find and Test login only read.** Neither writes a row, an alias, an event or a poll state; Find resolves an owner at query time only
    (constraint 12), Test login runs the `version` task alone and never on `role='node'`. Checked by R54.
38. **A failed poll says why, and never leaks a secret.** `app/pollerr.py` `explain()` is the one place that turns a caught error (Netmiko, paramiko, requests, net-snmp) into a one-line reason plus advice: login rejected, port refused, no answer within N s, no route, DNS, command did not finish (a `ReadTimeout` is not a connection problem), SSH negotiation, enable secret, TLS, API login, SNMP. Login errors are fixed texts; the library's own text is used only for negotiation and unknown errors, cut to one line. `poller._finish(conn, id, any_ok, errors)` writes the reason into the one `device_unreachable` event (subject carries the first sentence, detail the whole text; still once, constraint 11) and into `devices.last_poll_error` on every failed poll; a good poll clears it. The Devices tooltip, device page and topology toast show it. Checked by R55.

39. **A Confetti Traffic node is managed from its hub, read-only.** Platform `confetti_node` (transport `hub_api`, `collectors/hub.py`, role stays `node`):
    the poller asks the hub that imported the node (`device_sources` source `confetti`, ref = its URL), never the node. Per poll one cached read
    (20 s, shared by all nodes of a tick) of `GET /endpoints` and `GET /api/lldp`: GET only, http(s), no redirect, 5 s timeout, 2 MB cap, no credentials
    sent, every field length-limited text. Tasks `version` (build, `devices.hub_info` = clock and update state; never a serial or a hostname),
    `interfaces` (the one reported address and subnet) and `lldp` (the node's own LLDP report: the switch and port it is plugged into, drawn on
    the existing LLDP layer). A node the hub no longer lists, or whose `last_seen` is older than 15 minutes, or an unreachable hub, fails all
    three tasks with a reason (`pollerr`, constraint 38) and deletes nothing (constraint 14); an empty LLDP list is a real answer and clears. A plain
    `role='node'` device on another platform is still never polled. Checked by R56 (and R8, R10, R19).

## Regression gate

`python dev/regress.py` (stdlib only, except that `R39` runs ruff: `pip install -r dev/requirements-dev.txt`, else R39 is `NOT RUN`; `--static` skips the live tier, `-v` shows detail for passes) is the
gate to run before handing back any code change. `R1`..`R14` are constraints 1..14 above; `R15`..`R19` are
cross-file checks (docs vs code task count, shell/Alpine hygiene, poller lock, files the build scripts copy,
platform registry and read-only commands); `R20` starts the real server from a temp copy and drives it over
HTTP; `R21`..`R26` cover the ICMP state machine, `/metrics` format, LLDP crawl, scheduled discovery, the
container files and the BGP `Idle (Admin)` parse found on a real 15.4 router; `R27` checks the OpenSSH
(not Dropbear) setup; `R28` the credential order (device > Settings > env), that no GET returns a secret and
that a bulk apply is all-or-nothing; `R29` the right-click menus (shared `static/ctxmenu.js`, quote-safe `data-*`, scripts parse); `R30` (constraint 15)
the PAN-OS XML API: real-output fixtures, read-only `<show>` ops, secrets out of URLs, the poller's transport; `R31` (constraint 16) the optional LLDP chassis/name matching and its settings API; `R38` (constraint 23) the Default and Connected layers; `R37` (constraint 22) BGP flap detection; `R36` (constraint 21) the other Confetti project's URL; `R35` (constraint 20) topology history; `R34` (constraint 19) the VLAN task; `R33` (constraint 18) the optional access token; `R32` (constraint 17) the routes task: fixtures, empty table keeps rows, one event per changed poll; `R39` is the ruff `F` lint over `topo/` and `dev/` (not a constraint check); `R40` (constraint 24) the shared syslog parser and insert rule, `R40-sync` that `syslog_core.py`, `syslog_pull.py` and `syslog_cases.json` are byte-identical in both repos (`NOT RUN` if confetti-traffic is not next to this repo); `R41` (constraint 25) the syslog poll trigger; `R42` (constraint 26) the syslog pull; `R43`..`R50` (constraints 27..33) the checks that came with the former butler's features: template sandbox, the open `/configs/` pull, config backups,
SNMP, drift and interface error history; `R51` (constraint 34) the in-place upgrade of an old database; `R52`..`R55` (constraints 35..38) traffic rates, database backup/restore,
Find / Test login storing nothing, failed-poll reasons (R55), the managed Confetti Traffic node (R56) and IPAM prefixes (R57); the next constraint (40) takes `R58`. Output is one line per check
and a verdict: `CLEAR`, `CLEAR WITH GAPS` (something not run) or `BLOCKED`. The unit tier imports the app, so
it needs the app's own Python packages (else those checks are `NOT RUN`). Each check must be proven red by
reverting its fix in a scratch copy. Adding a constraint means adding its check with the same number; the
`regression-gate` agent runs this script. Doc-only changes need `docs-drift-checker` instead.
