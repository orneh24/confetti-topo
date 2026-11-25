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

import ipaddress
import re

from . import config, db

# Words in a model or hostname that say what a router-role device really is.
# A word matches a whole name part with its digits dropped ("sw1", "asa5506"), or sits inside the text.
SWITCH_WORDS = ("switch", "nexus", "c2960", "c3560", "c3750", "c3850", "c9200", "c9300", "c9500", "n9k", "n7k", "n5k")
SWITCH_PARTS = ("sw",)
FIREWALL_WORDS = ("firewall", "firepower", "fortigate", "fortinet", "paloalto")
FIREWALL_PARTS = ("fw", "asa", "ftd")


# A neighbor whose LLDP system description says Linux and names none of these is a plain Linux server.
NETWORK_VENDOR_WORDS = ("cisco", "juniper", "junos", "palo alto", "pan-os", "hewlett", "hpe", "aruba", "procurve")


def lldp_says_linux(descr):
    """True for an LLDP system description like "Ubuntu 22.04 Linux 5.15" that names no network vendor."""
    text = (descr or "").lower()
    return "linux" in text and not any(w in text for w in NETWORK_VENDOR_WORDS)


def device_type(role, model, hostname, platform=None):
    """router / switch / firewall / linux / host for a known device (the page draws an icon from it).

    The role decides unless it is 'router', the default: then the model and
    hostname are searched for switch / firewall words.
    """
    if role in ("switch", "firewall"):
        return role
    if platform == "paloalto_panos":
        return "firewall"
    if platform in ("linux", "confetti_node"):
        return "linux"
    if role not in ("router", None, ""):
        return "host"
    text = "{} {}".format(model or "", hostname or "").lower()
    parts = [x.rstrip("0123456789") for x in re.split(r"[^a-z0-9]+", text)]
    for kind, words, short in (("firewall", FIREWALL_WORDS, FIREWALL_PARTS), ("switch", SWITCH_WORDS, SWITCH_PARTS)):
        if any(w in text for w in words) or any(x in short for x in parts):
            return kind
    return "router"


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


def vpn_tunnels(conn, device_id=None):
    """Stored IPsec tunnels, each with the device its peer address belongs to (peer_device_id, or None).

    The match is made now and never stored (constraint 12): the one device with that address on an interface, else
    the one device managed at that address. A device is never its own peer.
    """
    owners = interface_owners(conn)
    managed = {r["mgmt_ip"]: r["id"] for r in conn.execute(
        "SELECT mgmt_ip, MIN(id) AS id FROM devices WHERE mgmt_ip IS NOT NULL AND mgmt_ip <> '' "
        "GROUP BY mgmt_ip HAVING COUNT(*) = 1")}
    shared = {r["ip"] for r in conn.execute("SELECT ip FROM interfaces WHERE ip IS NOT NULL AND ip <> '' "
                                            "GROUP BY ip HAVING COUNT(DISTINCT device_id) > 1")}
    sql = ("SELECT v.*, d.hostname FROM vpn_tunnels v JOIN devices d ON d.id = v.device_id"
           + (" WHERE v.device_id = ?" if device_id is not None else "") + " ORDER BY d.hostname, v.name, v.peer_ip")
    out = []
    for r in conn.execute(sql, () if device_id is None else (device_id,)):
        row = dict(r)
        found = owners.get(row["peer_ip"])
        if found is None and row["peer_ip"] not in shared:   # an address on two devices names none of them
            found = managed.get(row["peer_ip"])
        row["peer_device_id"] = resolve({}, row["device_id"], found, None)
        out.append(row)
    return out


def norm_mac(text):
    """12 lower-case hex digits for "02:00:00:00:00:09", "0200.0000.0009" or "02-00-00-00-00-09", else None."""
    text = (text or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F:.\-]+", text):
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", text)
    return digits.lower() if len(digits) == 12 else None


def mac_owners(conn):
    """{mac: device_id} for management MACs held by exactly one device."""
    holders = {}
    for r in conn.execute("SELECT id, mgmt_mac FROM devices WHERE mgmt_mac IS NOT NULL AND mgmt_mac <> ''"):
        mac = norm_mac(r["mgmt_mac"])
        if mac:
            holders.setdefault(mac, set()).add(r["id"])
    return {mac: next(iter(ids)) for mac, ids in holders.items() if len(ids) == 1}


