#!/bin/sh
# build-template.sh — Build the confetti-topo golden template on a fresh Alpine
# install. Run as root after booting the Alpine ISO and completing
# setup-alpine. Mirrors confetti-traffic/hub/confettictl-build-template.sh closely — same
# infra (chrony, open-vm-tools, sshd, OpenRC, guestinfo
# first-boot), same structure. See that file's comments for the reasoning
# behind each infra choice; this file only calls out what's different here.
#
# This script:
#   1. Enables the community repo
#   2. Installs all required packages (Python, Flask, Netmiko's deps, etc.)
#   3. Copies the app into place
#   4. Creates an OpenRC service
#   5. Cleans up for template conversion
#
# Usage:
#   1. SCP the entire topo/ directory to the Alpine VM
#   2. Run: sh /root/topo/build-template.sh
#   3. Shutdown and convert to template in vCenter
#
# Updating an already-deployed VM (backup, health check and rollback):
#   topo-update.sh <topo-dir | topo.tar.gz>     (see scripts/topo-update.sh)
# runs this script with --update. That mode installs everything the build
# does (packages, code, OpenRC and cron scripts, motd) but leaves the root
# password, topo.env, the database, host keys and the template clean-up
# alone. TOPO_STAGE_DIR (set by topo-update.sh) is where the app code
# is copied instead of /opt/confetti-topo.
#
# After cloning:
#   1. Set a static IP (or a DHCP reservation)
#   2. Boot — the dashboard starts automatically on port 80

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/confetti-topo"
DEST_DIR="${TOPO_STAGE_DIR:-$INSTALL_DIR}"
DB_DIR="/var/lib/confetti-topo"
LAB_ROOT_PASSWORD="${LAB_ROOT_PASSWORD:-confetti}"

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
log() {
    printf '[build-template] %s\n' "$1"
}

die() {
    printf '[build-template] FATAL: %s\n' "$1" >&2
    exit 1
}

# -------------------------------------------------------------------
# Sanity checks
# -------------------------------------------------------------------
UPDATE=""
case "${1:-}" in
    "") ;;
    --update) UPDATE=1 ;;
    *) die "Usage: build-template.sh [--update]" ;;
esac
[ "$(id -u)" -eq 0 ] || die "Must run as root"

log "=== confetti-topo template builder ==="

# -------------------------------------------------------------------
# 1. Enable community repository
# -------------------------------------------------------------------
log "Enabling community repository"
ALPINE_VERSION=$(cat /etc/alpine-release | cut -d. -f1,2)
if ! grep -q "^[^#].*community" /etc/apk/repositories; then
    echo "http://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/community" >> /etc/apk/repositories
    log "Community repo added"
else
    log "Community repo already enabled"
fi

# -------------------------------------------------------------------
# 2. Install packages
#
# py3-paramiko, py3-yaml and py3-requests are real Alpine community
# packages Netmiko/the collectors need; py3-jinja2 too. netmiko itself is
# NOT in Alpine's index (checked against the same discipline as confetti-traffic
# CLAUDE.md constraint 14 — verify, don't assume), so it's pip-installed
# below alongside waitress, same fallback pattern as the hub's build script.
# -------------------------------------------------------------------
# A failed apk step is fatal for a build, but only a warning for --update: the
# new code is still staged and import-checked, so a VM without internet (or a
# mirror that is down) can take a code-only update.
apk_step() {
    if ! "$@"; then
        [ -n "$UPDATE" ] || die "'$*' failed"
        log "WARNING: '$1 $2' failed (mirror unreachable?) -- updating the code anyway"
    fi
}

log "Updating package index"
apk_step apk update

log "Installing packages"
apk_step apk add --no-cache \
    python3 \
    py3-pip \
    py3-flask \
    py3-paramiko \
    py3-yaml \
    py3-requests \
    py3-jinja2 \
    sqlite \
    curl \
    open-vm-tools \
    chrony

log "Packages installed"

rc-update add open-vm-tools default

