# confetti-topo — roadmap

Ideas only. Nothing here is built until the user picks it.

## Near term
- **Validate on real routers** (`lab-validator`) and on a fresh Alpine VM; then update HANDOFF.
- **Check the features that came from the former butler on real hardware** (merged 2026-11-21): the `config` task and drift on a real router, SNMP against a
  real `snmpd`, an alert webhook and mail. The unit and mock-lab checks pass; none of it has run against the lab yet.

## Topology
- **Routing table follow-ups:** IPv6 (checked 2026-11-03: the lab has one IPv6 route on one router and none on another, so there is no real table to build from yet) and a cap on a very large table; validate ECMP against a real router (the
  fixture `ios_route_not_captured.txt` is not a capture).
- **Routing table docs and mock data:** update `docs/TOPOLOGY-MAPPING.md` (commands table, `routes` schema),
  `docs/HANDOFF.md`, and the skills `confetti-topo-add-platform` and `confetti-topo-api` (sixth task); add routes to
  `topo/scripts/seed_mock_lab.py`; run `docs-drift-checker`.
- **IOS 15.2 `vrf *` check:** confirm whether the Catalyst 3560 switch answers `show ip route vrf *` or uses the `show ip route`
  fallback.
- **CDP** next to LLDP (many Cisco switches speak only CDP). Checked 2026-11-03: `show cdp neighbors detail` answers `% CDP is not enabled` on every reachable lab IOS device, so there is no real output to build a parser from. Enable CDP on one lab switch first, capture, then add it (merge into the lldp task, never duplicate a cable that LLDP already shows).
- **L2 view, rest:** per-port spanning-tree roles (VLANs, trunks, the spanning-tree summary and the MAC table are done: the `vlans`, `trunks`, `stp` and `mac` tasks, switches only). Each needs its own command, verified on a real switch first.
- **Pair parallel sessions properly** by the local address of each session instead of by position.
- **History, rest:** the banner counts what changed since a snapshot and gone devices glow red, but new devices and links are not marked; snapshots keep the shape only when it changes, so a link that flaps within one interval is not seen.
- **Label crowding:** LLDP and Connected labels show only on hover or selection, and the Labels dropdown can make every
  label hover-only or hide them; BGP/OSPF labels and the vendor mark can still overlap on a dense map in the default mode.
- **Default / Connected layers, docs:** add them to `docs/TOPOLOGY-MAPPING.md` (limits: RIB only, IPv4 only), re-record `docs/img/`
  (`readme-image-capturer`) and run `docs-drift-checker`.
- **Down-links badge:** a small "N links down" badge by the FILTERED marker; clicking it selects the first down link, so a red
  link on a big map is easy to find.
- **Keyboard shortcuts** on the topology page: `F` to fit, `/` to focus the device search, `Esc` to close the cards. Only the
  search box handles keys today.
- **Collapse ghosts in Hierarchical:** the bottom row of unknown nodes makes the tree wide; group them like the "N unknown"
  node does.
- **Shorter legend:** it is long and covers the left of the map; fold it into Devices / Links sections that open one at a time.
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
- **Link load on the map:** colour or thicken an LLDP link by its port's traffic rate (the rates are stored since 2026-11-22, `interfaces.in_bps`).
- **Traffic on PAN-OS and SNMP:** byte counters from `<show><counter><interface>` and IF-MIB `ifHCInOctets`; IOS only today.
- **Restore on the VM checked for real:** `topo-db.sh restore` and the Settings restore were tested on Windows dev copies only.
- **Authentication, follow-up:** `TOPO_AUTH_TOKEN` gives one shared token (no users, no TLS). Per-user accounts and HTTPS would be next.
- **Packer template** for vCenter, once one exists in butler that has actually run (not ported in the merge).
- **vCenter import**: the API route is ported, it has no page and was never tested against a real vCenter.
