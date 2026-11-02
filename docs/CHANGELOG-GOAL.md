# Changelog: branch slash-goal-testing

Newest last. Times are local.

- 2026-11-02 21:50 Device page: `#routes` link now scrolls once the routes have loaded (`device.html`, `routesScrolled`).
- 2026-11-02 21:50 Device page: the "Unused (admin down)" filter is hidden when no interface reports an admin status (PAN-OS); an empty Admin cell no longer shows a red pill.
- 2026-11-02 21:55 Topology: search box in the Devices list. Matches name, management IP or AS; the title shows "N of M"; Enter goes to the first match, Escape clears.
- 2026-11-02 21:58 Topology: an LLDP cable is drawn red and dashed when a device reports the local port as not up (`topology.py`, exact port-name match only; an unmatched name stays normal). Legend updated.
- 2026-11-02 22:00 Regression gate: CLEAR (32 checks).
- 2026-11-02 22:35 Topology: cable link state now compares port names with the type spelled out ("Gi1" = "GigabitEthernet1"; `topology.py` `port_key`).
- 2026-11-02 22:36 Topology: the search box also fades non-matching devices on the map (stays while it refreshes).
- 2026-11-02 22:40 Dashboard: new Devices table (ping, poll state, last good poll), problems first, in every layout.
- 2026-11-02 22:41 `topo-setup.sh`: example address in a comment changed to the documentation range (192.0.2.x).
- 2026-11-02 22:50 README: topology search and red cable noted.
- 2026-11-02 23:10 Topology: the link card shows "local port state" for an LLDP cable and says when a port is down.
- 2026-11-02 23:12 Topology: search keeps the match's linked devices half clear on the map.
- 2026-11-02 23:20 Checked the down path on a scratch mock DB: cable gets up=false and port state down.
- 2026-11-03 00:05 Docs brought in line (TOPOLOGY-MAPPING, CLAUDE.md, two skills): cable state, `local_port_state`, search and fade.
- 2026-11-03 00:10 Search box placeholder shortened so it fits. README images re-recorded at 1280x900 (new Devices table, search box).