# Never route. Alpine already defaults forwarding off; pinned so nothing
# installed later can turn it on (same as the hub).
mkdir -p /etc/sysctl.d
cat > /etc/sysctl.d/99-confetti.conf <<'SYSEOF'
net.ipv4.ip_forward = 0
net.ipv6.conf.all.forwarding = 0
SYSEOF
rc-update add sysctl boot >/dev/null 2>&1 || true
sysctl -p /etc/sysctl.d/99-confetti.conf >/dev/null 2>&1 || true

# confetti-topo stamps every poll, event and ping result with its own clock,
# and the event timeline is only worth reading if that clock is right.
rc-update add chronyd default

# -------------------------------------------------------------------
# 2b. Set default lab credentials
# -------------------------------------------------------------------
if [ -z "$UPDATE" ]; then
    log "Setting root password"
    echo "root:${LAB_ROOT_PASSWORD}" | chpasswd

    log "Credentials: root / ${LAB_ROOT_PASSWORD}"
fi

# -------------------------------------------------------------------
# 3. Create directories
# -------------------------------------------------------------------
log "Creating directories"
mkdir -p "$DEST_DIR" "$DB_DIR"

# -------------------------------------------------------------------
# 4. Copy application
# -------------------------------------------------------------------
log "Installing application to $DEST_DIR"

cp -r "${SCRIPT_DIR}/app" "$DEST_DIR/"
cp -r "${SCRIPT_DIR}/templates" "$DEST_DIR/"

# static/ carries the vendored vis-network bundle — real
# content, not empty like the hub's static/, so this is a real copy, not
# the hub's mkdir-then-maybe-copy.
mkdir -p "$DEST_DIR/static"
cp -r "${SCRIPT_DIR}/static/"* "$DEST_DIR/static/"

mkdir -p "$DEST_DIR/seed"
if [ -d "${SCRIPT_DIR}/seed" ]; then
    cp -r "${SCRIPT_DIR}/seed/"* "$DEST_DIR/seed/" 2>/dev/null || true
fi

cp -f "${SCRIPT_DIR}/requirements.txt" "$DEST_DIR/"
cp -f "${SCRIPT_DIR}/serve.py" "$DEST_DIR/"
cp -f "${SCRIPT_DIR}/run.sh" "$DEST_DIR/"
cp -f "${SCRIPT_DIR}/scripts/topo-setup.sh" "$DEST_DIR/"
cp -f "${SCRIPT_DIR}/scripts/topo-update.sh" "$DEST_DIR/"
# Build id shown at login (services/login-setup.sh): a hash of the code, so two
# VMs with the same code show the same id. It lives in the code dir, so a
# rollback brings the old id back with the old code.
BUILD_ID=$(cd "$SCRIPT_DIR" && find app templates static serve.py -type f -not -path '*__pycache__*' |
    LC_ALL=C sort | xargs sha256sum | sha256sum | cut -c1-8)
printf 'build %s, installed %s UTC\n' "$BUILD_ID" "$(date -u '+%Y-%m-%d %H:%M')" > "$DEST_DIR/BUILD"
chmod +x "$DEST_DIR/run.sh" "$DEST_DIR/serve.py" "$DEST_DIR/topo-setup.sh" "$DEST_DIR/topo-update.sh"
ln -sf "$INSTALL_DIR/topo-setup.sh" /usr/local/bin/topo-setup.sh
ln -sf "$INSTALL_DIR/topo-update.sh" /usr/local/bin/topo-update.sh

# -------------------------------------------------------------------
# 5. Install Python dependencies not covered by apk
# -------------------------------------------------------------------
log "Checking Python dependencies"

# Versions come from requirements.txt, so a rebuild installs what was tested.
# Only the packages apk lacks are installed here; the py3-* ones above stay
# at whatever version this Alpine release ships.
pin() {
    spec=$(grep -i "^$1==" "${SCRIPT_DIR}/requirements.txt" || true)
    [ -n "$spec" ] || die "no pinned $1 in requirements.txt"
    printf '%s' "$spec"
}