def name_owners(conn):
    """{name: device_id} for hostnames held by exactly one device, lower case, each under its full name and
    its short name (before the first dot): "FW-1.lab.local" is found as "fw-1.lab.local" and as "fw-1"."""
    holders = {}
    for r in conn.execute("SELECT id, hostname FROM devices WHERE hostname IS NOT NULL AND hostname <> ''"):
        full = r["hostname"].strip().lower()
        for name in {full, full.split(".")[0]}:
            holders.setdefault(name, set()).add(r["id"])
    return {name: next(iter(ids)) for name, ids in holders.items() if len(ids) == 1}


def _toggle(conn, key, default):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] in (None, ""):
        return default, "env"
    return row["value"].lower() in ("1", "true", "yes"), "settings"


def lldp_matching(conn):
    """{"chassis": bool, "name": bool, "source": {...}}: the two LLDP matching switches in use
    (the Settings row if there is one, else the TOPO_LLDP_MATCH_* default)."""
    chassis, c_src = _toggle(conn, "lldp_match_chassis", config.LLDP_MATCH_CHASSIS)
    name, n_src = _toggle(conn, "lldp_match_name", config.LLDP_MATCH_NAME)
    return {"chassis": chassis, "name": name, "source": {"chassis": c_src, "name": n_src}}


def lldp_context(conn):
    """What resolve_lldp needs: the interface owners and, for a switched-on match, its owner map."""
    on = lldp_matching(conn)
    return {"owners": interface_owners(conn),
            "macs": mac_owners(conn) if on["chassis"] else None,
            "names": name_owners(conn) if on["name"] else None}


def resolve_lldp(ctx, device_id, stored_id, ip, chassis, sysname):
    """(device_id, how) for an LLDP neighbor. how is None for the poller's match or an interface IP (resolve),
    "chassis" or "name" for the optional matches, tried in that order and only when still unresolved.
    Never to the reporting device itself, never when two devices share the value."""
    found = resolve(ctx["owners"], device_id, stored_id, ip)
    if found or stored_id or (ip and ctx["owners"].get(ip)):
        return found, None   # a device's own address (found None) is not retried by chassis or name
    if ctx["macs"] is not None and norm_mac(chassis):
        found = ctx["macs"].get(norm_mac(chassis))
        if found and found != device_id:
            return found, "chassis"
    if ctx["names"] is not None and sysname:
        full = sysname.strip().lower()
        found = ctx["names"].get(full) or ctx["names"].get(full.split(".")[0])
        if found and found != device_id:
            return found, "name"
    return None, None


_PORT_LONG = {"gi": "gigabitethernet", "te": "tengigabitethernet", "fa": "fastethernet", "tw": "twogigabitethernet",
              "fo": "fortygigabitethernet", "hu": "hundredgige", "eth": "ethernet", "et": "ethernet",
              "po": "port-channel", "lo": "loopback", "vl": "vlan"}


def port_key(name):
    """A port name with its type prefix spelled out, lower case: "Gi0/1" and "GigabitEthernet0/1" both give
    "gigabitethernet0/1". A name that is not type + number comes back lower-cased as it is."""
    m = re.fullmatch(r"([A-Za-z\-]+?)\s*(\d[\d/.:]*)", (name or "").strip())
    if not m:
        return (name or "").strip().lower()
    kind = m.group(1).lower()
    for short, long_ in _PORT_LONG.items():
        if kind == short or (kind.startswith(short) and long_.startswith(kind)):
            kind = long_
            break
    return kind + m.group(2)


def port_text(port, descr):
    """The port name to show: a port id that is only a MAC address (Linux lldpd) is shown as the port description."""
    return descr if norm_mac(port) and descr else port


def _lldp_report(r, names, how=None, state=None):
    """What one end of a cable says about it, for the link details."""
    return {"reported_by": names.get(r["device_id"]), "local_port": r["local_if"], "remote_port": r["remote_port"],
            "remote_name": r["remote_sysname"], "remote_chassis": r["remote_chassis"], "remote_mgmt_ip": r["remote_mgmt_ip"],
            "remote_descr": r["remote_descr"], "remote_port_descr": r["remote_port_descr"], "matched_by": how,
            "local_port_state": state}


def _session_report(r, names):
    """What one router says about a BGP/OSPF session (remote_as is the peer's AS)."""
    return {"reported_by": names.get(r["device_id"]), "peer_ip": r["peer_ip"], "state": r["state"],
            "remote_as": r["remote_as"], "local_as": r["local_as"], "uptime": r["uptime"], "prefixes": r["prefixes"],
            "area": r["area"], "local_if": r["local_if"], "flapping": bool(r["flapping"])}


