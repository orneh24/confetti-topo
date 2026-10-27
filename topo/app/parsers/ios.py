"""Cisco IOS/IOS-XE 'show' output parsers.

Pure regex, deliberately not netmiko's use_textfsm=True. TextFSM needs the
ntc-templates package, and constraint 14 in confetti-traffic's CLAUDE.md is a
reminder that an Alpine package assumption can silently fail to hold — this
avoids that dependency entirely, same choice confetti-traffic itself made
everywhere (fping/traceroute/smbclient output, all regex).

Device output is untrusted input:
every function here fails soft — an unrecognised or empty response returns
an empty result, never an exception, so one bad command in a task list never
takes down the rest of a device's poll cycle.
"""

import re

# ---------------------------------------------------------------------------
# show version
# ---------------------------------------------------------------------------

_MODEL_RE = re.compile(r"^[Cc]isco\s+(\S+)\s*\(", re.MULTILINE)
_SERIAL_RE = re.compile(r"Processor board ID\s+(\S+)")
_VERSION_RE = re.compile(r"Version\s+([\w.()]+),\s*RELEASE")
_HOSTNAME_HINT_RE = re.compile(r"^(\S+)\s+uptime is", re.MULTILINE)


def parse_version(text):
    """Extract model, serial and OS version from 'show version'.

    Returns a dict with possibly-None values rather than raising — a
    platform whose banner doesn't match one of these patterns should still
    let the rest of the version task succeed with whatever it did find.
    """
    model = _MODEL_RE.search(text)
    serial = _SERIAL_RE.search(text)
    version = _VERSION_RE.search(text)
    hostname_hint = _HOSTNAME_HINT_RE.search(text)
    return {
        "model": model.group(1) if model else None,
        "serial": serial.group(1) if serial else None,
        "os_version": version.group(1) if version else None,
        "hostname_hint": hostname_hint.group(1) if hostname_hint else None,
    }


# ---------------------------------------------------------------------------
# show interfaces (full detail — one command covers status, IP, speed/duplex,
# errors and MTU, so nothing here depends on 'show ip interface brief' too)
# ---------------------------------------------------------------------------

_INTF_HEADER_RE = re.compile(
    r"^(?P<name>\S+) is (?P<admin>(?:administratively )?down|up),"
    r" line protocol is (?P<oper>up|down)",
    re.MULTILINE,
)
_DESCRIPTION_RE = re.compile(r"Description:\s*(.+)")
_IP_RE = re.compile(r"Internet address is (?P<ip>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d+)")
_MTU_RE = re.compile(r"MTU (\d+) bytes")
_DUPLEX_SPEED_RE = re.compile(
    r"(?P<duplex>Full|Half|Auto)[- ]?[Dd]uplex,\s*(?P<speed>\d+\s*[MGmg]b(?:ps|/s))"
)
_ERRORS_RE = re.compile(r"(?P<in_errors>\d+) input errors,\s*(?P<crc>\d+) CRC")


def parse_interfaces(text):
    """Parse 'show interfaces' into one dict per interface.

    Each entry: name, admin_status (up/down), oper_status (up/down),
    description, ip, prefix_len, network, speed, duplex, mtu, input_errors,
    crc_errors. A field this device's platform doesn't emit (no
    'Description:' line, no L3 address on a trunk parent) stays None/""
    rather than raising — see parse_version's reasoning.
    """
    headers = list(_INTF_HEADER_RE.finditer(text))
    out = []
    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        chunk = text[m.start():end]

        admin = "down" if "down" in m.group("admin") else "up"
        entry = {
            "name": m.group("name"),
            "admin_status": admin,
            "oper_status": m.group("oper"),
            "description": "",
            "ip": None,
            "prefix_len": None,
            "network": None,
            "speed": None,
            "duplex": None,
            "mtu": None,
            "input_errors": None,
            "crc_errors": None,
        }

        desc = _DESCRIPTION_RE.search(chunk)
        if desc:
            entry["description"] = desc.group(1).strip()

        ip = _IP_RE.search(chunk)
        if ip:
            entry["ip"] = ip.group("ip")
            entry["prefix_len"] = int(ip.group("prefix"))
            entry["network"] = _network(ip.group("ip"), int(ip.group("prefix")))

        mtu = _MTU_RE.search(chunk)
        if mtu:
            entry["mtu"] = int(mtu.group(1))

        ds = _DUPLEX_SPEED_RE.search(chunk)
        if ds:
            entry["duplex"] = ds.group("duplex")
            entry["speed"] = ds.group("speed")

        err = _ERRORS_RE.search(chunk)
        if err:
            entry["input_errors"] = int(err.group("in_errors"))
            entry["crc_errors"] = int(err.group("crc"))

        out.append(entry)
    return out


