"""UDP syslog receiver for confetti-topo.

Listens on UDP/514 in a daemon thread and stores parsed messages in the same SQLite database as
everything else. A message that says a link or a session changed asks for an early poll of the
device that sent it (syslog_trigger.py); nothing else here touches the topology.

Deliberately minimal: no relay, no TLS, no authentication. UDP syslog is lossy and anything on
the segment can inject into it, so this is a troubleshooting aid and a poll trigger, never an
audit trail. Hazards it guards against:
no socket address reuse, busy_timeout before journal_mode, a bounded row count, stderr and not
stdout.

Parsing and the row insert are syslog_core.py, the same file in
confetti-traffic and confetti-topo, so both read a line the same way.
"""

import socket
import socketserver
import sqlite3
import sys
import threading

from . import alerts
from . import config
from . import db
from . import syslog_core
from . import syslog_trigger

MAX_RAW = 4096          # truncate absurd messages before they reach the DB
PRUNE_EVERY = 500       # rows between row-cap enforcement

parse = syslog_core.parse

# In-memory since start; /metrics shows them as counters.
stats = {"received": 0, "duplicates": 0}


class _Store:
    """Single writer, guarded by a lock. WAL is already enabled by db.init_db."""

    def __init__(self, db_path, max_rows):
        self.max_rows = max_rows
        self.lock = threading.Lock()
        self.since_prune = 0
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        # A second writer next to the poller, the checker and Flask: without a busy timeout a
        # burst here makes a poller commit fail with "database is locked". Set before
        # journal_mode, which can itself contend.
        self.db.execute("PRAGMA busy_timeout={}".format(config.BUSY_TIMEOUT_MS))
        self.db.execute("PRAGMA journal_mode=WAL")
        # One commit per datagram at synchronous=FULL would fsync before every recvfrom. NORMAL
        # is safe under WAL (a crash can lose the last transactions, never corrupt the file) and
        # this is a troubleshooting aid, not an audit trail.
        self.db.execute("PRAGMA synchronous=NORMAL")

    def insert(self, rec):
        # Must match db.sqlite_now() exactly (constraint 4): SQLite's own 'YYYY-MM-DD HH:MM:SS'.
        now = db.sqlite_now()
        with self.lock:
            stored = syslog_core.insert(self.db, rec, now, dedup_s=config.SYSLOG_DEDUP_S)
            self.since_prune += 1
            if self.since_prune >= PRUNE_EVERY:
                # Row cap only, by id. AUTOINCREMENT ids are monotonic, so this is one indexed
                # range delete. The age limit is applied by the poller tick.
                self.db.execute(
                    "DELETE FROM syslog WHERE id <= (SELECT MAX(id) FROM syslog) - ?",
                    (self.max_rows,))
                self.since_prune = 0
            self.db.commit()
            if stored is None:
                stats["duplicates"] += 1
                return
            stats["received"] += 1
            syslog_trigger.maybe_request(self.db, rec)
        # Hand the message to the alert thread: appends to a deque and never raises, so alerting
        # can not slow or break the receiver.
        alerts.offer_syslog(rec)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request[0]
        try:
            raw = data.decode("utf-8", "replace").strip()
        except Exception:
            return
        if not raw:
            return
        if len(raw) > MAX_RAW:
            raw = raw[:MAX_RAW] + " …[truncated]"
        try:
            _store.insert(parse(raw, self.client_address[0]))
        except Exception as exc:            # never let one bad packet kill the listener
            # stderr, not stdout: under OpenRC stdout is a file and so block-buffered, and
            # waitress never returns to flush it.
            sys.stderr.write("[syslog] store failed: {}\n".format(exc))


class _Server(socketserver.UDPServer):
    # Deliberately NOT allow_reuse_address. SO_REUSEADDR on a UDP socket does not reliably reject
    # a duplicate bind on Linux: two processes could hold the port and the kernel would hand each
    # datagram to only one of them, leaving the log with silent holes. Without the flag a second
    # bind fails with EADDRINUSE, which start() expects.
    max_packet_size = 8192

    def server_bind(self):
        # A device logging at debug level bursts faster than one thread drains the socket.
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        socketserver.UDPServer.server_bind(self)


_store = None
_started = False
_start_lock = threading.Lock()


def is_listening():
    """Whether the UDP listener actually bound. Reads the flag start() sets, so it can never
    disagree with what happened at startup."""
    return _started


def start():
    """Start the listener once. Safe to call repeatedly; returns quietly when switched off or
    when the port is taken."""
    global _store, _started

    with _start_lock:
        if _started or not config.SYSLOG_ENABLED:
            return
        try:
            server = _Server((config.SYSLOG_BIND, config.SYSLOG_PORT), _Handler)
        except OSError as exc:
            sys.stderr.write("[syslog] not listening on {}:{} - {}\n".format(
                config.SYSLOG_BIND, config.SYSLOG_PORT, exc))
            return

        _store = _Store(config.DB_PATH, config.SYSLOG_MAX_ROWS)
        threading.Thread(target=server.serve_forever, name="syslog", daemon=True).start()
        _started = True
        sys.stderr.write("[syslog] listening on {}:{} (cap {} rows)\n".format(
            config.SYSLOG_BIND, config.SYSLOG_PORT, config.SYSLOG_MAX_ROWS))
