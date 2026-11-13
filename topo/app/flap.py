"""BGP flap detection (CLAUDE.md constraint 22).

A poll comes every few minutes, so a peer that drops and comes back between two polls looks unchanged to a plain
state compare. Two things count as one change of a BGP peer:
  - the state went to or from Established between two polls ("state"), or
  - the state is Established at both polls but the Up/Down timer went backwards ("reset"): the timer only grows,
    so a smaller value means the session dropped and came back in between.
A peer with TOPO_FLAP_COUNT changes inside TOPO_FLAP_WINDOW_S is flapping. Idle/Active/Connect cycling of a peer that
never comes up is not counted: that is a peer that is down, not one that flaps.
Nothing here talks to a device; it works on what the poller already collected.
"""

import re

from . import config
from . import db

_UNITS = {"y": 365 * 86400, "w": 7 * 86400, "d": 86400, "h": 3600}
_PAIR = re.compile(r"(\d+)([ywdh])")


def uptime_seconds(text):
    """IOS Up/Down timer to seconds: 'HH:MM:SS', '1d02h', '3w2d', '1y12w'. 'never' is 0. Anything else is None."""
    text = (text or "").strip()
    if text == "never":
        return 0
    m = re.fullmatch(r"(\d+):(\d\d):(\d\d)", text)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    pairs = _PAIR.findall(text)
    if pairs and "".join(n + u for n, u in pairs) == text:
        return sum(int(n) * _UNITS[u] for n, u in pairs)
    return None


def changed(old_state, old_uptime, new_state, new_uptime):
    """'state', 'reset' or '' for one peer between two polls."""
    if old_state != new_state:
        return "state" if "Established" in (old_state, new_state) else ""
    if new_state == "Established":
        a, b = uptime_seconds(old_uptime), uptime_seconds(new_uptime)
        if a is not None and b is not None and b < a:
            return "reset"
    return ""


def record(conn, device_id, peer_ip, kind):
    conn.execute("INSERT INTO bgp_changes (device_id, peer_ip, at, kind) VALUES (?, ?, ?, ?)",
                 (device_id, peer_ip, db.sqlite_now(), kind))


def count(conn, device_id, peer_ip):
    """Changes of this peer inside the window."""
    return conn.execute(
        "SELECT COUNT(*) FROM bgp_changes WHERE device_id = ? AND peer_ip = ? "
        "AND at >= datetime('now', ? || ' seconds')",
        (device_id, peer_ip, "-{:d}".format(config.FLAP_WINDOW_S))).fetchone()[0]


def prune(conn):
    conn.execute("DELETE FROM bgp_changes WHERE at < datetime('now', ? || ' seconds')",
                 ("-{:d}".format(config.FLAP_WINDOW_S * 2),))