def _network(ip, prefix_len):
    """CIDR network address for an interface IP — no ipaddress import needed
    at call sites."""
    try:
        octets = [int(o) for o in ip.split(".")]
        bits = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
        mask = (0xFFFFFFFF << (32 - prefix_len)) & 0xFFFFFFFF if prefix_len else 0
        net = bits & mask
        return "{}.{}.{}.{}/{}".format(
            (net >> 24) & 0xFF, (net >> 16) & 0xFF, (net >> 8) & 0xFF, net & 0xFF, prefix_len
        )
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# show lldp neighbors detail
#
# Always on in confetti-traffic's own VMs but frequently disabled on a router
# ("% LLDP is not enabled") — that is a normal device state, not a poll
# failure, so this returns an empty list rather than treating it as an error.
# The caller (poller.py) marks the task ok=True either way; only a
# transport-level failure (timeout, auth) is ok=False.
# ---------------------------------------------------------------------------

_LLDP_BLOCK_RE = re.compile(r"-{10,}")
_LLDP_LOCAL_IF_RE = re.compile(r"^Local Intf:\s*(\S+)", re.MULTILINE)
_LLDP_CHASSIS_RE = re.compile(r"^Chassis id:\s*(\S+)", re.MULTILINE)
_LLDP_PORT_RE = re.compile(r"^Port id:\s*(\S+)", re.MULTILINE)
_LLDP_PORT_DESCR_RE = re.compile(r"^Port Description:[ \t]*(\S[^\n]*)", re.MULTILINE)
_LLDP_SYSNAME_RE = re.compile(r"^System Name:\s*(\S+)", re.MULTILINE)
# The description is on the line after the label on IOS; take the first non-empty line.
_LLDP_DESCR_RE = re.compile(r"^System Description:[ \t]*\n?[ \t]*(\S[^\n]*)", re.MULTILINE)
_LLDP_MGMT_IP_RE = re.compile(r"IP:\s*(\d{1,3}(?:\.\d{1,3}){3})")


def parse_lldp_neighbors(text):
    """Parse 'show lldp neighbors detail' into one dict per neighbor block.

    Older IOS (IOS-XE 3.x / 15.4, confirmed on a real CSR1000v) prints no
    "Local Intf:" line in the detail output. Those rows come back with
    local_if=None; the collector fills it from the brief table
    (fill_lldp_local_if) and drops any row it still can't place.
    """
    out = []
    for block in _LLDP_BLOCK_RE.split(text):
        chassis = _LLDP_CHASSIS_RE.search(block)
        if not chassis:
            continue
        local_if = _LLDP_LOCAL_IF_RE.search(block)
        port = _LLDP_PORT_RE.search(block)
        sysname = _LLDP_SYSNAME_RE.search(block)
        mgmt_ip = _LLDP_MGMT_IP_RE.search(block)
        descr = _LLDP_DESCR_RE.search(block)
        port_descr = _LLDP_PORT_DESCR_RE.search(block)
        out.append({
            "local_if": local_if.group(1) if local_if else None,
            "remote_chassis": chassis.group(1),
            "remote_port": port.group(1) if port else None,
            "remote_sysname": sysname.group(1) if sysname else None,
            "remote_mgmt_ip": mgmt_ip.group(1) if mgmt_ip else None,
            "remote_descr": descr.group(1).strip()[:200] if descr else None,
            "remote_port_descr": port_descr.group(1).strip()[:100] if port_descr else None,
        })
    return out


