#!/usr/bin/env python3
"""dev/regress.py - the regression suite, as one script.

One check per numbered constraint in CLAUDE.md (R1..R14 are constraints 1..14),
plus checks for what was added later and cross-file consistency (R15 and up).
Prints one line per check; details only on failure.

    python dev/regress.py            # everything
    python dev/regress.py --static   # skip the live tier (no server started)
    python dev/regress.py -v         # details for passes too

Exit 0 when nothing failed (some checks may be NOT RUN), 1 on any failure.
The reasoning behind each check is the matching constraint in CLAUDE.md.

The script itself is stdlib only. The unit tier imports the app, so it needs
the app's own packages (flask, jinja2, netmiko ...); without them those checks
are NOT RUN, never PASS. R39 also needs ruff (pip install -r
dev/requirements-dev.txt), else it is NOT RUN.
"""
import glob
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

VERBOSE = "-v" in sys.argv
STATIC_ONLY = "--static" in sys.argv

results = []   # (status, id, desc, [detail lines])
LIVE = [("R20", "live: real server answers, pages render, /metrics, topology API, saved node positions, UDP syslog round trip")]
TMP = tempfile.mkdtemp(prefix="topo-regress-")


class Skip(Exception):
    pass


def check(cid, desc):
    """Register and run a check. It returns a list of problems (empty =
    pass) or raises Skip(reason). A crash is a FAIL, never a pass."""
    def wrap(fn):
        try:
            problems = fn() or []
            results.append(("FAIL" if problems else "PASS", cid, desc, problems))
        except Skip as e:
            results.append(("NOT RUN", cid, desc, [str(e)]))
        except Exception as e:
            results.append(("FAIL", cid, desc, ["check crashed: %r" % e]))
        return fn
    return wrap


# ---------------------------------------------------------------- helpers

_cache = {}


def read(path):
    if path not in _cache:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            _cache[path] = f.read()
    return _cache[path]


def lines(path):
    return read(path).splitlines()


def is_comment(line):
    s = line.strip()
    return s.startswith(("#", "//", "/*", "*", "<!--"))


def grep(pattern, path, flags=0, comments=False):
    """[(lineno, line)] matching pattern, skipping comment lines by default."""
    rx = re.compile(pattern, flags)
    return [(n, l) for n, l in enumerate(lines(path), 1)
            if rx.search(l) and (comments or not is_comment(l))]


def at(path, hits):
    return ["%s:%d %s" % (path, n, l.strip()) for n, l in hits]


def need(cond, msg, problems):
    if not cond:
        problems.append(msg)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def py_files(sub="topo/app"):
    return sorted(glob.glob(sub + "/**/*.py", recursive=True))


# The unit tier imports the real app against scratch databases. The env var
# must be set before app.config is first imported.
_mods = {}


def app_modules():
    if _mods:
        return _mods
    os.environ["TOPO_DB_PATH"] = os.path.join(TMP, "import.db")
    sys.path.insert(0, os.path.join(ROOT, "topo"))
    try:
        from app import config, db, identity, poller, events
        from app import app as app_pkg_app            # the Flask module
        from app.collectors import ssh, sweep, confetti, butler, panos
        from app.parsers import ios
        from app.parsers import panos as panos_parser
    except ImportError as e:
        raise Skip("app not importable here (%s)" % e)
    _mods.update(config=config, db=db, identity=identity,
                 poller=poller, events=events,
                 appmod=app_pkg_app, ssh=ssh, sweep=sweep, confetti=confetti, butler=butler, ios=ios,
                 panos=panos, panos_parser=panos_parser)
    return _mods


_n = [0]


def fresh_db():
    """A new empty scratch database; returns (modules, open connection)."""
    m = app_modules()
    _n[0] += 1
    m["config"].DB_PATH = os.path.join(TMP, "t%d.db" % _n[0])
    m["db"].init_db()
    return m, m["db"].connect()


def add_device(conn, ip, hostname="rtr", role="router"):
    m = app_modules()
    did = m["identity"].ingest(conn, [("mgmt_ip", ip)], {"hostname": hostname, "role": role},
                               source="manual")
    conn.commit()
    return did


# ============================================================ static tier

@check("R1", "aliases are never deleted (static + a re-IP keeps the old IP)")
def _():
    p = []
    for path in py_files():
        p += at(path, grep(r"DELETE\s+FROM\s+device_aliases", path, re.I))
    m, conn = fresh_db()
    ident = m["identity"]
    did = ident.ingest(conn, [("serial", "S1"), ("mgmt_ip", "192.0.2.1")], {}, source="manual")
    ident.ingest(conn, [("serial", "S1"), ("mgmt_ip", "192.0.2.2")], {}, source="manual")
    conn.commit()
    row = conn.execute("SELECT device_id FROM device_aliases WHERE kind='mgmt_ip' AND value='192.0.2.1'").fetchone()
    need(row and row["device_id"] == did, "old mgmt_ip alias 192.0.2.1 is gone after a re-IP", p)
    conn.close()
    return p


@check("R2", "stronger identifier disagreeing = conflict; weaker drifting = update")
def _():
    p = []
    m, conn = fresh_db()
    ident = m["identity"]
    ident.ingest(conn, [("serial", "S1"), ("mgmt_ip", "192.0.2.1")], {}, source="manual")
    conn.commit()
    try:   # match via the weak IP while a DIFFERENT serial arrives
        ident.ingest(conn, [("mgmt_ip", "192.0.2.1"), ("serial", "S2")], {}, source="manual")
        p.append("different serial behind a known IP was merged silently (no Conflict)")
    except ident.Conflict:
        conn.rollback()
    serial = conn.execute("SELECT serial FROM devices").fetchone()["serial"]
    need(serial in (None, "S1"), "stored serial was overwritten: %r" % serial, p)
    try:   # match via the strong serial while the IP drifted
        ident.ingest(conn, [("serial", "S1"), ("mgmt_ip", "192.0.2.9")], {}, source="manual")
    except ident.Conflict:
        p.append("a legitimate re-IP (same serial) was flagged as a conflict")
        conn.rollback()
    conn.close()
    return p


@check("R3", "IOS-XE commands: no invalid LLDP self-query, no textfsm, brief-table fallback present")
def _():
    p = []
    ssh_src = read("topo/app/collectors/ssh.py")
    cmds = " | ".join(app_modules()["ssh"].COMMANDS.values())
    for bad in ("lldp local-info", "lldp entry local"):
        need(bad not in cmds, "COMMANDS uses '%s' (invalid / always empty on IOS-XE 17.3)" % bad, p)
    import ast
    for path in py_files():
        for node in ast.walk(ast.parse(read(path))):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "use_textfsm" and getattr(kw.value, "value", None) is True:
                        p.append("%s:%d use_textfsm=True (needs ntc-templates on Alpine)" % (path, node.lineno))
    need("fill_lldp_local_if" in ssh_src, "ssh.py no longer fills Local Intf from the brief table", p)
    need("fill_lldp_local_if" in read("topo/app/parsers/ios.py"), "ios.fill_lldp_local_if is missing", p)
    return p


@check("R4", "timestamps: sqlite_now format, iso() conversion, no isoformat() stored")
def _():
    p = []
    m = app_modules()
    db = m["db"]
    now = db.sqlite_now()
    need(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", now) is not None, "sqlite_now() = %r" % now, p)
    need(db.iso(now) == now.replace(" ", "T") + "Z", "iso() does not produce ...T...Z", p)
    need(db.sqlite_ts_arg(db.iso(now)) == now, "sqlite_ts_arg(iso(x)) != x", p)
    for path in py_files():
        if not path.endswith("db.py"):
            p += at(path, grep(r"\.isoformat\(", path))
    return p


@check("R5", "busy_timeout on every connection, set before journal_mode")
def _():
    p = []
    m = app_modules()
    import inspect
    src = inspect.getsource(m["db"].connect)
    # The PRAGMA statements, not the docstring that also mentions both words.
    need("PRAGMA busy_timeout" in src and "PRAGMA journal_mode" in src
         and src.index("PRAGMA busy_timeout") < src.index("PRAGMA journal_mode"),
         "db.connect(): busy_timeout must be set before journal_mode", p)
    for path in py_files():
        text = read(path)
        if "sqlite3.connect(" in text and "busy_timeout" not in text:
            p.append("%s opens a raw sqlite3 connection without busy_timeout" % path)
    m["config"].DB_PATH = os.path.join(TMP, "r5.db")
    conn = m["db"].connect()
    got = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    need(got == m["config"].BUSY_TIMEOUT_MS, "busy_timeout is %r, expected %r" % (got, m["config"].BUSY_TIMEOUT_MS), p)
    conn.close()
    return p


@check("R6", "every page renders (a stray Jinja2 {{ in inline JS makes Flask 500)")
def _():
    p = []
    m, conn = fresh_db()
    did = add_device(conn, "192.0.2.1")
    conn.close()
    client = m["appmod"].app.test_client()
    for url in ("/", "/devices", "/devices/%d" % did, "/conflicts", "/topology", "/settings", "/ipam", "/templates", "/alerts"):
        code = client.get(url).status_code
        need(code == 200, "GET %s -> %s" % (url, code), p)
    return p


@check("R7", "poller path uses identity.observe, never identity.ingest")
def _():
    p = []
    for path in ("topo/app/collectors/ssh.py", "topo/app/poller.py"):
        p += at(path, grep(r"identity\.ingest\(", path))
    need(grep(r"identity\.observe\(", "topo/app/collectors/ssh.py"), "ssh.py no longer calls identity.observe", p)
    return p


@check("R8", "role='node' devices are never claimed by the poller")
def _():
    p = []
    m, conn = fresh_db()
    router = add_device(conn, "192.0.2.1", "r1", "router")
    add_device(conn, "192.0.2.2", "n1", "node")
    conn.close()

    class Rec:
        def __init__(self):
            self.ids = []

        def submit(self, fn, device_id):
            self.ids.append(device_id)
    rec = Rec()
    m["poller"]._tick(rec)
    need(rec.ids == [router], "poller claimed %r, expected only the router [%d]" % (rec.ids, router), p)
    return p


@check("R9", "a sweep never overwrites a known hostname with an IP")
def _():
    p = []
    m, conn = fresh_db()
    did = add_device(conn, "192.0.2.5", hostname="real-rtr")
    sweep = m["sweep"]
    real_probe = sweep._probe
    sweep._probe = lambda ip: str(ip)
    try:
        sweep.sweep(conn, "192.0.2.5/32")
        sweep.sweep(conn, "192.0.2.6/32")
    finally:
        sweep._probe = real_probe
    conn.commit()
    hn = conn.execute("SELECT hostname FROM devices WHERE id = ?", (did,)).fetchone()["hostname"]
    need(hn == "real-rtr", "re-sweep changed the hostname to %r" % hn, p)
    new = conn.execute("SELECT key, hostname FROM devices WHERE mgmt_ip = '192.0.2.6'").fetchone()
    need(new is not None and new["hostname"] == new["key"], "new swept device should fall back to its key as name", p)
    conn.close()
    return p


@check("R10", "confetti-traffic imports are vendor=alpine, platform=linux, role=node")
def _():
    p = []
    m, conn = fresh_db()
    conf = m["confetti"]

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"hostname": "alp1", "ip": "192.0.2.50", "group_name": "labgroup"}]
    real_get = conf.requests.get
    conf.requests.get = lambda *a, **k: Resp()
    try:
        conf.discover(conn, "http://hub.invalid")
    finally:
        conf.requests.get = real_get
    conn.commit()
    row = conn.execute("SELECT vendor, platform, role, site FROM devices").fetchone()
    need(row is not None and (row["vendor"], row["platform"], row["role"], row["site"])
         == ("alpine", "linux", "node", "labgroup"), "imported node row is %r" % (dict(row) if row else None), p)
    conn.close()
    return p


@check("R11", "events: no events on a task's first poll, none on repeat, emit() never commits")
def _():
    p = []
    m, conn = fresh_db()
    ssh, poller = m["ssh"], m["poller"]
    did = add_device(conn, "192.0.2.1")

    def intf(oper):
        return [dict(name="Gi1", description="", ip=None, prefix_len=None, network=None,
                     admin_status="up", oper_status=oper, speed=None, duplex=None, mtu=1500)]

    def count():
        return conn.execute("SELECT count(*) FROM events").fetchone()[0]
    ssh.apply_result(conn, did, "interfaces", intf("up"))
    ssh.apply_result(conn, did, "interfaces", intf("down"))
    need(count() == 0, "events were emitted during the baseline poll (%d)" % count(), p)
    conn.execute("INSERT INTO poll_history (device_id, transport, task, ok, started_at, received_at) "
                 "VALUES (?, 'ssh', 'interfaces', 1, ?, ?)", (did, m["db"].sqlite_now(), m["db"].sqlite_now()))
    ssh.apply_result(conn, did, "interfaces", intf("down"))
    need(count() == 0, "an unchanged interface emitted an event", p)
    ssh.apply_result(conn, did, "interfaces", intf("up"))
    need(count() == 1, "an interface coming up emitted %d events, expected 1" % count(), p)
    conn.commit()
    poller._finish(conn, did, False)
    poller._finish(conn, did, False)
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events ORDER BY id")]
    need(kinds.count("device_unreachable") == 1, "unreachable fired %d times, expected once: %r"
         % (kinds.count("device_unreachable"), kinds), p)
    conn.close()
    p += at("topo/app/events.py", grep(r"\.commit\(", "topo/app/events.py"))
    return p


def _put_iface(conn, dev, name, ip):
    conn.execute("INSERT INTO interfaces (device_id, name, ip, last_seen) VALUES (?, ?, ?, ?)",
                 (dev, name, ip, "2026-01-01 00:00:00"))


