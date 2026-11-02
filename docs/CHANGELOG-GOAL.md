# Changelog: branch slash-goal-testing

Newest last. Times are local.

- 2026-11-02 21:50 Device page: `#routes` link now scrolls once the routes have loaded (`device.html`, `routesScrolled`).
- 2026-11-02 21:50 Device page: the "Unused (admin down)" filter is hidden when no interface reports an admin status (PAN-OS); an empty Admin cell no longer shows a red pill.
- 2026-11-02 21:55 Topology: search box in the Devices list. Matches name, management IP or AS; the title shows "N of M"; Enter goes to the first match, Escape clears.
- 2026-11-02 21:58 Topology: an LLDP cable is drawn red and dashed when a device reports the local port as not up (`topology.py`, exact port-name match only; an unmatched name stays normal). Legend updated.
- 2026-11-02 22:00 Regression gate: CLEAR (32 checks).