def parse_lldp_brief(text):
    """Parse 'show lldp neighbors' (the table) into
    {device_id, local_if, remote_port} rows.

    Device ID is a fixed 20-char column and can run straight into Local Intf
    with no space ("LAB-RTR-C.lab.locGi1"), so the split is by the
    header's column position, not by whitespace.
    """
    out = []
    col = None
    for line in text.splitlines():
        if col is None:
            if line.startswith("Device ID") and "Local Intf" in line:
                col = line.index("Local Intf")
            continue
        if not line.strip() or line.startswith("Total entries"):
            continue
        rest = line[col:].split()
        if len(rest) < 2:
            continue
        out.append({"device_id": line[:col].strip(), "local_if": rest[0], "remote_port": rest[-1]})
    return out


def fill_lldp_local_if(neighbors, brief):
    """Fill local_if on detail rows that lack it, from the brief table.

    A brief row matches when Port ID is equal and its Device ID is either a
    prefix of the system name (it may be truncated, or the bare hostname of
    an FQDN) or the chassis id (what IOS shows when no system name is sent).
    Exactly one match is required; rows still without local_if are dropped,
    since local_if is part of lldp_neighbors' unique key.
    """
    out = []
    for row in neighbors:
        if not row["local_if"]:
            matches = [
                b for b in brief
                if b["remote_port"] == row["remote_port"] and b["device_id"]
                and ((row["remote_sysname"] or "").startswith(b["device_id"])
                     or b["device_id"] == row["remote_chassis"])
            ]
            if len(matches) != 1:
                continue
            row = dict(row, local_if=matches[0]["local_if"])
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# show bgp summary — ported from the network-bgp-diagnostics skill
# ---------------------------------------------------------------------------

_BGP_SUMMARY_RE = re.compile(
    r"^(?P<neighbor>\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"(?P<version>\d+)\s+"
    r"(?P<remote_as>\d+)\s+"
    r"(?P<msg_rcvd>\d+)\s+"
    r"(?P<msg_sent>\d+)\s+"
    r"(?P<tbl_ver>\d+)\s+"
    r"(?P<in_q>\d+)\s+"
    r"(?P<out_q>\d+)\s+"
    r"(?P<uptime>\S+)\s+"
    r"(?P<state_pfx>\S+(?:[ \t]+\(Admin\))?)",   # "Idle (Admin)" = shut down on purpose
    re.MULTILINE,
)


_BGP_LOCAL_AS_RE = re.compile(r"local AS number (\d+)")   # the router's own AS, in the header line


def parse_bgp_summary(text):
    """'% BGP not active' or no neighbors both fall through to an empty
    list naturally — the regex simply finds nothing to match."""
    neighbors = []
    local = _BGP_LOCAL_AS_RE.search(text)
    local_as = int(local.group(1)) if local else None
    for m in _BGP_SUMMARY_RE.finditer(text):
        state_pfx = m.group("state_pfx")
        try:
            prefixes = int(state_pfx)
            state = "Established"
        except ValueError:
            prefixes = None
            state = state_pfx
        neighbors.append({
            "peer_ip": m.group("neighbor"),
            "remote_as": int(m.group("remote_as")),
            "local_as": local_as,
            "state": state,
            "prefixes": prefixes,
            "uptime": m.group("uptime"),
        })
    return neighbors


# ---------------------------------------------------------------------------
# show ip ospf neighbor
# ---------------------------------------------------------------------------

_OSPF_NEIGHBOR_RE = re.compile(
    r"^(?P<neighbor_id>\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"(?P<priority>\d+)\s+"
    r"(?P<state>\S+)\s+"
    r"(?P<dead_time>\S+)\s+"
    r"(?P<address>\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"(?P<interface>\S+)",
    re.MULTILINE,
)


def parse_ospf_neighbors(text):
    out = []
    for m in _OSPF_NEIGHBOR_RE.finditer(text):
        # State is reported as e.g. "FULL/BDR" — the slash and role are
        # local to this router's view of the adjacency, not part of the
        # neighbor's own identity, so only the state half is kept.
        state = m.group("state").split("/")[0]
        out.append({
            "peer_ip": m.group("address"),
            "state": state,
            "local_if": m.group("interface"),
        })
    return out


