"""Configuration for the confetti-topo server."""

import os


def _flag(name, default):
    return os.environ.get(name, default).lower() in ("true", "1", "yes")


DB_PATH = os.environ.get("TOPO_DB_PATH", "topo.db")
PORT = int(os.environ.get("TOPO_PORT", "80"))

# The poller (its own thread pool), the ICMP checker and the discovery thread
# all write to this same SQLite file alongside Flask request handlers. WAL
# allows one writer at a time; without a busy timeout on every connection, a
# burst from any one of them makes another fail outright with "database is
# locked" instead of waiting its turn.
BUSY_TIMEOUT_MS = int(os.environ.get("TOPO_BUSY_TIMEOUT_MS", "5000"))

# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------
POLL_TICK_S = int(os.environ.get("TOPO_POLL_TICK_S", "15"))
# Halved from the netmiko-ssh-automation skill's 10-20 guidance — this runs
# on a small Alpine VM, not a workstation.
POLL_WORKERS = int(os.environ.get("TOPO_POLL_WORKERS", "8"))
POLL_DEFAULT_INTERVAL_S = int(os.environ.get("TOPO_POLL_DEFAULT_INTERVAL_S", "300"))
POLL_MAX_BACKOFF_S = int(os.environ.get("TOPO_POLL_MAX_BACKOFF_S", "3600"))
POLL_HISTORY_RETENTION_HOURS = int(os.environ.get("TOPO_POLL_HISTORY_RETENTION_HOURS", "168"))
EVENT_RETENTION_DAYS = int(os.environ.get("TOPO_EVENT_RETENTION_DAYS", "30"))

# ---------------------------------------------------------------------------
# ICMP reachability checker (reach.py)
# ---------------------------------------------------------------------------
PING_ENABLED = _flag("TOPO_PING_ENABLED", "true")
PING_INTERVAL_S = int(os.environ.get("TOPO_PING_INTERVAL_S", "30"))
PING_WORKERS = int(os.environ.get("TOPO_PING_WORKERS", "16"))
# Consecutive failed pings before a device counts as down (one lost packet is not an outage).
PING_FAILS_TO_DOWN = int(os.environ.get("TOPO_PING_FAILS_TO_DOWN", "2"))

# ---------------------------------------------------------------------------
# Scheduled discovery (discovery.py). 0 = off; nothing runs unless asked for.
# ---------------------------------------------------------------------------
DISCOVERY_INTERVAL_S = int(os.environ.get("TOPO_DISCOVERY_INTERVAL_S", "0"))
DISCOVERY_CONFETTI_URL = os.environ.get("TOPO_DISCOVERY_CONFETTI_URL", "").strip()
DISCOVERY_SWEEP_CIDRS = [c.strip() for c in os.environ.get("TOPO_DISCOVERY_SWEEP_CIDRS", "").split(",") if c.strip()]
DISCOVERY_SEEDFILE = os.environ.get("TOPO_DISCOVERY_SEEDFILE", "").strip()
# Add LLDP neighbors that advertise an unknown management IP as devices
# (inventory-only: polling stays off until an operator enables them).
LLDP_AUTO_ADOPT = _flag("TOPO_LLDP_AUTO_ADOPT", "false")

# ---------------------------------------------------------------------------
# Default device credentials — a per-device row in the credentials table
# overrides these. Never hardcode a real lab password here; this is a shared
# default for an isolated lab, same posture as confetti-traffic's default root password.
# ---------------------------------------------------------------------------
DEFAULT_SSH_USERNAME = os.environ.get("TOPO_SSH_USERNAME", "admin")
DEFAULT_SSH_PASSWORD = os.environ.get("TOPO_SSH_PASSWORD", "")
DEFAULT_SSH_SECRET = os.environ.get("TOPO_SSH_SECRET", "")

# ---------------------------------------------------------------------------
# Self-health (/api/health)
# ---------------------------------------------------------------------------
HEALTH_SERVICES = [
    s.strip() for s in os.environ.get(
        "TOPO_HEALTH_SERVICES", "confetti-topo,chronyd,sshd,open-vm-tools"
    ).split(",") if s.strip()
]
HEALTH_SERVICE_TIMEOUT_S = int(os.environ.get("TOPO_HEALTH_SERVICE_TIMEOUT_S", "3"))
