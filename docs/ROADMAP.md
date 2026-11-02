# confetti-topo — roadmap

Ideas only. Nothing here is built until the user picks it.

## Near term
- **Validate on real routers** (`lab-validator`) and on a fresh Alpine VM; then update HANDOFF.
- **Alerting.** Butler's alert rules stopped seeing topology events. Port `alerts.py`, the rules page and the
  sender thread (webhook / mail), or send events to butler.

## Topology
- **Routing table follow-ups:** IPv6 (checked 2026-11-03: the lab has one IPv6 route on one router and none on another, so there is no real table to build from yet) and a cap on a very large table; validate ECMP against a real router (the
  fixture `ios_route_not_captured.txt` is not a capture).
- **Routing table docs and mock data:** update `docs/TOPOLOGY-MAPPING.md` (commands table, `routes` schema),
  `docs/HANDOFF.md`, and the skills `confetti-topo-add-platform` and `confetti-topo-api` (sixth task); add routes to
  `topo/scripts/seed_mock_lab.py`; run `docs-drift-checker`.
- **IOS 15.2 `vrf *` check:** confirm whether the Catalyst 3560 switch answers `show ip route vrf *` or uses the `show ip route`
  fallback.
- **CDP** next to LLDP (many Cisco switches speak only CDP). Checked 2026-11-03: `show cdp neighbors detail` answers `% CDP is not enabled` on every reachable lab IOS device, so there is no real output to build a parser from. Enable CDP on one lab switch first, capture, then add it (merge into the lldp task, never duplicate a cable that LLDP already shows).
- **L2 view, rest:** MAC table and per-port spanning-tree roles (VLANs, trunks and the spanning-tree summary are done: the `vlans`, `trunks` and `stp` tasks, switches only). Each needs its own command, verified on a real switch first.
- **Pair parallel sessions properly** by the local address of each session instead of by position.
- **History, rest:** compare two snapshots (draw what was added or removed between them); snapshots keep the shape only when it changes, so a link that flaps within one interval is not seen.
- **Label crowding:** LLDP labels show only on hover or selection now; BGP/OSPF labels and the vendor mark can
  still overlap on a dense map.
- **Linux detection on real hosts:** the "LLDP says Linux" rule and the System Description parser are only
  tested on sample text. Check them against a real Linux host's `show lldp neighbors detail`.
- **Selection outline** is a wide rectangle that includes the vendor mark. A tighter outline around the icon alone
  would look better.
- **Real vendor marks** (Fortinet, Arista, Aruba, ...): an unknown vendor now gets a grey letter chip and its name; add a brand mark to `VENDOR_MARKS` and `VENDOR_NAMES` in topology.html when wanted.

## Platforms
- **PAN-OS** (XML API) is done and polled on a real PA-VM (11.1); to do: other versions, Panorama, more than the
  default virtual router, and LLDP rows that tell two neighbors with the same port name apart.
- **Junos**, then others. Follow the `confetti-topo-add-platform` skill: real output first.
- **SNMP LLDP-MIB** for devices without SSH.

## Operations
- **Authentication, follow-up:** `TOPO_AUTH_TOKEN` gives one shared token (no users, no TLS). Per-user accounts and HTTPS would be next.
- **Packer template** for vCenter, once one exists in butler that has actually run.
- **vCenter discovery**, only together with a real vCenter to test against.