# ---------------------------------------------------------------------------
# show ip route vrf *     (the global table first, then "Routing Table: <vrf>" for each VRF)
#
# Under "172.16.0.0/24 is subnetted" the route lines carry no mask, so the header's mask is used.
# A long prefix may sit alone on its line with "[110/2] via ..." on the next, and an equal-cost path is
# a further indented "[d/m] via ..." line: both belong to the last prefix seen. Checked against real
# IOS-XE 17.3 and 15.4 output; the continuation line is not in that output (see dev/samples/README.md).
# ---------------------------------------------------------------------------

_ROUTE_TABLE_RE = re.compile(r"^Routing Table:\s*(\S+)")
_ROUTE_HEADER_RE = re.compile(r"^\s+\d{1,3}(?:\.\d{1,3}){3}/(\d+) is (?:variably )?subnetted")
_ROUTE_LINE_RE = re.compile(
    r"^(?P<code>\S.{0,7}?)\s+(?P<prefix>\d{1,3}(?:\.\d{1,3}){3}(?:/\d+)?)(?:\s+(?P<rest>\S.*))?$")
_ROUTE_VIA_RE = re.compile(r"^\s*\[(\d+)/(\d+)\]\s+via\s+(\d{1,3}(?:\.\d{1,3}){3})(?:,\s*(.*))?$")
_ROUTE_CONNECTED_RE = re.compile(r"is directly connected,\s*(\S+)")
_ROUTE_SUMMARY_RE = re.compile(r"is a summary,\s*(?:(\d\S*),\s*)?(\S+)")
_ROUTE_PROTOCOLS = {"C": "connected", "L": "local", "S": "static", "R": "rip", "B": "bgp", "D": "eigrp",
                    "O": "ospf", "i": "isis", "o": "odr", "M": "mobile", "H": "nhrp", "P": "static"}


def parse_routes(text):
    """One row per next hop: vrf, kind ('rib'), prefix, protocol, next_hop, interface, distance, metric, age,
    flags (the route code as printed, e.g. 'O E2'). Text columns are '' not None (the table's key)."""
    out = []
    vrf, mask, last = "default", None, None   # last = (code, prefix) for a continuation line

    def row(code, prefix, next_hop="", interface="", distance=None, metric=None, age=None):
        out.append({
            "vrf": vrf, "kind": "rib", "prefix": prefix,
            "protocol": _ROUTE_PROTOCOLS.get(code.replace("*", "").strip()[:1], code.strip()),
            "next_hop": next_hop, "interface": interface, "distance": distance, "metric": metric,
            "age": age, "flags": code.strip(),
        })

    def via(code, prefix, text_after):
        m = _ROUTE_VIA_RE.match(text_after)
        if not m:
            return False
        # After the next hop: an age (starts with a digit: 2w5d, 00:01:00), an interface (starts with a letter), or both.
        age, interface = None, ""
        for tok in [t.strip() for t in (m.group(4) or "").split(",") if t.strip()]:
            if tok[0].isdigit():
                age = tok
            else:
                interface = tok.split()[0]
        row(code, prefix, m.group(3), interface, int(m.group(1)), int(m.group(2)), age)
        return True

    for line in text.splitlines():
        t = _ROUTE_TABLE_RE.match(line)
        if t:
            vrf, mask, last = t.group(1), None, None
            continue
        h = _ROUTE_HEADER_RE.match(line)
        if h:
            mask = h.group(1)
            continue
        if last and line[:1].isspace() and via(last[0], last[1], line):
            continue
        m = _ROUTE_LINE_RE.match(line)
        if not m:
            continue
        code, prefix, rest = m.group("code"), m.group("prefix"), m.group("rest") or ""
        if "/" not in prefix and mask:
            prefix += "/" + mask
        last = (code, prefix)
        c = _ROUTE_CONNECTED_RE.search(rest)
        s = _ROUTE_SUMMARY_RE.search(rest)
        if c:
            row(code, prefix, interface=c.group(1))
        elif s:
            row(code, prefix, interface=s.group(2), age=s.group(1))
        elif not via(code, prefix, rest):
            pass   # prefix alone on its line: the next line carries "[d/m] via ..."
    return out