def _put_adj(conn, dev, proto, peer_ip, state="Established", peer_device_id=None):
    conn.execute("INSERT INTO adjacencies (device_id, proto, peer_ip, peer_device_id, state, last_seen) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (dev, proto, peer_ip, peer_device_id, state, "2026-01-01 00:00:00"))


def _put_lldp(conn, dev, local_if, sysname, port, remote_id=None, chassis=None, mgmt_ip=None):
    conn.execute("INSERT INTO lldp_neighbors (device_id, local_if, remote_chassis, remote_sysname, remote_port, "
                 "remote_mgmt_ip, remote_device_id, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (dev, local_if, chassis, sysname, port, mgmt_ip, remote_id, "2026-01-01 00:00:00"))


@check("R12", "peers resolve by a unique interface IP: not an ambiguous one, not to themselves, never stored as an alias")
def _():
    p = []
    m, conn = fresh_db()
    from app import topology
    a = add_device(conn, "192.0.2.1", "a")
    b = add_device(conn, "192.0.2.2", "b")
    c = add_device(conn, "192.0.2.3", "c")
    _put_iface(conn, a, "Lo0", "10.255.0.1")
    _put_iface(conn, b, "Lo0", "10.255.0.2")
    _put_iface(conn, b, "Gi2", "10.9.9.9")      # the same address on two devices (HSRP, a reused lab IP)
    _put_iface(conn, c, "Gi2", "10.9.9.9")
    _put_adj(conn, a, "bgp", "10.255.0.2")                           # unique -> b
    _put_adj(conn, a, "bgp", "10.9.9.9")                             # ambiguous -> ghost
    _put_adj(conn, a, "ospf", "10.255.0.1")                          # its own address -> ghost, not a self-loop
    _put_adj(conn, a, "bgp", "10.77.0.1")                            # nobody's -> ghost
    _put_adj(conn, a, "ospf", "10.255.0.2", "FULL", peer_device_id=c)   # the poller's match wins
    _put_lldp(conn, a, "Gi1", "r-b", "Gi1", mgmt_ip="10.255.0.2")    # LLDP falls back to the same lookup
    conn.commit()
    g = topology.build(conn)
    need(any(e["layer"] == "bgp" and e["to"] == "dev:%d" % b for e in g["edges"]), "bgp peer on b's loopback did not resolve to b", p)
    need(any(e["layer"] == "bgp" and e["to"] == "ghost:peer:10.9.9.9" for e in g["edges"]), "an address held by two devices was resolved instead of staying a ghost", p)
    need(any(e["layer"] == "ospf" and e["to"] == "ghost:peer:10.255.0.1" for e in g["edges"]), "a device's own address became a self-loop or vanished", p)
    need(any(e["layer"] == "bgp" and e["to"] == "ghost:peer:10.77.0.1" for e in g["edges"]), "an unknown peer is not a ghost", p)
    need(any(e["layer"] == "ospf" and e["to"] == "dev:%d" % c for e in g["edges"]), "the poller's peer_device_id was overridden", p)
    need(any(e["layer"] == "lldp" and e["to"] == "dev:%d" % b for e in g["edges"]), "an LLDP neighbor's advertised IP did not fall back to the interface lookup", p)
    need(all(e["from"] != e["to"] for e in g["edges"]), "an edge loops back to its own device", p)
    adj = {r["peer_ip"]: r["peer_device_id"] for r in topology.adjacencies(conn, a) if r["proto"] == "bgp"}
    need(adj.get("10.255.0.2") == b and adj.get("10.9.9.9") is None, "/api/adjacencies peer_device_id disagrees with the graph: %r" % adj, p)
    stored = conn.execute("SELECT COUNT(*) FROM device_aliases WHERE value LIKE '10.%'").fetchone()[0]
    need(stored == 0, "interface addresses were stored as identity aliases (%d)" % stored, p)
    p += at("topo/app/topology.py", grep(r"device_aliases|INSERT|UPDATE|DELETE", "topo/app/topology.py"))
    conn.close()
    return p


@check("R13", "topology: an unresolved neighbor is a ghost node; a cable or session both ends report is drawn once")
def _():
    p = []
    m, conn = fresh_db()
    from app import topology
    a = add_device(conn, "192.0.2.1", "a")
    b = add_device(conn, "192.0.2.2", "b")
    _put_lldp(conn, a, "Gi1", "b", "Gi2", remote_id=b)
    _put_lldp(conn, b, "Gi2", "a", "Gi1", remote_id=a)                     # the same cable from the other end
    _put_lldp(conn, a, "Gi4", "b", "GigabitEthernet4", remote_id=b)
    _put_lldp(conn, b, "Gi4", "a", "GigabitEthernet4", remote_id=a)        # spelled differently: both stay
    _put_lldp(conn, a, "Gi5", "sw9", "Gi0/1")                              # unknown, has a name
    _put_lldp(conn, a, "Gi6", None, "Gi0/2", chassis="aabb.ccdd.eeff")     # unknown, only a chassis id
    _put_lldp(conn, a, "Gi7", "sw9", "Gi0/3")                              # a second cable to the same ghost
    conn.commit()
    g = topology.build(conn)
    lldp = [e for e in g["edges"] if e["layer"] == "lldp"]
    need(len([e for e in lldp if e["label"] == "Gi1 - Gi2" or e["label"] == "Gi2 - Gi1"]) == 1, "the Gi1/Gi2 cable is not drawn exactly once", p)
    need(len([e for e in lldp if "4" in e["label"] and "Gi5" not in e["label"]]) == 2, "differently spelled ports must keep both edges (a duplicate line beats a hidden link)", p)
    # A port id that is only a MAC (Linux lldpd) is labelled with the port description; the cable key keeps the id.
    _put_lldp(conn, a, "Gi8", "host9", "0200.0000.0008")
    conn.execute("UPDATE lldp_neighbors SET remote_port_descr = 'eth0' WHERE remote_port = '0200.0000.0008'")
    conn.commit()
    need("Gi8 - eth0" in [e["label"] for e in topology.build(conn)["edges"]], "a MAC port id must be shown as the port description", p)
    need(topology.port_text("Gi1", "Management") == "Gi1" and topology.port_text("0200.0000.0008", None) == "0200.0000.0008",
         "a real port name stays, and a MAC with no description stays", p)
    conn.execute("DELETE FROM lldp_neighbors WHERE local_if = 'Gi8'")
    conn.commit()
    g = topology.build(conn)
    ghosts = {n["id"]: n for n in g["nodes"] if n["ghost"]}
    need("ghost:lldp:sw9" in ghosts and "ghost:lldp:aabb.ccdd.eeff" in ghosts, "unresolved neighbors are not ghost nodes: %r" % sorted(ghosts), p)
    need(len([e for e in lldp if e["to"] == "ghost:lldp:sw9"]) == 2, "links to a ghost must not be de-duplicated", p)
    need(not [e for e in topology.build(conn, "bgp")["edges"] if e["layer"] == "lldp"], "?layer=bgp returned LLDP edges", p)
    # BGP/OSPF: a session both routers report is one edge, down if either end says so.
    _put_iface(conn, a, "Lo0", "10.255.0.1")
    _put_iface(conn, b, "Lo0", "10.255.0.2")
    _put_adj(conn, a, "bgp", "10.255.0.2")
    _put_adj(conn, b, "bgp", "10.255.0.1")
    _put_adj(conn, a, "ospf", "10.255.0.2", "FULL")
    _put_adj(conn, b, "ospf", "10.255.0.1", "EXSTART")
    conn.commit()
    sess = [e for e in topology.build(conn)["edges"] if e["layer"] in ("bgp", "ospf")]
    bgp = [e for e in sess if e["layer"] == "bgp"]
    ospf = [e for e in sess if e["layer"] == "ospf"]
    need(len(bgp) == 1 and bgp[0]["up"] is True, "a BGP session both ends report must be one up edge: %r" % bgp, p)
    need(len(ospf) == 1 and ospf[0]["up"] is False and "EXSTART" in ospf[0]["label"],
         "an OSPF session with one end not FULL must be one down edge showing that state: %r" % ospf, p)
    need(all(e["from"] == "dev:%d" % a and e["to"] == "dev:%d" % b for e in sess), "session edges between a pair must all point the same way", p)
    need(len(bgp[0]["details"]) == 2 and {d["reported_by"] for d in bgp[0]["details"]} == {"a", "b"},
         "a session both ends report must carry both reports in details: %r" % bgp[0].get("details"), p)
    need(all(len(e["details"]) == 1 for e in topology.build(conn, "lldp")["edges"] if e["to"].startswith("ghost:")),
         "a ghost link carries exactly the one report that saw it", p)
    mixed = {e["layer"] for e in topology.build(conn, "lldp,bgp")["edges"]}
    need("lldp" in mixed and "bgp" in mixed and "ospf" not in mixed, "?layer=lldp,bgp must show exactly those two layers: %r" % sorted(mixed), p)
    need(not topology.build(conn, "none")["edges"], "an unknown layer name must draw no edges", p)
    # The BGP label carries the AS numbers: a row stores its peer's AS, so a's own AS is b's row.
    conn.execute("UPDATE adjacencies SET remote_as = 65002 WHERE device_id = ? AND proto = 'bgp'", (a,))
    conn.execute("UPDATE adjacencies SET remote_as = 65001 WHERE device_id = ? AND proto = 'bgp'", (b,))
    conn.commit()
    built = topology.build(conn, "bgp")
    labels = [e["label"] for e in built["edges"]]
    need(labels == ["eBGP AS65001 - AS65002 Established"], "BGP label must name both AS numbers: %r" % labels, p)
    need([e["bgp_type"] for e in built["edges"]] == ["ebgp"], "two different ASes must be an eBGP edge: %r" % built["edges"], p)
    need({n["device_id"]: n["asn"] for n in built["nodes"]} == {a: 65001, b: 65002}, "nodes must carry their AS: %r" % built["nodes"], p)
    # Same AS at both ends is iBGP, and a router's own `local AS` wins over what the other row says about it.
    conn.execute("UPDATE adjacencies SET remote_as = 65001, local_as = 65001 WHERE proto = 'bgp'")
    conn.commit()
    same = topology.build(conn, "bgp")["edges"]
    need([(e["bgp_type"], e["label"]) for e in same] == [("ibgp", "iBGP AS65001 Established")], "same AS at both ends must be iBGP: %r" % same, p)
    conn.execute("UPDATE adjacencies SET remote_as = 65002, local_as = NULL WHERE device_id = ? AND proto = 'bgp'", (a,))
    conn.execute("UPDATE adjacencies SET remote_as = 65001, local_as = NULL WHERE device_id = ? AND proto = 'bgp'", (b,))
    conn.commit()
    need([e["label"] for e in topology.build(conn, "ospf")["edges"]] == ["ospf EXSTART"], "an OSPF label must not get an AS", p)
    # LLDP says "Linux" and names no network vendor: a Linux server (ghost or known device); a Cisco description is not
    did = conn.execute("SELECT id FROM devices ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO lldp_neighbors (device_id, local_if, remote_chassis, remote_sysname, remote_port, remote_descr, last_seen)"
                 " VALUES (?, 'Gi9', 'c1', 'web1', 'eth0', 'Ubuntu 22.04 Linux 5.15', '2000-01-01 00:00:00')", (did,))
    conn.execute("INSERT INTO lldp_neighbors (device_id, local_if, remote_chassis, remote_sysname, remote_port, remote_descr, last_seen)"
                 " VALUES (?, 'Gi8', 'c2', 'rtr9', 'Gi1', 'Cisco IOS Software, Linux-based', '2000-01-01 00:00:00')", (did,))
    conn.commit()
    ghosts = {n["label"]: n for n in topology.build(conn, "lldp")["nodes"] if n["ghost"]}
    need(ghosts.get("web1", {}).get("device_type") == "linux", "an LLDP neighbor described as Linux must be a linux ghost", p)
    need(ghosts.get("rtr9", {}).get("device_type") == "unknown", "a Cisco description that mentions Linux must not become a Linux server", p)
    # node icons: the type and vendor the page draws come from here
    dt = topology.device_type
    need(dt("router", "", "core-sw-01") == "switch" and dt("router", "ASA5506", "e1") == "firewall"
         and dt("router", "CSR1000V", "r1") == "router" and dt("node", "", "a", "linux") == "linux"
         and dt("router", "", "answer") == "router", "device_type words/platform rules are off", p)
    conn.execute("UPDATE devices SET vendor = 'Juniper'")
    conn.commit()
    nodes = [n for n in topology.build(conn)["nodes"] if not n["ghost"]]
    need(nodes and all(n["vendor"] == "juniper" and n["device_type"] for n in nodes), "topology nodes must carry vendor (lower case) and device_type", p)
    conn.close()
    return p


@check("R14", "a failed poll deletes nothing; a successful empty answer does")
def _():
    p = []
    m, conn = fresh_db()
    ssh, poller = m["ssh"], m["poller"]
    did = add_device(conn, "192.0.2.1", "a")
    _put_lldp(conn, did, "Gi1", "sw1", "Gi0/1")
    _put_adj(conn, did, "bgp", "192.0.2.9")
    _put_iface(conn, did, "Lo0", "10.0.0.1")
    conn.commit()
    conn.close()
    real = ssh.run_tasks

    def counts():
        c = m["db"].connect()
        try:
            return tuple(c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                         for t in ("lldp_neighbors", "adjacencies", "interfaces"))
        finally:
            c.close()
    try:
        ssh.run_tasks = lambda device, creds, tasks=None: iter(
            [(t, False, None, "connection timed out", None) for t in ("version", "interfaces", "lldp", "bgp", "ospf")])
        poller._poll_device(did)
        need(counts() == (1, 1, 1), "a timed-out poll deleted rows: %r" % (counts(),), p)
        ssh.run_tasks = lambda device, creds, tasks=None: iter([("lldp", True, "", None, [])])
        poller._poll_device(did)
        need(counts() == (0, 1, 1), "LLDP answering 'not enabled' should clear only the LLDP rows: %r" % (counts(),), p)
    finally:
        ssh.run_tasks = real
    c = m["db"].connect()
    row = c.execute("SELECT poll_state, fail_count FROM devices WHERE id = ?", (did,)).fetchone()
    need(row["poll_state"] == "idle", "the per-device lock was not released: %r" % row["poll_state"], p)
    c.close()
    return p


@check("R15", "CLAUDE.md names the right number of poll tasks and lists them all")
def _():
    p = []
    tasks = app_modules()["ssh"].TASKS
    words = {"four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
    doc = read("CLAUDE.md")
    for pat in (r"Per device, (\w+) tasks", r"All (\w+) tasks share"):
        m = re.search(pat, doc)
        need(m is not None, "CLAUDE.md lost the sentence matching %r" % pat, p)
        if m:
            need(words.get(m.group(1)) == len(tasks), "CLAUDE.md says %r tasks, code has %d" % (m.group(1), len(tasks)), p)
    m = re.search(r"Per device, \w+ tasks run independently.*?(?=\n\n)", doc, re.S)
    for t in tasks:
        need(m is not None and "`%s`" % t in m.group(0), "task %r is not listed in CLAUDE.md's per-device sentence" % t, p)
    return p


@check("R16", "shell/Alpine hygiene: LF, shebang, no bashisms, sh -n, exact pins")
def _():
    p = []
    sh = shutil.which("sh")
    files = sorted(glob.glob("topo/**/*.sh", recursive=True) + glob.glob("topo/services/*.initd"))
    need(len(files) >= 5, "expected the shell scripts under topo/, found %d" % len(files), p)
    for f in files:
        text = read(f)
        need("\r" not in text, "%s has CR characters (CRLF breaks the shebang on Alpine)" % f, p)
        # login-setup.sh is sourced from /etc/profile.d, not executed.
        need(text.startswith("#!") or f.endswith("login-setup.sh"), "%s has no shebang" % f, p)
        for n, l in grep(r"\[\[|<\(|\$RANDOM|\$\{[A-Za-z_]+,,|function\s+\w+\s*\{", f):
            p.append("%s:%d bashism: %s" % (f, n, l.strip()))
        if sh:
            r = subprocess.run([sh, "-n", f], capture_output=True, text=True)
            need(r.returncode == 0, "%s fails sh -n: %s" % (f, r.stderr.strip()[:120]), p)
    for n, l in enumerate(lines("topo/requirements.txt"), 1):
        if l.strip() and not l.startswith("#"):
            need(re.fullmatch(r"[A-Za-z0-9_.-]+==\d[\w.]*", l.strip()) is not None,
                 "topo/requirements.txt:%d is not an exact pin: %r" % (n, l), p)
    ga = read(".gitattributes")
    need("*.sh" in ga and "eol=lf" in ga, ".gitattributes no longer pins *.sh to LF", p)
    if not sh and not p:
        raise Skip("no sh for the syntax check (the other parts passed)")
    return p


@check("R17", "poller always releases its per-device lock and resets stuck rows at start")
def _():
    p = []
    import inspect
    poller = app_modules()["poller"]
    src = inspect.getsource(poller._poll_device)
    fin = src.find("finally:")
    need(fin != -1 and "_finish(" in src[fin:], "_poll_device no longer calls _finish() in a finally block", p)
    need("_reset_stuck()" in inspect.getsource(poller._loop), "poller._loop no longer resets stuck 'running' rows", p)
    return p


@check("R18", "every repo file the build/update/migrate scripts copy exists")
def _():
    p = []
    for script in ("topo/build-topo.sh", "topo/scripts/topo-update.sh"):
        for ref in sorted(set(re.findall(r'\$\{(?:SCRIPT_DIR|SRC_DIR|SRC)\}/([\w./-]+?)["\s]', read(script)))):
            if ref.endswith("/") or "*" in ref:
                continue
            need(os.path.exists(os.path.join("topo", ref)) or os.path.exists(ref),
                 "%s copies '%s' which does not exist" % (script, ref), p)
    bt = read("topo/build-topo.sh")
    need("/etc/periodic/daily/topo-backup" in bt, "nightly backup must be installed without a dot in its name", p)
    return p


# ---- features added after R14 -----------------------------------------

SAMPLES = "dev/samples/"


def sample(name):
    return read(SAMPLES + name)


@check("R19", "platform registry: same core tasks everywhere (vlans, trunks, stp and mac are IOS-only), unknown platform = IOS, every command read-only")
def _():
    p = []
    m = app_modules()
    from app import platforms
    ios_tasks = tuple(platforms.IOS["commands"])
    core = ("version", "interfaces", "lldp", "bgp", "ospf", "routes")
    need(ios_tasks == core + ("vlans", "trunks", "stp", "mac", "config"), "IOS tasks are %r" % (ios_tasks,), p)
    for name, plat in platforms.PLATFORMS.items():
        if plat["transport"] == "snmp":
            # SNMP (from confetti-butler) asks version and interfaces only, through net-snmp's get and walk: no CLI.
            need(tuple(plat["tasks"]) == ("version", "interfaces"), "%s tasks are %r" % (name, plat.get("tasks")), p)
            need("commands" not in plat, "%s: an SNMP platform has no CLI commands" % name, p)
            continue
        # "vlans" is a switch task of the IOS entry only, and "config" (the running-config backup) too;
        # every platform has the six core tasks.
        have = tuple(t for t in plat["commands"] if t not in platforms.SWITCH_TASKS and t != "config")
        need(have == core, "%s tasks are %r, expected the core %r" % (name, tuple(plat["commands"]), core), p)
        need(not (set(plat["commands"]) & (set(platforms.SWITCH_TASKS) | {"config"})) or plat is platforms.IOS, "%s: vlans, trunks, stp, mac and config are only for the IOS entry" % name, p)
        need(set(plat["parsers"]) == set(plat["commands"]), "%s: commands and parsers differ" % name, p)
        # SSH platforms run `show ...`; the XML API platform sends `<show>...</show>` operations. Nothing else.
        prefix = "<show>" if plat.get("transport") == "xml_api" else "show "
        extra = [("lldp_brief", plat.get("lldp_brief_command", prefix + "x")),
                 ("bgp_summary", plat.get("bgp_summary_command", prefix + "x")),
                 ("routes_fallback", plat.get("routes_fallback_command", prefix + "x")),
                 ("fib", plat.get("fib_command", prefix + "x"))]
        for task, cmd in list(plat["commands"].items()) + extra:
            need(cmd.startswith(prefix), "%s %s runs %r: only read-only 'show' commands are allowed" % (name, task, cmd), p)
    need(platforms.get(None) is platforms.IOS and platforms.get("no-such-os") is platforms.IOS, "unknown platform must fall back to IOS", p)
    need(m["ssh"].TASKS == ios_tasks, "ssh.TASKS drifted from platforms.IOS", p)
    for path in py_files():
        p += at(path, grep(r"send_config_set|send_config_from_file|\.config_mode\(", path))
    return p


@check("R21", "ICMP checker: baseline silent, down after N fails, up on recovery, never shells out a non-IP")
def _():
    p = []
    m, conn = fresh_db()
    from app import reach
    a = add_device(conn, "192.0.2.1", "a")
    b = add_device(conn, "192.0.2.2", "b", "node")      # nodes are pinged too
    conn.close()
    answers = {"192.0.2.1": [True, False, False, True], "192.0.2.2": [False, False, False, False]}
    step = [0]

    def fake_ping(host):
        return answers[host][step[0]]

    def kinds():
        c = m["db"].connect()
        try:
            return [(r["device_id"], r["kind"]) for r in c.execute("SELECT device_id, kind FROM events ORDER BY id")]
        finally:
            c.close()
    for i in range(4):
        step[0] = i
        reach.run_cycle(ping=fake_ping)
        if i == 0:
            need(kinds() == [], "first result emitted an event: %r" % kinds(), p)
        if i == 1:
            need(kinds() == [], "one lost ping already raised an event", p)
        if i == 2:
            need(kinds() == [(a, "ping_down")], "after 2 failures expected ping_down, got %r" % kinds(), p)
    need(kinds() == [(a, "ping_down"), (a, "ping_up")], "recovery expected ping_up, got %r" % kinds(), p)
    c = m["db"].connect()
    need(c.execute("SELECT reachable FROM devices WHERE id = ?", (b,)).fetchone()[0] == 0, "node device not marked down", p)
    c.close()
    need(reach._ping("-h") is None and reach._ping("192.0.2.1; id") is None and reach._ping("") is None,
         "_ping must refuse anything that is not an IP", p)
    return p


@check("R22", "/metrics is valid Prometheus text with escaped labels")
def _():
    p = []
    m, conn = fresh_db()
    did = add_device(conn, "192.0.2.1", 'a"b\\c')
    conn.execute("INSERT INTO adjacencies (device_id, proto, peer_ip, state, last_seen) VALUES (?, 'bgp', '192.0.2.9', 'Active', ?)",
                 (did, m["db"].sqlite_now()))
    conn.execute("UPDATE devices SET reachable = 0 WHERE id = ?", (did,))
    conn.commit()
    conn.close()
    resp = m["appmod"].app.test_client().get("/metrics")
    need(resp.status_code == 200 and resp.content_type.startswith("text/plain"), "status/content-type: %s %s" % (resp.status_code, resp.content_type), p)
    text = resp.get_data(as_text=True)
    sample_rx = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\\n]|\\.)*",?)*\})? -?\d+(\.\d+)?$')
    declared = []
    for ln in text.splitlines():
        if ln.startswith("# HELP "):
            declared.append(ln.split()[2])
        elif ln.startswith("# TYPE "):
            continue
        elif not sample_rx.match(ln):
            p.append("not valid exposition format: %r" % ln)
    need(len(declared) == len(set(declared)), "a metric is declared (# HELP) more than once", p)
    for want in ('topo_up 1', 'topo_adjacency_up{device="a\\"b\\\\c",peer="192.0.2.9",proto="bgp"} 0',
                 'topo_device_reachable{device="a\\"b\\\\c",role="router"} 0'):
        need(want in text, "missing line: %s" % want, p)
    return p


@check("R23", "LLDP crawl: adopted devices are inventory-only; off by default; bad IPs refused")
def _():
    p = []
    cfg_src = read("topo/app/config.py")
    need('"TOPO_LLDP_AUTO_ADOPT", "false"' in cfg_src, "TOPO_LLDP_AUTO_ADOPT must default to false", p)
    need('"TOPO_DISCOVERY_INTERVAL_S", "0"' in cfg_src, "TOPO_DISCOVERY_INTERVAL_S must default to 0 (off)", p)
    m, conn = fresh_db()
    from app import lldp_crawl as crawl
    ssh, config = m["ssh"], m["config"]
    did = add_device(conn, "192.0.2.1", "r1")

    def nb(ip, name, port):
        return [dict(local_if="Gi1", remote_chassis="aa", remote_sysname=name, remote_port=port, remote_mgmt_ip=ip)]
    ssh.apply_result(conn, did, "lldp", nb("192.0.2.50", "sw9", "Gi0/1"))
    need([u["ip"] for u in crawl.unknown_neighbors(conn)] == ["192.0.2.50"], "unknown neighbor not listed", p)
    need(conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1, "a neighbor became a device with auto-adopt off", p)
    new = crawl.adopt(conn, "192.0.2.50", "sw9", "r1")
    conn.commit()
    row = conn.execute("SELECT enabled, hostname FROM devices WHERE id = ?", (new,)).fetchone()
    need(row and row["enabled"] == 0 and row["hostname"] == "sw9", "adopted device must start disabled: %r" % (dict(row) if row else None), p)
    need(conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'device_discovered'").fetchone()[0] == 1, "no device_discovered event", p)
    need(conn.execute("SELECT remote_device_id FROM lldp_neighbors").fetchone()[0] == new, "lldp row not linked to the adopted device", p)
    need(crawl.unknown_neighbors(conn) == [], "adopted neighbor still listed as unknown", p)
    for bad in ("127.0.0.1", "0.0.0.0", "224.0.0.1", "999.1.1.1", "not-an-ip", ""):
        need(crawl.adopt(conn, bad, "x", "r1") is None, "adopt accepted %r" % bad, p)
    config.LLDP_AUTO_ADOPT = True
    try:
        ssh.apply_result(conn, did, "lldp", nb("192.0.2.51", "sw10", "Gi0/2"))
    finally:
        config.LLDP_AUTO_ADOPT = False
    auto = conn.execute("SELECT enabled FROM devices WHERE mgmt_ip = '192.0.2.51'").fetchone()
    need(auto is not None and auto["enabled"] == 0, "auto-adopt did not create a disabled device", p)
    conn.close()
    return p


@check("R24", "scheduled discovery: one source failing keeps the others; new devices announced once")
def _():
    p = []
    m, conn = fresh_db()
    conn.close()
    from app import discovery as disc
    config, conf = m["config"], m["confetti"]
    need(disc._thread is None and config.DISCOVERY_INTERVAL_S == 0, "discovery thread should not run by default", p)
    disc.start()
    need(disc._thread is None, "discovery.start() started a thread with the interval at 0", p)

    def fake(c, url):
        return [m["identity"].ingest(c, [("mgmt_ip", "192.0.2.77")], {"hostname": "found1"}, source="confetti")], []
    real, old = conf.discover, (config.DISCOVERY_CONFETTI_URL, config.DISCOVERY_SEEDFILE)
    conf.discover = fake
    config.DISCOVERY_CONFETTI_URL, config.DISCOVERY_SEEDFILE = "http://hub.invalid", os.path.join(TMP, "missing.yaml")
    try:
        s1 = disc.run_once()
        s2 = disc.run_once()
    finally:
        conf.discover = real
        config.DISCOVERY_CONFETTI_URL, config.DISCOVERY_SEEDFILE = old
    need(s1.get("confetti", {}).get("new") == 1 and "error" in s1.get("seedfile", {}), "first run summary: %r" % s1, p)
    need(s2.get("confetti", {}).get("new") == 0, "second run reported the device as new again: %r" % s2, p)
    c = m["db"].connect()
    n = c.execute("SELECT COUNT(*) FROM events WHERE kind = 'device_discovered'").fetchone()[0]
    need(n == 1, "device_discovered events: %d, expected exactly 1" % n, p)
    need(c.execute("SELECT COUNT(*) FROM devices WHERE hostname = 'found1'").fetchone()[0] == 1,
         "the confetti device was lost when the seedfile source failed", p)
    c.close()

    # confetti-butler import: merges by IP/serial, keeps role, a repeat run adds nothing.
    m2, conn2 = fresh_db()
    add_device(conn2, "192.0.2.10", "r-old")
    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return [{"hostname": "r-new", "mgmt_ip": "192.0.2.10", "serial": "SN1", "role": "router", "site": "lab"},
                    {"hostname": "n1", "mgmt_ip": "192.0.2.11", "role": "node", "vendor": "alpine", "platform": "linux"},
                    {"hostname": "", "mgmt_ip": ""}]
    real_get = m2["butler"].requests.get
    m2["butler"].requests.get = lambda url, timeout=10: FakeResp()
    try:
        ids1, _c1 = m2["butler"].discover(conn2, "http://butler.invalid/")
        ids2, _c2 = m2["butler"].discover(conn2, "http://butler.invalid")
    finally:
        m2["butler"].requests.get = real_get
    n = conn2.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    need(len(ids1) == 2 and n == 2, "butler import: %d imported, %d devices (expected 2 and 2)" % (len(ids1), n), p)
    need(ids1 == ids2, "butler import: a repeat run made different devices", p)
    role = conn2.execute("SELECT role FROM devices WHERE hostname = 'n1'").fetchone()
    need(role and role[0] == "node", "butler import lost role='node' (it would be SSH-polled)", p)
    conn2.close()
    return p


@check("R25", "container files agree with the repo (COPY sources, contexts, ignored secrets)")
def _():
    p = []
    df = read("container/Dockerfile")
    for src in re.findall(r"^COPY\s+(\S+)\s+\S+", df, re.M):
        need(os.path.exists(os.path.join("topo", src)), "Dockerfile copies '%s', not in topo/" % src, p)
    need(re.search(r"^FROM alpine:\d", df, re.M) is not None, "Dockerfile base image is not a pinned alpine:<version>", p)
    need("TOPO_HEALTH_SERVICES=" in df, "Dockerfile must empty TOPO_HEALTH_SERVICES (no OpenRC in a container)", p)
    for pin in ("netmiko", "waitress"):
        need("'^%s=='" % pin in df, "Dockerfile does not install %s from the requirements.txt pin" % pin, p)
    dc = read("container/docker-compose.yml")
    need("context: ../topo" in dc and "dockerfile: ../container/Dockerfile" in dc, "docker-compose.yml build paths are off", p)
    dev = read("container/docker-compose.dev.yml")
    need(dev.count("context: ../topo") == 2 and dev.count("dockerfile: ../container/Dockerfile") == 2,
         "docker-compose.dev.yml build paths are off", p)
    need(re.search(r'TOPO_PING_ENABLED: "false"', dev) is not None, "dev mock service must switch ping off (fake IPs)", p)
    need(not re.search(r"^\s*TOPO_SSH_(PASSWORD|SECRET):[ \t]*[^$\s]", dev, re.M), "docker-compose.dev.yml hardcodes an SSH secret", p)
    need("scripts/seed_mock_lab.py" in dev and os.path.exists("topo/scripts/seed_mock_lab.py"),
         "dev mock service seeds from scripts/seed_mock_lab.py, which is missing", p)
    di = read("topo/.dockerignore")
    need("topo.env" in di and "topo.db" in di, "topo/.dockerignore must keep topo.env and topo.db* out of the image", p)
    return p


@check("R27", "SSH server is OpenSSH: sshd installed, root password login on, switched from Dropbear last, health list says sshd")
def _():
    # Dropbear has no SFTP server and drops large packets (found 2026-10-14). The build
    # installs sshd, and on a VM that still has Dropbear it switches only after sshd is
    # confirmed running. The switch must sit above the --update exit, or a deployed VM
    # would never get it.
    p = []
    bt = read("topo/build-topo.sh")
    need(re.search(r"^switch_ssh_server\(\) \{", bt, re.M), "build-topo.sh lost switch_ssh_server()", p)
    need("openssh-server openssh-sftp-server" in bt, "build must install openssh-server and openssh-sftp-server", p)
    need("PermitRootLogin yes" in bt, "sshd drop-in must keep root password login on (the lab logins depend on it)", p)
    need(not re.search(r"^[^#\n]*apk add[^\n]*\bdropbear\b(?!-convert)", bt, re.M), "build installs dropbear again", p)
    call = [m.start() for m in re.finditer(r"^switch_ssh_server$", bt, re.M)]
    exit_at = bt.find('log "=== Update install complete ==="')
    need(call and 0 <= exit_at and call[0] < exit_at,
         "switch_ssh_server must be called before the --update exit", p)
    need("rc-update del dropbear" in bt and bt.find("rc-update del dropbear") > bt.find("rc-service sshd start"),
         "Dropbear must only be removed after sshd was started", p)
    need("dropbear" not in read("topo/app/config.py").split("HEALTH_SERVICES")[1].split(")")[0],
         "config.py health default still lists dropbear", p)
    need(re.search(r"HEALTH_SERVICES=[^\n]*\bsshd\b", bt), "topo.env health list must name sshd", p)
    return p


@check("R28", "credentials: device row > Settings page > TOPO_SSH_* env, empty falls through, no secret in any GET, bulk is all-or-nothing")
def _():
    p = []
    m, conn = fresh_db()
    config, ssh = m["config"], m["ssh"]
    a = add_device(conn, "192.0.2.1", "a")
    b = add_device(conn, "192.0.2.2", "b")
    conn.close()
    config.DEFAULT_SSH_USERNAME, config.DEFAULT_SSH_PASSWORD, config.DEFAULT_SSH_SECRET = "envuser", "envpw", "envsecret"
    client = m["appmod"].app.test_client()

    def creds(did, fields=("username", "password", "secret")):
        c = m["db"].connect()
        try:
            got = ssh.resolve_credentials(c, did)
            return {k: got[k] for k in fields}
        finally:
            c.close()
    need(creds(a) == {"username": "envuser", "password": "envpw", "secret": "envsecret"}, "env defaults not used: %r" % creds(a), p)
    client.put("/api/settings/credentials", json={"username": "setuser", "password": "setpw", "enable_secret": "SETSECRET"})
    need(creds(a) == {"username": "setuser", "password": "setpw", "secret": "SETSECRET"}, "Settings page does not beat env: %r" % creds(a), p)
    client.put("/api/settings/credentials", json={"enable_secret": ""})
    need(creds(a)["secret"] == "envsecret", "an emptied Settings value must fall back to env: %r" % creds(a), p)
    client.put("/api/devices/%d/credentials" % a, json={"username": "devuser", "password": "DEVPW"})
    need(creds(a) == {"username": "devuser", "password": "DEVPW", "secret": "envsecret"}, "device row does not beat Settings: %r" % creds(a), p)
    need(creds(b)["username"] == "setuser", "another device must keep using the Settings default: %r" % creds(b), p)
    for url in ("/api/settings/credentials", "/api/credentials", "/api/devices/%d/credentials" % a):
        body = client.get(url).get_data(as_text=True)
        for secret in ("DEVPW", "setpw", "SETSECRET"):
            need(secret not in body, "GET %s returns a secret" % url, p)
    r = client.post("/api/credentials/bulk", json={"device_ids": [a, 9999], "username": "bulk"})
    need(r.status_code == 404, "bulk with an unknown id should be 404, got %s" % r.status_code, p)
    need(creds(a)["username"] == "devuser", "a failed bulk apply changed a device", p)
    r = client.post("/api/credentials/bulk", json={"device_ids": [a, b], "username": "bulk"})
    need(r.status_code == 200 and creds(a)["username"] == creds(b)["username"] == "bulk", "bulk apply did not reach both devices", p)
    client.post("/api/credentials/clear", json={"device_ids": [a, b]})
    need(creds(a)["username"] == "setuser" and creds(a)["password"] == "setpw", "clearing own credentials should fall back to Settings: %r" % creds(a), p)
    # SNMP community and version (from confetti-butler) follow the same order and are never returned by a GET.
    snmp = ("snmp_community", "snmp_version")
    config.DEFAULT_SNMP_COMMUNITY, config.DEFAULT_SNMP_VERSION = "envcomm", "1"
    need(creds(a, snmp) == {"snmp_community": "envcomm", "snmp_version": "1"}, "env SNMP defaults not used: %r" % creds(a, snmp), p)
    client.put("/api/settings/credentials", json={"snmp_community": "SETCOMM", "snmp_version": "2c"})
    need(creds(a, snmp) == {"snmp_community": "SETCOMM", "snmp_version": "2c"}, "Settings SNMP does not beat env: %r" % creds(a, snmp), p)
    client.put("/api/devices/%d/credentials" % a, json={"snmp_community": "DEVCOMM"})
    need(creds(a, snmp)["snmp_community"] == "DEVCOMM", "device SNMP community does not beat Settings: %r" % creds(a, snmp), p)
    need(creds(b, snmp)["snmp_community"] == "SETCOMM", "another device must keep the Settings SNMP community: %r" % creds(b, snmp), p)
    for url in ("/api/settings/credentials", "/api/credentials", "/api/devices/%d/credentials" % a):
        body = client.get(url).get_data(as_text=True)
        for secret in ("DEVCOMM", "SETCOMM"):
            need(secret not in body, "GET %s returns an SNMP community" % url, p)
    return p


@check("R29", "right-click menus: base.html loads ctxmenu.js, rows and handlers pair up, data-* values are quote-safe, inline scripts parse")
def _():
    # static/ctxmenu.js is the same file in confetti-butler, confetti-traffic and here. A log line
    # or hostname can hold a double quote and esc() does not escape it, so attributes on data-ctx
    # rows are filled with ctxMenu.attr(). The changes page once had its own inline menu; it and the
    # topology map now use the shared one.
    p = []
    need(os.path.exists("topo/static/ctxmenu.js"), "topo/static/ctxmenu.js is missing", p)
    base = read("topo/templates/base.html")
    need('src="/static/ctxmenu.js"' in base, "base.html does not load /static/ctxmenu.js", p)
    need(base.find("/static/ctxmenu.js") < base.find("{% block scripts %}"),
         "ctxmenu.js must load before the page scripts (they call ctxMenu.register)", p)
    need(".ctx-menu" in read("topo/static/layout.css"), "layout.css lost the .ctx-menu rules", p)
    node = shutil.which("node")
    for f in sorted(glob.glob("topo/templates/*.html")):
        src = read(f)
        for m in re.finditer(r"data-[\w-]+=\"' \+ esc\(", src):
            p.append("%s: data-* attribute filled with esc(), use ctxMenu.attr(): %s" % (f, src[m.start():m.start() + 40]))
        used = set(re.findall(r'data-ctx="(\w+)', src)) | set(re.findall(r"setAttribute\('data-ctx', '(\w+)'", src))
        reg = set(re.findall(r"ctxMenu\.register\('(\w+)'", src))
        if not f.endswith("base.html"):
            for kind in sorted(used - reg):
                p.append("%s: data-ctx '%s' has no ctxMenu.register" % (f, kind))
            for kind in sorted(reg - used):
                p.append("%s: ctxMenu.register('%s') has no data-ctx row" % (f, kind))
        if "danger: true" in src and "confirm(" not in src and "confirm:" not in src:
            p.append("%s has a danger menu item but never asks confirm()" % f)
        if 'id="ctx-menu"' in src or "closeContextMenu" in src:
            p.append("%s still carries its own menu code; use ctxMenu" % f)
        if node:
            for i, m in enumerate(re.finditer(r"<script>\r?\n(.*?)</script>", src, re.S)):
                js = re.sub(r"\{\{.*?\}\}", "0", re.sub(r"\{%.*?%\}", "", m.group(1)))
                tmp = os.path.join(TMP, "r29_%d.js" % i)
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(js)
                r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
                need(r.returncode == 0, "%s script %d does not parse: %s" % (f, i, r.stderr.strip()[:150]), p)
    return p


@check("R26", "IOS BGP summary: an administratively shut peer keeps '(Admin)' (found on a real 15.4 router)")
def _():
    p = []
    ios = app_modules()["ios"]
    rows = {r["peer_ip"]: r for r in ios.parse_bgp_summary(sample("ios_bgp_summary_15_4.txt"))}
    need(rows.get("192.0.2.250", {}).get("state") == "Idle (Admin)", "admin-down peer parsed as %r" % rows.get("192.0.2.250"), p)
    need(rows.get("198.51.100.81", {}).get("state") == "Established" and rows["198.51.100.81"]["prefixes"] == 4, "established peer: %r" % rows.get("198.51.100.81"), p)
    need(rows.get("198.51.100.164", {}).get("state") == "Idle", "idle peer: %r" % rows.get("198.51.100.164"), p)
    need(all(r.get("local_as") == 65030 for r in rows.values()) and rows, "local AS from the header line: %r" % [r.get("local_as") for r in rows.values()], p)
    return p


@check("R30", "PAN-OS XML API: parses real output, read-only <show> ops only, password in the body and key in a header, never a URL")
def _():
    p = []
    m = app_modules()
    pp, panos, poller = m["panos_parser"], m["panos"], m["poller"]
    from app import platforms
    plat = platforms.PLATFORMS["paloalto_panos"]

    # Parsers, on fixtures captured from a real PA-VM (PAN-OS 11.1).
    v = pp.parse_version(sample("panos_version.xml"))
    need(v["model"] == "PA-VM" and v["serial"] and v["os_version"] == "11.1.10-h1" and v["hostname_hint"], "version: %r" % v, p)
    ifs = {r["name"]: r for r in pp.parse_interfaces(sample("panos_interfaces.xml"))}
    e1 = ifs.get("ethernet1/1", {})
    need(e1.get("ip") == "198.51.100.81" and e1.get("prefix_len") == 24 and e1.get("oper_status") == "up"
         and e1.get("network") == "198.51.100.0/24", "interface with an address: %r" % e1, p)
    need(ifs.get("ethernet1/1.3101", {}).get("oper_status") is None, "a sub-interface has no link state of its own, none must be invented", p)
    need(ifs.get("ethernet1/2", {}).get("ip") is None, "an interface with ip N/A must have no address", p)
    need(e1.get("mac") == "0200.0000.000a" and ifs.get("ethernet1/1.3101", {}).get("mac") is None, "interface hardware MAC (dotted, none for a sub-interface): %r" % (e1.get("mac"),), p)
    ll = pp.parse_lldp_neighbors(sample("panos_lldp.xml"))
    sw = [r for r in ll if r["remote_port"] == "Gi0/7"]
    need(len(ll) == 9 and len(sw) == 1 and sw[0]["local_if"] == "ethernet1/1" and sw[0]["remote_mgmt_ip"] == "198.51.100.2",
         "lldp: the remote port is the neighbor's port id, not its port description (%d rows): %r" % (len(ll), sw), p)
    need(any(r["remote_sysname"] is None and r["remote_mgmt_ip"] is None for r in ll), "a neighbor with no name or address must still parse", p)
    need(any(r["remote_port"] == "02:00:00:00:00:08" and r["remote_port_descr"] == "eth0" for r in ll) and sw[0]["remote_port_descr"] is not None,
         "lldp: the port description must be kept as remote_port_descr, beside the port id", p)
    peers = pp.fill_bgp_local_as(pp.parse_bgp_peers(sample("panos_bgp.xml")), pp.parse_bgp_local_as(sample("panos_bgp_summary.xml")))
    by_ip = {r["peer_ip"]: r for r in peers}
    est = by_ip.get("198.51.100.167", {})
    need(est.get("state") == "Established" and est.get("remote_as") == 65030 and est.get("local_as") == 65000 and est.get("prefixes") == 3,
         "established peer (the :179 port must be dropped from its address): %r" % est, p)
    need(by_ip.get("198.51.100.164", {}).get("state") == "Connect", "a peer that is not up keeps the state it reports: %r" % by_ip.get("198.51.100.164"), p)
    need([r["state"] for r in pp.parse_ospf_neighbors(sample("panos_ospf_neighbor.xml"))] == ["FULL"], "ospf 'full' must read FULL like IOS", p)
    for fn in (pp.parse_version, pp.parse_interfaces, pp.parse_lldp_neighbors, pp.parse_bgp_peers, pp.parse_ospf_neighbors):
        for junk in ("", "<x", None, '<response status="error"><msg>no</msg></response>', '<response status="success"><result></result></response>'):
            try:
                fn(junk)
            except Exception as exc:
                p.append("%s raised %r on %r: parsers must fail soft" % (fn.__name__, exc, junk))

    # Static: the collector sends only keygen and op requests.
    path = "topo/app/collectors/panos.py"
    p += at(path, grep(r'"type":\s*"(?!keygen"|op")', path))
    p += at(path, grep(r"requests\.(get|put|delete)|\.get\(\s*[\"']https", path))

    # The collector against a fake HTTP session.
    class Resp:
        status_code = 200

        def __init__(self, text):
            self.text = text

    class FakeSession:
        calls = []
        answers = {}
        reject_login = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, data=None, headers=None, verify=None, timeout=None):
            FakeSession.calls.append({"url": url, "data": dict(data), "headers": dict(headers or {})})
            if data["type"] == "keygen":
                if FakeSession.reject_login:
                    return Resp('<response status="error"><result><msg>Invalid credentials.</msg></result></response>')
                return Resp('<response status="success"><result><key>FAKEKEY123</key></result></response>')
            return Resp(FakeSession.answers.get(data["cmd"], '<response status="error"><msg>no such</msg></response>'))

    cmds = plat["commands"]
    FakeSession.answers = {cmds["version"]: sample("panos_version.xml"), cmds["interfaces"]: sample("panos_interfaces.xml"),
                           cmds["lldp"]: sample("panos_lldp.xml"), cmds["bgp"]: sample("panos_bgp.xml"),
                           cmds["ospf"]: sample("panos_ospf_neighbor.xml"),
                           plat["bgp_summary_command"]: sample("panos_bgp_summary.xml"),
                           cmds["routes"]: sample("panos_route.xml"), plat["fib_command"]: sample("panos_fib.xml")}
    real = panos.requests.Session
    panos.requests.Session = FakeSession
    try:
        creds = {"username": "reader", "password": "s3cret!#pw", "secret": ""}
        dev = {"mgmt_ip": "192.0.2.9", "platform": "paloalto_panos"}
        got = list(panos.run_tasks(dev, creds))
        need([t for t, ok, *_ in got] == list(cmds) and all(ok for _, ok, *_ in got), "every task must succeed on a good answer: %r" % [(t, ok, e) for t, ok, _, e, _ in got], p)
        types = [c["data"]["type"] for c in FakeSession.calls]
        need(types[0] == "keygen" and set(types) == {"keygen", "op"}, "only keygen and op requests may be sent: %r" % types, p)
        need(all(c["data"]["cmd"].startswith("<show>") for c in FakeSession.calls if c["data"]["type"] == "op"), "an op request is not a <show> command", p)
        need(all("?" not in c["url"] and "s3cret" not in c["url"] and "FAKEKEY" not in c["url"] for c in FakeSession.calls), "a secret or a query string is in a URL", p)
        need(FakeSession.calls[0]["data"].get("password") == "s3cret!#pw" and not FakeSession.calls[0]["headers"], "keygen: the password goes in the POST body, with no key header", p)
        need(all(c["headers"] == {"X-PAN-KEY": "FAKEKEY123"} and "password" not in c["data"] for c in FakeSession.calls[1:]), "an op call must carry the key in the header and no password", p)
        need(all("s3cret" not in (out or "") and "FAKEKEY" not in (out or "") for _, _, out, _, _ in got), "a secret ended up in the stored raw output", p)
        bgp = [parsed for t, _, _, _, parsed in got if t == "bgp"][0]
        need({r["local_as"] for r in bgp} == {65000}, "bgp rows must carry the router's own AS from the summary: %r" % bgp, p)

        # The summary is a bonus: without it the peers are still reported.
        del FakeSession.answers[plat["bgp_summary_command"]]
        got = list(panos.run_tasks(dev, creds, ["bgp"]))
        need(got[0][1] and {r["local_as"] for r in got[0][4]} == {None}, "a failed bgp summary must not lose the peers: %r" % (got,), p)

        # A wrong password fails every task, and nothing else is sent.
        FakeSession.calls.clear()
        FakeSession.reject_login = True
        got = list(panos.run_tasks(dev, creds))
        need(len(got) == len(cmds) and not any(ok for _, ok, *_ in got) and len(FakeSession.calls) == 1, "a failed login must fail every task after one request", p)
        need(all("s3cret" not in (e or "") for _, _, _, e, _ in got), "the password is in an error text", p)
        FakeSession.reject_login = False

        # A management address that could change the URL is refused before any request.
        FakeSession.calls.clear()
        got = list(panos.run_tasks({"mgmt_ip": "192.0.2.9/evil", "platform": "paloalto_panos"}, creds))
        need(not any(ok for _, ok, *_ in got) and not FakeSession.calls, "a mgmt_ip with a slash must be refused without a request", p)
        try:
            panos._op(FakeSession(), "192.0.2.9", "k", "<request><restart><system/></restart></request>")
            p.append("_op sent a command that is not <show>")
        except ValueError:
            pass

        # Through the poller: a PAN-OS device is polled by the XML collector, recorded as that transport,
        # and a failed poll deletes nothing (R14 covers the shared apply steps).
        mm, conn = fresh_db()
        did = add_device(conn, "192.0.2.9", "fw1")
        conn.execute("UPDATE devices SET platform = 'paloalto_panos' WHERE id = ?", (did,))
        conn.commit()
        conn.close()
        FakeSession.answers[plat["bgp_summary_command"]] = sample("panos_bgp_summary.xml")
        real_ssh = poller.ssh_collector.run_tasks
        poller.ssh_collector.run_tasks = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a PAN-OS device was polled over SSH"))
        try:
            res = poller._poll_device(did)
        finally:
            poller.ssh_collector.run_tasks = real_ssh
        need(res and all(r["ok"] for r in res.values()), "polling a PAN-OS device through the poller: %r" % (res,), p)
        c = mm["db"].connect()
        try:
            need({r["transport"] for r in c.execute("SELECT transport FROM poll_history")} == {"xml_api"}, "poll_history must record the xml_api transport", p)
            need(c.execute("SELECT count(*) FROM adjacencies WHERE device_id = ? AND proto = 'bgp' AND local_as = 65000", (did,)).fetchone()[0] == 2, "bgp peers with local_as must be stored", p)
            before = c.execute("SELECT count(*) FROM lldp_neighbors").fetchone()[0]
            need(c.execute("SELECT mgmt_mac FROM devices WHERE id = ?", (did,)).fetchone()[0] == "02:00:00:00:00:09", "the version task must store the management MAC", p)
        finally:
            c.close()
        FakeSession.reject_login = True
        poller._poll_device(did)
        c = mm["db"].connect()
        try:
            need(c.execute("SELECT count(*) FROM lldp_neighbors").fetchone()[0] == before > 0, "a failed login deleted LLDP neighbors", p)
        finally:
            c.close()
    finally:
        panos.requests.Session = real
    return p


@check("R31", "LLDP chassis/name matching: off by default, unique owner only, never itself, never an alias, one cable once")
def _():
    p = []
    m, conn = fresh_db()
    from app import topology
    cfg = m["config"]
    client = m["appmod"].app.test_client()
    fw = add_device(conn, "192.0.2.10", "lab-fw-1", "firewall")
    sw = add_device(conn, "192.0.2.2", "sw1")
    csr = add_device(conn, "192.0.2.3", "csr-1.lab.local")
    conn.execute("UPDATE devices SET mgmt_mac = '02:00:00:00:00:09' WHERE id = ?", (fw,))
    _put_lldp(conn, sw, "Gi0/7", "lab-fw-1", "ethernet1/1", chassis="0200.0000.0009")   # the switch's view of the firewall
    _put_lldp(conn, fw, "ethernet1/1", "sw1", "Gi0/7", remote_id=sw)                         # the firewall's view of the switch
    conn.commit()

    def edges():
        return [e for e in topology.build(conn, "lldp")["edges"]]

    def ghosts():
        return sorted(n["id"] for n in topology.build(conn)["nodes"] if n["ghost"])

    def set_on(chassis, name):
        conn.commit()   # the API writes through its own connection; an open write here would lock it
        r = client.put("/api/settings/lldp-matching", json={"chassis": chassis, "name": name})
        need(r.status_code == 200, "PUT lldp-matching -> %s" % r.status_code, p)

    # Default: off, nothing changes (constraints 3 and 13).
    cfg.LLDP_MATCH_CHASSIS = cfg.LLDP_MATCH_NAME = False
    need(topology.lldp_matching(conn)["chassis"] is False and topology.lldp_matching(conn)["name"] is False, "matching must be off by default", p)
    need(ghosts() == ["ghost:lldp:lab-fw-1"] and len(edges()) == 2, "off: the unknown neighbor must stay a ghost: %r %d" % (ghosts(), len(edges())), p)

    # Chassis on: one cable, two reports, no ghost.
    set_on(True, False)
    e = edges()
    need(not ghosts() and len(e) == 1 and len(e[0]["details"]) == 2, "chassis on: one edge with two reports and no ghost: %r %r" % (ghosts(), e), p)
    need({d["matched_by"] for d in e[0]["details"]} == {"chassis", None}, "the report must say it was matched by chassis: %r" % e[0]["details"], p)
    need(topology.lldp_neighbors(conn, sw)[0]["remote_device_id"] == fw, "/api/lldp-neighbors disagrees with the graph", p)

    # Name only (chassis off): same result, other label. Name on its own must not need a MAC.
    conn.execute("UPDATE devices SET mgmt_mac = NULL")
    set_on(False, True)
    e = edges()
    need(not ghosts() and len(e) == 1 and {d["matched_by"] for d in e[0]["details"]} == {"name", None}, "name on: matched by name: %r %r" % (ghosts(), e), p)
    set_on(True, False)
    need(ghosts() == ["ghost:lldp:lab-fw-1"], "chassis on with no MAC on file must not match by name: %r" % ghosts(), p)
    set_on(False, False)
    need(ghosts() == ["ghost:lldp:lab-fw-1"], "both switched off again: the ghost must return", p)

    # The domain suffix and the case of a name do not matter, in both directions.
    _put_lldp(conn, sw, "Gi0/9", "CSR-1", "Gi1")
    _put_lldp(conn, sw, "Gi0/10", "csr-1.LAB.local", "Gi2")
    conn.commit()
    set_on(False, True)
    need(len([x for x in edges() if x["to"] == "dev:%d" % csr]) == 2, "CSR-1 and csr-1.LAB.local must both find csr-1.lab.local", p)

    # Ambiguous: two devices with one name or one MAC stay ghosts.
    dup = add_device(conn, "192.0.2.4", "csr-1")
    need("ghost:lldp:CSR-1" in ghosts(), "a name two devices answer to must stay a ghost: %r" % ghosts(), p)
    conn.execute("DELETE FROM devices WHERE id = ?", (dup,))
    conn.execute("UPDATE devices SET mgmt_mac = '02-00-00-00-00-09' WHERE id IN (?, ?)", (fw, sw))
    conn.commit()
    set_on(True, False)
    need("ghost:lldp:lab-fw-1" in ghosts(), "a MAC two devices hold must stay a ghost: %r" % ghosts(), p)
    conn.execute("UPDATE devices SET mgmt_mac = NULL WHERE id = ?", (sw,))

    # Never to itself; the stored match and the IP match win.
    _put_lldp(conn, fw, "ethernet1/5", "lab-fw-1", "x", chassis="0200.0000.0009")
    conn.commit()
    set_on(True, True)
    need(all(x["from"] != x["to"] for x in edges()), "a neighbor that is the device itself became a self-loop", p)
    need(topology.resolve_lldp({"owners": {}, "macs": {"020000000009": fw}, "names": None}, sw, csr, None, "0200.0000.0009", None) == (csr, None),
         "the poller's stored match must win over a chassis match", p)
    need(topology.resolve_lldp({"owners": {"10.0.0.9": csr}, "macs": None, "names": {"lab-fw-1": fw}}, sw, None, "10.0.0.9", None, "lab-fw-1") == (csr, None),
         "an interface IP match must win over a name match", p)

    # Formats, and nothing written as an identity.
    for t in ("02:00:00:00:00:09", "0200.0000.0009", "02-00-00-00-00-09", "020000000009"):
        need(topology.norm_mac(t) == "020000000009", "norm_mac(%r) = %r" % (t, topology.norm_mac(t)), p)
    need(topology.norm_mac("sw1") is None and topology.norm_mac("0200.0000") is None and topology.norm_mac(None) is None, "norm_mac must refuse what is not a MAC", p)
    n = conn.execute("SELECT COUNT(*) FROM device_aliases WHERE lower(value) LIKE '%0009%' OR lower(value) LIKE '%lab-fw-1%'").fetchone()[0]
    need(n == 0, "a matched MAC or name was stored as an identity alias (%d)" % n, p)
    p += at("topo/app/topology.py", grep(r"device_aliases|INSERT|UPDATE|DELETE", "topo/app/topology.py"))

    # Settings API: partial update, bad body, env fallback.
    set_on(True, True)
    client.put("/api/settings/lldp-matching", json={"name": False})
    got = client.get("/api/settings/lldp-matching").get_json()
    need(got["chassis"] is True and got["name"] is False and got["source"] == {"chassis": "settings", "name": "settings"}, "partial PUT: %r" % got, p)
    need(client.put("/api/settings/lldp-matching", json={"chassis": "yes"}).status_code == 400, "a non-boolean value must be refused", p)
    need(client.put("/api/settings/lldp-matching", json={}).status_code == 400, "an empty body must be refused", p)
    conn.execute("DELETE FROM settings WHERE key LIKE 'lldp_match_%'")
    conn.commit()
    cfg.LLDP_MATCH_NAME = True
    got = topology.lldp_matching(conn)
    need(got["name"] is True and got["chassis"] is False and got["source"]["name"] == "env", "env default not used when no Settings row: %r" % got, p)
    cfg.LLDP_MATCH_NAME = False
    conn.close()
    return p



# ---- merged in from confetti-butler: templates, config backups, drift, alerting, SNMP, error history ----


@check("R43", "template rendering: StrictUndefined and sandboxed")
def _():
    p = []
    import jinja2
    import jinja2.sandbox
    app_modules()
    from app import rendering as r
    need(isinstance(r._ENV, jinja2.sandbox.SandboxedEnvironment), "environment is not a SandboxedEnvironment", p)
    need(r._ENV.undefined is jinja2.StrictUndefined, "undefined is not StrictUndefined", p)
    for src, what in (("{{ vars.nope }}", "unset variable"),
                      ("{{ cycler.__init__.__globals__ }}", "sandbox escape")):
        try:
            out = r.render(src, {"vars": {}})
            p.append("%s rendered instead of raising: %r" % (what, out[:60]))
        except r.RenderError:
            pass
    return p


@check("R44", "GET /configs/<key>.cfg stays unauthenticated, also with TOPO_AUTH_TOKEN set (a device cannot send a token); the API still needs it")
def _():
    p = []
    m, conn = fresh_db()
    did = add_device(conn, "192.0.2.1")
    key = conn.execute("SELECT key FROM devices WHERE id = ?", (did,)).fetchone()["key"]
    conn.execute("INSERT INTO templates (name, body, updated_at) VALUES ('t', 'hostname {{ device.hostname }}', ?)",
                 (m["db"].sqlite_now(),))
    conn.execute("UPDATE devices SET template_name = 't' WHERE id = ?", (did,))
    conn.commit()
    conn.close()
    client = m["appmod"].app.test_client()
    resp = client.get("/configs/%s.cfg" % key)
    need(resp.status_code == 200 and b"hostname rtr" in resp.data,
         "pull returned %s %r" % (resp.status_code, resp.data[:60]), p)
    need(client.get("/configs/nope.cfg").status_code == 404, "unknown key should be 404, not an auth error", p)
    need(client.get("/configs/").status_code == 200, "/configs/ index should be open", p)
    config = m["config"]
    old, config.AUTH_TOKEN = config.AUTH_TOKEN, "s3cret-token"
    try:
        resp = client.get("/configs/%s.cfg" % key)
        need(resp.status_code == 200 and b"hostname rtr" in resp.data,
             "with an access token set, the pull returned %s" % resp.status_code, p)
        need(client.get("/api/templates").status_code == 401, "with an access token set, /api/templates must need it", p)
        need(client.get("/api/devices/%d/configs" % did).status_code == 401, "with an access token set, the config backups must need it", p)
        need(client.get("/templates").status_code == 302, "with an access token set, the templates page must redirect to /login", p)
    finally:
        config.AUTH_TOKEN = old
    return p


RUNNING = """Building configuration...

Current configuration : 1234 bytes
!
! Last configuration change at 10:00:00 UTC Mon Oct 5 2026
version 17.3
hostname r1
enable secret 9 $9$abc$def
username bob password 0 hunter2
snmp-server community lab123 RO
ntp clock-period 123456
end
"""


@check("R45", "running-config backup: only complete configs, stored on change only, secrets masked")
def _():
    p = []
    m, conn = fresh_db()
    ios, ssh = m["ios"], m["ssh"]
    from app import rendering
    need(ios.parse_running_config("% Invalid input detected at '^' marker.") == "", "error text accepted as a config", p)
    need(ios.parse_running_config(RUNNING.replace("end\n", "")) == "", "truncated config (no 'end') accepted", p)
    a = ios.parse_running_config(RUNNING)
    b = ios.parse_running_config(RUNNING.replace("10:00:00", "11:11:11").replace("123456", "999"))
    need(a and a == b, "volatile lines (last change / clock-period) make identical configs differ", p)
    did = add_device(conn, "192.0.2.1")
    ssh.apply_result(conn, did, "config", a)
    ssh.apply_result(conn, did, "config", b)
    conn.commit()
    n = conn.execute("SELECT count(*) FROM config_versions").fetchone()[0]
    need(n == 1, "same config stored %d times" % n, p)
    masked = rendering.redact_secrets(a)
    for leak in ("$9$abc", "hunter2", "lab123"):
        need(leak not in masked, "secret %r survives redact_secrets" % leak, p)
    # Found leaking on a real router's config (the isakmp pre-shared key came back unmasked).
    more = rendering.redact_secrets(
        "crypto isakmp key s3cretpsk address 198.51.100.7\n"
        "ntp authentication-key 1 md5 ntpk3y\n"
        "standby 1 authentication md5 key-string hsrpk3y\n"
        " description crypto isakmp key is a topic\n")
    for leak in ("s3cretpsk", "ntpk3y", "hsrpk3y"):
        need(leak not in more, "secret %r survives redact_secrets" % leak, p)
    need(" description crypto isakmp key is a topic" in more, "redact_secrets masked a description line", p)
    conn.close()
    resp = m["appmod"].app.test_client().get("/api/devices/%d/configs/1" % did)
    need(resp.status_code == 200 and b"hunter2" not in resp.data, "API returns an unmasked secret by default", p)
    return p


class _Capture:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, target, payload):
        self.sent.append((target, payload))
        if self.fail:
            raise RuntimeError("target down")


