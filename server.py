#!/usr/bin/env python3
"""
xkg-stripe — Stripe checkout + license key issuance for XKG Desktop.

Endpoints:
  POST /api/checkout              Create Stripe Checkout Session (one-time or subscription)
  POST /api/webhook               Stripe webhook receiver (checkout.session.completed, invoice.paid)
  GET  /api/license/activate      Activate a license key on a device (called from xkg-desktop)
  GET  /api/license/verify        Verify a license key (called from xkg-desktop on startup)
  GET  /api/admin/licenses        List all issued licenses (admin token required)
  GET  /api/admin/stats           Sales stats (admin token)
  GET  /                          Health check

Pricing:
  - xkg-desktop-pro       $29 one-time        → pro license
  - xkg-desktop-pro-year  $108/yr ($9/mo)     → pro license, 365-day
  - xkg-cloud-sync        $5/mo               → sync subscription
  - xkg-cloud-sync-year   $48/yr              → sync subscription, 365-day
  - xkg-voice             $19 one-time        → voice add-on license
  - xkg-obsidian          $12 one-time        → obsidian add-on license
  - xkg-browser-ext       $9 one-time         → browser-ext add-on license
  - xkg-vps               $9/mo               → vps subscription
  - xkg-team              $29/mo              → team subscription
  - xkg-api               $39 one-time        → api add-on license
  - xkg-bundle            $199 one-time       → all add-ons license
  - xkg-hardware          $199+$39            → hardware (handled separately)

Storage: SQLite at /home/x2/.openclaw/workspace/xkg-stripe/xkg-stripe.db
Keys:     stored as raw strings (for dev). Production should hash + use a KMS.

Run:
  cd /home/x2/.openclaw/workspace/xkg-stripe
  STRIPE_SECRET_KEY=sk_test_xxx ADMIN_TOKEN=yourtoken python3 server.py
"""

import os
import json
import time
import hmac
import hashlib
import secrets
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, request, jsonify, abort

# Optional stripe import — server runs in "demo mode" without a key
try:
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_ENABLED = bool(stripe.api_key)
except ImportError:
    STRIPE_ENABLED = False

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "xkg-stripe.db"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token-change-me")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# Pricing (cents) — keep in sync with checkout.html
PRICES = {
    "pro":             {"amount": 2900,  "interval": None,    "name": "XKG Desktop Pro",          "kind": "license"},
    "pro-yearly":      {"amount": 10800, "interval": "year",  "name": "XKG Desktop Pro (yearly)", "kind": "license"},
    "cloud-sync":      {"amount": 500,   "interval": "month", "name": "Cloud Sync (monthly)",     "kind": "subscription"},
    "cloud-sync-year": {"amount": 4800,  "interval": "year",  "name": "Cloud Sync (yearly)",      "kind": "subscription"},
    "voice":           {"amount": 1900,  "interval": None,    "name": "Voice Capture",            "kind": "license"},
    "obsidian":        {"amount": 1200,  "interval": None,    "name": "Obsidian Export",          "kind": "license"},
    "browser-ext":     {"amount": 900,   "interval": None,    "name": "Browser Capture",          "kind": "license"},
    "vps":             {"amount": 900,   "interval": "month", "name": "Hosted VPS (5 GB)",        "kind": "subscription"},
    "team":            {"amount": 2900,  "interval": "month", "name": "Team Workspace",           "kind": "subscription"},
    "api":             {"amount": 3900,  "interval": None,    "name": "Public API Access",        "kind": "license"},
    "hardware":        {"amount": 19900, "interval": None,    "name": "Pre-flashed Hardware",     "kind": "physical"},
    "bundle":          {"amount": 19900, "interval": None,    "name": "Everything Bundle",        "kind": "license"},
}

