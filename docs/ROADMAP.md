# confetti-topo — roadmap

Ideas only. Nothing here is built until the user picks it.

## Near term
- **Validate on real routers** (`lab-validator`) and on a fresh Alpine VM; then update HANDOFF.
- **Alerting.** Butler's alert rules stopped seeing topology events. Port `alerts.py`, the rules page and the
  sender thread (webhook / mail), or send events to butler.
- **Save node positions.** The layout is recomputed in the browser on every load. A saved position per device
  (and a "reset layout" button) would make the map stable.

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

## Platforms
- **Junos**, then others. Follow the `confetti-topo-add-platform` skill: real output first.
- **SNMP LLDP-MIB** for devices without SSH.

## Operations
- **Authentication** for the UI and API (the siblings have none either).
- **Packer template** for vCenter, once one exists in butler that has actually run.
- **vCenter discovery**, only together with a real vCenter to test against.