def _rule(conn, **kw):
    row = dict(name="r", enabled=1, source="event", kinds="", min_severity="info", syslog_max_severity=None,
               pattern="", device="", target="webhook:http://127.0.0.1:9/x", cooldown_s=300, created_at="2026-01-01 00:00:00")
    row.update(kw)
    cols = ",".join(row)
    conn.execute("INSERT INTO alert_rules (%s) VALUES (%s)" % (cols, ",".join(":" + k for k in row)), row)
    conn.commit()


@check("R46", "alerting: matching, cooldown, no replay of old events, failures contained, no write lock during a send")
def _():
    p = []
    m, conn = fresh_db()
    from app import alerts, events
    did = add_device(conn, "192.0.2.1", "core-r1")
    # --- pure matching
    ev = {"kind": "bgp_state", "severity": "warning", "subject": "core-r1 BGP peer 10.0.0.2: Active", "detail": "Established -> Active", "hostname": "core-r1"}
    base = dict(source="event", kinds="", min_severity="info", device="", pattern="")
    need(alerts.matches_event(dict(base), ev), "empty rule should match every event", p)
    need(not alerts.matches_event(dict(base, kinds="ping_down"), ev), "kind filter ignored", p)
    need(alerts.matches_event(dict(base, kinds="ping_down, bgp_state"), ev), "kind list with a space failed", p)
    need(not alerts.matches_event(dict(base, min_severity="critical"), ev), "min_severity ignored", p)
    need(alerts.matches_event(dict(base, min_severity="warning"), ev), "min_severity=warning should match a warning", p)
    need(not alerts.matches_event(dict(base, device="edge"), ev) and alerts.matches_event(dict(base, device="CORE"), ev), "device filter", p)
    need(alerts.matches_event(dict(base, pattern="established ->"), ev) and not alerts.matches_event(dict(base, pattern="nomatch"), ev), "pattern", p)
    sysb = dict(source="syslog", syslog_max_severity=3, device="", pattern="")
    msg = {"severity": 3, "host": "core-r1", "source_ip": "192.0.2.1", "mnemonic": "BGP-3-NOTIFICATION", "message": "sent to neighbor"}
    need(alerts.matches_syslog(sysb, msg) and not alerts.matches_syslog(sysb, dict(msg, severity=5)), "syslog severity limit", p)
    need(not alerts.matches_syslog(sysb, dict(msg, severity=None)), "a message without a severity must not pass a severity limit", p)
    need(alerts.matches_syslog(dict(sysb, pattern="NOTIFICATION"), msg), "syslog pattern over the mnemonic", p)
    need(not alerts.matches_syslog(dict(base, source="event"), msg), "an event rule matched syslog", p)

    # --- pipeline
    _rule(conn, name="drift", kinds="config_drift,ping_down", min_severity="warning", cooldown_s=300)
    events.emit(conn, did, "config_drift", "warning", "core-r1: drifted", "1 line(s) missing")
    events.emit(conn, did, "interface_up", "info", "core-r1 Gi1: up", "")           # not matched
    # An old event on a different device key: the cooldown must not be what keeps it quiet.
    conn.execute("INSERT INTO events (at, device_id, kind, severity, subject, detail) VALUES ('2020-01-01 00:00:00', NULL, 'ping_down', 'warning', 'old', '')")
    conn.commit()
    cap = _Capture()
    now = time.time()
    alerts.process_once(deliver_fn=cap, now=now)
    need(len(cap.sent) == 1 and cap.sent[0][1]["kind"] == "config_drift", "first pass sent %r" % [(c[1]["kind"]) for c in cap.sent], p)
    need(cap.sent and cap.sent[0][1]["text"].startswith("[confetti-topo] core-r1: drifted"), "payload text: %r" % (cap.sent[0][1]["text"] if cap.sent else None), p)
    alerts.process_once(deliver_fn=cap, now=now + 1)
    need(len(cap.sent) == 1, "events were alerted twice", p)
    events.emit(conn, did, "ping_down", "warning", "core-r1: not answering ping", "")
    events.emit(conn, did, "ping_down", "warning", "core-r1: not answering ping again", "")
    conn.commit()
    alerts.process_once(deliver_fn=cap, now=now + 10)
    need(len(cap.sent) == 1, "cooldown ignored: %d sent" % len(cap.sent), p)
    events.emit(conn, did, "ping_down", "warning", "core-r1: still down", "")
    conn.commit()
    alerts.process_once(deliver_fn=cap, now=now + 400)
    need(len(cap.sent) == 2 and "held back" in cap.sent[1][1]["text"], "after the cooldown: %r" % (cap.sent[-1][1]["text"] if cap.sent else None), p)

    # --- syslog queue
    _rule(conn, name="sys", source="syslog", syslog_max_severity=3, cooldown_s=0)
    for i in range(3):
        alerts.offer_syslog(dict(msg, message="m%d" % i))
    alerts.offer_syslog(dict(msg, severity=6))
    alerts.offer_syslog(None)
    cap2 = _Capture()
    alerts.process_once(deliver_fn=cap2, now=now + 500)
    need(len(cap2.sent) == 3, "syslog alerts sent: %d, expected 3" % len(cap2.sent), p)
    need(alerts._syslog_queue.maxlen is not None, "the syslog hand-over queue must be bounded", p)

    # --- a failing target is recorded, not raised, not retried forever
    conn.execute("UPDATE alert_rules SET cooldown_s = 0")      # the cooldown is covered above
    conn.commit()
    alerts._last_sent.clear()                                  # the clock above was simulated
    alerts._held_back.clear()
    bad = _Capture(fail=True)
    events.emit(conn, did, "config_drift", "warning", "core-r1: drifted again", "")
    conn.commit()
    alerts.process_once(deliver_fn=bad)
    err = conn.execute("SELECT last_error FROM alert_rules WHERE name = 'drift'").fetchone()[0]
    need(err and "target down" in err, "failure not recorded on the rule: %r" % err, p)
    n = len(bad.sent)
    alerts.process_once(deliver_fn=bad)
    need(len(bad.sent) == n, "a failed alert was retried on the next pass", p)

    # --- no write lock held while sending
    import sqlite3
    seen = []

    def probe(target, payload):
        c = sqlite3.connect(m["config"].DB_PATH, timeout=0.2)
        try:
            c.execute("BEGIN IMMEDIATE")
            c.rollback()
            seen.append("free")
        except sqlite3.OperationalError:
            seen.append("LOCKED")
        finally:
            c.close()
    events.emit(conn, did, "config_drift", "warning", "core-r1: drifted third time", "")
    conn.commit()
    alerts.process_once(deliver_fn=probe)
    need(seen == ["free"], "database write lock was held during the send: %r" % seen, p)
    conn.close()
    need("alerts.offer_syslog" in read("topo/app/syslog_server.py"), "syslog receiver no longer hands messages to alerts", p)
    return p


