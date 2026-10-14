#!/usr/bin/env python3
"""Production entrypoint for the confetti-topo server.

Reads TOPO_PORT at runtime, so changing it in topo.env actually takes
effect (OpenRC expands command_args at parse time, before start_pre sources
the env file — see confetti-traffic's hub/serve.py, same constraint here).

Serves through waitress when available. Flask's built-in server is
single-threaded, which would queue the poller's and the dashboard's requests
behind one another; it remains as a fallback only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.app import app  # noqa: E402
from app import config  # noqa: E402


def _start_background_services():
    """Start the poller, the ICMP checker and the discovery timer.

    Each start() is idempotent and returns quietly on failure (nothing to
    poll yet, feature switched off): a missing background service must never
    stop the server from serving requests, which is the job that matters.
    """
    from app import poller, reach, discovery
    poller.start()
    reach.start()
    discovery.start()


def main():
    host = os.environ.get("TOPO_HOST", "0.0.0.0")
    port = config.PORT

    _start_background_services()

    try:
        from waitress import serve
    except ImportError:
        sys.stderr.write(
            "waitress not installed; falling back to the Flask development "
            "server (single-threaded, not recommended)\n"
        )
        app.run(host=host, port=port, threaded=True)
        return

    sys.stderr.write(f"confetti-topo listening on {host}:{port}\n")
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
