"""Topology graph — {nodes, edges} in the shape vis.js wants.

Edges come from LLDP neighbors (cables) and BGP/OSPF adjacencies (sessions).
A neighbor or peer that does not resolve to a known device becomes a ghost
node rather than being dropped: an edge to nowhere reads as a bug, a
distinctly styled ghost reads as "something is out there, unidentified".

Who is that neighbor? The poller already matched what it could through the
alias table (an LLDP neighbor's advertised management IP) and stored it in
remote_device_id / peer_device_id. What is still NULL after that gets one more
try here, at query time: an address that exactly one device holds on one of
its interfaces. BGP/OSPF peers are usually a loopback or point-to-point
address, so this is what lets most of them resolve. It is never written
back as an identity alias (CLAUDE.md constraints 1 and 2): a re-addressed
interface must not become a permanent claim on a device.
"""

from . import db


def interface_owners(conn):
    """{ip: device_id} for addresses configured on exactly one device.

    An address on two devices (HSRP/VRRP virtual IP, a reused lab address)
    is ambiguous and left out: the neighbor stays a ghost rather than being
    drawn to a guess.
    """
    holders = {}
    for r in conn.execute("SELECT ip, device_id FROM interfaces WHERE ip IS NOT NULL AND ip <> ''"):
        holders.setdefault(r["ip"], set()).add(r["device_id"])
    return {ip: next(iter(ids)) for ip, ids in holders.items() if len(ids) == 1}


def resolve(owners, device_id, stored_id, ip):
    """The poller's match if it has one, else a unique interface-IP match.

    A device never peers with itself: an address it owns itself resolves to
    None (a ghost), not a self-loop.
    """
    found = stored_id or (owners.get(ip) if ip else None)
    return None if found == device_id else found


def _is_up(row):
    return row["state"] == ("Established" if row["proto"] == "bgp" else "FULL")


