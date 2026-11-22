"""Find: where is this IP, MAC, prefix or device? (the header search box and the /find page).

Read-only and query-time only, over what the poller already stored: interfaces, routes, the MAC table, LLDP
neighbors and BGP/OSPF sessions. Nothing found here is stored, and nothing is ever written as an identity alias
(constraints 1 and 12): an address on an interface says where it is configured, not who the device is.

The answer is a list of sections, each with columns and rows, so the page can draw any of them the same way.
A row's "device_id" makes its Device cell a link.
"""

import ipaddress
import re

from . import db

LIMIT = 50
_HEX = re.compile(r"^[0-9a-f]{12}$")
# The stored MAC formats differ (IOS 0011.2233.4455, PAN-OS 00:11:22:33:44:55): compare the bare hex digits.
_HEXSQL = "replace(replace(replace(lower({}), '.', ''), ':', ''), '-', '')"


def classify(q):
    """('ip', ip_address) | ('prefix', ip_network) | ('mac', 12 hex digits) | ('text', q)."""
    q = q.strip()
    try:
        return "ip", ipaddress.ip_address(q)
    except ValueError:
        pass
    if "/" in q:
        try:
            return "prefix", ipaddress.ip_network(q, strict=False)
        except ValueError:
            pass
    bare = re.sub(r"[.:\-]", "", q.lower())
    # With separators (0011.2233.4455, 00:11:...) or 12 bare hex digits that are not all digits.
    if _HEX.match(bare) and (bare != q.lower() or not q.isdigit()):
        return "mac", bare
    return "text", q


def _section(key, title, columns, rows, hint=""):
    return {"key": key, "title": title, "columns": columns, "rows": rows[:LIMIT], "more": max(0, len(rows) - LIMIT),
            "hint": hint}


def _dev(row, field="hostname", id_field="device_id"):
    return {"device": row[field], "device_id": row[id_field]}


def find(conn, q):
    kind, value = classify(q)
    sections = []
    if kind == "ip":
        sections = _find_ip(conn, value)
    elif kind == "prefix":
        sections = _find_prefix(conn, value)
    elif kind == "mac":
        sections = _find_mac(conn, value)
    if kind == "text" or not any(s["rows"] for s in sections):
        sections += _find_text(conn, q.strip())
    return {"query": q, "kind": kind, "sections": [s for s in sections if s["rows"]]}


