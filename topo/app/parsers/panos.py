"""PAN-OS XML API response parsers.

The same row shapes as parsers/ios.py, so the apply steps, events and topology need no change. XML is
parsed with the standard library's ElementTree (no regex here, and no new dependency); every function
fails soft like the IOS parsers: an empty, unparsable or unexpected answer returns an empty result,
never an exception.

Field names come from real output captured on a PA-VM running PAN-OS 11.1 (dev/samples/panos_*.xml).
Only IPv4 is read, as in the IOS parsers.
"""

import re
import xml.etree.ElementTree as ET

from .ios import _network

_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def _root(text):
    """The <result> element of a successful response, or None."""
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, TypeError):
        return None
    if root.get("status") != "success":
        return None
    return root.find("result")


def _text(el, tag):
    """Stripped text of a child element, None when missing or empty."""
    child = el.find(tag) if el is not None else None
    value = (child.text or "").strip() if child is not None else ""
    return value or None


def _ipv4(value):
    """'192.0.2.1:179' or '192.0.2.1/24' -> '192.0.2.1'; None when it is not an IPv4 address."""
    m = _IPV4.fullmatch((value or "").split(":")[0].split("/")[0])
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# <show><system><info/></system></show>
# ---------------------------------------------------------------------------

def parse_version(text):
    system = _root(text)
    system = system.find("system") if system is not None else None
    return {
        "model": _text(system, "model"),
        "serial": _text(system, "serial"),
        "os_version": _text(system, "sw-version"),
        "hostname_hint": _text(system, "hostname"),
        "mgmt_mac": _text(system, "mac-address"),
    }


# ---------------------------------------------------------------------------
# <show><interface>all</interface></show>
#
# <ifnet> has every interface with its address (and sub-interfaces, loopbacks, tunnels); <hw> has link
# state, speed and duplex of the physical ones. Only an <hw> row says whether a link is up, so an
# interface without one (a sub-interface) has no oper_status rather than a guessed one.
# ---------------------------------------------------------------------------

def parse_interfaces(text):
    result = _root(text)
    if result is None:
        return []
    hw = {}
    for e in result.findall("hw/entry"):
        name = _text(e, "name")
        if name:
            hw[name] = e
    rows = []
    seen = set()
    for e in result.findall("ifnet/entry"):
        name = _text(e, "name")
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append(_interface_row(name, e, hw.get(name)))
    for name, h in hw.items():   # a physical port the firewall lists no address row for
        if name not in seen:
            rows.append(_interface_row(name, None, h))
    return rows


def _interface_row(name, ifnet, hw):
    ip = prefix = None
    addr = _text(ifnet, "ip") if ifnet is not None else None
    m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3})/(\d+)", addr or "")
    if m:
        ip, prefix = m.group(1), int(m.group(2))
    zone = _text(ifnet, "zone") if ifnet is not None else None
    state = _text(hw, "state") if hw is not None else None
    speed = _text(hw, "speed") if hw is not None else None
    duplex = _text(hw, "duplex") if hw is not None else None
    return {
        "name": name,
        "admin_status": None,   # the firewall does not say
        "oper_status": state if state in ("up", "down") else None,
        "description": "zone " + zone if zone else "",
        "ip": ip,
        "prefix_len": prefix,
        "network": _network(ip, prefix) if ip else None,
        "speed": speed + "Mb/s" if speed and speed.isdigit() else None,
        "duplex": duplex.capitalize() if duplex in ("full", "half", "auto") else None,
        "mtu": None,
        "input_errors": None,
        "crc_errors": None,
    }


# ---------------------------------------------------------------------------
# <show><lldp><neighbors>all</neighbors></lldp></show>
#
# <entry name="ethernet1/1"><neighbors><entry>...: one outer entry per local port, one inner per neighbor.
# The remote port is the neighbor's port id, as in the IOS parser ("Port id:"). It is what the neighbor's own
# row calls that port ("Gi0/7"), so a cable both ends report is drawn once. The port description is not used for that: it
# is free text an admin sets ("uplink to esx"). It is kept as remote_port_descr for the map to show when the port id is
# only a MAC address; it is the port id's fallback only when that is missing.
# ---------------------------------------------------------------------------