if ! python3 -c "import flask" 2>/dev/null; then
    log "Installing Flask via pip"
    pip3 install --break-system-packages "$(pin flask)"
fi

if ! python3 -c "import netmiko" 2>/dev/null; then
    log "Installing Netmiko via pip (not an Alpine package)"
    pip3 install --break-system-packages "$(pin netmiko)"
fi

# Waitress serves the app instead of Flask's development server, which is
# single-threaded: the poller's own requests and
# the dashboard would all queue behind each other otherwise.
log "Installing waitress WSGI server"
if ! python3 -c "import waitress" 2>/dev/null; then
    apk add --no-cache py3-waitress 2>/dev/null ||         pip3 install --break-system-packages "$(pin waitress)"
fi

# -------------------------------------------------------------------
# 6. Create configuration file
# -------------------------------------------------------------------
if [ -z "$UPDATE" ]; then
log "Creating configuration"
cat > "$INSTALL_DIR/topo.env" <<'ENVEOF'
# confetti-topo environment configuration.
# Edit these values after cloning if needed.

TOPO_DB_PATH=/var/lib/confetti-topo/topo.db
TOPO_PORT=80
# TOPO_HOST=0.0.0.0

# Several writers share this database (Flask, the poller's thread pool, the
# ICMP checker, the discovery timer). Without this, a burst on any one of
# them can fail another with "database is locked".
TOPO_BUSY_TIMEOUT_MS=5000

# --- Poller ------------------------------------------------------------
TOPO_POLL_TICK_S=15
TOPO_POLL_WORKERS=8
TOPO_POLL_DEFAULT_INTERVAL_S=300
TOPO_POLL_MAX_BACKOFF_S=3600
TOPO_POLL_HISTORY_RETENTION_HOURS=168

# --- Retention ---------------------------------------------------------
TOPO_EVENT_RETENTION_DAYS=30
# Nightly database backups kept (/var/lib/confetti-topo/backups).
TOPO_BACKUP_KEEP=7

# --- ICMP reachability ---------------------------------------------------
TOPO_PING_ENABLED=true
TOPO_PING_INTERVAL_S=30
# TOPO_PING_WORKERS=16
# Failed pings in a row before a device is marked down.
TOPO_PING_FAILS_TO_DOWN=2

# --- Discovery (all off until configured) --------------------------------
# Seconds between scheduled runs of the sources below; 0 = never.
TOPO_DISCOVERY_INTERVAL_S=0
# TOPO_DISCOVERY_CONFETTI_URL=http://10.0.0.100
# TOPO_DISCOVERY_SWEEP_CIDRS=10.0.1.0/24,10.0.2.0/24
# TOPO_DISCOVERY_SEEDFILE=/opt/confetti-topo/seed/devices.yaml

# --- Default device credentials -----------------------------------------
# Shared lab defaults; override per device via PUT /api/devices/<id>/credentials.
# Never put a real password here outside an isolated lab.
TOPO_SSH_USERNAME=admin
TOPO_SSH_PASSWORD=
TOPO_SSH_SECRET=

# --- Self-health (/api/health) ------------------------------------------
TOPO_HEALTH_SERVICES=confetti-topo,chronyd,sshd,open-vm-tools
TOPO_HEALTH_SERVICE_TIMEOUT_S=3
ENVEOF
# Holds the default SSH password (and anything guestinfo adds): root only.
chmod 600 "$INSTALL_DIR/topo.env"
fi

# -------------------------------------------------------------------
# 7. Create OpenRC init script
# -------------------------------------------------------------------
log "Creating OpenRC init script"
cp -f "${SCRIPT_DIR}/services/confetti-topo.initd" /etc/init.d/confetti-topo

chmod +x /etc/init.d/confetti-topo

# Nightly database backup. run-parts (Alpine's crond runs /etc/periodic/daily
# at 02:00) skips file names containing a dot, hence no .sh.
mkdir -p /etc/periodic/daily
cp -f "${SCRIPT_DIR}/services/topo-backup.sh" /etc/periodic/daily/topo-backup
chmod +x /etc/periodic/daily/topo-backup

