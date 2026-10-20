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
are NOT RUN, never PASS.
"""
import glob
import os
import re
import shutil
import socket
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
LIVE = [("R20", "live: real server answers, pages render, /metrics, topology API, saved node positions")]
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
        from app.collectors import ssh, sweep, confetti, butler
        from app.parsers import ios
    except ImportError as e:
        raise Skip("app not importable here (%s)" % e)
    _mods.update(config=config, db=db, identity=identity,
                 poller=poller, events=events,
                 appmod=app_pkg_app, ssh=ssh, sweep=sweep, confetti=confetti, butler=butler, ios=ios)
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
    for url in ("/", "/devices", "/devices/%d" % did, "/conflicts", "/topology", "/settings"):
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
    to = {(e["layer"], e["label"].split()[-1] if e["layer"] != "lldp" else "lldp"): e["to"] for e in g["edges"]}
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
    labels = [e["label"] for e in topology.build(conn, "bgp")["edges"]]
    need(labels == ["bgp AS65001 - AS65002 Established"], "BGP label must name both AS numbers: %r" % labels, p)
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
    words = {"four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
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
    for script in ("topo/build-template.sh", "topo/scripts/topo-update.sh"):
        for ref in sorted(set(re.findall(r'\$\{(?:SCRIPT_DIR|SRC_DIR|SRC)\}/([\w./-]+?)["\s]', read(script)))):
            if ref.endswith("/") or "*" in ref:
                continue
            need(os.path.exists(os.path.join("topo", ref)) or os.path.exists(ref),
                 "%s copies '%s' which does not exist" % (script, ref), p)
    bt = read("topo/build-template.sh")
    need("/etc/periodic/daily/topo-backup" in bt, "nightly backup must be installed without a dot in its name", p)
    return p


# ---- features added after R14 -----------------------------------------

SAMPLES = "dev/samples/"


def sample(name):
    return read(SAMPLES + name)


@check("R19", "platform registry: same tasks everywhere, unknown platform = IOS, every command read-only")
def _():
    p = []
    m = app_modules()
    from app import platforms
    ios_tasks = tuple(platforms.IOS["commands"])
    need(ios_tasks == ("version", "interfaces", "lldp", "bgp", "ospf"), "IOS tasks are %r" % (ios_tasks,), p)
    for name, plat in platforms.PLATFORMS.items():
        need(tuple(plat["commands"]) == ios_tasks, "%s tasks are %r, expected %r" % (name, tuple(plat["commands"]), ios_tasks), p)
        need(set(plat["parsers"]) == set(plat["commands"]), "%s: commands and parsers differ" % name, p)
        for task, cmd in list(plat["commands"].items()) + [("lldp_brief", plat.get("lldp_brief_command", "show x"))]:
            need(cmd.startswith("show "), "%s %s runs %r: only read-only 'show' commands are allowed" % (name, task, cmd), p)
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
    bt = read("topo/build-template.sh")
    need(re.search(r"^switch_ssh_server\(\) \{", bt, re.M), "build-template.sh lost switch_ssh_server()", p)
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

    def creds(did):
        c = m["db"].connect()
        try:
            return ssh.resolve_credentials(c, did)
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
    return p


# ============================================================ live tier

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
    env = dict(os.environ, TOPO_PORT=str(port),
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
        for url in ("/", "/devices", "/topology", "/conflicts", "/settings"):
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
