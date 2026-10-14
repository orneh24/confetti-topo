"""Prometheus text exposition for GET /metrics.

Hand-written on purpose: the format is a few lines of text, and a client
library would be one more package to verify on Alpine. State is read from the
database at scrape time, so nothing here keeps counters of its own.
"""

import os

from . import config
from . import poller
from . import reach

BGP_UP = "Established"
OSPF_UP = "FULL"


def _esc(value):
    """Label values: backslash, double quote and newline must be escaped."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Out:
    def __init__(self):
        self.lines = []
        self._declared = set()

    def sample(self, name, help_text, kind, value, **labels):
        if name not in self._declared:
            self._declared.add(name)
            self.lines.append("# HELP {} {}".format(name, help_text))
            self.lines.append("# TYPE {} {}".format(name, kind))
        if labels:
            lab = ",".join('{}="{}"'.format(k, _esc(v)) for k, v in sorted(labels.items()))
            self.lines.append("{}{{{}}} {}".format(name, lab, value))
        else:
            self.lines.append("{} {}".format(name, value))


def render(conn):
    o = _Out()
    o.sample("topo_up", "confetti-topo is serving requests", "gauge", 1)
    o.sample("topo_poller_running", "the SSH poller thread is alive", "gauge", int(poller.is_running()))
    o.sample("topo_reach_running", "the ICMP checker thread is alive", "gauge", int(reach.is_running()))
    try:
        o.sample("topo_db_size_bytes", "size of the SQLite file", "gauge", os.path.getsize(config.DB_PATH))
    except OSError:
        pass

    for r in conn.execute("SELECT role, COUNT(*) AS n FROM devices GROUP BY role"):
        o.sample("topo_devices", "devices in the inventory", "gauge", r["n"], role=r["role"])

    for d in conn.execute(
            "SELECT hostname, mgmt_ip, role, reachable, fail_count, enabled FROM devices ORDER BY hostname"):
        lab = dict(device=d["hostname"], role=d["role"])
        if d["reachable"] is not None:
            o.sample("topo_device_reachable", "1 if the device answers ping, 0 if not", "gauge",
                     d["reachable"], **lab)
        o.sample("topo_device_poll_failures", "consecutive failed SSH polls", "gauge",
                 d["fail_count"], **lab)

    for a in conn.execute(
            """SELECT d.hostname, a.proto, a.peer_ip, a.state FROM adjacencies a
               JOIN devices d ON d.id = a.device_id ORDER BY d.hostname, a.peer_ip"""):
        up = BGP_UP if a["proto"] == "bgp" else OSPF_UP
        o.sample("topo_adjacency_up", "1 if the BGP session is Established / OSPF neighbor is FULL",
                 "gauge", int(a["state"] == up), device=a["hostname"], proto=a["proto"], peer=a["peer_ip"])

    for i in conn.execute(
            """SELECT d.hostname, i.name, i.oper_status FROM interfaces i
               JOIN devices d ON d.id = i.device_id ORDER BY d.hostname, i.name"""):
        o.sample("topo_interface_up", "1 if the interface is operationally up", "gauge",
                 int(i["oper_status"] == "up"), device=i["hostname"], interface=i["name"])

    for r in conn.execute(
            """SELECT d.hostname, COUNT(*) AS n FROM lldp_neighbors l
               JOIN devices d ON d.id = l.device_id GROUP BY d.hostname ORDER BY d.hostname"""):
        o.sample("topo_lldp_neighbors", "LLDP neighbors a device currently reports", "gauge",
                 r["n"], device=r["hostname"])

    o.sample("topo_merge_conflicts_open", "unresolved device merge conflicts", "gauge",
             conn.execute("SELECT COUNT(*) FROM merge_conflicts WHERE resolved = 0").fetchone()[0])
    for r in conn.execute(
            "SELECT severity, COUNT(*) AS n FROM events WHERE at >= datetime('now', '-1 day') GROUP BY severity"):
        o.sample("topo_events_24h", "change events recorded in the last 24h", "gauge",
                 r["n"], severity=r["severity"])
    return "\n".join(o.lines) + "\n"
