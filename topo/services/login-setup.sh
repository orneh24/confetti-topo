# /etc/profile.d/confetti-topo-setup.sh — invite an unconfigured confetti-topo VM
# to run its setup wizard at first interactive login. Guarded to interactive
# shells with a real tty so it never fires for scp/rsync/non-interactive SSH
# commands. Identical pattern to confetti-traffic's hub/services/confettictl-login-setup.sh.

case "$-" in
    *i*)
        if [ -t 0 ] && [ -f /opt/confetti-topo/BUILD ]; then
            echo "  confetti-topo $(cat /opt/confetti-topo/BUILD)"
        fi
        if [ -t 0 ] && [ ! -f /etc/confetti-topo/.setup-done ]; then
            /opt/confetti-topo/topo-setup.sh || true
        elif [ -t 0 ]; then
            echo "  To re-run setup script:       topo-setup.sh"
            echo "  To re-configure static IP:    set-static-ip <ip/cidr> <gateway> [dns] [hostname]"
            echo
        fi
        ;;
esac