# ── App + DB ─────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── CORS + Private Network Access ─────────────────────────────────────
# The static site is at seele.agency (Cloudflare Pages) and the API is at
# x2-nuc.tailb0d54b.ts.net (Tailscale Funnel). These are different origins,
# and the funnel's Tailscale hostname is in the "local network" address
# space from a browser's perspective, so we need:
#   1. CORS headers (Access-Control-Allow-Origin) for cross-origin fetches
#   2. Private Network Access (PNA) header response to the preflight
@app.after_request
def _add_cors_and_pna(resp):
    origin = request.headers.get("Origin")
    # Allow our known static origins + any funnel tailnet hostname + localhost
    if origin and (
        "seele.agency" in origin
        or "tailb0d54b.ts.net" in origin
        or "localhost" in origin
        or "127.0.0.1" in origin
    ):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Token, X-PAYMENT, Authorization"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Max-Age"] = "3600"
    # Private Network Access: respond to the PNA preflight so the browser
    # allows the public page (seele.agency) to call a local-network server
    # (x2-nuc.tailb0d54b.ts.net).
    if request.headers.get("Access-Control-Request-Private-Network") == "true":
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp

@app.route("/<path:_any>", methods=["OPTIONS"])
def _cors_preflight(_any):
    return ("", 204)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            stripe_session  TEXT UNIQUE,
            customer_email  TEXT,
            customer_name   TEXT,
            plan            TEXT NOT NULL,
            amount_cents    INTEGER NOT NULL,
            status          TEXT DEFAULT 'pending',
            created_at      TEXT NOT NULL,
            completed_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS licenses (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key  TEXT UNIQUE NOT NULL,
            plan         TEXT NOT NULL,
            email        TEXT,
            order_id     INTEGER REFERENCES orders(id),
            issued_at    TEXT NOT NULL,
            expires_at   TEXT,
            revoked      INTEGER DEFAULT 0,
            activations  INTEGER DEFAULT 0,
            max_activations INTEGER DEFAULT 3
        );
        CREATE TABLE IF NOT EXISTS activations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key  TEXT NOT NULL,
            device_id    TEXT NOT NULL,
            device_name  TEXT,
            os           TEXT,
            activated_at TEXT NOT NULL,
            UNIQUE(license_key, device_id)
        );
        CREATE TABLE IF NOT EXISTS support_tickets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_ref   TEXT UNIQUE NOT NULL,
            name         TEXT,
            email        TEXT,
            topic        TEXT,
            subject      TEXT,
            message      TEXT,
            status       TEXT DEFAULT 'open',
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_licenses_email ON licenses(email);
        CREATE INDEX IF NOT EXISTS idx_activations_license ON activations(license_key);
    """)
    conn.commit()
    conn.close()


init_db()


# ── Helpers ──────────────────────────────────────────────────────────────
def make_license_key():
    """Generate a license key like XKG-XXXX-XXXX-XXXX-XXXX."""
    parts = []
    for _ in range(4):
        # Avoid ambiguous chars (0/O, 1/I/L)
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        parts.append("".join(secrets.choice(alphabet) for _ in range(4)))
    return f"XKG-{'-'.join(parts)}"


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Admin-Token", "")
        if not hmac.compare_digest(token, ADMIN_TOKEN):
            abort(401, description="admin token required")
        return f(*args, **kwargs)
    return wrapper


def now():
    return datetime.now(timezone.utc).isoformat()


# ── Routes ───────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({
        "service": "xkg-stripe",
        "version": "0.1.0",
        "stripe_enabled": STRIPE_ENABLED,
        "license_keys_issued": db().execute("SELECT COUNT(*) AS n FROM licenses").fetchone()["n"],
    })


@app.route("/api/checkout", methods=["POST"])
def checkout():
    """Create a Stripe Checkout Session for the given plan."""
    body = request.get_json(force=True, silent=True) or {}
    plan = body.get("plan", "pro")
    email = body.get("email", "")
    success_url = body.get("success_url", "https://seele.agency/dashboard.html?session={CHECKOUT_SESSION_ID}")
    cancel_url = body.get("cancel_url", "https://seele.agency/checkout.html?cancelled=1")

    if plan not in PRICES:
        return jsonify({"error": f"unknown plan: {plan}"}), 400

    cfg = PRICES[plan]

    # Demo mode (no Stripe key) — issue a fake session
    if not STRIPE_ENABLED:
        session_id = "cs_demo_" + secrets.token_urlsafe(16)
        conn = db()
        conn.execute(
            "INSERT INTO orders (stripe_session, customer_email, plan, amount_cents, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (session_id, email, plan, cfg["amount"], now()),
        )
        conn.commit()
        conn.close()
        return jsonify({
            "mode": "demo",
            "session_id": session_id,
            "checkout_url": success_url.replace("{CHECKOUT_SESSION_ID}", session_id),
            "plan": plan,
            "amount": cfg["amount"],
        })

    # Real Stripe Checkout
    line_items = [{
        "price_data": {
            "currency": "usd",
            "unit_amount": cfg["amount"],
            "product_data": {"name": cfg["name"]},
            **({"recurring": {"interval": cfg["interval"]}} if cfg["interval"] else {}),
        },
        "quantity": 1,
    }]

    checkout_mode = "subscription" if cfg["interval"] else "payment"

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=line_items,
        mode=checkout_mode,
        customer_email=email or None,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"plan": plan, "email": email or ""},
    )

    conn = db()
    conn.execute(
        "INSERT INTO orders (stripe_session, customer_email, plan, amount_cents, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
        (session.id, email, plan, cfg["amount"], now()),
    )
    conn.commit()
    conn.close()

    return jsonify({
        "mode": "live",
        "session_id": session.id,
        "checkout_url": session.url,
        "plan": plan,
        "amount": cfg["amount"],
    })


@app.route("/api/webhook", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhook events."""
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")

    if STRIPE_ENABLED and WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
        except (ValueError, stripe.error.SignatureVerificationError):
            return "bad signature", 400
    else:
        # Demo mode — accept JSON event directly
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return "bad json", 400

    etype = event.get("type", "") if isinstance(event, dict) else event.type
    data = event.get("data", {}).get("object", {}) if isinstance(event, dict) else event.data.object

    if etype in ("checkout.session.completed", "invoice.paid"):
        session_id = data.get("id") if isinstance(data, dict) else data.id
        # Demo fallback: read from the JSON for non-Stripe test events
        email = (data.get("customer_email") or data.get("customer_details", {}).get("email") or "") if isinstance(data, dict) else ""
        plan = (data.get("metadata", {}).get("plan") or "pro") if isinstance(data, dict) else "pro"

        conn = db()
        order = conn.execute("SELECT * FROM orders WHERE stripe_session=?", (session_id,)).fetchone()
        if order is None:
            # Auto-create an order row if this is a fresh demo event
            cfg = PRICES.get(plan, PRICES["pro"])
            cur = conn.execute(
                "INSERT INTO orders (stripe_session, customer_email, plan, amount_cents, status, created_at, completed_at) VALUES (?, ?, ?, ?, 'completed', ?, ?)",
                (session_id, email, plan, cfg["amount"], now(), now()),
            )
            order_id = cur.lastrowid
        else:
            conn.execute("UPDATE orders SET status='completed', completed_at=?, customer_email=? WHERE id=?", (now(), email or order["customer_email"], order["id"]))
            order_id = order["id"]

        # Issue license (unless this is a subscription — we'll issue on each renewal)
        if etype == "checkout.session.completed":
            cfg = PRICES.get(plan, PRICES["pro"])
            key = make_license_key()
            expires = None
            if cfg.get("interval") == "year":
                expires = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
            conn.execute(
                "INSERT INTO licenses (license_key, plan, email, order_id, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key, plan, email or order["customer_email"], order_id, now(), expires),
            )

        conn.commit()
        conn.close()

    return "", 200


