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
- 2026-11-03 00:30 Topology: "Export PNG" button and map right-click item download the map as a PNG (page colour behind it).
- 2026-11-03 00:32 Leak scan: one lab hostname in ROADMAP.md replaced by a model name (it is also in older commits on origin/routing-table). README capture viewport raised to 1280x1000.
- 2026-11-03 01:00 Recent changes: "export CSV" downloads the events shown now (filters applied); cells that start with = + - @ are prefixed with ' so a spreadsheet does not run them.
- 2026-11-03 01:40 Topology: "Changed 24h" toggle glows yellow around devices with a change event in the last 24 h (hover shows the count). Off by default. Export PNG right-click item checked.
- 2026-11-03 01:45 README images re-recorded at 1280x1000: no tile is cut off now.
- 2026-11-03 02:10 Checked CDP on the real lab (read-only): not enabled on any reachable IOS device, so not built. Noted in ROADMAP.
- 2026-11-03 02:20 Verified on a scratch mock lab: a down port shows a red dashed cable and the link card text; "Changed 24h" leaves a device with no events unlit; CSV formula prefix checked.
- 2026-11-03 02:40 Topology: a vendor with no mark of its own (Fortinet, Arista...) gets a grey letter chip and its name. alpine, linux and unknown get none. Plain characters only go into the SVG.
- 2026-11-03 02:42 Checked IPv6 on the real lab (read-only `show ipv6 route`): one route on one router, none on another, so no parser built. Noted in ROADMAP.
- 2026-11-03 03:10 Devices page: filter box (name, IP, site, role, vendor, platform, model, key) with an "N of M" count; it survives re-renders and sorting. Not checked at 390px width.
- 2026-11-03 03:12 Looked at a VLAN/L2 poll task: not built. On a router, `show vlan brief` answers "Invalid input", which the poller counts as a failed task, so it needs a switch-only gate and a new table; left on the roadmap.
- 2026-11-03 03:40 Dashboard: disk used is yellow from 85% and red from 95%, memory from 80% / 90%; the Server tile says "degraded, disk N% full" at 95%. Neon Green CRT underlines the warning, since its colours are close. Checked in 5 layouts (disk was 91% here, so the red case was only checked in code).
