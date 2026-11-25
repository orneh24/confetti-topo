# confetti-topo — handoff

## Topology page: compare two versions (2026-11-25)

New Compare row on the Topology page (CLAUDE.md, "Topology history"), browser only. Also added the missing `--orange` theme colour
(the Connected layer was drawn blue) and stopped node drags in History view from saving places. Checked in Chrome on the mock lab and
on the dev machine's real database (22 nodes), Rainbow and Dark themes, Classic layout.

Not verified: Retro 95 and MS-DOS layouts. No new `dev/regress.py` check (including for "nothing is saved in History / Compare").
`docs/img/` is not re-recorded. confetti-traffic was not checked for the `theme.css` change. R15 fails on a CRLF checkout of
`CLAUDE.md` (its pattern needs LF line ends); it matches the LF text.

## Database restore validated on the lab VM (2026-11-22)

The lab VM (Alpine, updated with `topo-update.sh -y <dir>` from an older build without the merged tables; 6 devices) was put on this build and
the backup/restore was run there:
- **Web:** a Settings download, a marker map note, then a restore of that download: the note was gone, `topo-pre-restore-*.db` was saved,
  the server kept the same process (no restart) and the next poll ran and released its lock. A non-database upload was refused (400), a
  download by a path name was 404 and a GET download 405.
- **Old schema:** a nightly backup from 2026-11-16 (13 tables, no `config_versions`, no `map_notes`) restored through the web upgraded in
  place: notes could be saved, `interfaces.in_bps` / `out_bps` were added, every page answered 200.
- **`topo-db.sh`:** `backup`, `backup <file>`, `list`, `env` (no secret values; `TOPO_SSH_USERNAME` is exported, passwords are not), a junk file
  refused, `restore` without `-y` and a closed stdin cancelled, `restore -y` stopped and started the service and came back healthy with the data.
- **Bug found and fixed:** backup copies kept the live database's WAL mode, so opening one (a restore check) left `-wal`/`-shm` files beside it,
  in the backups folder and next to the file given to `topo-db.sh restore`. Backups are now written in rollback-journal mode (`backup.make`,
  `topo-db.sh backup_to`), the web restore removes its upload's side files, and `topo-db.sh restore` checks a copy. R53 now checks the mode and
  that a restore leaves nothing behind. Re-run on the VM after the fix: no leftovers from either path.

Not verified: polling after the restore against a real router with a working login (the VM has no router login stored; every device has
been failing authentication since 2026-11-18, before this change). The nightly `topo-backup.sh` and `topo-update.sh` still write WAL-mode
copies (harmless now: both restore paths clean up after reading one). The lab VM's `topo.db` was mode 0644 before the first restore; a
restore leaves it 0600.

## Butler retired as a peer (2026-11-22)

Removed the Butler URL (Settings, `url_butler`), its header button, the syslog pull from butler, the `butler` origin name in `syslog_core.PEERS`,
and the "Butler" label (Classic's second row is now Tools). The shared files (`syslog_core.py`, `syslog_pull.py`, `syslog_cases.json`,
`confetti-links.js`, `ctxmenu.js`, `dashgrid.js`) were changed the same way in `../confetti-traffic` (working tree there, not committed). The lab
router notes moved into this project's memory, and `network-config-validation` and `network-interface-health` were copied from butler's skills.
Old `settings` rows and syslog rows with origin `butler` stay in a deployed database and are harmless.

## Butler import removed (2026-11-22)

Removed `POST /api/discover/butler`, `collectors/butler.py`, the Devices page import form, the scheduled source (`TOPO_DISCOVERY_BUTLER_URL`) and the
import test in R24. Kept: the Butler URL on Settings, its header button and the syslog pull from butler. Existing `device_sources` rows with source
`butler` stay as they are. Menu order and the `tools_routes.py` rename were done the same day.

## Merge of confetti-butler (2026-11-21)

confetti-butler's functionality was ported into this project, which keeps its name, service, paths and `TOPO_*` variables. `../confetti-butler`
was not touched, and no butler database import exists (decided with the user).

