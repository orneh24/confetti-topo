"""Optional access token for the UI and API.

Off unless TOPO_AUTH_TOKEN is set; then nothing changes for anyone who does not set it. When it is set, a request
needs one of: the cookie that /login hands out, "Authorization: Bearer <token>", or "X-Topo-Token: <token>".
/api/health (so monitoring and the header dot keep working), /login, /logout and /static stay open.

This is one shared secret, not user accounts, and plain HTTP sends it in the clear: it keeps a casual visitor out,
it does not replace a firewall or TLS in front of the server. The cookie holds a hash of the token, not the token.
"""

import hashlib
import hmac
import time

import urllib.parse

from flask import Response, jsonify, redirect, render_template, request

from . import config

COOKIE = "topo_auth"
OPEN_PATHS = ("/api/health", "/login", "/logout")
COOKIE_MAX_AGE_S = 30 * 24 * 3600


def _digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _same(a, b):
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def allowed():
    """True when this request carries the token (or when no token is configured)."""
    token = config.AUTH_TOKEN
    if not token:
        return True
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer ") and _same(header[7:].strip(), token):
        return True
    sent = request.headers.get("X-Topo-Token")
    if sent and _same(sent, token):
        return True
    cookie = request.cookies.get(COOKIE)
    return bool(cookie) and _same(cookie, _digest(token))


def check():
    """before_request: let the request through, answer 401 (API, metrics) or send a page to /login."""
    if not config.AUTH_TOKEN or request.path in OPEN_PATHS or request.path.startswith("/static/"):
        return None
    if allowed():
        return None
    if request.path.startswith("/api/") or request.path == "/metrics":
        return jsonify({"error": "access token required"}), 401
    return redirect("/login?next=" + urllib.parse.quote(request.path))


def _safe_next(value):
    """Where to go after login: a path on this server only (no //host, no scheme)."""
    return value if value and value.startswith("/") and not value.startswith("//") and "\\" not in value else "/"


def register(app):
    app.before_request(check)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        nxt = _safe_next(request.values.get("next", "/"))
        if not config.AUTH_TOKEN:
            return redirect(nxt)
        error = ""
        if request.method == "POST":
            if _same(request.form.get("token", ""), config.AUTH_TOKEN):
                resp = redirect(nxt)
                resp.set_cookie(COOKIE, _digest(config.AUTH_TOKEN), max_age=COOKIE_MAX_AGE_S,
                                httponly=True, samesite="Strict")
                return resp
            time.sleep(1)   # a wrong guess costs a second
            error = "Wrong token."
        return render_template("login.html", next=nxt, error=error), (401 if error else 200)

    @app.route("/logout")
    def logout():
        resp = redirect("/login")
        resp.delete_cookie(COOKIE)
        return resp