@app.route("/api/license/activate", methods=["POST"])
def license_activate():
    """Activate a license key on a device (called from xkg-desktop on first launch)."""
    body = request.get_json(force=True, silent=True) or {}
    key = (body.get("license_key") or "").strip().upper()
    device_id = body.get("device_id", "")
    device_name = body.get("device_name", "")
    os_name = body.get("os", "")

    if not key or not device_id:
        return jsonify({"error": "license_key and device_id required"}), 400

    conn = db()
    lic = conn.execute("SELECT * FROM licenses WHERE license_key=? AND revoked=0", (key,)).fetchone()
    if lic is None:
        conn.close()
        return jsonify({"error": "invalid or revoked license key"}), 404

    # Check expiry
    if lic["expires_at"]:
        try:
            expires = datetime.fromisoformat(lic["expires_at"])
            if expires < datetime.now(timezone.utc):
                conn.close()
                return jsonify({"error": "license expired", "expired_at": lic["expires_at"]}), 403
        except ValueError:
            pass

    # Check activation limit
    n = conn.execute("SELECT COUNT(*) AS n FROM activations WHERE license_key=?", (key,)).fetchone()["n"]
    if n >= lic["max_activations"]:
        # Allow re-activating same device
        already = conn.execute("SELECT 1 FROM activations WHERE license_key=? AND device_id=?", (key, device_id)).fetchone()
        if not already:
            conn.close()
            return jsonify({"error": f"max activations ({lic['max_activations']}) reached"}), 403

    conn.execute(
        "INSERT OR REPLACE INTO activations (license_key, device_id, device_name, os, activated_at) VALUES (?, ?, ?, ?, ?)",
        (key, device_id, device_name, os_name, now()),
    )
    conn.execute("UPDATE licenses SET activations=(SELECT COUNT(*) FROM activations WHERE license_key=?) WHERE license_key=?", (key, key))
    conn.commit()
    conn.close()

    return jsonify({
        "valid": True,
        "plan": lic["plan"],
        "email": lic["email"],
        "issued_at": lic["issued_at"],
        "expires_at": lic["expires_at"],
        "activations": n + 1,
        "max_activations": lic["max_activations"],
        "features": license_features(lic["plan"]),
    })