def _find_ip(conn, ip):
    s = str(ip)
    out = []
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, i.name, i.prefix_len, i.oper_status, i.description
           FROM interfaces i JOIN devices d ON d.id = i.device_id WHERE i.ip = ? ORDER BY d.hostname""", (s,)).fetchall()
    out.append(_section("owner", "Configured on", ["device", "interface", "address", "status", "description"],
                        [dict(_dev(r), interface=r["name"], address="{}/{}".format(s, r["prefix_len"]),
                              status=r["oper_status"], description=r["description"] or "") for r in rows],
                        "An interface address. Two or more devices here is a duplicate (or HSRP/VRRP)."))
    rows = conn.execute("SELECT id AS device_id, hostname, role FROM devices WHERE mgmt_ip = ?", (s,)).fetchall()
    out.append(_section("mgmt", "Management address of", ["device", "role"],
                        [dict(_dev(r), role=r["role"]) for r in rows]))
    # Subnets that hold the address: who is on the same segment (and could be its gateway).
    nets = []
    for r in conn.execute(
            """SELECT d.id AS device_id, d.hostname, i.name, i.ip, i.prefix_len, i.network, i.oper_status
               FROM interfaces i JOIN devices d ON d.id = i.device_id
               WHERE i.network IS NOT NULL AND i.prefix_len < 32 AND i.ip <> ?""", (s,)):
        try:
            net = ipaddress.ip_network(r["network"], strict=False)
        except ValueError:
            continue
        if ip.version == net.version and ip in net:
            nets.append((net.prefixlen, dict(_dev(r), subnet=str(net), interface=r["name"],
                                              address="{}/{}".format(r["ip"], r["prefix_len"]), status=r["oper_status"])))
    nets.sort(key=lambda t: (-t[0], t[1]["device"]))
    out.append(_section("subnet", "On the same subnet as", ["subnet", "device", "interface", "address", "status"],
                        [n for _, n in nets], "Devices with an interface in a subnet that holds this address."))
    out.append(_section("route", "Route to it, per device", ["device", "vrf", "prefix", "protocol", "next_hop", "interface"],
                        _best_routes(conn, ip),
                        "The most specific route in each device's routing table (RIB): where that device sends it."))
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, n.local_if, n.remote_sysname, n.remote_port
           FROM lldp_neighbors n JOIN devices d ON d.id = n.device_id WHERE n.remote_mgmt_ip = ? ORDER BY d.hostname""",
        (s,)).fetchall()
    out.append(_section("lldp", "Seen by LLDP", ["device", "local_port", "neighbor", "remote_port"],
                        [dict(_dev(r), local_port=r["local_if"], neighbor=r["remote_sysname"] or "",
                              remote_port=r["remote_port"] or "") for r in rows],
                        "A neighbor that advertises this management address, and the port it is cabled to."))
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, a.proto, a.state, a.remote_as, a.local_if
           FROM adjacencies a JOIN devices d ON d.id = a.device_id WHERE a.peer_ip = ? ORDER BY d.hostname""",
        (s,)).fetchall()
    out.append(_section("session", "BGP / OSPF peer of", ["device", "proto", "state", "remote_as", "interface"],
                        [dict(_dev(r), proto=r["proto"], state=r["state"],
                              remote_as="" if r["remote_as"] is None else r["remote_as"], interface=r["local_if"] or "")
                         for r in rows]))
    return out


def _best_routes(conn, ip):
    """Longest-prefix match per (device, vrf) over the stored RIB."""
    best = {}
    for r in conn.execute(
            """SELECT d.id AS device_id, d.hostname, r.vrf, r.prefix, r.protocol, r.next_hop, r.interface
               FROM routes r JOIN devices d ON d.id = r.device_id WHERE r.kind = 'rib'"""):
        try:
            net = ipaddress.ip_network(r["prefix"], strict=False)
        except ValueError:
            continue
        if net.version != ip.version or ip not in net:
            continue
        key = (r["device_id"], r["vrf"])
        if key not in best or net.prefixlen > best[key][0]:
            best[key] = (net.prefixlen, [r])
        elif net.prefixlen == best[key][0]:
            best[key][1].append(r)    # equal-cost paths
    out = []
    for _, rows in best.values():
        for r in rows:
            out.append(dict(_dev(r), vrf=r["vrf"], prefix=r["prefix"], protocol=r["protocol"], next_hop=r["next_hop"],
                            interface=r["interface"]))
    out.sort(key=lambda d: (d["device"], d["vrf"]))
    return out


def _find_prefix(conn, net):
    out = []
    addrs = []
    for r in conn.execute(
            """SELECT d.id AS device_id, d.hostname, i.name, i.ip, i.prefix_len, i.oper_status
               FROM interfaces i JOIN devices d ON d.id = i.device_id WHERE i.ip IS NOT NULL"""):
        try:
            a = ipaddress.ip_address(r["ip"])
        except ValueError:
            continue
        if a.version == net.version and a in net:
            addrs.append(dict(_dev(r), address="{}/{}".format(r["ip"], r["prefix_len"]), interface=r["name"],
                              status=r["oper_status"]))
    addrs.sort(key=lambda d: ipaddress.ip_address(d["address"].split("/")[0]))
    out.append(_section("address", "Interface addresses in it", ["address", "device", "interface", "status"], addrs))
    routes = []
    for r in conn.execute(
            """SELECT d.id AS device_id, d.hostname, r.vrf, r.prefix, r.protocol, r.next_hop, r.interface
               FROM routes r JOIN devices d ON d.id = r.device_id WHERE r.kind = 'rib'"""):
        try:
            p = ipaddress.ip_network(r["prefix"], strict=False)
        except ValueError:
            continue
        if p.version == net.version and (p.subnet_of(net) or net.subnet_of(p)):
            routes.append(dict(_dev(r), vrf=r["vrf"], prefix=r["prefix"], protocol=r["protocol"], next_hop=r["next_hop"],
                               interface=r["interface"]))
    routes.sort(key=lambda d: (d["device"], d["vrf"], d["prefix"]))
    out.append(_section("route", "Routes to it or covering it", ["device", "vrf", "prefix", "protocol", "next_hop", "interface"],
                        routes))
    return out


def _find_mac(conn, hexmac):
    out = []
    trunk = {(r["device_id"], r["port"]) for r in conn.execute("SELECT device_id, port FROM trunks")}
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, m.vlan, m.mac, m.port, m.last_seen
           FROM mac_table m JOIN devices d ON d.id = m.device_id
           WHERE {} = ? ORDER BY d.hostname""".format(_HEXSQL.format("m.mac")), (hexmac,)).fetchall()
    learned = []
    for r in rows:
        on_trunk = (r["device_id"], r["port"]) in trunk or r["port"].lower().startswith(("po", "port-channel"))
        learned.append(dict(_dev(r), vlan=r["vlan"], port=r["port"],
                            where="through a trunk" if on_trunk else "access port (likely where it is)",
                            last_seen=db.iso(r["last_seen"]), _rank=1 if on_trunk else 0))
    learned.sort(key=lambda d: (d.pop("_rank"), d["device"]))
    out.append(_section("learned", "Learned by switches", ["device", "vlan", "port", "where", "last_seen"], learned,
                        "From the switches' MAC tables. An access port is usually where the host is cabled; a trunk only "
                        "says which way it is."))
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, i.name, i.ip, i.prefix_len FROM interfaces i
           JOIN devices d ON d.id = i.device_id WHERE {} = ?""".format(_HEXSQL.format("i.mac")), (hexmac,)).fetchall()
    owners = [dict(_dev(r), interface=r["name"], address="" if not r["ip"] else "{}/{}".format(r["ip"], r["prefix_len"]))
              for r in rows]
    for r in conn.execute("SELECT id AS device_id, hostname FROM devices WHERE {} = ?".format(_HEXSQL.format("mgmt_mac")),
                          (hexmac,)):
        owners.append(dict(_dev(r), interface="management", address=""))
    out.append(_section("owner", "Hardware address of", ["device", "interface", "address"], owners))
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, n.local_if, n.remote_sysname, n.remote_port FROM lldp_neighbors n
           JOIN devices d ON d.id = n.device_id WHERE {} = ?""".format(_HEXSQL.format("n.remote_chassis")),
        (hexmac,)).fetchall()
    out.append(_section("lldp", "LLDP chassis ID seen by", ["device", "local_port", "neighbor", "remote_port"],
                        [dict(_dev(r), local_port=r["local_if"], neighbor=r["remote_sysname"] or "",
                              remote_port=r["remote_port"] or "") for r in rows]))
    return out


