"""Fill a NEW scratch database with a fake lab, for README screenshots and demos.

Three routers (documentation-range IPs, fake serials 9SIMLAB000x), their
interfaces, LLDP/OSPF/BGP neighbours (some resolve, some stay ghosts), one
unknown LLDP neighbour waiting to be added, one identity conflict, a few
change events, a config template with two running-config backups per router (one
drifted), interface error history, an overlapping prefix for IPAM
and some syslog. No real lab data.

    python scripts/seed_mock_lab.py mock-lab.db

Refuses to touch a file that already exists. Devices are marked "polled just
now" and next_poll_at is far in the future, so a server started on this file
never dials the fake addresses. Used by capture_readme.py.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

if len(sys.argv) != 2:
    sys.exit("usage: seed_mock_lab.py <new-db-path>")
if os.path.exists(sys.argv[1]):
    sys.exit("{} already exists - pick a new path".format(sys.argv[1]))
os.environ["TOPO_DB_PATH"] = sys.argv[1]

from app import db, drift, history, identity, ipam, rendering  # noqa: E402  (env var must be set first)
from app.parsers import ios  # noqa: E402

ROUTERS = [
    # hostname, mgmt_ip, serial, os_version
    ("lab-rtr-a", "192.0.2.10", "9SIMLAB0001", "17.3.8a"),
    ("lab-rtr-b", "192.0.2.171", "9SIMLAB0002", "17.3.2"),
    ("lab-rtr-c", "192.0.2.167", "9SIMLAB0003", "3.11.4S"),
]

# device index -> (name, description, ip, prefix_len)
INTERFACES = {
    0: [("GigabitEthernet1", "mgmt", "192.0.2.10", 24),
        ("GigabitEthernet2", "to lab-rtr-b", "10.0.12.1", 24),
        ("GigabitEthernet3", "to lab-rtr-c", "10.0.13.1", 30),
        ("GigabitEthernet4", "shared LAN", "10.50.0.1", 24),
        ("Loopback0", "router id", "10.255.0.1", 32)],
    1: [("GigabitEthernet1", "mgmt", "192.0.2.171", 24),
        ("GigabitEthernet2", "to lab-rtr-a", "10.0.12.2", 24),
        ("GigabitEthernet3", "to lab-rtr-c", "10.0.23.1", 24),
        ("GigabitEthernet4", "shared LAN", "10.50.0.2", 24),
        ("Loopback0", "router id", "10.255.0.2", 32)],
    2: [("GigabitEthernet1", "mgmt", "192.0.2.167", 24),
        ("GigabitEthernet2", "to lab-rtr-b", "10.0.23.2", 24),
        ("GigabitEthernet3", "to lab-rtr-a", "10.0.13.2", 30),
        ("GigabitEthernet5", "shared LAN", "10.50.0.3", 24),
        ("Loopback0", "router id", "10.255.0.3", 32)],
}

# device index -> next hop of its default route: a goes out to an outside address (a ghost), b via a, c via b
DEFAULT_ROUTES = {0: "203.0.113.1", 1: "10.0.12.1", 2: "10.0.23.1"}

# device index, local_if, remote sysname, remote port, remote device index (None = unknown), remote mgmt ip
LLDP = [
    (0, "GigabitEthernet2", "lab-rtr-b", "GigabitEthernet2", 1, "192.0.2.171"),
    (1, "GigabitEthernet2", "lab-rtr-a", "GigabitEthernet2", 0, "192.0.2.10"),
    (1, "GigabitEthernet3", "lab-rtr-c", "GigabitEthernet2", 2, "192.0.2.167"),
    (2, "GigabitEthernet2", "lab-rtr-b", "GigabitEthernet3", 1, "192.0.2.171"),
    (2, "GigabitEthernet4", "sw-access-1", "Fa0/24", None, "192.0.2.50"),
]

LAB_AS = 65000   # all three routers: the 65000 peers are iBGP, the 64500 / 64501 ones eBGP

# device index, proto, peer_ip, remote_as, area, state, uptime, prefixes, local_if
# Peers on a loopback or a link address resolve through the interface table
# (the poller stored no peer_device_id); the 203.0.113.x / 198.51.100.x peers
# are outside the lab and stay ghosts.
ADJACENCIES = [
    (0, "ospf", "10.255.0.2", None, "0", "FULL", "2d03h", None, "GigabitEthernet2"),
    (1, "ospf", "10.255.0.1", None, "0", "FULL", "2d03h", None, "GigabitEthernet2"),
    (0, "bgp", "10.255.0.3", 65000, None, "Established", "1d02h", 14, None),
    (2, "bgp", "10.255.0.1", 65000, None, "Established", "1d02h", 9, None),
    (0, "bgp", "203.0.113.9", 64500, None, "Established", "1d02h", 14, None),
    (1, "bgp", "198.51.100.7", 64501, None, "Established", "05:12:44", 6, None),
    (2, "bgp", "198.51.100.8", 64501, None, "Idle (Admin)", "never", None, None),
]

# minutes ago, router index, kind, severity, subject, detail
EVENTS = [
    (3, 1, "bgp_state", "info", "lab-rtr-b BGP peer 198.51.100.7: Established", "Active -> Established"),
    (11, 2, "bgp_peer_added", "info", "lab-rtr-c BGP peer 198.51.100.8: Idle (Admin)", ""),
    (24, 0, "lldp_neighbor_added", "info", "lab-rtr-a GigabitEthernet3: neighbor lab-rtr-c", "remote port GigabitEthernet3"),
    (47, 2, "device_unreachable", "warning", "lab-rtr-c: polling failed", ""),
    (95, 1, "ospf_state", "warning", "lab-rtr-b OSPF peer 10.255.0.1: EXSTART", "FULL -> EXSTART"),
    (6, 1, "bgp_reset", "warning", "lab-rtr-b BGP peer 198.51.100.7: session reset", "07:10:02 -> 00:40:12"),
    (4, 1, "bgp_flapping", "warning", "lab-rtr-b BGP peer 198.51.100.7: flapping", "3 changes in 60 min"),
]

# The BGP peer that flaps (device index, peer_ip) and when its changes were seen (minutes ago), see flap.py.
FLAPPING = (1, "198.51.100.7", [(40, "reset"), (22, "state"), (9, "state")])


# minutes ago, router index, severity, mnemonic, message
SYSLOG = [
    (1, 0, 5, "BGP-5-ADJCHANGE", "neighbor 203.0.113.9 Up"),
    (2, 1, 3, "CRYPTO-3-IKMP_NO_SA", "IKE message from 198.51.100.7 has no SA and is not an initialization offer"),
    (4, 0, 6, "SYS-6-LOGOUT", "User admin has exited tty session 2(192.0.2.5)"),
    (7, 2, 4, "BGP-4-NOTIFICATION", "received from neighbor 10.255.0.2 4/0 (hold time expired) 0 bytes"),
    (12, 2, 5, "LINEPROTO-5-UPDOWN", "Line protocol on Interface GigabitEthernet3, changed state to up"),
    (22, 0, 5, "OSPF-5-ADJCHG", "Process 1, Nbr 10.255.0.3 on GigabitEthernet3 from LOADING to FULL, Loading Done"),
    (41, 0, 5, "LINK-5-CHANGED", "Interface GigabitEthernet4, changed state to administratively down"),
]

# The config template every router uses, and what a router's own variables fill in.
TEMPLATE = """hostname {{ device.hostname }}
!
{% for i in interfaces if i.ip %}interface {{ i.name }}
 description {{ i.description }}
 ip address {{ i.ip }}/{{ i.prefix_len }}
 no shutdown
