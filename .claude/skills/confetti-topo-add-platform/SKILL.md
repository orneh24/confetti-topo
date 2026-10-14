---
name: confetti-topo-add-platform
description: Add a new device platform (for example Junos) to confetti-topo - registry entry, LLDP/BGP/OSPF/interface commands, parsers, captured-output fixtures, regression check. Use when asked to support another vendor or OS.
origin: confetti-topo
---

# Adding a platform

Constraint 3 is the reason for this order: command names and output are never assumed, they are checked on a
real device. Parsers written from documentation were removed from the butler lineage on 2026-10-10 for that
reason.

1. **Get real output first.** Ask the user for a lab device. Capture the raw output of each command (version,
   interfaces, lldp, bgp, ospf) into `dev/samples/<platform>_<task>.txt`. Replace real hostnames, IPs and
   secrets with placeholders (see `dev/samples/README.md`).
2. **Registry.** One entry in `topo/app/platforms.py` with the same five task names. An unknown platform falls
   back to IOS, so a typo is silent — spell the key as stored in `devices.platform`.
3. **Commands are `show` only.** `dev/regress.py` R19 fails on anything else. Never add a config-push path.
4. **Parser.** New `topo/app/parsers/<name>.py`, pure regex (no textfsm). Same row shapes as `ios.py`:
   LLDP rows need `local_if`, `remote_sysname`, `remote_port`, `remote_chassis`, `remote_mgmt_ip`; BGP/OSPF
   rows need `peer_ip`, `state` (BGP keeps `Idle (Admin)`; OSPF keeps the part before `/`). An empty answer
   ("LLDP is not enabled") parses to an empty list and is still a successful task.
5. **State names.** `topology._is_up` and `ssh._apply_adjacencies` treat `Established` / `FULL` as up. If the
   platform spells them differently, normalise in the parser.
6. **Regress.** Add a check that parses the fixtures and prove it fails when the parser is broken. Update the
   task-count wording in CLAUDE.md if it changes (R15).
7. **Docs.** CLAUDE.md "The poller", README, ROADMAP status, `docs/TOPOLOGY-MAPPING.md` section 2 and 12,
   the `confetti-topo-api` skill if routes change.
8. **Validate on the device** with the `lab-validator` agent. Do not mark the platform verified before that.