def parse_lldp_neighbors(text):
    result = _root(text)
    if result is None:
        return []
    out = []
    for port in result.findall("entry"):
        local_if = port.get("name")
        for n in port.findall("neighbors/entry"):
            chassis = _text(n, "chassis-id")
            if not local_if or not chassis:
                continue
            mgmt_ip = None
            for a in n.findall("management-address/entry"):
                if _text(a, "address-type") == "ipv4" and _ipv4(a.get("name")):
                    mgmt_ip = _ipv4(a.get("name"))
                    break
            descr = _text(n, "system-description")
            out.append({
                "local_if": local_if,
                "remote_chassis": chassis,
                "remote_port": _text(n, "port-id") or _text(n, "port-description"),
                "remote_sysname": _text(n, "system-name"),
                "remote_mgmt_ip": mgmt_ip,
                "remote_descr": descr.splitlines()[0].strip()[:200] if descr else None,
                "remote_port_descr": (_text(n, "port-description") or "")[:100] or None,
            })
    return out


# ---------------------------------------------------------------------------
# <show><routing><protocol><bgp><peer/></bgp></protocol></routing></show>
# <show><routing><protocol><bgp><summary/></bgp></protocol></routing></show>   (the router's own AS)
# ---------------------------------------------------------------------------

def _age(seconds):
    """Seconds as IOS prints an uptime: 'never', 'HH:MM:SS', or 'NdHHh' from a day on."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return "never"
    if s >= 86400:
        return "{}d{:02d}h".format(s // 86400, s % 86400 // 3600)
    return "{:02d}:{:02d}:{:02d}".format(s // 3600, s % 3600 // 60, s % 60)


def parse_bgp_peers(text):
    """One row per peer. A peer address that two virtual routers both have keeps its first row (adjacencies
    is unique per device, protocol and peer address)."""
    result = _root(text)
    if result is None:
        return []
    out = []
    seen = set()
    for e in result.findall("entry"):
        ip = _ipv4(_text(e, "peer-address"))
        asn = _text(e, "remote-as")
        if not ip or ip in seen or not (asn or "").isdigit():
            continue
        seen.add(ip)
        prefixes = None
        for pc in e.findall("prefix-counter/entry"):
            total = _text(pc, "incoming-total")
            if total and total.isdigit():
                prefixes = int(total)
                break
        out.append({
            "peer_ip": ip,
            "remote_as": int(asn),
            "local_as": None,   # parse_bgp_local_as + fill_bgp_local_as
            "state": _text(e, "status") or "unknown",
            "prefixes": prefixes,
            "uptime": _age(_text(e, "status-duration")),
            "vr": e.get("vr"),
        })
    return out


def parse_bgp_local_as(text):
    """{virtual router: its local AS number} from the bgp summary."""
    result = _root(text)
    if result is None:
        return {}
    out = {}
    for e in result.findall("entry"):
        asn = _text(e, "local-as")
        if asn and asn.isdigit() and e.get("virtual-router"):
            out[e.get("virtual-router")] = int(asn)
    return out


def fill_bgp_local_as(peers, local_as):
    """Give each peer the local AS of its virtual router; a peer in a router the summary does not list keeps None."""
    return [dict(p, local_as=local_as.get(p["vr"])) for p in peers]


# ---------------------------------------------------------------------------
# <show><routing><protocol><ospf><neighbor/></ospf></protocol></routing></show>
# ---------------------------------------------------------------------------

def parse_ospf_neighbors(text):
    """State is upper-cased and dashes dropped so it reads as on IOS: 'full' -> 'FULL', '2-way' -> '2WAY'."""
    result = _root(text)
    if result is None:
        return []
    out = []
    seen = set()
    for e in result.findall("entry"):
        ip = _ipv4(_text(e, "neighbor-address"))
        if not ip or ip in seen:
            continue
        seen.add(ip)
        out.append({
            "peer_ip": ip,
            "state": (_text(e, "status") or "unknown").upper().replace("-", ""),
            "local_if": None,
        })
    return out