!
{% endfor %}enable secret {{ vars.enable_secret }}
"""


def ts(minutes_ago=0):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def main():
    db.init_db()
    conn = db.connect()
    now = ts()
    ids = []
    for host, ip, serial, ver in ROUTERS:
        fields = {"hostname": host, "mgmt_ip": ip, "serial": serial, "os_version": ver,
                  "model": "CSR1000V", "site": "lab"}
        ids.append(identity.ingest(
            conn, [("serial", serial), ("mgmt_ip", ip), ("hostname", host)], fields, "manual"))

    for idx, dev_id in enumerate(ids):
        failing = idx == 2
        conn.execute(
            "UPDATE devices SET poll_state='idle', next_poll_at='2099-01-01 00:00:00', "
            "fail_count=?, last_poll_ok=?, reachable=? WHERE id=?",
            (3 if failing else 0, ts(180) if failing else now, 0 if failing else 1, dev_id))
        for name, desc, ip, plen in INTERFACES[idx]:
            conn.execute(
                "INSERT INTO interfaces (device_id, name, description, ip, prefix_len, network, "
                "admin_status, oper_status, speed, duplex, input_errors, crc_errors, mtu, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1500,?)",
                (dev_id, name, desc, ip, plen, ios._network(ip, plen),
                 "up", "up", "1000", "full", 0, 0, now))
    # Error history on lab-rtr-b's link to lab-rtr-a: the counters grew over the last hours.
    for ago, in_err, crc in ((300, 10, 2), (120, 31, 7), (30, 42, 9)):
        conn.execute("INSERT INTO interface_stats (device_id, name, at, input_errors, crc_errors) VALUES (?,?,?,?,?)",
                     (ids[1], "GigabitEthernet2", ts(ago), in_err, crc))
    conn.execute("UPDATE interfaces SET input_errors = 42, crc_errors = 9 WHERE device_id = ? AND name = 'GigabitEthernet2'",
                 (ids[1],))

    for idx, next_hop in DEFAULT_ROUTES.items():
        conn.execute("INSERT INTO routes (device_id, vrf, kind, prefix, protocol, next_hop, interface, distance, metric, last_seen) "
                     "VALUES (?, 'default', 'rib', '0.0.0.0/0', 'static', ?, '', 1, 0, ?)", (ids[idx], next_hop, now))

    for idx, local_if, sysname, port, remote_idx, rip in LLDP:
        conn.execute(
            "INSERT INTO lldp_neighbors (device_id, local_if, remote_sysname, remote_port, "
            "remote_mgmt_ip, remote_device_id, last_seen) VALUES (?,?,?,?,?,?,?)",
            (ids[idx], local_if, sysname, port, rip,
             ids[remote_idx] if remote_idx is not None else None, now))
    for idx, proto, peer, asn, area, state, up, pfx, lif in ADJACENCIES:
        conn.execute(
            "INSERT INTO adjacencies (device_id, proto, peer_ip, remote_as, local_as, area, state, uptime, "
            "prefixes, local_if, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ids[idx], proto, peer, asn, LAB_AS if proto == "bgp" else None, area, state, up, pfx, lif, now))

    fidx, fpeer, fchanges = FLAPPING
    conn.execute("UPDATE adjacencies SET flapping = 1 WHERE device_id = ? AND peer_ip = ?", (ids[fidx], fpeer))
    for ago, kind in fchanges:
        conn.execute("INSERT INTO bgp_changes (device_id, peer_ip, at, kind) VALUES (?,?,?,?)",
                     (ids[fidx], fpeer, ts(ago), kind))

    for ago, idx, kind, sev, subject, detail in EVENTS:
        conn.execute(
            "INSERT INTO events (at, device_id, kind, severity, subject, detail) VALUES (?,?,?,?,?,?)",
            (ts(ago), ids[idx], kind, sev, subject, detail))

    for ago, idx, sev, mnem, msg in SYSLOG:
        host, ip = ROUTERS[idx][0], ROUTERS[idx][1]
        conn.execute(
            "INSERT INTO syslog (received_at, source_ip, host, facility, severity, mnemonic, "
            "device_time, message, raw) VALUES (?,?,?,23,?,?,?,?,?)",
            (ts(ago), ip, host, sev, mnem, ts(ago), msg, "%{}: {}".format(mnem, msg)))

    # A template for all three routers, each with the same variable.
    conn.execute("INSERT INTO templates (name, body, note, updated_at, version) VALUES (?,?,?,?,1)",
                 ("base-router", TEMPLATE, "Mock lab template", now))
    conn.execute("INSERT INTO template_versions (name, version, body, saved_at) VALUES (?,1,?,?)",
                 ("base-router", TEMPLATE, now))
    conn.execute("UPDATE devices SET template_name = 'base-router'")
    for dev_id in ids:
        conn.execute("INSERT INTO template_vars (device_id, key, value) VALUES (?,?,?)",
                     (dev_id, "enable_secret", "CHANGE-ME"))
    # Two running-config backups each. The newest one of lab-rtr-c lacks its last interface: that router is drifted.
    for idx, dev_id in enumerate(ids):
        rendered = rendering.render_for_device(conn, dev_id, TEMPLATE).rstrip(chr(10)) + chr(10)
        old = "version 17.3" + chr(10) + rendered.replace("no shutdown", "shutdown") + "end" + chr(10)
        new = "version 17.3" + chr(10) + rendered + "end" + chr(10)
        if idx == 2:
            blocks = new.split("interface ")
            new = "interface ".join(blocks[:-1]) + "!" + chr(10) + "end" + chr(10)
        for ago, body in ((240, old), (20, new)):
            conn.execute("INSERT INTO config_versions (device_id, sha256, body, captured_at) VALUES (?,?,?,?)",
                         (dev_id, "mock{}{}".format(idx, ago), body, ts(ago)))
        drift.refresh(conn, dev_id)
    # An overlapping prefix for IPAM.
    conn.execute(
        "INSERT INTO interfaces (device_id, name, description, ip, prefix_len, network, admin_status, oper_status, "
        "speed, duplex, input_errors, crc_errors, mtu, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1500,?)",
        (ids[2], "GigabitEthernet6", "lab segment", "10.0.13.254", 24, ios._network("10.0.13.254", 24),
         "up", "up", "1000", "full", 0, 0, now))
    ipam.analyze(conn)

    # An identity conflict: lab-rtr-b's IP now reports a different serial.
    try:
        identity.ingest(conn, [("serial", "9SIMLAB0099"), ("mgmt_ip", "192.0.2.171")],
                        {"serial": "9SIMLAB0099", "mgmt_ip": "192.0.2.171"}, "sweep", "192.0.2.0/24")
    except identity.Conflict:
        pass

    # One topology snapshot, so the dashboard's Map changes widget has a row.
    history.maybe_snapshot(conn, force=True)

    conn.commit()
    conn.close()
    print("seeded {} with {} devices".format(sys.argv[1], len(ids)))


main()