def build(conn, layer=None):
    """layer limits the edges to some sources: "lldp", "bgp", "ospf", or a comma list
    such as "lldp,bgp". None or "" overlays all three; an unknown name draws none."""
    wanted = {"lldp", "bgp", "ospf"} if not layer else {x.strip() for x in layer.split(",")}
    owners = interface_owners(conn)
    nodes = {}
    for d in conn.execute("SELECT id, hostname, role, reachable FROM devices"):
        nid = "dev:{}".format(d["id"])
        nodes[nid] = {
            "id": nid, "label": d["hostname"], "group": d["role"],
            "device_id": d["id"], "ghost": False, "reachable": d["reachable"],
        }

    def ghost_id(kind, key):
        gid = "ghost:{}:{}".format(kind, key)
        if gid not in nodes:
            nodes[gid] = {"id": gid, "label": key, "group": "unknown", "device_id": None, "ghost": True}
        return gid

    edges = []

    if "lldp" in wanted:
        rows = conn.execute(
            """SELECT device_id, local_if, remote_chassis, remote_sysname, remote_port,
                      remote_mgmt_ip, remote_device_id
               FROM lldp_neighbors"""
        ).fetchall()
        # Two known devices each report the other, so the same cable shows up
        # twice. Key a device-to-device link by both (device, interface) ends
        # and draw it once. If the two sides name a port differently ("Gi1"
        # vs "GigabitEthernet1") the keys differ and both edges stay — a
        # duplicate line is safer than hiding a real parallel link. Ghost
        # links are never de-duplicated.
        seen = set()
        for r in rows:
            remote = resolve(owners, r["device_id"], r["remote_device_id"], r["remote_mgmt_ip"])
            if remote:
                link = frozenset([(r["device_id"], r["local_if"]), (remote, r["remote_port"])])
                if link in seen:
                    continue
                seen.add(link)
            target = ("dev:{}".format(remote) if remote
                      else ghost_id("lldp", r["remote_sysname"] or r["remote_chassis"] or "unknown"))
            edges.append({
                "from": "dev:{}".format(r["device_id"]), "to": target, "layer": "lldp",
                "label": "{} - {}".format(r["local_if"], r["remote_port"] or "?"),
            })

    protos = [p for p in ("bgp", "ospf") if p in wanted]
    if protos:
        rows = conn.execute(
            "SELECT device_id, proto, peer_ip, peer_device_id, state FROM adjacencies "
            "WHERE proto IN ({})".format(",".join("?" * len(protos))),
            protos,
        ).fetchall()
        sessions = {}   # (proto, {device, device}) -> {device_id: [rows that device reports]}
        for r in rows:
            remote = resolve(owners, r["device_id"], r["peer_device_id"], r["peer_ip"])
            if remote is None:
                # One ghost per peer IP across BGP and OSPF — the same router
                # peering both ways is one box, not two.
                edges.append({
                    "from": "dev:{}".format(r["device_id"]), "to": ghost_id("peer", r["peer_ip"]),
                    "layer": r["proto"], "label": "{} {}".format(r["proto"], r["state"]),
                    "up": _is_up(r),
                })
            else:
                key = (r["proto"], frozenset([r["device_id"], remote]))
                sessions.setdefault(key, {}).setdefault(r["device_id"], []).append(r)
        # Both routers report the same session, so it arrives twice. Draw it
        # once, from the lower device id to the higher so every edge between
        # a pair points the same way. A session counts as up only if both
        # ends say so; the state shown is the first one that is not. Two
        # real sessions between the same pair (loopback and link address) stay
        # two edges: they are paired by position, Established first.
        for (proto, pair), by_dev in sessions.items():
            lo, hi = sorted(pair)
            a = sorted(by_dev.get(lo, []), key=lambda x: not _is_up(x))
            b = sorted(by_dev.get(hi, []), key=lambda x: not _is_up(x))
            for k in range(max(len(a), len(b))):
                ends = [x[k] for x in (a, b) if k < len(x)]
                bad = [x for x in ends if not _is_up(x)]
                edges.append({
                    "from": "dev:{}".format(lo), "to": "dev:{}".format(hi), "layer": proto,
                    "label": "{} {}".format(proto, (bad or ends)[0]["state"]),
                    "up": not bad,
                })

    return {"nodes": list(nodes.values()), "edges": edges}


def adjacencies(conn, device_id=None):
    """BGP/OSPF rows for the API, with the effective peer_device_id."""
    owners = interface_owners(conn)
    sql = """SELECT a.device_id, d.hostname, a.proto, a.peer_ip, a.peer_device_id, a.remote_as,
                    a.area, a.state, a.uptime, a.prefixes, a.local_if, a.last_seen
             FROM adjacencies a JOIN devices d ON d.id = a.device_id"""
    params = []
    if device_id:
        sql += " WHERE a.device_id = ?"
        params.append(device_id)
    sql += " ORDER BY d.hostname, a.proto, a.peer_ip"
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["peer_device_id"] = resolve(owners, d["device_id"], d["peer_device_id"], d["peer_ip"])
        d["last_seen"] = db.iso(d["last_seen"])
        out.append(d)
    return out


def lldp_neighbors(conn, device_id=None):
    """LLDP rows for the API, with the effective remote_device_id."""
    owners = interface_owners(conn)
    sql = """SELECT l.device_id, d.hostname, l.local_if, l.remote_chassis, l.remote_sysname,
                    l.remote_port, l.remote_mgmt_ip, l.remote_device_id, l.last_seen
             FROM lldp_neighbors l JOIN devices d ON d.id = l.device_id"""
    params = []
    if device_id:
        sql += " WHERE l.device_id = ?"
        params.append(device_id)
    sql += " ORDER BY d.hostname, l.local_if"
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["remote_device_id"] = resolve(owners, d["device_id"], d["remote_device_id"], d["remote_mgmt_ip"])
        d["last_seen"] = db.iso(d["last_seen"])
        out.append(d)
    return out