def _is_up(row):
    return row["state"] == ("Established" if row["proto"] == "bgp" else "FULL")


LAYERS = ("lldp", "bgp", "ospf", "ipsec", "default", "connected")


def build(conn, layer=None):
    """layer limits the edges to some sources: "lldp", "bgp", "ospf", "ipsec", "default", "connected", or a comma
    list such as "lldp,bgp". None or "" overlays all six; an unknown name draws none."""
    wanted = set(LAYERS) if not layer else {x.strip() for x in layer.split(",")}
    ctx = lldp_context(conn)
    owners = ctx["owners"]
    device_as = bgp_as_numbers(conn, owners)
    nodes = {}
    names = {}
    for d in conn.execute("SELECT id, hostname, role, model, vendor, platform, reachable, mgmt_ip, site FROM devices"):
        nid = "dev:{}".format(d["id"])
        names[d["id"]] = d["hostname"]
        nodes[nid] = {
            "id": nid, "label": d["hostname"], "group": d["role"],
            "device_id": d["id"], "ghost": False, "reachable": d["reachable"], "mgmt_ip": d["mgmt_ip"],
            "asn": device_as.get(d["id"]),
            "device_type": device_type(d["role"], d["model"], d["hostname"], d["platform"]),
            "vendor": (d["vendor"] or "").lower(),
            "site": d["site"] or "",   # the map's "By site" layout groups by it
        }

    def ghost_id(kind, key, linux=False):
        gid = "ghost:{}:{}".format(kind, key)
        if gid not in nodes:
            nodes[gid] = {"id": gid, "label": key, "group": "unknown", "device_id": None, "ghost": True,
                          "device_type": "linux" if linux else "unknown", "vendor": ""}
        return gid

    # A known device that a neighbor sees as plain Linux is a Linux server, whatever vendor it was added with
    # (an LLDP-adopted device starts with the Cisco defaults).
    for r in conn.execute("SELECT device_id, remote_device_id, remote_mgmt_ip, remote_descr, remote_chassis, remote_sysname "
                          "FROM lldp_neighbors WHERE remote_descr LIKE '%inux%'"):
        remote, _ = resolve_lldp(ctx, r["device_id"], r["remote_device_id"], r["remote_mgmt_ip"],
                                 r["remote_chassis"], r["remote_sysname"])
        if remote and lldp_says_linux(r["remote_descr"]) and "dev:{}".format(remote) in nodes:
            nodes["dev:{}".format(remote)].update(device_type="linux", vendor="")

    edges = []

    if "lldp" in wanted:
        rows = conn.execute(
            """SELECT device_id, local_if, remote_chassis, remote_sysname, remote_port,
                      remote_mgmt_ip, remote_device_id, remote_descr, remote_port_descr
               FROM lldp_neighbors"""
        ).fetchall()
        # Two known devices each report the other, so the same cable shows up
        # twice. Key a device-to-device link by both (device, interface) ends
        # and draw it once. If the two sides name a port differently ("Gi1"
        # vs "GigabitEthernet1") the keys differ and both edges stay — a
        # duplicate line is safer than hiding a real parallel link. Ghost
        # links are never de-duplicated.
        seen = {}   # cable (both ends) -> the edge already drawn for it
        # A cable whose local port a device reports as not up is drawn red and dashed. Names are compared with the
        # type spelled out ("Gi1" = "GigabitEthernet1"); a port that matches no interface is unknown, not down.
        oper = {(i["device_id"], port_key(i["name"])): i["oper_status"]
                for i in conn.execute("SELECT device_id, name, oper_status FROM interfaces")}

        def port_state(device_id, port):
            return oper.get((device_id, port_key(port)))

        def port_down(device_id, port):
            status = port_state(device_id, port)
            return status is not None and status != "up"
        for r in rows:
            remote, how = resolve_lldp(ctx, r["device_id"], r["remote_device_id"], r["remote_mgmt_ip"],
                                       r["remote_chassis"], r["remote_sysname"])
            if remote:
                link = frozenset([(r["device_id"], r["local_if"]), (remote, r["remote_port"])])
                if link in seen:
                    seen[link]["details"].append(_lldp_report(r, names, how, port_state(r["device_id"], r["local_if"])))   # the other end's view of the same cable
                    if port_down(r["device_id"], r["local_if"]):
                        seen[link]["up"] = False
                    continue
            target = ("dev:{}".format(remote) if remote
                      else ghost_id("lldp", r["remote_sysname"] or r["remote_chassis"] or "unknown",
                                  lldp_says_linux(r["remote_descr"])))
            edge = {
                "from": "dev:{}".format(r["device_id"]), "to": target, "layer": "lldp",
                "label": "{} - {}".format(r["local_if"], port_text(r["remote_port"], r["remote_port_descr"]) or "?"),
                "details": [_lldp_report(r, names, how, port_state(r["device_id"], r["local_if"]))],
            }
            if port_down(r["device_id"], r["local_if"]):
                edge["up"] = False
            edges.append(edge)
            if remote:
                seen[link] = edge

    protos = [p for p in ("bgp", "ospf") if p in wanted]
    if protos:
        rows = conn.execute(
            "SELECT device_id, proto, peer_ip, peer_device_id, remote_as, local_as, state, uptime, prefixes, area, local_if, flapping FROM adjacencies "
            "WHERE proto IN ({})".format(",".join("?" * len(protos))),
            protos,
        ).fetchall()
        sessions = {}   # (proto, {device, device}) -> {device_id: [rows that device reports]}
        for r in rows:
            remote = resolve(owners, r["device_id"], r["peer_device_id"], r["peer_ip"])
            if remote is None:
                # One ghost per peer IP across BGP and OSPF — the same router
                # peering both ways is one box, not two.
                gid = ghost_id("peer", r["peer_ip"])
                ends = [r["local_as"] if r["local_as"] is not None else device_as.get(r["device_id"]), r["remote_as"]]
                kind = _bgp_type(r["proto"], ends)
                if r["proto"] == "bgp" and r["remote_as"] is not None and nodes[gid].get("asn") is None:
                    nodes[gid]["asn"] = r["remote_as"]   # the area on the page puts the unknown peer in its AS
                edges.append({
                    "from": "dev:{}".format(r["device_id"]), "to": gid,
                    "layer": r["proto"], "label": _session_label(r["proto"], ends, r["state"], kind),
                    "bgp_type": kind, "up": _is_up(r), "flapping": bool(r["flapping"]),
                    "details": [_session_report(r, names)],
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
                # A router's own AS (device_as) first; what the two rows say about each other fills a gap.
                said = _as_numbers(ends, lo)
                as_pair = [device_as.get(lo, said[0]), device_as.get(hi, said[1])]
                kind = _bgp_type(proto, as_pair)
                edges.append({
                    "from": "dev:{}".format(lo), "to": "dev:{}".format(hi), "layer": proto,
                    "label": _session_label(proto, as_pair, (bad or ends)[0]["state"], kind),
                    "bgp_type": kind,
                    "up": not bad, "flapping": any(x["flapping"] for x in ends),
                    "details": [_session_report(x, names) for x in ends],
                })

    if "ipsec" in wanted:
        _ipsec(conn, ghost_id, edges)

    if "default" in wanted:
        # A device's 0.0.0.0/0 route(s): one edge per next hop, from the device to the owner of the next hop.
        # RIB only (a PAN-OS FIB row would draw the same line twice). Not de-duplicated: a default is one-way.
        for r in conn.execute("SELECT device_id, vrf, next_hop, interface, protocol, distance, metric FROM routes "
                              "WHERE kind = 'rib' AND prefix = '0.0.0.0/0' AND next_hop <> '' ORDER BY device_id, vrf, next_hop"):
            remote = resolve(owners, r["device_id"], None, r["next_hop"])
            target = "dev:{}".format(remote) if remote else ghost_id("peer", r["next_hop"])
            edges.append({
                "from": "dev:{}".format(r["device_id"]), "to": target, "layer": "default",
                "label": "default via {}{}".format(r["next_hop"], "" if r["vrf"] == "default" else " vrf " + r["vrf"]),
                "details": [{"reported_by": names.get(r["device_id"]), "vrf": r["vrf"], "next_hop": r["next_hop"],
                             "interface": r["interface"], "protocol": r["protocol"], "distance": r["distance"],
                             "metric": r["metric"]}],
            })

    if "connected" in wanted:
        _connected(conn, nodes, edges, names)

    # Saved places from the topology page; a node with no row is left for the page to lay out.
    # "positions" has every saved place, also those of nodes the page makes up itself (a group of unknown nodes).
    positions = {}
    for p in conn.execute("SELECT node_id, x, y FROM node_positions"):
        positions[p["node_id"]] = {"x": p["x"], "y": p["y"]}
        if p["node_id"] in nodes:
            nodes[p["node_id"]].update(x=p["x"], y=p["y"])

    return {"nodes": list(nodes.values()), "edges": edges, "positions": positions}


def _vpn_report(r):
    """What one end says about an IPsec tunnel, for the link details."""
    def traffic(unit):
        got = (r[unit + "_in"], r[unit + "_out"])
        return "" if got == (None, None) else "in {} / out {}".format(*("?" if x is None else x for x in got))
    return {"reported_by": r["hostname"], "tunnel": r["name"], "interface": r["interface"], "local_ip": r["local_ip"],
            "peer_ip": r["peer_ip"], "state": r["state"] or r["status"],
            "ike": " ".join(x for x in ("IKEv" + r["ike_version"] if r["ike_version"] else "", r["ike_state"]) if x),
            "ipsec_state": r["ipsec_state"], "encryption": r["encryption"], "selectors": r["selectors"], "vrf": r["vrf"],
            "packets": traffic("packets"), "bytes": traffic("bytes"), "uptime": r["uptime"]}


def _ipsec(conn, ghost_id, edges):
    """IPsec layer: one line per tunnel. A tunnel both ends report is drawn once, up only if both say up; a peer that
    is not a known device is the same unknown node a BGP peer at that address would be. Built from vpn_tunnels when
    asked; the peer's device is never stored (constraint 12)."""
    pairs = {}   # {device, device} -> {device_id: [rows that device reports]}
    for r in vpn_tunnels(conn):
        remote = r["peer_device_id"]
        if remote is None:
            edges.append({"from": "dev:{}".format(r["device_id"]), "to": ghost_id("peer", r["peer_ip"]), "layer": "ipsec",
                          "label": "IPsec " + r["status"], "up": r["status"] == "up", "details": [_vpn_report(r)]})
        else:
            pairs.setdefault(frozenset([r["device_id"], remote]), {}).setdefault(r["device_id"], []).append(r)
    for pair, by_device in pairs.items():
        lo, hi = sorted(pair)
        a = sorted(by_device.get(lo, []), key=lambda x: x["status"] != "up")
        b = sorted(by_device.get(hi, []), key=lambda x: x["status"] != "up")
        tunnels = []
        # First the two reports whose addresses mirror each other; what is left (NAT, an end with no local address)
        # is paired by position, up first.
        for x in list(a):
            twin = next((y for y in b if x["local_ip"] and (x["local_ip"], x["peer_ip"]) == (y["peer_ip"], y["local_ip"])), None)
            if twin is not None:
                tunnels.append([x, twin])
                a.remove(x)
                b.remove(twin)
        for k in range(max(len(a), len(b))):
            tunnels.append([side[k] for side in (a, b) if k < len(side)])
        for ends in tunnels:
            up = all(x["status"] == "up" for x in ends)
            edges.append({"from": "dev:{}".format(lo), "to": "dev:{}".format(hi), "layer": "ipsec",
                          "label": "IPsec " + ("up" if up else "down"), "up": up,
                          "details": [_vpn_report(x) for x in ends]})


def _connected(conn, nodes, edges, names):
    """Connected layer: devices with an address in the same subnet. Two devices get one line; three or more get a
    "subnet:<cidr>" node with a line from each. A subnet that holds a device's management IP, a /32 and a subnet
    with one device draw nothing. Built from stored interfaces, never stored, never an alias (constraint 12)."""
    mgmt = []
    for d in conn.execute("SELECT mgmt_ip FROM devices WHERE mgmt_ip IS NOT NULL AND mgmt_ip <> ''"):
        try:
            mgmt.append(ipaddress.ip_address(d["mgmt_ip"].strip()))
        except ValueError:
            pass
    subnets = {}   # network -> {device_id: (interface name, ip, oper_status)}
    for i in conn.execute("SELECT device_id, name, ip, network, prefix_len, oper_status FROM interfaces "
                          "WHERE ip IS NOT NULL AND ip <> '' AND network IS NOT NULL AND network <> '' "
                          "AND prefix_len IS NOT NULL AND prefix_len < 32 ORDER BY device_id, name"):
        subnets.setdefault(i["network"], {}).setdefault(i["device_id"], (i["name"], i["ip"], i["oper_status"]))
    for network in sorted(subnets):
        ends = subnets[network]
        if len(ends) < 2:
            continue
        try:
            net = ipaddress.ip_network(network, strict=False)
        except ValueError:
            continue
        if any(ip in net for ip in mgmt):
            continue
        reports = {dev: {"reported_by": names.get(dev), "interface": ifname, "ip": ip, "subnet": network,
                         "state": state} for dev, (ifname, ip, state) in ends.items()}
        if len(ends) == 2:
            lo, hi = sorted(ends)
            edges.append({
                "from": "dev:{}".format(lo), "to": "dev:{}".format(hi), "layer": "connected", "label": network,
                "up": not any(ends[d][2] not in (None, "up") for d in ends),
                "details": [reports[lo], reports[hi]],
            })
            continue
        sid = "subnet:{}".format(network)
        nodes[sid] = {"id": sid, "label": network, "group": "subnet", "device_id": None, "ghost": False,
                      "device_type": "subnet", "vendor": ""}
        for dev in sorted(ends):
            edges.append({
                "from": "dev:{}".format(dev), "to": sid, "layer": "connected", "label": ends[dev][0],
                "up": ends[dev][2] in (None, "up"), "details": [reports[dev]],
            })


def _as_numbers(ends, lo):
    """The AS number of each end of a drawn session, lower device id first. A row
    stores the AS of the peer it points at, so the lower device's own AS comes
    from the higher device's row and the other way round. An end that did not
    report (or a non-BGP row) leaves its AS out."""
    own = {}
    for x in ends:
        if x["remote_as"] is not None:
            own[x["device_id"] == lo] = x["remote_as"]   # True: lo's row names hi's AS
    return [own.get(False), own.get(True)]


def bgp_as_numbers(conn, owners):
    """{device_id: its own AS}. A router's `local AS number` wins; a router that never reported
    one (not polled since local_as was added) takes the AS another router names for it."""
    rows = conn.execute("SELECT device_id, peer_ip, peer_device_id, remote_as, local_as FROM adjacencies "
                        "WHERE proto = 'bgp'").fetchall()
    out = {r["device_id"]: r["local_as"] for r in rows if r["local_as"] is not None}
    for r in rows:
        remote = resolve(owners, r["device_id"], r["peer_device_id"], r["peer_ip"])
        if remote and r["remote_as"] is not None:
            out.setdefault(remote, r["remote_as"])
    return out


def _bgp_type(proto, as_numbers):
    """"ibgp" (same AS at both ends), "ebgp" (different), None when not BGP or an end's AS is unknown."""
    if proto != "bgp" or None in as_numbers:
        return None
    return "ibgp" if as_numbers[0] == as_numbers[1] else "ebgp"


def _session_label(proto, as_numbers, state, bgp_type=None):
    """"eBGP AS65001 - AS65002 Established", "iBGP AS65001 Established". Without a known type:
    "bgp AS65002 Established" (one AS when only one is known)."""
    known = [n for n in as_numbers if n is not None]
    if proto != "bgp" or not known:
        return "{} {}".format(proto, state)
    asn = "AS{}".format(known[0]) if len(set(known)) == 1 else " - ".join("AS{}".format(n) for n in known)
    name = {"ibgp": "iBGP", "ebgp": "eBGP"}.get(bgp_type, proto)
    return "{} {} {}".format(name, asn, state)


def adjacencies(conn, device_id=None):
    """BGP/OSPF rows for the API, with the effective peer_device_id."""
    owners = interface_owners(conn)
    sql = """SELECT a.device_id, d.hostname, a.proto, a.peer_ip, a.peer_device_id, a.remote_as, a.local_as,
                    a.area, a.state, a.uptime, a.prefixes, a.local_if, a.last_seen, a.flapping
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
        d["flapping"] = bool(d["flapping"])
        out.append(d)
    return out


def lldp_neighbors(conn, device_id=None):
    """LLDP rows for the API, with the effective remote_device_id."""
    ctx = lldp_context(conn)
    sql = """SELECT l.device_id, d.hostname, l.local_if, l.remote_chassis, l.remote_sysname,
                    l.remote_port, l.remote_mgmt_ip, l.remote_device_id, l.remote_descr, l.remote_port_descr, l.last_seen
             FROM lldp_neighbors l JOIN devices d ON d.id = l.device_id"""
    params = []
    if device_id:
        sql += " WHERE l.device_id = ?"
        params.append(device_id)
    sql += " ORDER BY d.hostname, l.local_if"
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["remote_device_id"], d["matched_by"] = resolve_lldp(
            ctx, d["device_id"], d["remote_device_id"], d["remote_mgmt_ip"], d["remote_chassis"], d["remote_sysname"])
        d["last_seen"] = db.iso(d["last_seen"])
        out.append(d)
    return out