@check("R47", "alert delivery: webhook JSON, mail, retry once, target never returned in full")
def _():
    p = []
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    m, conn = fresh_db()
    from app import alerts
    got = []

    class H(BaseHTTPRequestHandler):
        code = 200

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            got.append((self.path, self.headers.get("Content-Type"), json.loads(body)))
            self.send_response(H.code)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/hook/SECRETTOKEN" % srv.server_address[1]
    try:
        payload = alerts.make_payload("warning", "ping_down", "r1", "r1: not answering ping", "2 failed")
        alerts.deliver("webhook:" + url, payload)
        need(len(got) == 1 and got[0][0] == "/hook/SECRETTOKEN" and "json" in (got[0][1] or ""), "webhook request: %r" % got, p)
        if got:
            need(all(k in got[0][2] for k in ("text", "title", "severity", "kind", "device", "at")), "webhook JSON keys: %r" % sorted(got[0][2]), p)
        H.code = 500
        try:
            alerts.deliver("webhook:" + url, payload)
            p.append("an HTTP 500 from the webhook was treated as success")
        except RuntimeError:
            pass
        n = len(got)
        real_sleep, alerts.time.sleep = alerts.time.sleep, lambda s: None
        try:
            alerts.deliver_with_retry("webhook:" + url, payload)
        except RuntimeError:
            pass
        finally:
            alerts.time.sleep = real_sleep
        need(len(got) - n == 2, "retry sent %d requests, expected exactly 2" % (len(got) - n), p)
    finally:
        srv.shutdown()

    # mail: smtplib replaced by a recorder
    config = m["config"]
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["conn"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, u, pw):
            sent["login"] = (u, pw)

        def send_message(self, msg):
            sent["msg"] = msg
    real = alerts.smtplib.SMTP
    alerts.smtplib.SMTP = FakeSMTP
    old = (config.SMTP_HOST, config.SMTP_TLS, config.SMTP_USER, config.SMTP_PASSWORD)
    try:
        config.SMTP_HOST, config.SMTP_TLS, config.SMTP_USER, config.SMTP_PASSWORD = "", False, "", ""
        try:
            alerts.deliver("mail:ops@example.net", payload)
            p.append("mail without TOPO_SMTP_HOST did not fail")
        except RuntimeError as exc:
            need("SMTP" in str(exc), "unhelpful error: %s" % exc, p)
        config.SMTP_HOST, config.SMTP_TLS, config.SMTP_USER, config.SMTP_PASSWORD = "smtp.invalid", True, "u", "pw"
        alerts.deliver("mail:ops@example.net", payload)
        msg = sent.get("msg")
        need(msg is not None and msg["To"] == "ops@example.net" and "ping_down" in msg["Subject"], "mail headers: %r" % (dict(msg) if msg else None), p)
        need(sent.get("tls") and sent.get("login") == ("u", "pw"), "TLS / login not used: %r" % sent, p)
    finally:
        alerts.smtplib.SMTP = real
        config.SMTP_HOST, config.SMTP_TLS, config.SMTP_USER, config.SMTP_PASSWORD = old

    # API: validation, masking, partial update
    c = m["appmod"].app.test_client()
    good = {"name": "n", "source": "event", "target": "webhook:" + url}
    r = c.post("/api/alert-rules", json=good)
    need(r.status_code == 201 and "SECRETTOKEN" not in r.get_data(as_text=True), "create / the token leaked in the response", p)
    rid = r.get_json()["id"]
    need("SECRETTOKEN" not in c.get("/api/alert-rules").get_data(as_text=True), "the webhook token leaked in the list", p)
    for bad in ({"target": "ftp://x"}, {"target": "mail:nope"}, {"pattern": "("}, {"cooldown_s": "-1"},
                {"source": "other"}, {"min_severity": "loud"}, {"syslog_max_severity": 9}, {"name": " "}):
        code = c.post("/api/alert-rules", json=dict(good, **bad)).status_code
        need(code == 400, "invalid %r accepted (%s)" % (bad, code), p)
    need(c.put("/api/alert-rules/%d" % rid, json={"enabled": False}).get_json()["enabled"] == 0, "PUT enabled", p)
    stored = m["db"].connect().execute("SELECT target FROM alert_rules WHERE id = ?", (rid,)).fetchone()[0]
    need(stored == "webhook:" + url, "a PUT without target changed the stored target: %r" % stored, p)
    need(c.post("/api/alert-rules/%d/test" % rid).get_json().get("ok") in (True, False), "test endpoint", p)
    need(c.delete("/api/alert-rules/%d" % rid).status_code == 200 and c.delete("/api/alert-rules/%d" % rid).status_code == 404, "delete", p)
    need(c.get("/alerts").status_code == 200, "alerts page", p)
    return p