- **Ported (same code, `BUTLER_*` → `TOPO_*`):** `rendering.py`, `drift.py`, `ipam.py`, `alerts.py`, `collectors/snmp.py`, `collectors/vcenter.py`;
  the `config` task and error-counter history in `collectors/ssh.py`; `parse_running_config`; the `snmp` platform; the routes as `app/tools_routes.py`
  (a Blueprint); pages `ipam.html`, `templates_editor.html` (+ `static/vendor/codemirror`), `alerts.html`; the device-page boxes (template, SNMP credentials,
  drift, config history and diff, interface errors); the Template column on Devices; SNMP defaults on Settings; butler's extra `/metrics` lines
  (`topo_interface_*_errors`, `topo_alert_*`, `topo_ipam_findings`, `topo_syslog_rows`).
- **Schema (additive, on an existing `topo.db`):** tables `ipam_findings`, `templates`, `template_versions`, `template_vars`, `config_versions`,
  `interface_stats`, `alert_rules`; columns `devices.template_name` / `drift_status`, `credentials.snmp_community` / `snmp_version`,
  `interfaces.input_errors` / `crc_errors`, `events.alerted` (existing events are marked alerted, so nothing old is sent).
- **Menu:** Classic shows Alerts, IPAM and Templates as a second row (`.nav-row-tools`, label Tools; first named Butler, renamed 2026-11-22); the other layouts list them with the rest.
- **Not ported:** the Packer template, `firstboot.initd`, butler's setup/update scripts (topo has its own), the butler-only agents and skills.
- **Constraints 27..33 and checks R43..R50** are new (butler's checks, adapted); R15, R19 and R28 were extended (eleven tasks, the SNMP platform,
  SNMP credentials), and R6/R20 list the new pages. `python dev/regress.py` → `CLEAR`, 51 checks including the live tier.
- **Verified:** the gate, and the mock lab (`scripts/seed_mock_lab.py` now has a template, two backups per router with one drifted, error history, an
  IPAM overlap, an alert rule and syslog) over HTTP and in a browser.
- **Not verified:** the `config` task, drift and error counters on a real router; SNMP against a real device; an alert webhook or mail delivered to a
  real target; the vCenter import; an upgrade of a deployed VM (`topo-update.sh`) with the new schema; `net-snmp-tools` in `build-topo.sh` on a real Alpine VM.

## What this session did (2026-10-14)

Split the topology mapping out of confetti-butler into this project. The plan and the answers behind it:

- **Fully standalone.** Own inventory, identity ladder, credentials, discovery, ICMP checker, SSH poller, LLDP
  crawl, events and graph UI. No runtime link to butler.
- **Copied from butler, then trimmed:** `identity.py`, `poller.py`, `reach.py`, `discovery.py`,
  `lldp_crawl.py`, `events.py`, `metrics.py`, `collectors/{ssh,sweep,seedfile,confetti}.py`,
  `parsers/ios.py`, the base page, themes and layouts, the build, update, backup and setup scripts, the
  container files, and the regression harness. Left behind: IPAM, config templates and drift, running-config
  backup, alerting, SNMP, vCenter discovery, interface error history, Packer, the lab-butler migration.
- **New:** syslog (UDP receiver, `/syslog` page, early-poll trigger, pull from butler and traffic: `syslog_core.py`, `syslog_server.py`,
  `syslog_trigger.py`, `syslog_pull.py`, constraints 24-26), `app/topology.py` (the graph moved out of `app.py`), interface-IP peer resolution
  (constraint 12), one edge for a session both routers report, `GET /api/lldp-neighbors`, the
  `?device_id=` filter on `/api/adjacencies`, a per-device LLDP and BGP/OSPF section on the device page,
  `interfaces` polled for addresses only.
- **Regression gate:** `python dev/regress.py` → `CLEAR`, 26 checks including the live tier. R12, R13 and R14
  are new; each was proven red by reverting its logic in a scratch copy.
