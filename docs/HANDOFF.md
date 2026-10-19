# confetti-topo — handoff

## What this session did (2026-10-14)

Split the topology mapping out of confetti-butler into this project. The plan and the answers behind it:

- **Fully standalone.** Own inventory, identity ladder, credentials, discovery, ICMP checker, SSH poller, LLDP
  crawl, events and graph UI. No runtime link to butler.
- **Copied from butler, then trimmed:** `identity.py`, `poller.py`, `reach.py`, `discovery.py`,
  `lldp_crawl.py`, `events.py`, `metrics.py`, `collectors/{ssh,sweep,seedfile,confetti}.py`,
  `parsers/ios.py`, the base page, themes and layouts, the build, update, backup and first-boot scripts, the
  container files, and the regression harness. Left behind: IPAM, config templates and drift, running-config
  backup, syslog, alerting, SNMP, vCenter discovery, interface error history, Packer, the lab-butler migration.
- **New:** `app/topology.py` (the graph moved out of `app.py`), interface-IP peer resolution
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
- **`build-template.sh`, the OpenRC scripts, `topo-update.sh`, the nightly backup and the guestinfo first
  boot have not run on an Alpine VM.** They are renamed copies of butler's, which have. The user plans a new
  Alpine VM on port 80.
- **Settings page (2026-10-19):** `/settings` sets the default SSH credentials (DB) and applies or removes
  per-device credentials in bulk; R28 covers it. Checked over HTTP in the mock container and with the Flask test
  client; the page was not yet looked at in a browser, and no real router has used these credentials.
- **Container (2026-10-19):** the image builds from `container/Dockerfile` and `docker-compose.dev.yml` (mock
  profile) runs healthy on Docker 29.8; topology API, `/metrics` and a restart that keeps the DB were checked. The
  production `docker-compose.yml` (port 80) was not run, and the lab profile was only checked for health and an
  empty device list.
- **Butler's alert rules on BGP/OSPF/LLDP events no longer fire** once butler drops those tasks; topo has no
  alerting.
- Pairing of two sessions between the same pair of routers is by position (Established first). Wrong only if
  the two ends list them in a different order.
- On Windows `ping` can exit 0 on a "host unreachable" reply, so the ICMP checker misreads fake addresses
  there (use `TOPO_PING_ENABLED=false` for mock labs). Alpine and Linux `ping` are fine.

## Aligned with confetti-butler and confetti-traffic (2026-10-14)

Ported butler's update flow (`topo-update.sh [-y]` runs `build-template.sh --update`, `TOPO_STAGE_DIR`), the
`BUILD` id and login hints, `set-static-ip` with DNS/hostname, the setup wizard, guestinfo `topo.dns` /
`topo.hostname`, apk-warning-on-update, the sysctl pin, `resolv.conf` clean-up and the "Make it rain!" label.
**Not run on a VM yet** (`sh -n` and the regression gate only).

## SSH server switched from Dropbear to OpenSSH (2026-10-14)

Same change as confetti-butler (see its HANDOFF): `build-template.sh` `switch_ssh_server()` installs `sshd` +
`openssh-sftp-server`, keeps root password login, converts the Dropbear ed25519 host key and removes Dropbear only
after sshd runs. R27 checks it. Logic tested in an `alpine:3.24` container with real `dropbear` / `sshd` binaries and
a fake `rc-service`. **Not run on a VM** (the topo build and update scripts have not run on one at all yet).
