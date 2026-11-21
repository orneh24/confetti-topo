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
# Topology history (history.py): look for a changed map this often (0 = never keep snapshots); keep them this many days.
SNAPSHOT_INTERVAL_S = int(os.environ.get("TOPO_SNAPSHOT_INTERVAL_S", "300"))
SNAPSHOT_RETENTION_DAYS = int(os.environ.get("TOPO_SNAPSHOT_RETENTION_DAYS", "14"))
# BGP flap detection (flap.py): a peer with this many changes inside this many seconds is flapping.
FLAP_COUNT = int(os.environ.get("TOPO_FLAP_COUNT", "3"))
FLAP_WINDOW_S = int(os.environ.get("TOPO_FLAP_WINDOW_S", "3600"))

# Running-config backups (merged from confetti-butler): keep this many per device (0 = all), and error-counter history days.
CONFIG_VERSIONS_KEEP = int(os.environ.get("TOPO_CONFIG_VERSIONS_KEEP", "0"))
STATS_RETENTION_DAYS = int(os.environ.get("TOPO_STATS_RETENTION_DAYS", "7"))

# ---------------------------------------------------------------------------
# Alerting (alerts.py, merged from confetti-butler): rules send a webhook or mail for events and syslog.
# ---------------------------------------------------------------------------
ALERTS_ENABLED = _flag("TOPO_ALERTS_ENABLED", "true")
ALERT_TICK_S = int(os.environ.get("TOPO_ALERT_TICK_S", "10"))
ALERT_MAX_AGE_S = int(os.environ.get("TOPO_ALERT_MAX_AGE_S", "600"))
SMTP_HOST = os.environ.get("TOPO_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("TOPO_SMTP_PORT", "25"))
SMTP_USER = os.environ.get("TOPO_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("TOPO_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("TOPO_SMTP_FROM", "confetti-topo@localhost")
SMTP_TLS = _flag("TOPO_SMTP_TLS", "false")

# ---------------------------------------------------------------------------
# ICMP reachability checker (reach.py)
# ---------------------------------------------------------------------------
PING_ENABLED = _flag("TOPO_PING_ENABLED", "true")
PING_INTERVAL_S = int(os.environ.get("TOPO_PING_INTERVAL_S", "30"))
PING_WORKERS = int(os.environ.get("TOPO_PING_WORKERS", "16"))
# Consecutive failed pings before a device counts as down (one lost packet is not an outage).
PING_FAILS_TO_DOWN = int(os.environ.get("TOPO_PING_FAILS_TO_DOWN", "2"))

# ---------------------------------------------------------------------------
# Syslog (syslog_server.py, syslog_trigger.py, syslog_pull.py). On by default, like
# confetti-butler and confetti-traffic; a device that logs to this server gets polled early.
# ---------------------------------------------------------------------------
SYSLOG_ENABLED = _flag("TOPO_SYSLOG_ENABLED", "true")
SYSLOG_BIND = os.environ.get("TOPO_SYSLOG_BIND", "0.0.0.0")
SYSLOG_PORT = int(os.environ.get("TOPO_SYSLOG_PORT", "514"))
SYSLOG_MAX_ROWS = int(os.environ.get("TOPO_SYSLOG_MAX_ROWS", "300000"))
SYSLOG_RETENTION_DAYS = int(os.environ.get("TOPO_SYSLOG_RETENTION_DAYS", "14"))   # 0 = row cap only
# A copy pulled from another server inside this many seconds of a stored one is a duplicate.
SYSLOG_DEDUP_S = int(os.environ.get("TOPO_SYSLOG_DEDUP_S", "10"))
# A device is asked for an early poll at most this often because of syslog.
SYSLOG_TRIGGER_MIN_S = int(os.environ.get("TOPO_SYSLOG_TRIGGER_MIN_S", "30"))
SYSLOG_PULL_INTERVAL_S = int(os.environ.get("TOPO_SYSLOG_PULL_INTERVAL_S", "15"))

# ---------------------------------------------------------------------------
# Scheduled discovery (discovery.py). 0 = off; nothing runs unless asked for.
# ---------------------------------------------------------------------------
DISCOVERY_INTERVAL_S = int(os.environ.get("TOPO_DISCOVERY_INTERVAL_S", "0"))
DISCOVERY_CONFETTI_URL = os.environ.get("TOPO_DISCOVERY_CONFETTI_URL", "").strip()
DISCOVERY_BUTLER_URL = os.environ.get("TOPO_DISCOVERY_BUTLER_URL", "").strip()
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
DEFAULT_SNMP_COMMUNITY = os.environ.get("TOPO_SNMP_COMMUNITY", "")
DEFAULT_SNMP_VERSION = os.environ.get("TOPO_SNMP_VERSION", "2c")

# PAN-OS XML API (collectors/panos.py). A firewall ships with a self-signed management certificate, so
# the certificate is not checked unless asked: the same isolated-lab posture as the other defaults.
PANOS_VERIFY_TLS = os.environ.get("TOPO_PANOS_VERIFY_TLS", "false").lower() in ("1", "true", "yes")
PANOS_TIMEOUT_S = int(os.environ.get("TOPO_PANOS_TIMEOUT_S", "20"))

# Extra ways to tell who an LLDP neighbor is when it advertises no known management IP. Off by default; the
# Settings page overrides these at run time (settings keys lldp_match_chassis / lldp_match_name).
LLDP_MATCH_CHASSIS = os.environ.get("TOPO_LLDP_MATCH_CHASSIS", "false").lower() in ("1", "true", "yes")
LLDP_MATCH_NAME = os.environ.get("TOPO_LLDP_MATCH_NAME", "false").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Self-health (/api/health)
# ---------------------------------------------------------------------------
HEALTH_SERVICES = [
    s.strip() for s in os.environ.get(
        "TOPO_HEALTH_SERVICES", "confetti-topo,chronyd,sshd,open-vm-tools"
    ).split(",") if s.strip()
]
HEALTH_SERVICE_TIMEOUT_S = int(os.environ.get("TOPO_HEALTH_SERVICE_TIMEOUT_S", "3"))

# Optional access token. Empty (the default) = no login, as before. When set, every page and API call needs it
# (cookie from the /login page, or "Authorization: Bearer <token>" / "X-Topo-Token" for scripts and Prometheus).
# /api/health and /static stay open. See app/auth.py.
AUTH_TOKEN = os.environ.get("TOPO_AUTH_TOKEN", "")