- **Agents and skills** in `.claude/`: the three reference skills (`cisco-ios-patterns`,
  `netmiko-ssh-automation`, `network-bgp-diagnostics`) copied from butler; `confetti-topo-api`, `-dev-run`,
  `-add-platform`, `-lab-validation`, `-release`, `-ui` adapted; agents `leak-scanner`, `lab-validator`,
  `dashboard-ui-checker`, `readme-image-capturer`. `regression-gate`, `docs-drift-checker`,
  `confetti-interop-checker` and the skills `alpine-appliance` and `constraint-regression` are global.
- Local `git init`, nothing committed, no remote.

## Topology page: layout, grouping, labels (2026-10-19)

Saved layout (`node_positions`), "N unknown" groups, LLDP labels on hover, and the Butler/Traffic import
accepting a bare IP (`http://` added, `collectors/__init__.py` `base_url`). Checked in a browser on the mock
lab only; the 18-node real graph was not judged. A real mouse drag, the group's Expand menu item and the
group tooltip rendering were not exercised. R20 covers the positions API; no check covers the grouping or
the bare-IP import (both are page or collector behaviour).

## What is verified

- The regression suite, including a real server started from a temp copy and driven over HTTP.
- The pages and the graph in Chromium against the mock lab (`scripts/capture_readme.py`, a manual screenshot
  pass): no console errors, ghosts, red-dashed down sessions, red outline on a silent device, interface-IP
  peer resolution, one line per session.
- The IOS parsers and the BGP `Idle (Admin)` fixture, inherited from butler (where they were checked on real
  IOS-XE 17.3 and 15.4 routers).

## What is NOT verified — check before relying on it

- **No real router has been polled by confetti-topo.** Run the `lab-validator` agent: TOPOLOGY-MAPPING.md
  section 11, plus peers resolving through interface IPs and a one-edge session on `/topology`.
- **`build-topo.sh`, the OpenRC scripts, `topo-update.sh`, the nightly backup
  have not run on an Alpine VM.** They are renamed copies of butler's, which have. The user plans a new
  Alpine VM on port 80.
- **Settings page (2026-10-19):** `/settings` sets the default SSH credentials (DB) and applies or removes
  per-device credentials in bulk; R28 covers it. Checked over HTTP in the mock container and with the Flask test
  client; the page was not yet looked at in a browser, and no real router has used these credentials.
- **Container (2026-10-19):** the image builds from `container/Dockerfile` and `docker-compose.dev.yml` (mock
  profile) runs healthy on Docker 29.8; topology API, `/metrics` and a restart that keeps the DB were checked. The
  production `docker-compose.yml` (port 80) was not run, and the lab profile was only checked for health and an
  empty device list.
- Alerting (rules on events and syslog, webhook/mail) is ported but not tried against a real target (see the 2026-11-21 section).
- Pairing of two sessions between the same pair of routers is by position (Established first). Wrong only if
  the two ends list them in a different order.
- On Windows `ping` can exit 0 on a "host unreachable" reply, so the ICMP checker misreads fake addresses
  there (use `TOPO_PING_ENABLED=false` for mock labs). Alpine and Linux `ping` are fine.

## Aligned with confetti-butler and confetti-traffic (2026-10-14)

Ported butler's update flow (`topo-update.sh [-y]` runs `build-topo.sh --update`, `TOPO_STAGE_DIR`), the
`BUILD` id and login hints, `set-static-ip` with DNS/hostname, the setup wizard, apk-warning-on-update, the sysctl pin, `resolv.conf` clean-up and the "Make it rain!" label.
**Not run on a VM yet** (`sh -n` and the regression gate only).

## SSH server switched from Dropbear to OpenSSH (2026-10-14)

Same change as confetti-butler (see its HANDOFF): `build-topo.sh` `switch_ssh_server()` installs `sshd` +
`openssh-sftp-server`, keeps root password login, converts the Dropbear ed25519 host key and removes Dropbear only
after sshd runs. R27 checks it. Logic tested in an `alpine:3.24` container with real `dropbear` / `sshd` binaries and
a fake `rc-service`. **Not run on a VM** (the topo build and update scripts have not run on one at all yet).