@app.route("/api/license/verify", methods=["POST"])
def license_verify():
    """Quick verify without recording a new activation."""
    body = request.get_json(force=True, silent=True) or {}
    key = (body.get("license_key") or "").strip().upper()
    device_id = body.get("device_id", "")

    conn = db()
    lic = conn.execute("SELECT * FROM licenses WHERE license_key=? AND revoked=0", (key,)).fetchone()
    if lic is None:
        conn.close()
        return jsonify({"valid": False, "reason": "invalid"}), 404

    activated_here = False
    if device_id:
        activated_here = bool(conn.execute("SELECT 1 FROM activations WHERE license_key=? AND device_id=?", (key, device_id)).fetchone())

    conn.close()
    return jsonify({
        "valid": True,
        "plan": lic["plan"],
        "expires_at": lic["expires_at"],
        "activated_on_this_device": activated_here,
        "features": license_features(lic["plan"]),
    })


def license_features(plan: str) -> dict:
    base = {"providers": 4, "max_history_days": 365, "export": True, "priority_support": False}
    if plan in ("pro", "pro-yearly", "bundle"):
        return {**base, "providers": 4, "max_history_days": None, "priority_support": True}
    if plan == "team":
        return {**base, "providers": 4, "max_history_days": None, "priority_support": True, "team_seats": 10}
    if plan == "api":
        return {**base, "providers": 4, "max_history_days": None, "priority_support": True, "api_access": True}
    return base