@check("R48", "SNMP collector: parses real snmpd output, refuses v3, bad hosts, missing tools")
def _():
    p = []
    app_modules()
    from app.collectors import snmp
    walks = {c: {} for c in (snmp.IF_DESCR, snmp.IF_MTU, snmp.IF_SPEED, snmp.IF_ADMIN, snmp.IF_OPER,
                             snmp.IF_IN_ERRORS, snmp.IF_ALIAS, snmp.IP_IFINDEX, snmp.IP_MASK)}
    for col, f in ((snmp.IF_DESCR, "snmp_ifDescr.txt"), (snmp.IF_ADMIN, "snmp_ifAdminStatus.txt"),
                   (snmp.IF_OPER, "snmp_ifOperStatus.txt"), (snmp.IP_IFINDEX, "snmp_ipAdEntIfIndex.txt"),
                   (snmp.IP_MASK, "snmp_ipAdEntNetMask.txt")):
        walks[col] = snmp.parse_pairs(sample(f))
    rows = {r["name"]: r for r in snmp.parse_interfaces(walks)}
    need(set(rows) == {"lo", "eth0"}, "interfaces parsed: %r" % sorted(rows), p)
    if "lo" in rows:
        need((rows["lo"]["ip"], rows["lo"]["prefix_len"], rows["lo"]["oper_status"]) == ("127.0.0.1", 8, "up"), "lo row: %r" % rows["lo"], p)
    v = snmp.parse_version(snmp.parse_pairs(sample("snmp_sys.txt")))
    need((v["hostname_hint"], v["os_version"]) == ("lab-snmp-1", "15.4(1)S2"), "version: %r" % v, p)

    def errors(device, creds):
        return [e for (_t, ok, _o, e, _p) in snmp.run_tasks(device, creds) if not ok]
    need(all("not supported" in e for e in errors({"mgmt_ip": "192.0.2.1"}, {"snmp_version": "3"})), "v3 must fail with a clear message", p)
    need(all("not an IP" in e for e in errors({"mgmt_ip": "192.0.2.1; id"}, {"snmp_version": "2c"})), "a non-IP host reached the subprocess", p)
    real = snmp.subprocess.run

    def missing(*a, **k):
        raise FileNotFoundError()
    snmp.subprocess.run = missing
    try:
        errs = errors({"mgmt_ip": "192.0.2.1"}, {"snmp_version": "2c", "snmp_community": "x"})
    finally:
        snmp.subprocess.run = real
    need(errs and all("net-snmp-tools" in e for e in errs), "missing snmpwalk should name the apk package: %r" % errs, p)
    return p


@check("R49", "drift check: containment by parent chain, secrets skipped, statuses, events only on a change")
def _():
    p = []
    m, conn = fresh_db()
    from app import drift
    ssh = m["ssh"]
    run = ("version 17.3\nhostname r1\nenable secret 9 $9$abc\n!\ninterface GigabitEthernet1\n"
           " ip address 10.0.0.1 255.255.255.0\n no shutdown\n!\nrouter bgp 65000\n"
           " neighbor 10.0.0.2 remote-as 65001\n address-family ipv4\n  neighbor 10.0.0.2 activate\n"
           " exit-address-family\nend\n")
    tpl = ("hostname r1\nenable secret {{ vars.x }}\n\ninterface GigabitEthernet1\n    ip address 10.0.0.1 255.255.255.0\n"
           "exit\nrouter bgp 65000\n  neighbor 10.0.0.2 remote-as 65001\n  address-family ipv4\n"
           "      neighbor 10.0.0.2 activate\n")
    miss, skipped = drift.compare(tpl, run)
    need(miss == [] and skipped == 1, "matching config reported %r (skipped %r)" % (miss, skipped), p)
    miss, _ = drift.compare(tpl.replace("65001", "65002").replace("10.0.0.1 255", "10.0.0.9 255"), run)
    need({x["line"] for x in miss} == {"ip address 10.0.0.9 255.255.255.0", "neighbor 10.0.0.2 remote-as 65002"},
         "changed values: %r" % miss, p)
    # the same line under a different parent is NOT a match
    miss, _ = drift.compare("interface GigabitEthernet2\n ip address 10.0.0.1 255.255.255.0\n", run)
    need(len(miss) == 2, "a line under the wrong interface counted as present: %r" % miss, p)
    need(drift.compare(tpl, run + "interface Loopback9\n description extra\n")[0] == [], "extra running lines must not be drift", p)
    # `no X` in a template: met when X is absent (a default is never printed); drift when X is present
    nos = "interface GigabitEthernet1\n no shutdown\nno ip http server\n"
    bare = run.replace(" no shutdown\n", "")                    # IOS prints nothing for an enabled interface
    need(drift.compare(nos, bare)[0] == [], "`no shutdown` must be met by the line being absent: %r" % drift.compare(nos, bare)[0], p)
    shut = run.replace(" no shutdown\n", " shutdown\n") + "ip http server\n"
    need({x["line"] for x in drift.compare(nos, shut)[0]} == {"no shutdown", "no ip http server"},
         "`no X` with X present must be drift: %r" % drift.compare(nos, shut)[0], p)

    did = add_device(conn, "192.0.2.1", "r1")
    need(drift.evaluate(conn, did)["status"] == "no_template", "no template -> %r" % drift.evaluate(conn, did)["status"], p)
    conn.execute("INSERT INTO templates (name, body, updated_at) VALUES ('t', ?, ?)", (tpl, m["db"].sqlite_now()))
    conn.execute("UPDATE devices SET template_name = 't' WHERE id = ?", (did,))
    need(drift.evaluate(conn, did)["status"] == "no_backup", "no backup -> %r" % drift.evaluate(conn, did)["status"], p)
    conn.execute("INSERT INTO config_versions (device_id, sha256, body, captured_at) VALUES (?, 'x', ?, ?)",
                 (did, run, m["db"].sqlite_now()))
    need(drift.evaluate(conn, did)["status"] == "render_error", "an unset template variable must be a render_error", p)
    conn.execute("INSERT INTO template_vars (device_id, key, value) VALUES (?, 'x', 'whatever')", (did,))
    need(drift.evaluate(conn, did)["status"] == "compliant", "compliant expected: %r" % drift.evaluate(conn, did), p)

    def kinds():
        return [r["kind"] for r in conn.execute("SELECT kind FROM events WHERE kind LIKE 'config_%' AND kind != 'config_changed' ORDER BY id")]
    drift.refresh(conn, did)                                    # baseline
    need(kinds() == [], "baseline evaluation emitted %r" % kinds(), p)
    drifted = run.replace("10.0.0.1 255", "10.0.0.7 255")
    ssh.apply_result(conn, did, "config", drifted)              # a poll sees a changed config
    need(kinds() == ["config_drift"], "compliant -> drifted emitted %r" % kinds(), p)
    ssh.apply_result(conn, did, "config", drifted)
    need(kinds() == ["config_drift"], "an unchanged drifted config emitted again: %r" % kinds(), p)
    ssh.apply_result(conn, did, "config", run)
    need(kinds() == ["config_drift", "config_compliant"], "drifted -> compliant emitted %r" % kinds(), p)
    conn.commit()
    conn.close()
    # saving the template re-evaluates the devices that use it
    client = m["appmod"].app.test_client()
    client.put("/api/templates/t", json={"body": tpl + "banner motd ^hello^\n"})
    status = client.get("/api/devices").get_json()[0]["drift_status"]
    need(status == "drifted", "template save did not refresh drift_status (%r)" % status, p)
    need(client.get("/api/devices/%d/drift" % did).get_json()["status"] == "drifted", "drift API", p)
    return p


@check("R50", "interface error history: sampled on change, resets ignored, 24h growth in the API")
def _():
    p = []
    m, conn = fresh_db()
    ssh = m["ssh"]
    need(ssh.growth([{"x": v} for v in (10, 15, 3, 8)], "x") == 10, "growth() should ignore a counter reset", p)
    did = add_device(conn, "192.0.2.1")

    def intf(crc):
        return [dict(name="Gi1", description="", ip=None, prefix_len=None, network=None, admin_status="up",
                     oper_status="up", speed=None, duplex=None, input_errors=crc, crc_errors=crc, mtu=1500)]

    def samples():
        return conn.execute("SELECT COUNT(*) FROM interface_stats").fetchone()[0]
    ssh.apply_result(conn, did, "interfaces", intf(5))
    ssh.apply_result(conn, did, "interfaces", intf(5))
    need(samples() == 1, "unchanged counters stored %d samples, expected 1" % samples(), p)
    ssh.apply_result(conn, did, "interfaces", intf(9))
    need(samples() == 2, "changed counters not sampled (%d)" % samples(), p)
    conn.commit()
    conn.close()
    c = m["appmod"].app.test_client()
    row = c.get("/api/devices/%d/interfaces" % did).get_json()[0]
    need(row["crc_errors_24h"] == 4, "crc_errors_24h = %r, expected 4" % row["crc_errors_24h"], p)
    hist = c.get("/api/devices/%d/interface-stats?name=Gi1" % did).get_json()
    need([h["crc_errors"] for h in hist] == [5, 9], "history: %r" % hist, p)
    return p


# ============================================================ live tier

@check("R32", "routes: real-output fixtures parse, an empty table deletes nothing, one summary event per changed poll, none on baseline")
def _():
    p = []
    m, conn = fresh_db()
    from app.parsers import ios, panos
    ssh = m["ssh"]
    a = ios.parse_routes(read(SAMPLES + "ios_route_17_3.txt"))
    need(len(a) == 12 and {r["vrf"] for r in a} == {"default", "MGMT"}, "ios_route_17_3: %d rows, vrfs %r" % (len(a), sorted({r["vrf"] for r in a})), p)
    need(any(r["prefix"] == "203.0.113.0/25" and r["protocol"] == "ospf" and r["age"] == "2w5d" for r in a),
         "a route under 'is subnetted' lost its mask or fields", p)
    need(any(r["protocol"] == "bgp" and r["interface"] == "" and r["next_hop"] == "192.0.2.81" for r in a), "a BGP route without an interface did not parse", p)
    b = ios.parse_routes(read(SAMPLES + "ios_route_not_captured.txt"))
    need(len(b) == 4 and sorted(r["next_hop"] for r in b)[:2] == ["", "192.0.2.1"], "ECMP / long-prefix / summary lines: %d rows" % len(b), p)
    rib, fib = panos.parse_routes(read(SAMPLES + "panos_route.xml")), panos.parse_fib(read(SAMPLES + "panos_fib.xml"))
    need(len(rib) == 6 and {r["vrf"] for r in rib} == {"default", "vr-b"}, "panos_route: %d rows (multicast and IPv6 must be skipped)" % len(rib), p)
    need(len(fib) == 4 and all(r["kind"] == "fib" for r in fib), "panos_fib: %d rows" % len(fib), p)
    need(panos.parse_routes("") == [] and panos.parse_fib("<x/>") == [] and ios.parse_routes("") == [], "an empty answer must parse to []", p)

    did = add_device(conn, "192.0.2.1", "rt")
    count = lambda kind=None: conn.execute("SELECT COUNT(*) FROM routes WHERE device_id=?" + (" AND kind='%s'" % kind if kind else ""), (did,)).fetchone()[0]
    events = lambda: conn.execute("SELECT subject FROM events WHERE kind='routes_changed'").fetchall()
    def stale():
        # Every apply here lands in the same second, which is the stamp a stored row carries; backdate them so
        # "not seen in this poll" is true, as it is between two real polls.
        conn.execute("UPDATE routes SET last_seen = '2000-01-01 00:00:00'")
    def poll(rows):
        stale()
        ssh.apply_result(conn, did, "routes", rows)
        conn.execute("INSERT INTO poll_history (device_id, transport, task, ok, started_at, received_at) VALUES (?, 'ssh', 'routes', 1, '', '')", (did,))
        conn.commit()
    ssh.apply_result(conn, did, "routes", a + fib)    # first poll: no poll_history row yet = baseline
    conn.commit()
    need(count() == 16 and not events(), "first poll: %d rows, %d events (baseline must be silent)" % (count(), len(events())), p)
    poll(a + fib)
    need(not events(), "an unchanged table emitted an event", p)
    poll(a[:-1] + fib)
    ev = events()
    need(len(ev) == 1 and "+0 -1" in ev[0][0], "one removed prefix must give exactly one 'routes +0 -1' event, got %r" % ev, p)
    stale()
    ssh.apply_result(conn, did, "routes", []); conn.commit()
    need(count("rib") == 11, "an empty routes answer deleted rows: %d left" % count("rib"), p)
    stale()
    ssh.apply_result(conn, did, "routes", a); conn.commit()   # RIB only, as when the FIB could not be read
    need(count("fib") == 4, "a poll without FIB rows wiped the stored FIB", p)
    return p


