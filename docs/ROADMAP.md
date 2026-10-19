# confetti-topo — roadmap

Ideas only. Nothing here is built until the user picks it.

## Near term
- **Validate on real routers** (`lab-validator`) and on a fresh Alpine VM; then update HANDOFF.
- **Alerting.** Butler's alert rules stopped seeing topology events. Port `alerts.py`, the rules page and the
  sender thread (webhook / mail), or send events to butler.

## Topology
- **Device routing table** (do this first): a `show ip route` task per device (verified on a real router first,
  see `confetti-topo-add-platform`), a table for it, and a routing-table section on the device page.
- **Right-click a device on the map, "View routing table".** Needs the device routing table above first. The
  right-click menu already exists (`topo/templates/topology.html`, `openContextMenu`); this adds one item.
- **CDP** next to LLDP (many Cisco switches speak only CDP).
- **L2 view:** MAC table, VLANs, spanning tree. Needs its own commands, verified on real devices first.
- **Pair parallel sessions properly** by the local address of each session instead of by position.
- **Link state on cables:** colour an LLDP edge red when the local interface is down.
- **Search and focus:** find a device or IP on the graph and highlight its neighbors.
- **History:** keep the last N topologies so "what changed since yesterday" can be drawn.
- **Label crowding:** LLDP labels show only on hover or selection now; BGP/OSPF labels and the vendor mark can
  still overlap on a dense map.
- **Linux detection on real hosts:** the "LLDP says Linux" rule and the System Description parser are only
  tested on sample text. Check them against a real Linux host's `show lldp neighbors detail`.
- **Selection outline** is a wide rectangle that includes the vendor mark. A tighter outline around the icon alone
  would look better.
- **More vendor marks** (Fortinet, Arista, Aruba, ...): add to `VENDOR_MARKS` and `VENDOR_NAMES` in topology.html.

## Platforms
- **Junos**, then others. Follow the `confetti-topo-add-platform` skill: real output first.
- **SNMP LLDP-MIB** for devices without SSH.

## Operations
- **Authentication** for the UI and API (the siblings have none either).
- **Placeholder address:** `topo/scripts/topo-setup.sh:61` has a non-documentation private address in a comment
  example; change it to a documentation-range one.
- **Theme drift:** confetti-traffic and confetti-butler still have the Terminal Green theme that was removed here.
  Run `confetti-interop-checker`, then decide where the fix goes.
- **Packer template** for vCenter, once one exists in butler that has actually run.
- **vCenter discovery**, only together with a real vCenter to test against.
