# confetti-topo as a container, for development, demos and trying it out
# without an ESXi VM. The supported deployment is still the Alpine VM
# (topo/build-topo.sh); this image uses the same Alpine packages and the
# same pinned pip packages.
#
# Build (from the repo root):
#   docker build -f container/Dockerfile -t confetti-topo topo
# Run: see container/docker-compose.yml.

FROM alpine:3.24

# Same package set as build-topo.sh, minus what only makes sense on a VM
# (open-vm-tools, chrony, sshd, OpenRC services). curl is for the
# HEALTHCHECK, sqlite for the backup tool.
RUN apk add --no-cache \
        python3 py3-pip py3-flask py3-paramiko py3-yaml py3-requests py3-jinja2 \
        sqlite curl

COPY requirements.txt /opt/confetti-topo/requirements.txt
# netmiko and waitress are not Alpine packages; install them at the pinned versions.
RUN pip3 install --no-cache-dir --break-system-packages \
        "$(grep -i '^netmiko==' /opt/confetti-topo/requirements.txt)" \
        "$(grep -i '^waitress==' /opt/confetti-topo/requirements.txt)"

COPY app /opt/confetti-topo/app
COPY templates /opt/confetti-topo/templates
COPY static /opt/confetti-topo/static
COPY seed /opt/confetti-topo/seed
COPY serve.py /opt/confetti-topo/serve.py

# TOPO_HEALTH_SERVICES is empty: there is no OpenRC in a container to ask.
ENV TOPO_DB_PATH=/var/lib/confetti-topo/topo.db \
    TOPO_PORT=80 \
    TOPO_HEALTH_SERVICES=

WORKDIR /opt/confetti-topo
VOLUME /var/lib/confetti-topo
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD curl -fs "http://127.0.0.1:${TOPO_PORT}/api/health" >/dev/null || exit 1

CMD ["python3", "/opt/confetti-topo/serve.py"]