@check("R33", "optional access token: off by default; when set, pages redirect to /login, API and /metrics give 401, health and static stay open, bearer / header / cookie work, no open redirect")
def _():
    p = []
    m, conn = fresh_db()
    conn.close()
    config = m["config"]
    client = m["appmod"].app.test_client()
    need(config.AUTH_TOKEN == "", "TOPO_AUTH_TOKEN must default to empty (no login)", p)
    need(client.get("/api/devices").status_code == 200, "with no token set the API must stay open", p)
    config.AUTH_TOKEN = "regress-token"
    try:
        r = client.get("/devices")
        need(r.status_code == 302 and "/login" in r.headers.get("Location", ""), "a page must redirect to /login: %s" % r.status_code, p)
        for url in ("/api/devices", "/metrics", "/api/topology"):
            need(client.get(url).status_code == 401, "%s must be 401 without the token" % url, p)
        need(client.get("/api/health").status_code == 200, "/api/health must stay open", p)
        need(client.get("/static/theme.css").status_code == 200, "/static must stay open", p)
        need(client.get("/login").status_code == 200, "/login page must render", p)
        need(client.get("/api/devices", headers={"Authorization": "Bearer regress-token"}).status_code == 200, "bearer token rejected", p)
        need(client.get("/api/devices", headers={"X-Topo-Token": "regress-token"}).status_code == 200, "X-Topo-Token rejected", p)
        need(client.get("/api/devices", headers={"Authorization": "Bearer wrong"}).status_code == 401, "a wrong bearer token was accepted", p)
        need(client.get("/api/devices", headers={"Authorization": "Bearer "}).status_code == 401, "an empty bearer token was accepted", p)
        r = client.post("/login", data={"token": "wrong"})
        need(r.status_code == 401 and "Set-Cookie" not in r.headers, "a wrong login must be 401 with no cookie", p)
        r = client.post("/login", data={"token": "regress-token", "next": "//evil.example"})
        cookie = r.headers.get("Set-Cookie", "")
        need(r.status_code == 302 and r.headers.get("Location") == "/", "login must not redirect off-site: %s" % r.headers.get("Location"), p)
        need("HttpOnly" in cookie and "SameSite=Strict" in cookie, "the cookie must be HttpOnly and SameSite=Strict: %s" % cookie, p)
        need("regress-token" not in cookie, "the cookie must not hold the token itself", p)
        need(client.get("/api/devices").status_code == 200, "the cookie from /login did not open the API", p)
        client.get("/logout")
        need(client.get("/api/devices").status_code == 401, "/logout did not close access", p)
    finally:
        config.AUTH_TOKEN = ""
    return p


@check("R34", "vlans, trunks, stp and mac (switches only): parse the real output, a router is never asked, an empty VLAN list deletes nothing, a \"%\" answer fails the task, a real list replaces the old one")
def _():
    p = []
    m, conn = fresh_db()
    from app.parsers import ios
    ssh = m["ssh"]
    text = read(SAMPLES + "ios_vlan_brief.txt")
    v = ios.parse_vlans(text)
    need(len(v) == 11 and v[0]["vlan_id"] == 1 and v[0]["ports"].startswith("Gi0/1, Gi0/2"), "ios_vlan_brief: %d rows, first %r" % (len(v), v[:1]), p)
    need([x["status"] for x in v if x["vlan_id"] == 1002] == ["act/unsup"], "act/unsup status lost", p)
    cont = ios.parse_vlans(text + "                                                Gi0/11, Gi0/12\n")   # written from IOS's documented format
    need(cont[-1]["ports"] == "Gi0/11, Gi0/12", "a continuation line must add to the row above: %r" % cont[-1], p)
    need({"vlans", "trunks", "stp", "mac"} <= set(ssh.TASKS), "vlans / trunks / stp / mac missing from the IOS tasks", p)
    mac = ios.parse_mac(read(SAMPLES + "ios_mac_table.txt"))
    need(len(mac) == 17 and mac[0] == {"vlan": "All", "mac": "0100.0ccc.cccc", "type": "static", "port": "CPU"}
         and mac[3] == {"vlan": "1", "mac": "0200.f9ba.effa", "type": "dynamic", "port": "Gi0/1"}, "ios_mac_table: %d rows, %r" % (len(mac), mac[:1]), p)
    need(ios.parse_mac("% Invalid input") == [], "an error answer must parse to nothing", p)
    stp = ios.parse_stp(read(SAMPLES + "ios_stp_summary.txt"))
    need(stp == [{"mode": "pvst", "root_for": "1,10-12,15,100,156,170,200", "blocking_vlans": "11", "instances": 6}],
         "ios_stp_summary: %r" % (stp,), p)
    need(ios.parse_stp("% Invalid input") == [], "an answer with no mode line must parse to nothing", p)
    tr = ios.parse_trunks(read(SAMPLES + "ios_trunk.txt"))
    need([(t["port"], t["native_vlan"], t["status"]) for t in tr] == [("Gi0/3", 253, "trunking"), ("Gi0/7", 1, "trunking"), ("Gi0/8", 253, "trunking")],
         "ios_trunk: %r" % [(t["port"], t["native_vlan"]) for t in tr], p)
    need(tr[0]["allowed"] == "1-4094" and tr[0]["active"] == "1,10-12,15,100,3961-3963", "trunk VLAN lists: %r %r" % (tr[0]["allowed"], tr[0]["active"]), p)
    wrapped = ios.parse_trunks(read(SAMPLES + "ios_trunk.txt").replace("1,10-12,15,100,3961-3963", "1,10-12,15,\n            100,3961-3963", 1))
    need(wrapped[0]["active"] == "1,10-12,15,100,3961-3963", "a wrapped VLAN list must be joined: %r" % wrapped[0]["active"], p)
    sw = {"role": "router", "model": "WS-C3560CG-8TC-S", "hostname": "sw1", "platform": "cisco_ios"}
    rt = {"role": "router", "model": "CSR1000V", "hostname": "csr1", "platform": "cisco_ios"}
    need(ssh._is_switch(sw) and not ssh._is_switch(rt), "only a switch may be asked for VLANs", p)
    asked = []
    class _Conn:
        def send_command(self, cmd, **kw):
            asked.append(cmd)
            return ""   # an empty answer
        def disconnect(self):
            pass
        def check_enable_mode(self):
            return True
    real = ssh.ConnectHandler
    ssh.ConnectHandler = lambda **kw: _Conn()
    try:
        for dev in (sw, rt):
            asked.clear()
            list(ssh.run_tasks(dict(dev, mgmt_ip="192.0.2.9"), {"username": "u", "password": "p", "secret": ""}))
            need(all((c in asked) == (dev is sw) for c in ("show vlan brief", "show interfaces trunk", "show spanning-tree summary", "show mac address-table")),
                 "switch-only commands asked=%r for %s" % (asked, dev["model"]), p)
    finally:
        ssh.ConnectHandler = real
    did = add_device(conn, "192.0.2.9", "sw1")
    ssh.apply_result(conn, did, "vlans", v)
    conn.commit()
    n = lambda: conn.execute("SELECT COUNT(*) FROM vlans WHERE device_id = ?", (did,)).fetchone()[0]
    need(n() == 11, "stored %d vlans" % n(), p)
    ssh.apply_result(conn, did, "vlans", [])
    conn.commit()
    need(n() == 11, "an empty answer must delete nothing", p)
    import time
    time.sleep(1.1)
    ssh.apply_result(conn, did, "vlans", [x for x in v if x["vlan_id"] != 12])
    conn.commit()
    need(n() == 10, "a real list must replace the old one (VLAN 12 should be gone): %d" % n(), p)
    r = m["appmod"].app.test_client().get("/api/vlans?device_id=%d" % did)
    need(r.status_code == 200 and len(r.get_json()) == 10, "GET /api/vlans: %s" % r.status_code, p)
    need(m["appmod"].app.test_client().get("/api/vlans").status_code == 400, "/api/vlans needs device_id", p)
    # trunks: stored, replaced, and an empty list (a switch with no trunks) is a real answer that clears them
    ssh.apply_result(conn, did, "trunks", tr)
    conn.commit()
    nt = lambda: conn.execute("SELECT COUNT(*) FROM trunks WHERE device_id = ?", (did,)).fetchone()[0]
    need(nt() == 3, "stored %d trunks" % nt(), p)
    time.sleep(1.1)
    ssh.apply_result(conn, did, "trunks", tr[:2])
    conn.commit()
    need(nt() == 2, "a real trunk list must replace the old one: %d" % nt(), p)
    r = m["appmod"].app.test_client().get("/api/trunks?device_id=%d" % did)
    need(r.status_code == 200 and len(r.get_json()) == 2, "GET /api/trunks: %s" % r.status_code, p)
    # a "%" answer must fail the task (so nothing is deleted), not come back as an empty list
    class _Err(_Conn):
        def send_command(self, cmd, **kw):
            return "% Invalid input detected at the '^' marker."
    ssh.ConnectHandler = lambda **kw: _Err()
    try:
        res = {t: ok for t, ok, *_ in ssh.run_tasks(dict(sw, mgmt_ip="192.0.2.9"), {"username": "u", "password": "p", "secret": ""})}
    finally:
        ssh.ConnectHandler = real
    need(res.get("vlans") is False and res.get("trunks") is False and res.get("stp") is False and res.get("mac") is False, "an error answer must fail vlans, trunks, stp and mac: %r" % res, p)
    # stp: stored; a change of root VLANs or blocking VLANs is one event each, none on the first poll or when nothing changed
    ev = lambda: conn.execute("SELECT subject, detail FROM events WHERE device_id = ? AND kind = 'stp_changed' ORDER BY id", (did,)).fetchall()
    ssh.apply_result(conn, did, "stp", stp)
    conn.commit()
    need(conn.execute("SELECT blocking_vlans FROM stp WHERE device_id = ?", (did,)).fetchone()[0] == "11", "stp row not stored", p)
    need(ev() == [], "the first stp poll must emit nothing: %r" % [tuple(e) for e in ev()], p)
    ssh.apply_result(conn, did, "stp", [])
    need(conn.execute("SELECT COUNT(*) FROM stp WHERE device_id = ?", (did,)).fetchone()[0] == 1, "an empty stp parse must change nothing", p)
    conn.execute("INSERT INTO poll_history (device_id, transport, task, ok, started_at, received_at) VALUES (?, 'ssh', 'stp', 1, datetime('now'), datetime('now'))", (did,))
    ssh.apply_result(conn, did, "stp", stp)
    need(ev() == [], "an unchanged stp answer must emit nothing", p)
    ssh.apply_result(conn, did, "stp", [dict(stp[0], blocking_vlans="", root_for="1,10")])
    conn.commit()
    got = [tuple(e) for e in ev()]
    need(len(got) == 2 and got[0][1] == "1,10-12,15,100,156,170,200 -> 1,10" and got[1][1] == "11 -> none", "stp change events: %r" % (got,), p)
    r = m["appmod"].app.test_client().get("/api/stp?device_id=%d" % did)
    need(r.status_code == 200 and r.get_json().get("root_for") == "1,10", "GET /api/stp: %s %r" % (r.status_code, r.get_json()), p)
    # mac: only the dynamic rows are stored; an empty parse deletes nothing; a real answer replaces the old rows
    nm = lambda: conn.execute("SELECT COUNT(*) FROM mac_table WHERE device_id = ?", (did,)).fetchone()[0]
    ssh.apply_result(conn, did, "mac", mac)
    conn.commit()
    need(nm() == 14, "mac: only the 14 dynamic rows must be stored: %d" % nm(), p)
    time.sleep(1.1)   # a later second than the rows above, so a wrongful delete would show
    ssh.apply_result(conn, did, "mac", [])
    need(nm() == 14, "an empty mac parse must delete nothing: %d" % nm(), p)
    time.sleep(1.1)
    ssh.apply_result(conn, did, "mac", mac[:5])
    conn.commit()
    need(nm() == 2, "a real mac answer must replace the old rows: %d" % nm(), p)
    c = m["appmod"].app.test_client()
    need(len(c.get("/api/mac?device_id=%d" % did).get_json()) == 2 and len(c.get("/api/mac?device_id=%d&q=gi0/5" % did).get_json()) == 1
         and c.get("/api/mac").status_code == 400, "GET /api/mac and its filter", p)
    # a learned MAC that is an interface address on exactly one other device names that device; two owners name none
    hw = ios.parse_interfaces("GigabitEthernet1 is up, line protocol is up" + chr(10) + "  Hardware is CSR vNIC, address is 0200.F9BA.EFFA (bia 0200.f9ba.effa)" + chr(10) + "  MTU 1500 bytes" + chr(10))
    need(hw and hw[0]["mac"] == "0200.f9ba.effa", "parse_interfaces must read the hardware address: %r" % (hw,), p)
    other = add_device(conn, "192.0.2.77", "mac-owner")
    ssh.apply_result(conn, other, "interfaces", hw)
    conn.commit()
    row = [r for r in c.get("/api/mac?device_id=%d" % did).get_json() if r["mac"] == "0200.f9ba.effa"]
    need(row and row[0]["device_id"] == other and row[0]["interface"] == "GigabitEthernet1", "a MAC owned by one device must name it: %r" % (row,), p)
    twin = add_device(conn, "192.0.2.78", "mac-twin")
    ssh.apply_result(conn, twin, "interfaces", hw)
    conn.commit()
    row = [r for r in c.get("/api/mac?device_id=%d" % did).get_json() if r["mac"] == "0200.f9ba.effa"]
    need(row and row[0]["device_id"] is None, "a MAC on two devices must name none: %r" % (row,), p)
    return p


@check("R35", "topology history: a snapshot only when the map's shape changed, none with the interval at 0, old ones pruned, read-only API, no secrets in it")
def _():
    p = []
    m, conn = fresh_db()
    from app import history
    config = m["config"]
    need(config.SNAPSHOT_INTERVAL_S > 0 and config.SNAPSHOT_RETENTION_DAYS > 0, "history must be on by default", p)
    need(history.maybe_snapshot(conn, force=True) is None, "an empty map must not be stored", p)
    did = add_device(conn, "192.0.2.10", "h1")
    conn.execute("INSERT INTO credentials (device_id, username, password, updated_at) VALUES (?, 'u', 'SECRET-PW-123', datetime('now'))", (did,))
    conn.commit()
    first = history.maybe_snapshot(conn, force=True)
    conn.commit()
    need(first is not None, "the first map must be stored", p)
    need(history.maybe_snapshot(conn, force=True) is None, "an unchanged map must not add a snapshot", p)
    add_device(conn, "192.0.2.11", "h2")
    second = history.maybe_snapshot(conn, force=True)
    conn.commit()
    need(second is not None and second != first, "a new device must add a snapshot", p)
    old = config.SNAPSHOT_INTERVAL_S
    config.SNAPSHOT_INTERVAL_S = 0
    try:
        add_device(conn, "192.0.2.12", "h3")
        need(history.maybe_snapshot(conn) is None, "with the interval at 0 nothing may be stored", p)
    finally:
        config.SNAPSHOT_INTERVAL_S = old
    need("SECRET-PW-123" not in " ".join(r[0] for r in conn.execute("SELECT data FROM topology_snapshots")), "a snapshot must not hold a credential", p)
    client = m["appmod"].app.test_client()
    lst = client.get("/api/topology/snapshots").get_json()
    need([x["id"] for x in lst] == [second, first], "snapshot list (newest first): %r" % (lst,), p)
    one = client.get("/api/topology/snapshots/%d" % first).get_json()
    need(len(one["nodes"]) == 1 and one.get("taken_at"), "GET snapshot: %r" % (one,), p)
    need(client.get("/api/topology/snapshots/99999").status_code == 404, "a missing snapshot must be 404", p)
    need(client.post("/api/topology/snapshots").status_code == 405 and client.delete("/api/topology/snapshots/%d" % first).status_code == 405,
         "snapshots must be read-only over the API", p)
    conn.execute("UPDATE topology_snapshots SET taken_at = datetime('now', '-400 days') WHERE id = ?", (first,))
    history.prune(conn)
    conn.commit()
    need([r[0] for r in conn.execute("SELECT id FROM topology_snapshots")] == [second], "an old snapshot must be pruned, a new one kept", p)
    return p


@check("R36", "other Confetti projects' URLs: http(s) only, all-or-nothing, empty clears, partial update, no secret, header shortcut file safe")
def _():
    p = []
    m, conn = fresh_db()
    client = m["appmod"].app.test_client()
    url = "/api/settings/links"
    need(client.get(url).get_json() == {"butler": "", "traffic": ""}, "nothing set yet: %r" % client.get(url).get_json(), p)
    r = client.put(url, json={"butler": " http://butler.example:8080/ "})
    need(r.status_code == 200 and r.get_json() == {"butler": "http://butler.example:8080", "traffic": ""},
         "a good URL is trimmed, has no trailing slash and leaves the other field: %s %r" % (r.status_code, r.get_json()), p)
    r = client.put(url, json={"traffic": "https://hub.example"})
    need(r.get_json() == {"butler": "http://butler.example:8080", "traffic": "https://hub.example"}, "a partial PUT must keep the other field: %r" % r.get_json(), p)
    before = client.get(url).get_json()
    bad = ["javascript:alert(1)", "ftp://x.example", "http://", "x.example", "http://u:p@h.example", "http://a b.example",
           "http://h.example/\"x", "http://h.example:99999", "http://" + "a" * 300, 123, None, ["http://h.example"]]
    for v in bad:
        r = client.put(url, json={"butler": v})
        need(r.status_code == 400 and client.get(url).get_json() == before,
             "%r must be refused (400) and change nothing: %s" % (v, r.status_code), p)
    r = client.put(url, json={"butler": "http://other.example", "traffic": "javascript:x"})
    need(r.status_code == 400 and client.get(url).get_json() == before, "one bad value must change neither field (all or nothing)", p)
    for body in ({}, {"unknown": "http://h.example"}, [], "text"):
        need(client.put(url, json=body).status_code == 400, "body %r must be refused" % (body,), p)
    r = client.put(url, json={"butler": ""})
    need(r.get_json() == {"butler": "", "traffic": "https://hub.example"}, "an empty value must clear that field only: %r" % r.get_json(), p)
    need(conn.execute("SELECT COUNT(*) FROM settings WHERE key = 'url_butler'").fetchone()[0] == 0, "an empty value must delete the row", p)
    need(conn.execute("SELECT COUNT(*) FROM settings WHERE key NOT IN ('url_butler', 'url_traffic')").fetchone()[0] == 0, "only url_* rows may be written", p)
    need(client.delete(url).status_code == 405 and client.post(url, json={}).status_code == 405, "only GET and PUT", p)
    page = client.get("/settings").get_data(as_text=True)
    need("Other Confetti projects" in page and 'id="links-form"' in page, "/settings has no Other Confetti projects section", p)
    # The header shortcut: a placeholder in base.html, the shared script, never HTML built from the URL, opens safely.
    base = open("topo/templates/base.html", encoding="utf-8").read()
    need('id="confetti-links"' in base and 'src="/static/confetti-links.js"' in base, "base.html must hold the shortcut span and load confetti-links.js", p)
    js = open("topo/static/confetti-links.js", encoding="utf-8").read()
    need("innerHTML" not in js and "document.write" not in js, "confetti-links.js must not build HTML from the URL", p)
    need("noopener" in js and "_blank" in js and "https?" in js, "confetti-links.js must open in a new tab with noopener and re-check http(s)", p)
    return p