# First-boot autoconfiguration from guestinfo — same pattern as the hub's
# confettid-hub-firstboot: stands down without both required keys rather
# than blocking boot on a prompt nobody can answer from an OpenRC start().
cp -f "${SCRIPT_DIR}/services/firstboot.initd" /etc/init.d/confetti-topo-firstboot
chmod +x /etc/init.d/confetti-topo-firstboot

# Invite an unconfigured VM to run topo-setup.sh at first interactive
# login, where a real tty is guaranteed.
cp -f "${SCRIPT_DIR}/services/login-setup.sh" /etc/profile.d/confetti-topo-setup.sh

# -------------------------------------------------------------------
# 8. Enable services
# -------------------------------------------------------------------
log "Enabling services"

rc-update add confetti-topo default
rc-update add confetti-topo-firstboot default
rc-update add crond default

# SSH server: OpenSSH sshd (it was Dropbear until 2026-10-14). Dropbear has no
# SFTP server, so scp/sftp and Paramiko uploads fail, and it drops large
# packets. Called once, after everything else is installed (see below).
#
# Safe to run on a VM that still has Dropbear (the first --update, or a fresh
# build on an Alpine that was installed with Dropbear) and a no-op afterwards.
# Dropbear is stopped only after sshd is installed and its config checks out,
# and it is removed only after sshd is confirmed running; on any failure it is
# started again, so the VM never loses SSH. The session running this script
# survives: stopping Dropbear only stops its listener, not open connections.
switch_ssh_server() {
    apk_step apk add --no-cache --no-progress openssh-server openssh-sftp-server
    if [ ! -x /usr/sbin/sshd ] || [ ! -f /etc/init.d/sshd ]; then
        log "WARNING: OpenSSH is not installed -- SSH stays as it is"
        return 0
    fi

    # Root password login stays on (the lab logins and the hub depend on it);
    # OpenSSH's own default would refuse it. MaxStartups: the mesh test makes
    # every node SSH to every peer on the same cron tick, and the default
    # (10:30:100) starts dropping connections in a mesh of more than ~10 nodes.
    mkdir -p /etc/ssh/sshd_config.d
    _conf=/etc/ssh/sshd_config.d/10-confetti.conf
    cat > "$_conf.new" <<'SSHEOF'
# written by build-template.sh, replaced on every build and update
PermitRootLogin yes
PasswordAuthentication yes
MaxStartups 100:30:200
SSHEOF
    _conf_changed=1
    if cmp -s "$_conf.new" "$_conf" 2>/dev/null; then
        _conf_changed=""
        rm -f "$_conf.new"
    else
        mv -f "$_conf.new" "$_conf"
    fi
    # ed25519 only: RSA key generation is slow on a small clone's first boot.
    if grep -q '^key_types_to_generate=' /etc/conf.d/sshd 2>/dev/null; then
        sed -i 's/^key_types_to_generate=.*/key_types_to_generate="ed25519"/' /etc/conf.d/sshd
    else
        echo 'key_types_to_generate="ed25519"' >> /etc/conf.d/sshd
    fi

    # Keep the host key clients already know, so nobody gets a "host key
    # changed" warning (needs the Dropbear ed25519 key, which Alpine makes).
    if [ -f /etc/dropbear/dropbear_ed25519_host_key ] && [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
        apk_step apk add --no-cache --no-progress dropbear-convert
        if command -v dropbearconvert >/dev/null 2>&1 &&
           dropbearconvert dropbear openssh /etc/dropbear/dropbear_ed25519_host_key /etc/ssh/ssh_host_ed25519_key >/dev/null 2>&1; then
            chmod 600 /etc/ssh/ssh_host_ed25519_key
            log "Kept the SSH host key (converted from Dropbear)"
        else
            rm -f /etc/ssh/ssh_host_ed25519_key
            log "NOTE: could not convert the Dropbear host key; clients will see a new one"
        fi
    fi

    if rc-service sshd status >/dev/null 2>&1; then
        # Already running: reload only if the drop-in changed (a reload is a re-exec).
        [ -z "$_conf_changed" ] || rc-service sshd reload >/dev/null 2>&1 || true
    else
        # Makes the missing host keys and runs sshd -t.
        if ! rc-service sshd checkconfig >/dev/null 2>&1; then
            log "WARNING: sshd config check failed -- SSH stays on Dropbear"
            return 0
        fi
        rc-service dropbear stop >/dev/null 2>&1 || true
        rc-service sshd start >/dev/null 2>&1 || true
        _i=0
        while [ "$_i" -lt 5 ] && ! rc-service sshd status >/dev/null 2>&1; do
            sleep 1
            _i=$((_i + 1))
        done
        if ! rc-service sshd status >/dev/null 2>&1; then
            [ ! -e /etc/runlevels/default/dropbear ] || rc-service dropbear start >/dev/null 2>&1 || true
            log "WARNING: sshd did not start -- SSH stays on Dropbear"
            return 0
        fi
    fi

    rc-update add sshd default >/dev/null 2>&1 || true
    if [ -e /etc/runlevels/default/dropbear ] || [ -f /etc/init.d/dropbear ]; then
        rc-update del dropbear default >/dev/null 2>&1 || true
        apk del --no-progress dropbear dropbear-openrc dropbear-convert >/dev/null 2>&1 || true
        log "SSH server switched from Dropbear to OpenSSH"
    fi

    # Deployed VMs keep the old service list in their env file.
    if [ -f "$INSTALL_DIR/topo.env" ]; then
        sed -i '/_HEALTH_SERVICES=/{s/=dropbear,/=sshd,/;s/,dropbear,/,sshd,/;s/,dropbear$/,sshd/}' "$INSTALL_DIR/topo.env"
    fi
}

log "Services enabled"

# -------------------------------------------------------------------
# 9. First-boot instructions
# -------------------------------------------------------------------
log "Creating first-boot instructions"
cat > /etc/motd <<'MOTDEOF'

  +-----------------------------------------------+
  |                                               |
  | confetti-topo VM                            |
  |                                               |
  | Dashboard: http://<this-vm-ip>/               |
  | Config:    /opt/confetti-topo/topo.env    |
  | DB:        /var/lib/confetti-topo/topo.db |
  | Logs:      rc-service confetti-topo status  |
  |                                               |
  | To update this VM to a newer build:           |
  |   copy topo/ here (scp -r topo root@<ip>: |
  |   from the repo), then run:                   |
  |     topo-update.sh /root/topo             |
  |   (topo-update.sh --rollback goes back)     |
  +-----------------------------------------------+

MOTDEOF

# -------------------------------------------------------------------
# 10. Static IP configuration helper (same as the hub's confettictl-set-static-ip)
# -------------------------------------------------------------------
log "Creating static IP helper script"
cat > /usr/local/bin/set-static-ip <<'SIPEOF'
#!/bin/sh
# Helper to configure a static IP on this VM.
# Usage: set-static-ip <ip/cidr> <gateway> [dns] [hostname]
# Example: set-static-ip 10.0.0.101/24 10.0.0.1 10.0.0.53 confetti-topo

set -eu

if [ $# -lt 2 ]; then
    echo "Usage: set-static-ip <ip/cidr> <gateway> [dns] [hostname]"
    echo "Example: set-static-ip 10.0.0.101/24 10.0.0.1 10.0.0.53 confetti-topo"
    exit 1
fi

IP_CIDR="$1"
GATEWAY="$2"
DNS="${3:-}"
NEW_HOSTNAME="${4:-}"

IFACE=$(ip -o link show | awk -F': ' '!/lo/{print $2; exit}')

cat > /etc/network/interfaces <<EOF
auto lo
iface lo inet loopback

auto ${IFACE}
iface ${IFACE} inet static
    address ${IP_CIDR}
    gateway ${GATEWAY}
EOF

echo "Static IP configured on ${IFACE}: ${IP_CIDR} via ${GATEWAY}"
echo "Restart networking: rc-service networking restart"

if [ -n "$DNS" ]; then
    printf 'nameserver %s\n' "$DNS" > /etc/resolv.conf
    echo "DNS configured: ${DNS} (written to /etc/resolv.conf)"
else
    echo "DNS is not configured -- pass a third argument, or edit"
    echo "/etc/resolv.conf by hand, if the VM needs name resolution"
    echo "(an NTP pool name, a hostname in a confetti-traffic import URL)."
fi

if [ -n "$NEW_HOSTNAME" ]; then
    CURRENT_HOSTNAME=$(hostname)
    if [ "$CURRENT_HOSTNAME" != "$NEW_HOSTNAME" ]; then
        printf '%s\n' "$NEW_HOSTNAME" > /etc/hostname
        hostname "$NEW_HOSTNAME"
        if grep -q "127.0.1.1" /etc/hosts 2>/dev/null; then
            sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${NEW_HOSTNAME}/" /etc/hosts
        else
            printf '127.0.1.1\t%s\n' "$NEW_HOSTNAME" >> /etc/hosts
        fi
        echo "Hostname set: ${CURRENT_HOSTNAME} -> ${NEW_HOSTNAME}"
    fi
fi
SIPEOF

chmod +x /usr/local/bin/set-static-ip

# -------------------------------------------------------------------
# 11. Clean up for template conversion (not on --update: this would delete
# the database and the host keys of a VM in use)
# -------------------------------------------------------------------
# SSH last: everything else is installed, so a problem here leaves a complete VM.
switch_ssh_server

if [ -n "$UPDATE" ]; then
    log "=== Update install complete ==="
    exit 0
fi

log "Cleaning up for template conversion"

rm -f /etc/ssh/ssh_host_* /etc/dropbear/dropbear_*_host_key
: > /etc/machine-id 2>/dev/null || true
# DHCP wrote resolv.conf during the build's internet access. Once the VM goes
# static nothing refreshes it, so a clone would keep resolving through the
# build network's DNS server. Empty it (set-static-ip / the wizard set DNS).
: > /etc/resolv.conf 2>/dev/null || true
find /var/log -type f -exec truncate -s 0 {} \; 2>/dev/null || true
: > /root/.ash_history 2>/dev/null || true
apk cache clean 2>/dev/null || true
rm -rf /var/cache/apk/*

# Remove the DB and its backups if they were created during testing.
rm -f "$DB_DIR/topo.db"
rm -rf "$DB_DIR/backups"

# Remove any setup stamp left from build-time testing, or every clone would
# consider itself already configured and skip topo-setup.sh.
rm -f /etc/confetti-topo/.setup-done /etc/confetti-topo/.env-done

rm -f "${SCRIPT_DIR}/build-template.sh"

log "Zeroing free space for thin provisioning (this may take a minute)..."
dd if=/dev/zero of=/zero.fill bs=1M 2>/dev/null || true
rm -f /zero.fill
sync

log ""
log "=== Template build complete ==="
log ""
log "Next steps:"
log "  1. Shutdown:   poweroff"
log "  2. In vCenter: right-click VM -> Template -> Convert to Template"
log ""
log "To deploy a clone (guestinfo, zero-touch):"
log "  1. Clone from template"
log "  2. Set these guestinfo keys on the clone in vCenter, then boot:"
log "     guestinfo.topo.ip       10.0.0.101/24"
log "     guestinfo.topo.gateway  10.0.0.1"
log "     optional: guestinfo.topo.dns 10.0.0.53, guestinfo.topo.hostname"
log "               guestinfo.topo.ssh_username / ssh_password / ssh_secret /"
log "               poll_interval_s  (written into topo.env on first boot)"
log "  3. The dashboard starts automatically on port 80"
log ""
log "To deploy a clone (manual):"
log "  1. Clone from template, boot, log in (root / ${LAB_ROOT_PASSWORD})"
log "  2. topo-setup.sh runs automatically at login and prompts for the"
log "     static IP -- or run it by hand any time"
log "  3. The dashboard starts automatically on port 80"