def _find_text(conn, q):
    if not q:
        return []
    like = db.like_arg(q.lower())
    out = []
    rows = conn.execute(
        """SELECT id AS device_id, hostname, mgmt_ip, role, site, model FROM devices
           WHERE lower(hostname) LIKE ? ESCAPE '\\' OR lower(COALESCE(serial, '')) LIKE ? ESCAPE '\\'
              OR lower(key) LIKE ? ESCAPE '\\' OR lower(site) LIKE ? ESCAPE '\\' OR COALESCE(mgmt_ip, '') LIKE ? ESCAPE '\\'
           ORDER BY hostname""", (like,) * 5).fetchall()
    out.append(_section("device", "Devices", ["device", "mgmt_ip", "role", "site", "model"],
                        [dict(_dev(r), mgmt_ip=r["mgmt_ip"] or "", role=r["role"], site=r["site"], model=r["model"] or "")
                         for r in rows]))
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, i.name, i.description, i.ip, i.prefix_len, i.oper_status
           FROM interfaces i JOIN devices d ON d.id = i.device_id
           WHERE lower(i.description) LIKE ? ESCAPE '\\' OR lower(i.name) = ? OR COALESCE(i.ip, '') LIKE ? ESCAPE '\\'
           ORDER BY d.hostname, i.name""", (like, q.lower(), like)).fetchall()
    out.append(_section("interface", "Interfaces", ["device", "interface", "description", "address", "status"],
                        [dict(_dev(r), interface=r["name"], description=r["description"] or "",
                              address="" if not r["ip"] else "{}/{}".format(r["ip"], r["prefix_len"]), status=r["oper_status"])
                         for r in rows]))
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, n.local_if, n.remote_sysname, n.remote_port, n.remote_mgmt_ip
           FROM lldp_neighbors n JOIN devices d ON d.id = n.device_id
           WHERE lower(COALESCE(n.remote_sysname, '')) LIKE ? ESCAPE '\\' ORDER BY d.hostname""", (like,)).fetchall()
    out.append(_section("lldp", "LLDP neighbors named like it", ["neighbor", "mgmt_ip", "device", "local_port", "remote_port"],
                        [dict(_dev(r), neighbor=r["remote_sysname"], mgmt_ip=r["remote_mgmt_ip"] or "",
                              local_port=r["local_if"], remote_port=r["remote_port"] or "") for r in rows],
                        "Includes neighbors that are not devices here."))
    return out