@check("R37", "BGP flap detection: timer formats, silent baseline, a timer reset or a state change counts, 3 in the window = one flapping event, quiet = one stop event, BGP only")
def _():
    p = []
    m, conn = fresh_db()
    from app import flap
    ssh, config = m["ssh"], m["config"]
    for text, want in (("never", 0), ("00:05:12", 312), ("1d02h", 93600), ("3w2d", 1987200), ("1y12w", 38793600), ("junk", None), (None, None)):
        need(flap.uptime_seconds(text) == want, "uptime_seconds(%r) must be %r, got %r" % (text, want, flap.uptime_seconds(text)), p)
    need(config.FLAP_COUNT == 3 and config.FLAP_WINDOW_S == 3600, "the defaults must be 3 changes in 3600 s", p)
    did = add_device(conn, "192.0.2.10", "h1")
    peer = "198.51.100.1"

    def poll(state, up, proto="bgp"):
        row = {"peer_ip": peer, "state": state, "uptime": up, "remote_as": 65001, "local_as": 65000, "prefixes": 1}
        if proto == "ospf":
            row = {"peer_ip": peer, "state": state, "local_if": "Gi1"}
        ssh.apply_result(conn, did, proto, [row])
        conn.execute("INSERT INTO poll_history (device_id, transport, task, ok, started_at, received_at) VALUES (?, 'ssh', ?, 1, '', '')", (did, proto))
        conn.commit()
    kinds = lambda: [r[0] for r in conn.execute("SELECT kind FROM events ORDER BY id")]
    flapping = lambda: conn.execute("SELECT flapping FROM adjacencies WHERE proto = 'bgp'").fetchone()[0]

    poll("Established", "01:00:00")                      # first poll: baseline, silent
    need(not kinds() and not conn.execute("SELECT COUNT(*) FROM bgp_changes").fetchone()[0], "the first poll must record nothing: %r" % kinds(), p)
    poll("Established", "01:05:00")                      # the timer grows: no change
    need(not conn.execute("SELECT COUNT(*) FROM bgp_changes").fetchone()[0], "a growing timer is not a change", p)
    poll("Established", "00:01:00")                      # the timer went back: it dropped and came back between two polls
    need(kinds() == ["bgp_reset"] and not flapping(), "a timer reset must give one bgp_reset and not flapping yet: %r" % kinds(), p)
    poll("Idle", "00:00:10")                             # change 2
    need(not flapping(), "two changes must not be flapping", p)
    poll("Active", "00:00:20")                           # Idle -> Active is a peer that is down, not a flap
    need(conn.execute("SELECT COUNT(*) FROM bgp_changes").fetchone()[0] == 2, "a change between two states that are not Established must not count", p)
    poll("Established", "00:00:30")                      # change 3
    need(flapping() == 1 and kinds().count("bgp_flapping") == 1, "three changes must give flapping=1 and exactly one bgp_flapping: %r" % kinds(), p)
    poll("Established", "00:05:30")
    need(kinds().count("bgp_flapping") == 1, "a flapping peer must not emit bgp_flapping again", p)
    conn.execute("UPDATE bgp_changes SET at = datetime('now', '-2 hours')")
    conn.commit()
    poll("Established", "00:10:30")
    need(flapping() == 0 and kinds().count("bgp_flap_stopped") == 1, "a quiet window must clear flapping with one bgp_flap_stopped: %r" % kinds(), p)
    # OSPF never records, and a flap check never talks to a device.
    poll("FULL", None, "ospf")
    poll("EXSTART", None, "ospf")
    poll("FULL", None, "ospf")
    need(conn.execute("SELECT COUNT(*) FROM adjacencies WHERE proto = 'ospf' AND flapping = 1").fetchone()[0] == 0, "OSPF must never be flapping", p)
    need(conn.execute("SELECT COUNT(*) FROM bgp_changes WHERE peer_ip = ? AND at > datetime('now', '-1 hour')", (peer,)).fetchone()[0] == 0, "OSPF changes must not be recorded", p)
    src = read("topo/app/flap.py")
    need("send_command" not in src and "ConnectHandler" not in src, "flap.py must not talk to a device", p)
    need(len(grep(r"flap\.prune", "topo/app/poller.py")) == 1, "the poller tick must prune bgp_changes", p)
    return p


@check("R38", "Default and Connected layers: a default route points at the unique owner of its next hop (else a ghost, never itself), a shared subnet is one line or a subnet node, the management subnet and /32 are skipped, nothing stored")
def _():
    p = []
    m, conn = fresh_db()
    from app import topology
    a = add_device(conn, "192.0.2.1", "a")
    b = add_device(conn, "192.0.2.2", "b")
    c = add_device(conn, "192.0.2.3", "c")
    now = "2026-01-01 00:00:00"

    def iface(dev, name, ip, net, plen, oper="up"):
        conn.execute("INSERT INTO interfaces (device_id, name, ip, prefix_len, network, oper_status, last_seen) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (dev, name, ip, plen, net, oper, now))

    def route(dev, nh, vrf="default", kind="rib", prefix="0.0.0.0/0"):
        conn.execute("INSERT INTO routes (device_id, vrf, kind, prefix, protocol, next_hop, interface, last_seen) "
                     "VALUES (?, ?, ?, ?, 'static', ?, '', ?)", (dev, vrf, kind, prefix, nh, now))

    iface(b, "Gi1", "10.1.1.2", "10.1.1.0/30", 30)        # a's next hop: only b owns it
    iface(b, "Gi9", "10.9.9.9", "10.9.9.0/24", 24)        # an address on two devices stays a ghost
    iface(c, "Gi9", "10.9.9.9", "10.9.9.0/24", 24)
    iface(a, "Gi0", "10.0.0.1", "10.0.0.0/24", 24)        # a's own address
    route(a, "10.1.1.2")                                  # -> b
    route(a, "10.77.0.1")                                 # -> ghost
    route(a, "10.0.0.1")                                  # its own address -> ghost, not a self-loop
    route(a, "10.9.9.9")                                  # two owners -> ghost
    route(c, "10.1.1.2", kind="fib")                      # RIB only
    route(c, "10.1.1.2", prefix="10.5.0.0/16")            # not a default route
    # Connected: a /30 shared by a and b, a /24 shared by all three, a /32 pair, the management subnet.
    iface(a, "Gi1", "10.1.1.1", "10.1.1.0/30", 30)
    for dev, n in ((a, 1), (b, 2), (c, 3)):
        iface(dev, "Gi5", "10.5.5.%d" % n, "10.5.5.0/24", 24)
        iface(dev, "Lo9", "10.255.9.%d" % n, "10.255.9.%d/32" % n, 32)
        iface(dev, "Mgmt", "192.0.2.%d" % n, "192.0.2.0/24", 24)
    iface(c, "Gi7", "10.7.7.1", "10.7.7.0/24", 24)        # a subnet with one device
    conn.commit()
    before = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("device_aliases", "routes", "interfaces")}
    g = topology.build(conn)
    dflt = [e for e in g["edges"] if e["layer"] == "default"]
    to = sorted(e["to"] for e in dflt)
    need(to == sorted(["dev:%d" % b, "ghost:peer:10.77.0.1", "ghost:peer:10.0.0.1", "ghost:peer:10.9.9.9"]),
         "default edges must go to b and three ghosts (unknown, own address, two owners): %r" % to, p)
    need(all(e["from"] == "dev:%d" % a for e in dflt), "only a has a RIB default route: %r" % [e["from"] for e in dflt], p)
    conn_edges = [e for e in g["edges"] if e["layer"] == "connected"]
    pair = [e for e in conn_edges if e["label"] == "10.1.1.0/30"]
    need(len(pair) == 1 and {pair[0]["from"], pair[0]["to"]} == {"dev:%d" % a, "dev:%d" % b},
         "a /30 shared by two devices must be one direct line: %r" % pair, p)
    hub = [n for n in g["nodes"] if n["id"] == "subnet:10.5.5.0/24"]
    need(len(hub) == 1 and not hub[0]["ghost"], "a /24 shared by three devices must make one subnet node", p)
    need(len([e for e in conn_edges if e["to"] == "subnet:10.5.5.0/24"]) == 3, "the subnet node needs one line from each of the three devices", p)
    need(not [e for e in conn_edges if "192.0.2" in e["label"] or "192.0.2" in e["to"]], "the management subnet must draw nothing", p)
    need(not [n for n in g["nodes"] if "/32" in n["label"]] and not [e for e in conn_edges if "/32" in e["label"]], "a /32 must draw nothing", p)
    need(not [e for e in conn_edges if "10.7.7.0" in e["label"] or "10.7.7.0" in e["to"]], "a subnet with one device must draw nothing", p)
    need(all(e["from"] != e["to"] for e in g["edges"]), "an edge loops back to its own device", p)
    conn.execute("UPDATE interfaces SET oper_status = 'down' WHERE device_id = ? AND name = 'Gi1'", (b,))
    conn.commit()
    pair = [e for e in topology.build(conn)["edges"] if e["layer"] == "connected" and e["label"] == "10.1.1.0/30"]
    need(len(pair) == 1 and pair[0]["up"] is False, "a down port must draw the connected line down", p)
    layers = lambda s: {e["layer"] for e in topology.build(conn, s)["edges"]}
    need(layers("default") == {"default"} and layers("connected") == {"connected"}, "?layer= must limit to the one new layer", p)
    need(not layers("lldp,bgp,ospf"), "?layer=lldp,bgp,ospf must not draw the new layers", p)
    after = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("device_aliases", "routes", "interfaces")}
    need(before == after, "building the map must not store anything (an alias, a route or an interface): %r -> %r" % (before, after), p)
    need("INSERT" not in read("topo/app/topology.py").split("def _connected")[1].split("def _as_numbers")[0], "_connected must not write to the database", p)
    return p


