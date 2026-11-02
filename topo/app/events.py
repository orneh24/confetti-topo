"""Change events (CLAUDE.md constraint 11) — "BGP peer 10.0.0.2 went Established -> Active", "config changed".

The poller's apply steps already hold the old state just before they overwrite
it, so they call emit() with the difference. emit() never commits: the event
is written in the caller's transaction, so it exists exactly when the change
it describes does.
"""

import re

from . import db


def emit(conn, device_id, kind, severity, subject, detail=""):
    conn.execute(
        "INSERT INTO events (at, device_id, kind, severity, subject, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (db.sqlite_now(), device_id, kind, severity, subject, detail),
    )


def is_baseline(conn, device_id, task):
    """True until this task has succeeded once for the device.

    The first poll of a new device finds every interface, neighbor and peer
    "new" — that is the starting picture, not a change, and would otherwise
    flood the timeline with one event per row.
    """
    row = conn.execute(
        "SELECT 1 FROM poll_history WHERE device_id = ? AND task = ? AND ok = 1 LIMIT 1",
        (device_id, task),
    ).fetchone()
    return row is None


def _arrow(detail):
    """'a -> b' -> ('a', 'b'); anything else -> ('', '')."""
    if " -> " in (detail or ""):
        a, b = detail.split(" -> ", 1)
        return a.strip(), b.strip()
    return "", ""


def describe(kind, subject, detail, hostname):
    """Split a stored event into what the Recent changes table shows.

    Read-time only: the events table keeps subject/detail as emit() wrote them,
    so old rows get the same treatment. Returns type, change (added / changed /
    removed), item (the interface, peer, port ... the event is about), before
    and after. A kind not listed here keeps its kind as type and empty fields.
    """
    subject = subject or ""
    rest = subject
    if hostname and rest.startswith(hostname):
        rest = rest[len(hostname):].lstrip(" :")
    out = {"type": kind, "change": "", "item": "", "before": "", "after": ""}
    before, after = _arrow(detail)

    if kind in ("interface_up", "interface_down"):
        out.update(type="Interface", change="changed", item=rest.rsplit(": ", 1)[0].strip(),
                   before=before, after=after or kind[len("interface_"):])
    elif kind in ("lldp_neighbor_added", "lldp_neighbor_removed"):
        m = re.match(r"(.+?): (?:lost )?neighbor (.*)$", rest)
        port, neighbor = (m.group(1), m.group(2)) if m else (rest, "")
        if kind.endswith("added"):
            out.update(type="LLDP neighbor", change="added", item=port, before="n/a", after=neighbor)
        else:
            out.update(type="LLDP neighbor", change="removed", item=port, before=neighbor, after="n/a")
    elif kind.startswith(("bgp_", "ospf_")):
        label = "BGP peer" if kind.startswith("bgp_") else "OSPF neighbor"
        # "BGP peer 10.0.0.2: Idle" or "BGP peer 10.0.0.2 removed". Split on ": " (not ":") so an
        # IPv6 peer stays whole.
        peer, state = re.sub(r"^(?:BGP|OSPF) peer ", "", rest), ""
        if peer.endswith(" removed"):
            peer = peer[:-len(" removed")]
        elif ": " in peer:
            peer, state = peer.rsplit(": ", 1)
        if kind.endswith("_peer_added"):
            out.update(type=label, change="added", item=peer, before="n/a", after=state)
        elif kind.endswith("_peer_removed"):
            out.update(type=label, change="removed", item=peer, before="", after="n/a")
        else:
            out.update(type=label, change="changed", item=peer, before=before, after=after or state)
    elif kind == "routes_changed":
        m = re.search(r"routes (\+\d+ -\d+)", rest)
        out.update(type="Routes", change="changed", item=m.group(1) if m else rest)
    elif kind == "stp_changed":
        out.update(type="Spanning tree", change="changed", item=rest.replace("spanning tree ", "", 1), before=before, after=after)
    elif kind == "os_version_changed":
        out.update(type="OS version", change="changed", before=before, after=after)
    elif kind in ("device_unreachable", "device_recovered"):
        down = kind == "device_unreachable"
        out.update(type="Polling", change="changed",
                   before="ok" if down else "failing", after="failing" if down else "ok")
    elif kind in ("ping_down", "ping_up"):
        m = re.search(r"\(([^)]*)\)", subject)
        down = kind == "ping_down"
        out.update(type="Ping", change="changed", item=m.group(1) if m else "",
                   before="answering" if down else "not answering", after="not answering" if down else "answering")
    elif kind == "device_discovered":
        out.update(type="Discovery", change="added", item=subject)
    return out