# ── Support form → ticket (called from support.html) ──────────────────────
@app.route("/api/support/submit", methods=["POST"])
def support_submit():
    body = request.get_json(force=True, silent=True) or {}
    name = body.get("name", "").strip()
    email = body.get("email", "").strip()
    topic = body.get("topic", "")
    subject = body.get("subject", "")
    message = body.get("message", "")
    if not email or not message:
        return jsonify({"error": "email and message required"}), 400

    ref = "TKT-" + secrets.token_hex(4).upper()
    conn = db()
    conn.execute(
        "INSERT INTO support_tickets (ticket_ref, name, email, topic, subject, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ref, name, email, topic, subject, message, now()),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "ticket_ref": ref})


# ── Admin ────────────────────────────────────────────────────────────────
@app.route("/api/admin/licenses")
@require_admin
def admin_licenses():
    conn = db()
    rows = conn.execute("SELECT * FROM licenses ORDER BY issued_at DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/orders")
@require_admin
def admin_orders():
    conn = db()
    rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/tickets")
@require_admin
def admin_tickets():
    conn = db()
    rows = conn.execute("SELECT * FROM support_tickets ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/tickets/<ref>/close", methods=["POST"])
@require_admin
def admin_ticket_close(ref):
    conn = db()
    conn.execute("UPDATE support_tickets SET status='closed' WHERE ticket_ref=?", (ref,))
    conn.commit()
    n = conn.execute("SELECT changes() AS n").fetchone()["n"]
    conn.close()
    return jsonify({"ok": bool(n), "updated": n})


@app.route("/api/admin/licenses/<key>/revoke", methods=["POST"])
@require_admin
def admin_license_revoke(key):
    conn = db()
    conn.execute("UPDATE licenses SET revoked=1 WHERE license_key=?", (key,))
    conn.commit()
    n = conn.execute("SELECT changes() AS n").fetchone()["n"]
    conn.close()
    return jsonify({"ok": bool(n), "updated": n})


@app.route("/api/admin/stats")
@require_admin
def admin_stats():
    conn = db()
    return jsonify({
        "orders_total": conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"],
        "orders_completed": conn.execute("SELECT COUNT(*) AS n FROM orders WHERE status='completed'").fetchone()["n"],
        "revenue_cents": conn.execute("SELECT COALESCE(SUM(amount_cents),0) AS n FROM orders WHERE status='completed'").fetchone()["n"],
        "licenses_issued": conn.execute("SELECT COUNT(*) AS n FROM licenses").fetchone()["n"],
        "licenses_active": conn.execute("SELECT COUNT(*) AS n FROM licenses WHERE revoked=0").fetchone()["n"],
        "activations": conn.execute("SELECT COUNT(*) AS n FROM activations").fetchone()["n"],
        "support_tickets_open": conn.execute("SELECT COUNT(*) AS n FROM support_tickets WHERE status='open'").fetchone()["n"],
        "support_tickets_total": conn.execute("SELECT COUNT(*) AS n FROM support_tickets").fetchone()["n"],
        "by_plan": [dict(r) for r in conn.execute("SELECT plan, COUNT(*) AS n, COALESCE(SUM(amount_cents),0) AS revenue FROM orders WHERE status='completed' GROUP BY plan ORDER BY n DESC").fetchall()],
        "recent_orders_24h": conn.execute("SELECT COUNT(*) AS n FROM orders WHERE created_at > datetime('now', '-1 day')").fetchone()["n"],
        "recent_revenue_24h_cents": conn.execute("SELECT COALESCE(SUM(amount_cents),0) AS n FROM orders WHERE status='completed' AND completed_at > datetime('now', '-1 day')").fetchone()["n"],
    })


# ── x402 crypto payment routes ──────────────────────────────────────────
# Lazy-load the x402 module and register its routes
def issue_license_for_plan(conn, order_id, plan, email):
    """Issue a license key for a given order/plan. Used by Stripe webhook and x402 settle."""
    cfg = PRICES.get(plan, PRICES["pro"])
    key = make_license_key()
    expires = None
    if cfg.get("interval") == "year":
        expires = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    conn.execute(
        "INSERT INTO licenses (license_key, plan, email, order_id, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (key, plan, email, order_id, now(), expires),
    )
    return key

try:
    from x402_routes import register_routes as register_x402
    register_x402(app, db, now, make_license_key, issue_license_for_plan, PRICES, ADMIN_TOKEN)
    print("[xkg-stripe] x402 crypto payment routes registered")
except ImportError as e:
    print(f"[xkg-stripe] x402 routes NOT loaded: {e}")


# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8095))
    print(f"[xkg-stripe] listening on 0.0.0.0:{port}, stripe={'enabled' if STRIPE_ENABLED else 'DEMO mode'}, admin_token={'set' if ADMIN_TOKEN != 'dev-admin-token-change-me' else 'INSECURE default'}")
    app.run(host="0.0.0.0", port=port, debug=False)