@check("R39", "ruff F rules: no undefined names, unused imports or variables, redefinitions (topo/, dev/)")
def _():
    # Not a constraint check; the next constraint (25) takes R41. ruff is a dev tool
    # (dev/requirements-dev.txt), so a machine without it is NOT RUN, never a pass.
    ruff = [shutil.which("ruff")] if shutil.which("ruff") else [sys.executable, "-m", "ruff"]
    if subprocess.run(ruff + ["--version"], capture_output=True).returncode != 0:
        raise Skip("ruff is not installed (pip install -r dev/requirements-dev.txt)")
    r = subprocess.run(ruff + ["check", "--config", "ruff.toml", "--output-format", "concise", "topo", "dev"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return []
    return [l for l in r.stdout.splitlines() if l.strip() and not l.startswith(("Found", "[*]"))] or [r.stderr.strip()[:200]]


SYSLOG_CORE = "topo/app/syslog_core.py"
SYSLOG_CASES = "dev/samples/syslog_cases.json"
SYSLOG_PULL = "topo/app/syslog_pull.py"
# The other two repos: (repo directory, the directory in it that holds syslog_core.py)
SYSLOG_SIBLINGS = (("../confetti-butler", "butler/app"), ("../confetti-traffic", "hub/app"))


def _syslog_core():
    spec = importlib.util.spec_from_file_location("syslog_core", SYSLOG_CORE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@check("R40", "syslog_core: every fixture line parses as expected, a copy pulled from another server is stored once, two local repeats are both kept")
def _():
    # syslog_core.py is the SAME file in confetti-butler, confetti-traffic and here. It imports no
    # app code, so this check needs no app packages.
    p = []
    sc = _syslog_core()
    cases = json.loads(read(SYSLOG_CASES))["cases"]
    need(len(cases) >= 9, "dev/samples/syslog_cases.json lost cases", p)
    for c in cases:
        got = sc.parse(c["raw"], c["source_ip"])
        need(got["raw"] == c["raw"] and got["source_ip"] == c["source_ip"], "%s: raw or source_ip changed" % c["name"], p)
        for k, v in c["expect"].items():
            need(got[k] == v, "%s: %s expected %r, got %r" % (c["name"], k, v, got[k]), p)
    for raw in ("", " ", "<>", "<999>x", "<1>1 2026-01-01T00:00:00Z"):
        try:
            sc.parse(raw, "192.0.2.1")
        except Exception as e:
            p.append("parse(%r) raised %r; it must never raise" % (raw, e))

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE syslog (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, "
               "source_ip TEXT NOT NULL, host TEXT, facility INTEGER, severity INTEGER, mnemonic TEXT, "
               "device_time TEXT, message TEXT, raw TEXT)")
    sc.migrate(db)
    sc.migrate(db)          # twice: must be harmless
    cols = [r[1] for r in db.execute("PRAGMA table_info(syslog)")]
    need({"origin", "origin_id", "msg_hash"} <= set(cols), "migrate() did not add the extra columns: %r" % cols, p)
    rec = sc.parse("<189>Nov 18 10:15:02 R1 %LINK-3-UPDOWN: Interface Gi1, changed state to down", "192.0.2.1")
    t0 = "2026-11-18 10:00:00"
    a = sc.insert(db, rec, t0)
    b = sc.insert(db, rec, "2026-11-18 10:00:02")
    need(a and b, "two identical messages this server received itself must both be kept (a real repeat)", p)
    pulled = dict(rec, received_at="2026-11-18T10:00:01Z")
    need(sc.insert(db, pulled, t0, origin="butler", origin_id=7) is None,
         "a copy pulled from another server within the window must not be stored again", p)
    later = dict(rec, received_at="2026-11-18T10:05:00Z")
    need(sc.insert(db, later, t0, origin="butler", origin_id=8) is not None,
         "the same text five minutes later is a new message and must be stored", p)
    need(sc.insert(db, later, t0, origin="butler", origin_id=8) is None,
         "the same (origin, origin_id) twice must store once", p)
    other = sc.parse(rec["raw"], "192.0.2.2")
    need(sc.insert(db, dict(other, received_at="2026-11-18T10:05:01Z"), t0, origin="traffic", origin_id=1) is not None,
         "the same line from a different source IP is a different message", p)
    first_pull = sc.insert(db, dict(rec, received_at="2026-11-18T11:00:00Z"), t0, origin="butler", origin_id=9)
    need(first_pull is not None and sc.insert(db, rec, "2026-11-18 11:00:03") is None,
         "a local copy arriving just after a pulled copy must not be stored again", p)
    need(sc.insert(db, dict(rec, received_at="2026-11-18T11:00:02Z"), t0, origin="traffic", origin_id=3) is None,
         "a copy pulled from a second server must not duplicate one pulled from the first", p)
    n = db.execute("SELECT COUNT(*) FROM syslog").fetchone()[0]
    db.rollback()
    need(db.execute("SELECT COUNT(*) FROM syslog").fetchone()[0] == 0 and n > 0,
         "insert() must not commit (the caller does)", p)
    need(".commit(" not in read(SYSLOG_CORE), "syslog_core.py must never call commit (the caller does)", p)
    need("syslog_core.migrate" in read("topo/app/db.py"), "db.init_db must call syslog_core.migrate", p)
    return p


def _lf_bytes(path):
    with open(path, "rb") as f:
        return f.read().replace(b"\r\n", b"\n")


@check("R40-sync", "syslog_core.py, syslog_pull.py and syslog_cases.json are byte-identical in confetti-butler, confetti-traffic and confetti-topo")
def _():
    p = []
    for repo, appdir in SYSLOG_SIBLINGS:
        if not os.path.isdir(repo):
            raise Skip("%s is not next to this repo, cannot compare" % repo)
        for mine, theirs in ((SYSLOG_CORE, os.path.join(repo, appdir, "syslog_core.py")),
                             (SYSLOG_PULL, os.path.join(repo, appdir, "syslog_pull.py")),
                             (SYSLOG_CASES, os.path.join(repo, "dev/samples/syslog_cases.json"))):
            if not os.path.exists(theirs):
                p.append("%s does not exist" % theirs)
                continue
            need(_lf_bytes(mine) == _lf_bytes(theirs), "%s differs from %s" % (mine, theirs), p)
    return p


@check("R41", "syslog poll trigger: only a topology message from a known, enabled, healthy device asks for an early poll, never touches poll_state, never stored as an alias")
def _():
    # Constraint 25. A spoofed UDP packet must cost at most one extra poll: the trigger only sets
    # devices.poll_requested_at; poller._tick claims it through the normal 'idle' check.
    p = []
    m, conn = fresh_db()
    trig = importlib.import_module("app.syslog_trigger")
    sc = _syslog_core()
    poller = m["poller"]

    def rec(line, ip):
        return sc.parse(line, ip)
    down = "<189>Nov 18 10:15:02 R1 %LINK-3-UPDOWN: Interface GigabitEthernet1, changed state to down"
    a = add_device(conn, "192.0.2.1", "a")
    conn.execute("UPDATE devices SET enabled = 1 WHERE id = ?", (a,))
    conn.commit()

    def requested(did):
        return conn.execute("SELECT poll_requested_at, poll_state, next_poll_at FROM devices WHERE id = ?", (did,)).fetchone()
    before = requested(a)
    trig._last_request.clear()
    need(trig.maybe_request(conn, rec(down, "192.0.2.1"), min_s=30) == a, "a link-down message from the device's management IP did not ask for a poll", p)
    after = requested(a)
    need(after["poll_requested_at"] != "", "poll_requested_at was not set", p)
    need((after["poll_state"], after["next_poll_at"]) == (before["poll_state"], before["next_poll_at"]),
         "the trigger must not touch poll_state or next_poll_at: %r -> %r" % (tuple(before), tuple(after)), p)
    need(trig.maybe_request(conn, rec(down, "192.0.2.1"), min_s=30) is None, "a second message inside the window asked again", p)

    def asks(line, ip, **setup):
        trig._last_request.clear()
        conn.execute("UPDATE devices SET poll_requested_at = '', enabled = ?, role = ?, fail_count = ? WHERE id = ?",
                     (setup.get("enabled", 1), setup.get("role", "router"), setup.get("fail", 0), a))
        conn.commit()
        return trig.maybe_request(conn, rec(line, ip), min_s=30)
    need(asks("<189>Nov 18 10:15:02 R1 %SYS-5-CONFIG_I: Configured from console", "192.0.2.1") is None, "a message that is not a topology change asked for a poll", p)
    need(asks(down, "192.0.2.99") is None, "a message from an unknown address asked for a poll", p)
    need(asks(down, "192.0.2.1", fail=1) is None, "a failing device (backoff) was asked for a poll", p)
    need(asks(down, "192.0.2.1", role="node") is None, "a role='node' device was asked for a poll", p)
    need(asks(down, "192.0.2.1", enabled=0) is None, "a disabled device was asked for a poll", p)
    need(asks("<189>Nov 18 10:15:02 R1 %BGP-5-ADJCHANGE: neighbor 192.0.2.9 Down", "192.0.2.1") == a, "a BGP adjacency message did not ask for a poll", p)
    need(asks("<189>Nov 18 10:15:02 R1 %OSPF-5-ADJCHG: Process 1, Nbr 192.0.2.9 on Gi1 from FULL to DOWN", "192.0.2.1") == a, "an OSPF adjacency message did not ask for a poll", p)

    # source address = an interface address held by exactly one device; never stored as an alias
    b = add_device(conn, "192.0.2.2", "b")
    c2 = add_device(conn, "192.0.2.3", "c")
    conn.execute("UPDATE devices SET enabled = 1 WHERE id IN (?, ?)", (b, c2))
    _put_iface(conn, b, "Lo0", "10.9.9.1")
    _put_iface(conn, b, "Lo1", "10.9.9.2")
    _put_iface(conn, c2, "Lo0", "10.9.9.2")      # held by two devices: ambiguous
    conn.commit()
    aliases = conn.execute("SELECT COUNT(*) FROM device_aliases").fetchone()[0]
    trig._last_request.clear()
    need(trig.maybe_request(conn, rec(down, "10.9.9.1"), min_s=30) == b, "an address held by exactly one device did not resolve", p)
    need(trig.maybe_request(conn, rec(down, "10.9.9.2"), min_s=30) is None, "an address held by two devices was resolved to a guess", p)
    need(conn.execute("SELECT COUNT(*) FROM device_aliases").fetchone()[0] == aliases, "the trigger stored an alias (constraint 12)", p)

    # the poller claims a requested device, clears the request, and skips a failing one
    class FakePool:
        def __init__(self):
            self.ids = []

        def submit(self, fn, did):
            self.ids.append(did)
    conn.execute("UPDATE devices SET poll_requested_at = '', poll_state = 'idle', fail_count = 0, next_poll_at = '2999-01-01 00:00:00'")
    conn.execute("UPDATE devices SET poll_requested_at = '2026-01-01 00:00:00' WHERE id = ?", (a,))
    conn.execute("UPDATE devices SET poll_requested_at = '2026-01-01 00:00:00', fail_count = 2 WHERE id = ?", (b,))
    conn.commit()
    pool = FakePool()
    poller._tick(pool)
    need(pool.ids == [a], "_tick claimed %r, expected only the requested healthy device %r" % (pool.ids, a), p)
    row = requested(a)
    need(row["poll_state"] == "running" and row["poll_requested_at"] == "", "a claimed request must set 'running' and clear the request: %r" % (tuple(row),), p)
    need(requested(b)["poll_state"] == "idle", "a failing device with a request was claimed (backoff bypassed)", p)

    src = read("topo/app/syslog_trigger.py")
    need("poll_state" not in src.split('"""')[2], "syslog_trigger.py must not touch poll_state", p)
    need("allow_reuse_address" not in re.sub(r"#.*", "", read("topo/app/syslog_server.py")), "the syslog listener must not set allow_reuse_address", p)
    need("syslog_core.insert" in read("topo/app/syslog_server.py"), "syslog_server.py must store through syslog_core.insert", p)
    return p


def _syslog_pull_engine(sc, sp, conn, get, put, peer, p, token=None):
    """The pull rules (the syslog pull constraint), against a fake peer server on 127.0.0.1. The same helper is in
    all three regress.py files; the caller passes its own settings get/put and the peer's name."""
    import http.server
    import threading

    state = {"rows": [], "max": 0, "mode": "ok", "seen": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["seen"].append((self.path, self.headers.get("Authorization")))
            if state["mode"] == "500":
                self.send_response(500)
                self.end_headers()
                return
            if state["mode"] == "redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/followed" % self.server.server_address[1])
                self.end_headers()
                return
            after = int(re.search(r"after_id=(\d+)", self.path).group(1))
            def newer(r):
                return not (isinstance(r, dict) and isinstance(r.get("id"), int)) or r["id"] > after
            body = json.dumps([r for r in state["rows"] if newer(r)]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if state["mode"] != "nomax":
                self.send_header("X-Syslog-Max-Id", str(state["max"]))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % server.server_address[1]
    sp.status.clear()

    def row(i, text, ip="192.0.2.1", at="2026-11-18T10:00:00Z"):
        r = sc.parse("<189>Nov 18 10:00:00 R1 %LINK-3-UPDOWN: " + text, ip)
        r.update(id=i, received_at=at, origin="local")
        return r

    def stored():
        return conn.execute("SELECT COUNT(*) FROM syslog WHERE origin = ?", (peer,)).fetchone()[0]

    def pull():
        return sp.pull_server(conn, peer, get, put, dedup_s=10)
    try:
        put(conn, "url_" + peer, url)
        if token:
            put(conn, "syslog_token_" + peer, token)
        conn.commit()
        need(pull() is None and state["seen"] == [], "a server that is not switched on was contacted", p)

        put(conn, "syslog_pull_" + peer, "1")
        conn.commit()
        state["rows"] = [row(i, "old %d" % i) for i in range(1, 6)]
        state["max"] = 5
        need(pull() == 0 and stored() == 0 and get(conn, "syslog_cursor_" + peer) == "5",
             "switching on must start from the newest message and copy no history (cursor %r)" % get(conn, "syslog_cursor_" + peer), p)

        state["rows"] += [row(i, "new %d" % i) for i in (6, 7, 8)]
        state["max"] = 8
        need(pull() == 3 and stored() == 3 and get(conn, "syslog_cursor_" + peer) == "8",
             "the next pull must store the 3 new rows and move the cursor to 8 (stored %d)" % stored(), p)
        path, auth = state["seen"][-1]
        need(path.startswith("/api/syslog?after_id=5&origin=local"), "the pull must ask for after_id=N&origin=local only: %s" % path, p)
        if token:
            need(auth == "Bearer " + token and token not in path, "the token must travel in the Authorization header, never in the URL", p)
        else:
            need(auth is None, "no token is set, but an Authorization header was sent", p)
        got = conn.execute("SELECT origin_id, origin, host, mnemonic FROM syslog WHERE origin = ? ORDER BY origin_id", (peer,)).fetchall()
        need([tuple(g)[:2] for g in got] == [(6, peer), (7, peer), (8, peer)] and got[0][2] == "R1" and got[0][3] == "%LINK-3-UPDOWN",
             "pulled rows must keep origin, origin_id and the parsed fields: %r" % ([tuple(g) for g in got],), p)

        # the same device line, received here and also pulled: stored once
        sc.insert(conn, sc.parse("<189>Nov 18 10:00:00 R1 %LINK-3-UPDOWN: dup", "192.0.2.5"), "2026-11-18 10:00:00")
        conn.commit()
        state["rows"].append(row(9, "dup", ip="192.0.2.5", at="2026-11-18T10:00:03Z"))
        state["max"] = 9
        need(pull() == 0 and stored() == 3 and get(conn, "syslog_cursor_" + peer) == "9",
             "a message this server already has must not be stored again, but the cursor moves on", p)

        # the other server was reset: its newest id is below our cursor
        state["rows"] = [row(1, "after the reset")]
        state["max"] = 1
        need(pull() == 1 and get(conn, "syslog_cursor_" + peer) == "1", "a reset peer must restart the cursor at 0 and pull again", p)

        # a failing peer: nothing stored, cursor kept, the error is shown, other servers are not affected.
        # It has a new row, so a client that did not refuse the answer would store it.
        state["rows"] = [row(1, "after the reset"), row(2, "while failing", at="2026-11-18T10:03:00Z")]
        state["max"] = 2
        for mode in ("500", "nomax", "redirect"):
            state["mode"] = mode
            before = (stored(), get(conn, "syslog_cursor_" + peer))
            sp.status.clear()
            pull()
            need((stored(), get(conn, "syslog_cursor_" + peer)) == before, "mode %s changed the stored rows or the cursor" % mode, p)
            need(sp.status.get(peer, {}).get("last_error"), "mode %s: the error was not recorded" % mode, p)
        state["mode"] = "ok"
        seen_before = len(state["seen"])
        for q in state["seen"]:
            need("/followed" not in q[0], "a redirect was followed", p)

        # junk rows are skipped, a good row after them is stored
        state["rows"] = ["junk", {"id": "7"}, {"id": 10, "source_ip": "192.0.2.1"}, row(11, "good", at="2026-11-18T10:05:00Z")]
        state["max"] = 11
        need(pull() == 1 and get(conn, "syslog_cursor_" + peer) == "11", "rows that are not usable must be skipped, not stop the pull", p)
        need(len(state["seen"]) > seen_before, "the fake peer was not asked", p)

        for bad in ("file:///etc/passwd", "ftp://127.0.0.1/", "http://user:pw@127.0.0.1/", "127.0.0.1"):
            try:
                sc.fetch(bad, 0)
                p.append("fetch() accepted %r; only http(s) addresses without a user name may be fetched" % bad)
            except ValueError:
                pass
    finally:
        server.shutdown()
        server.server_close()


@check("R42", "syslog pull: off until switched on, starts from now, one request shape, skips duplicates, survives a bad or reset peer, never follows redirects or non-http URLs, serves only its own rows")
def _():
    # Constraint 26. Everything another server sends is untrusted text; nothing is stored from a failed request.
    p = []
    m, conn = fresh_db()
    sc = _syslog_core()
    sp = importlib.import_module("app.syslog_pull")
    db = m["db"]
    _syslog_pull_engine(sc, sp, conn, db.get_setting, db.set_setting, "butler", p)
    conn.close()

    m, conn = fresh_db()
    client = m["appmod"].app.test_client()
    url = "/api/settings/syslog-pull"
    state = client.get(url).get_json()
    need(set(state["servers"]) == {"butler", "traffic"} and not any(v["pull"] for v in state["servers"].values()),
         "pulling must be off for every server by default: %r" % state, p)
    r = client.put(url, json={"butler": {"pull": True}})
    need(r.status_code == 400 and conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0,
         "switching a server on without its URL must be a 400 that changes nothing: %s" % r.status_code, p)
    client.put("/api/settings/links", json={"butler": "http://butler.example:8080"})
    r = client.put(url, json={"butler": {"pull": True}})
    need(r.status_code == 200 and r.get_json()["servers"]["butler"]["pull"] is True, "switching on with a URL set failed: %s" % r.status_code, p)
    for bad in ({"butler": True}, {"butler": {"pull": "yes"}}, {"nope": {"pull": True}}, {}, {"butler": {"pull": True, "x": 1}},
                {"butler": {"pull": False}, "traffic": {"pull": True}}, [1], "x"):
        r = client.put(url, json=bad)
        need(r.status_code == 400, "%r was accepted (%s)" % (bad, r.status_code), p)
    need(client.get(url).get_json()["servers"]["butler"]["pull"] is True, "a rejected PUT changed something (all or nothing)", p)
    db.set_setting(conn, "syslog_cursor_butler", "42")
    conn.commit()
    client.put("/api/settings/links", json={"butler": "http://other.example:8080"})
    need(db.get_setting(conn, "syslog_cursor_butler") is None, "a different URL is a different server: its cursor must be forgotten", p)
    db.set_setting(conn, "syslog_cursor_butler", "42")
    conn.commit()
    client.put(url, json={"butler": {"pull": False}})
    need(db.get_setting(conn, "syslog_pull_butler") is None and db.get_setting(conn, "syslog_cursor_butler") is None,
         "switching off must delete the switch and the cursor", p)

    # only this server's own messages are served to a puller: a pulled row is never passed on
    sc.insert(conn, sc.parse("<189>Nov 18 10:00:00 R1 %LINK-3-UPDOWN: mine", "192.0.2.1"), db.sqlite_now())
    sc.insert(conn, dict(sc.parse("<189>Nov 18 10:00:00 R2 %LINK-3-UPDOWN: theirs", "192.0.2.2"), received_at=db.sqlite_now()),
              db.sqlite_now(), origin="butler", origin_id=5)
    conn.commit()
    r = client.get("/api/syslog?after_id=0&origin=local")
    rows = r.get_json()
    need([x["host"] for x in rows] == ["R1"], "origin=local must return only rows this server received: %r" % ([x.get("host") for x in rows],), p)
    need(r.headers.get("X-Syslog-Max-Id", "").isdigit(), "X-Syslog-Max-Id header is missing", p)
    need(client.get("/api/syslog?after_id=x").status_code == 400, "a bad after_id must be a 400", p)
    did = add_device(conn, "192.0.2.1", "r1", "router")
    r = client.get("/api/syslog?minutes=5&device_id=%d" % did)
    need(r.status_code == 200 and [x["host"] for x in r.get_json()] == ["R1"], "the device_id filter must find the device's rows: %s %r" % (r.status_code, r.get_json()), p)
    need(client.get("/api/syslog?device_id=x").status_code == 400, "a bad device_id must be a 400", p)
    r = client.get("/api/syslog?minutes=5&q=mine")
    need(r.status_code == 200 and [x["host"] for x in r.get_json()] == ["R1"], "the q filter must work: %s" % r.status_code, p)
    page = client.get("/api/syslog?minutes=5").get_json()
    need(len(page) == 2 and all("raw" not in x and "msg_hash" not in x for x in page) and {x["origin"] for x in page} == {"local", "butler"},
         "the page API must show both rows with their origin and without raw or msg_hash", p)

    src = read("topo/app/syslog_core.py")
    need("urlopen(" not in src.replace("opener.open(", "") and "Authorization" in src, "the pull must use the no-redirect opener, with the token in a header", p)
    need(re.search(r"token[^\n]*\{", src.split("def fetch")[1].split("def pull_once")[0].split("req = ")[0]) is None, "the token must not be built into the URL", p)
    return p


def run_live():
    """Start the real server (serve.py) from a temp copy on free ports and
    drive it over HTTP and UDP."""
    import json
    import urllib.request
    desc = LIVE[0][1]
    work = os.path.join(TMP, "live")
    shutil.copytree("topo", work, ignore=shutil.ignore_patterns(
        "topo.db*", "__pycache__", "*.pyc", "topo.env"))
    port = free_port()
    syslog_port = free_port()
    env = dict(os.environ, TOPO_PORT=str(port), TOPO_SYSLOG_PORT=str(syslog_port),
               TOPO_DB_PATH=os.path.join(work, "live.db"), PYTHONDONTWRITEBYTECODE="1",
               TOPO_POLL_TICK_S="3600")
    log = open(os.path.join(TMP, "live.log"), "w")
    proc = subprocess.Popen([sys.executable, "serve.py"], cwd=work, env=env, stdout=log, stderr=log)
    problems = []
    base = "http://127.0.0.1:%d" % port

    def get(path):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, r.read()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("server exited early: " + open(os.path.join(TMP, "live.log")).read()[-300:])
            try:
                get("/api/health")
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("server never answered /api/health")
        health = json.loads(get("/api/health")[1])
        need(health.get("poller_running") is True, "poller is not running", problems)
        need(health.get("reach_running") is True, "ICMP checker is not running", problems)
        need(b"topo_up 1" in get("/metrics")[1], "/metrics does not answer", problems)
        for url in ("/", "/devices", "/topology", "/conflicts", "/settings", "/syslog", "/changes", "/ipam", "/templates", "/alerts", "/configs/", "/api/ipam/findings", "/api/templates", "/api/alert-rules"):
            code = get(url)[0]
            need(code == 200, "GET %s -> %s" % (url, code), problems)
        req = urllib.request.Request(base + "/api/devices", method="POST",
                                     data=json.dumps({"hostname": "regress1", "mgmt_ip": "192.0.2.77"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            need(r.status == 201, "POST /api/devices -> %s" % r.status, problems)
        need(b"regress1" in get("/api/devices")[1], "new device missing from GET /api/devices", problems)
        graph = json.loads(get("/api/topology")[1])
        need(any(n["label"] == "regress1" for n in graph.get("nodes", [])), "new device missing from /api/topology", problems)
        need(get("/api/lldp-neighbors")[0] == 200 and get("/api/adjacencies")[0] == 200, "neighbor APIs do not answer", problems)

        def send(method, path, body):
            req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        nid = next(n["id"] for n in graph["nodes"] if n["label"] == "regress1")
        need(send("PUT", "/api/topology/positions", {"positions": {nid: {"x": 12.5, "y": -3}}}) == 200, "saving a node position failed", problems)
        saved = next(n for n in json.loads(get("/api/topology")[1])["nodes"] if n["id"] == nid)
        need((saved.get("x"), saved.get("y")) == (12.5, -3), "saved position not returned by /api/topology: %r" % saved, problems)
        need(json.loads(get("/api/topology")[1]).get("positions", {}).get(nid) == {"x": 12.5, "y": -3}, "/api/topology 'positions' lacks the saved place", problems)
        need(send("PUT", "/api/topology/positions", {"positions": {nid: {"x": "a", "y": 1}}}) == 400, "a non-numeric position was accepted", problems)
        need(send("DELETE", "/api/topology/positions", None) == 200, "clearing positions failed", problems)
        cleared = next(n for n in json.loads(get("/api/topology")[1])["nodes"] if n["id"] == nid)
        need("x" not in cleared, "position still returned after DELETE", problems)
        # syslog: a real UDP datagram is stored, parsed, served and not passed on as a pulled row
        need(health.get("syslog_listening") is True, "the syslog listener is not bound", problems)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.sendto(b"<189>123: SW1: *Sep  8 12:00:00.123 UTC: %LINK-3-UPDOWN: Interface Gi2, changed state to down",
                   ("127.0.0.1", syslog_port))
        udp.close()
        rows = []
        for _ in range(25):
            rows = json.loads(get("/api/syslog?minutes=5")[1])
            if rows:
                break
            time.sleep(0.2)
        need(rows and rows[0]["host"] == "SW1" and rows[0]["mnemonic"] == "%LINK-3-UPDOWN" and rows[0]["origin"] == "local",
             "UDP syslog was not stored and parsed: %r" % (rows,), problems)
        pulled = json.loads(get("/api/syslog?after_id=0&origin=local")[1])
        need(pulled and pulled[0].get("raw") and len(pulled[0].get("msg_hash", "")) == 40, "after_id must return raw and msg_hash", problems)
        need(rows and "raw" not in rows[0] and "msg_hash" not in rows[0], "the page API must not return raw or msg_hash", problems)
        need(json.loads(get("/api/syslog?after_id=0&origin=butler")[1]) == [], "origin=butler returned local rows", problems)
        need(b"topo_syslog_received_total 1" in get("/metrics")[1], "/metrics does not count the stored message", problems)
        results.append(("FAIL" if problems else "PASS", "R20", desc, problems))
    except Exception as e:
        results.append(("FAIL", "R20", desc, ["live tier crashed: %r" % e]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


# ============================================================ run

def tree_state():
    """What git sees changed/untracked, plus any bytecode: a run must not
    add to either."""
    out = sorted(glob.glob("**/__pycache__", recursive=True))
    if shutil.which("git"):
        r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        out += r.stdout.splitlines()
    return out


before = tree_state()
if STATIC_ONLY:
    for cid, desc in LIVE:
        results.append(("NOT RUN", cid, desc, ["--static"]))
else:
    try:
        run_live()
    except Exception as e:
        results.append(("FAIL", "R20", "live tier", ["crashed: %r" % e]))
after = tree_state()
if after != before:
    results.append(("FAIL", "clean", "run left files in the tree",
                    ["before %s, after %s" % (before, after)]))
shutil.rmtree(TMP, ignore_errors=True)

results.sort(key=lambda r: (len(r[1]), r[1]))
fails = [r for r in results if r[0] == "FAIL"]
gaps = [r for r in results if r[0] == "NOT RUN"]
for status, cid, desc, detail in results:
    print("%-8s %-6s %s" % (status, cid, desc))
    if status != "PASS" or VERBOSE:
        for d in detail:
            print("           " + d)
verdict = "BLOCKED" if fails else ("CLEAR WITH GAPS" if gaps else "CLEAR")
print("\n%s - %d checks, %d failed, %d not run" % (verdict, len(results), len(fails), len(gaps)))
sys.exit(1 if fails else 0)
