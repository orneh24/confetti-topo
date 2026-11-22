"""Database backup and restore from the Settings page (and scripts/topo-db.sh on the VM).

A backup is SQLite's online backup of the live database (the same as `sqlite3 .backup`, safe while the poller writes),
kept in <database folder>/backups next to the nightly and pre-update ones. It holds device credentials in plaintext,
like topo.db itself: the folder is 0700, the files 0600, and the API only hands one out on a POST.

A restore copies an uploaded database INTO the live one with the same backup API, so every open connection (Flask,
the poller, the ICMP checker) sees the restored data without a restart. Before that the upload is checked
(an SQLite file that passes quick_check and has a devices table) and the current database is saved as
topo-pre-restore-<time>.db. Then db.init_db() upgrades an older schema in place (constraint 34) and no device is left
marked 'running'.
"""

import os
import re
import sqlite3
import time

from . import config
from . import db

WEB_KEEP = 5               # topo-web-*.db: backups made by the Download button
PRE_RESTORE_KEEP = 5       # topo-pre-restore-*.db
NAME_RE = re.compile(r"^topo-[a-z-]+-[0-9TZ-]+\.db$")
SECRET_KEY_RE = re.compile(r"PASS|SECRET|TOKEN|COMMUNITY|KEY", re.I)


class RestoreError(Exception):
    pass


def _open(path, read_only=False):
    """A connection to a file that is not the live database (a backup being written, an upload being read), with the
    same busy_timeout as every other connection (constraint 5)."""
    conn = sqlite3.connect("file:{}?mode=ro".format(path) if read_only else path, uri=read_only)
    conn.execute("PRAGMA busy_timeout={:d}".format(config.BUSY_TIMEOUT_MS))
    return conn


def backup_dir(create=True):
    path = os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "backups")
    if not create:
        return path
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def _rotate(prefix, keep):
    d = backup_dir()
    files = sorted((f for f in os.listdir(d) if f.startswith(prefix) and f.endswith(".db")),
                   key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True)
    for old in files[keep:]:
        try:
            os.remove(os.path.join(d, old))
        except OSError:
            pass


def make(prefix="topo-web"):
    """Back up the live database now. Written under a temporary name and renamed, so a half-written file never
    counts as a backup. Returns the file's path."""
    out = os.path.join(backup_dir(), "{}-{}.db".format(prefix, _stamp()))
    tmp = out + ".tmp"
    src = db.connect()
    dst = _open(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, out)
    _rotate(prefix + "-", WEB_KEEP if prefix == "topo-web" else PRE_RESTORE_KEEP)
    return out


def listing():
    """Stored backups, newest first: name, size, modified (ISO). Only reads: no folder is made for it."""
    d = backup_dir(create=False)
    out = []
    for f in (os.listdir(d) if os.path.isdir(d) else []):
        if NAME_RE.match(f):
            st = os.stat(os.path.join(d, f))
            out.append({"name": f, "size": st.st_size,
                        "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))})
    out.sort(key=lambda r: r["modified"], reverse=True)
    return out


def path_of(name):
    """The path of a stored backup by its bare file name; None for anything else (no paths, no other files)."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    p = os.path.join(backup_dir(create=False), name)
    return p if os.path.isfile(p) else None


def check(path):
    """Refuse a file that is not a confetti-topo database. Returns the number of devices in it."""
    try:
        conn = _open(path, read_only=True)
    except sqlite3.Error as exc:
        raise RestoreError("not an SQLite database ({})".format(exc))
    try:
        ok = conn.execute("PRAGMA quick_check").fetchone()[0]
        if ok != "ok":
            raise RestoreError("the database is damaged: " + str(ok)[:200])
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'devices'").fetchone():
            raise RestoreError("this is not a confetti-topo database (no devices table)")
        return conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise RestoreError("not a usable SQLite database ({})".format(exc))
    finally:
        conn.close()


def restore(upload_path):
    """Replace the live database's content with the uploaded one. Returns {devices, saved_as}."""
    devices = check(upload_path)
    saved = make("topo-pre-restore")
    src = _open(upload_path, read_only=True)
    dst = db.connect()
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    db.init_db()    # an older backup gains the newer tables and columns (constraint 34)
    conn = db.connect()
    try:
        conn.execute("UPDATE devices SET poll_state = 'idle' WHERE poll_state = 'running'")
        conn.commit()
    finally:
        conn.close()
    return {"devices": devices, "saved_as": os.path.basename(saved)}


def env_text():
    """The TOPO_* settings this server runs with, as topo.env lines, with every secret-looking value left out."""
    lines = ["# confetti-topo settings exported {} (secrets left out: set them again on the new VM)".format(
        time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))]
    for k in sorted(os.environ):
        if not k.startswith("TOPO_"):
            continue
        v = os.environ[k]
        if SECRET_KEY_RE.search(k[5:]):
            lines.append("# {}=  (secret, not exported)".format(k))
        else:
            lines.append("{}={}".format(k, v.replace("\n", " ")))
    return "\n".join(lines) + "\n"
