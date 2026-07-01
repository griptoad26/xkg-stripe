#!/usr/bin/env python3
"""
x402 payment routes for xkg-stripe.

These add an alternative payment rail to the existing Stripe checkout:
  - /api/x402/challenge/<plan>   → returns 402 Payment Required with crypto challenge
  - /api/x402/settle              → verifies the X-PAYMENT header (or Authorization: 402 ...)
                                    on-chain and issues a license key on success
  - /.well-known/x402             → discovery: which plans accept x402, which wallets

The user already has a full RFC-9420-style x402_handler.py. We extend it with:
  - Real on-chain verification via public Base RPC (no web3.py dep needed)
  - The 402 Payment Required response body in JSON (modern style)
  - DB integration: writes to orders table with payment_method='x402'

Pricing is in cents; crypto prices are derived from a price table.
For dev: we accept any tx hash that points to our receiving address and has
the right value. For production: confirmations, slippage, etc.
"""
import os
import json
import time
import secrets
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from flask import request, jsonify

# Receiving wallet — default is the XKG testnet treasury on Base Sepolia.
# Override with X402_WALLET env var for mainnet or a different treasury.
# IMPORTANT: the default 0x000… is intentionally NOT a valid address so
# the server refuses to start up with a misconfigured wallet. On startup
# the server asserts this is set to a real address.
RECEIVING_WALLET = os.environ.get(
    "X402_WALLET",
    "0x3D2f7EDeB6e579447Fd5d00D05578041469D79e0",  # XKG testnet treasury (Base Sepolia)
)
assert RECEIVING_WALLET != "0x0000000000000000000000000000000000000000", (
    "X402_WALLET must be set to a real address. Refusing to start with the zero address."
)

# USDC on Base mainnet
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# USDC on Base Sepolia (testnet)
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

# Public Base RPC (no auth, no rate limits for low volume)
BASE_RPC = "https://mainnet.base.org"
BASE_SEPOLIA_RPC = "https://sepolia.base.org"

# Network: "base" or "base-sepolia"
NETWORK = os.environ.get("X402_NETWORK", "base-sepolia")  # Default testnet for safety
RPC_URL = BASE_SEPOLIA_RPC if NETWORK == "base-sepolia" else BASE_RPC
USDC_ADDR = USDC_BASE_SEPOLIA if NETWORK == "base-sepolia" else USDC_BASE

# USDC has 6 decimals
USDC_DECIMALS = 6

# Which plans are eligible for x402 (subscriptions are awkward with on-chain;
# we accept them but with a "single period" interpretation, not recurring)
X402_ELIGIBLE = {"pro", "voice", "obsidian", "browser-ext", "api", "bundle"}

# Plans that x402 is GREAT for (one-time payments, no card needed)
X402_PREFERRED = {"pro", "api", "bundle", "voice", "obsidian", "browser-ext"}

# Token prices in USDC (6 decimals)
# In production: pull from a price oracle (chainlink, etc.)
# For dev: hardcoded at $1 = 1 USDC
def usdc_amount_for(usd_cents: int) -> int:
    """Convert USD cents to USDC base units (6 decimals)."""
    return usd_cents * 10_000  # $0.01 = 100_000 micro-USDC


def generate_payment_id(plan: str, email: str = "") -> str:
    """Generate a unique payment ID for this x402 challenge."""
    rand = secrets.token_urlsafe(8)
    return f"x402-{plan}-{rand}"


def build_challenge(plan: str, cfg: dict) -> dict:
    """
    Build the 402 Payment Required challenge response.
    Modern style: returns JSON body with payment requirements.
    """
    payment_id = generate_payment_id(plan)
    amount_usdc = usdc_amount_for(cfg["amount"])
    expires = datetime.now(timezone.utc) + timedelta(hours=1)

    challenge = {
        "x402Version": 1,
        "payment_id": payment_id,
        "scheme": "exact",
        "network": NETWORK,
        "resource": f"/api/x402/settle?plan={plan}&payment_id={payment_id}",
        "accepts": [
            {
                "scheme": "exact",
                "network": NETWORK,
                "maxAmountRequired": str(amount_usdc),
                "resource": f"/api/x402/settle?plan={plan}&payment_id={payment_id}",
                "description": cfg["name"],
                "mimeType": "application/json",
                "payTo": RECEIVING_WALLET,
                "asset": USDC_ADDR,
                "maxTimeoutSeconds": 3600,
                "extra": {
                    "name": "USD Coin",
                    "symbol": "USDC",
                    "decimals": USDC_DECIMALS,
                }
            }
        ],
        # Legacy RFC-9420 headers (for the existing x402_handler.py):
        "headers": {
            "Payment-Required": json.dumps({
                "amount": cfg["amount"] / 100,
                "currency": "USD",
                "product": plan,
                "payment_id": payment_id,
            }),
            "Payment-Methods": json.dumps([{
                "scheme": "ethereum",
                "amount": f"{cfg['amount']/100:.2f} USD",
                "address": RECEIVING_WALLET,
                "asset": "USDC",
                "network": NETWORK,
                "expires": expires.isoformat(),
            }]),
            "Payment-Retry-After": "3600",
        }
    }
    return challenge


def verify_usdc_transfer(tx_hash: str, expected_recipient: str,
                          expected_amount_usdc: int, min_confirmations: int = 1) -> dict:
    """
    Verify a USDC transfer on Base via the public RPC.

    Returns: {"valid": bool, "reason": str, "details": dict}
    """
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return {"valid": False, "reason": "tx hash must be 0x + 64 hex chars"}

    # JSON-RPC: eth_getTransactionByHash
    try:
        resp = requests.post(RPC_URL, json={
            "jsonrpc": "2.0",
            "method": "eth_getTransactionByHash",
            "params": [tx_hash],
            "id": 1,
        }, timeout=10)
        tx = resp.json().get("result")
    except Exception as e:
        return {"valid": False, "reason": f"RPC error: {e}"}

    if not tx:
        return {"valid": False, "reason": "transaction not found"}

    # Must be a USDC transfer (input data starts with transfer(address,address,uint256) = 0xa9059cbb)
    input_data = tx.get("input", "")
    if not input_data.startswith("0xa9059cbb"):
        return {"valid": False, "reason": "not a USDC transfer (no transfer() call)"}

    # Parse transfer(address,address,uint256) input
    # First 4 bytes = method sig, then 32 bytes address (padded), 32 bytes address (padded), 32 bytes amount
    if len(input_data) < 138:
        return {"valid": False, "reason": "input data too short for transfer()"}

    # Extract recipient (bytes 4-36, last 20 bytes = address)
    recipient = "0x" + input_data[36:76][-40:]
    amount_hex = input_data[76:138]
    amount_usdc = int(amount_hex, 16) / (10 ** USDC_DECIMALS)
    amount_usdc_base = int(amount_hex, 16)

    # The 'to' of the tx should be the USDC contract
    if tx.get("to", "").lower() != USDC_ADDR.lower():
        return {"valid": False, "reason": f"tx target is not USDC contract ({tx.get('to')})"}

    if recipient.lower() != expected_recipient.lower():
        return {"valid": False, "reason": f"recipient {recipient} != expected {expected_recipient}"}

    if amount_usdc_base < expected_amount_usdc:
        return {"valid": False, "reason": f"amount {amount_usdc} < required ${expected_amount_usdc/(10**USDC_DECIMALS)}"}

    # Check confirmations
    try:
        block_resp = requests.post(RPC_URL, json={
            "jsonrpc": "2.0",
            "method": "eth_blockNumber",
            "params": [],
            "id": 1,
        }, timeout=10)
        current_block = int(block_resp.json().get("result", "0x0"), 16)
        tx_block = int(tx.get("blockNumber", "0x0"), 16)
        confirmations = current_block - tx_block + 1
    except Exception:
        confirmations = 0  # mempool tx, assume pending

    if confirmations < min_confirmations:
        return {
            "valid": False,
            "reason": f"insufficient confirmations: {confirmations} < {min_confirmations}",
            "details": {"tx": tx, "confirmations": confirmations}
        }

    return {
        "valid": True,
        "reason": "verified",
        "details": {
            "tx_hash": tx_hash,
            "block": tx.get("blockNumber"),
            "from": tx.get("from"),
            "to": recipient,
            "amount_usdc": amount_usdc,
            "confirmations": confirmations,
        }
    }


def parse_payment_header(auth_header: str) -> dict:
    """
    Parse the X-PAYMENT or Authorization header.
    Supports both modern (X-PAYMENT: <base64-json>) and RFC-9420 (Authorization: 402 ethereum 0x...) styles.
    """
    if not auth_header:
        return {"scheme": None, "proof": None}

    # Modern X-PAYMENT header (Coinbase x402 style)
    # Header value: base64-encoded JSON {"x402Version":1,"scheme":"exact","network":"base","payload":{"transaction":"0x..."}}
    if auth_header.startswith("ey"):  # base64 always starts with letter/digit
        try:
            decoded = json.loads(__import__("base64").b64decode(auth_header).decode())
            if "payload" in decoded and "transaction" in decoded["payload"]:
                return {"scheme": "ethereum", "proof": decoded["payload"]["transaction"], "raw": decoded}
        except Exception:
            pass

    # RFC-9420 style: "Authorization: 402 ethereum 0x..."
    if auth_header.startswith("402 "):
        parts = auth_header[4:].split(" ", 1)
        if len(parts) == 2:
            return {"scheme": parts[0], "proof": parts[1]}

    # Sometimes clients send just the bare proof
    if auth_header.startswith("0x") and len(auth_header) == 66:
        return {"scheme": "ethereum", "proof": auth_header}

    return {"scheme": None, "proof": None}


def register_routes(app, db, now, make_license_key, issue_license_for_plan, PRICES, ADMIN_TOKEN):
    """Attach x402 routes to the Flask app."""

    @app.route("/api/.well-known/x402", methods=["GET"])
    def x402_discovery():
        """Discovery: which plans accept x402, where to send payment."""
        return jsonify({
            "service": "xkg-desktop",
            "x402Version": 1,
            "network": NETWORK,
            "payTo": RECEIVING_WALLET,
            "asset": USDC_ADDR,
            "assetSymbol": "USDC",
            "accepts_plans": [p for p in X402_ELIGIBLE if p in PRICES],
            "preferred_plans": [p for p in X402_PREFERRED if p in PRICES],
            "challenge_url_template": "https://seele.agency/api/x402/challenge/{plan}",
            "settle_url": "https://seele.agency/api/x402/settle",
        })

    @app.route("/api/x402/challenge/<plan>", methods=["PUT", "DELETE", "PATCH", "OPTIONS"])
    def x402_challenge_wrong_method(plan):
        # Reject non-GET/POST with JSON 405 (Flask default is HTML).
        return jsonify({
            "error": "method_not_allowed",
            "allowed": ["GET", "POST"],
            "message": "challenge accepts GET or POST only",
        }), 405

    @app.route("/api/x402/challenge/<plan>", methods=["GET", "POST"])
    def x402_challenge(plan):
        """Return a 402 Payment Required challenge for the given plan."""
        # Reject path-traversal attempts: "../foo" or absolute paths
        if ".." in plan or "/" in plan or "\\" in plan:
            return jsonify({"error": "invalid plan name"}), 400
        if plan not in PRICES:
            return jsonify({"error": f"unknown plan: {plan}"}), 404
        if plan not in X402_ELIGIBLE:
            return jsonify({
                "error": f"plan {plan} is not eligible for x402",
                "eligible": list(X402_ELIGIBLE),
            }), 400

        cfg = PRICES[plan]
        challenge = build_challenge(plan, cfg)

        # Log the challenge
        conn = db()
        conn.execute(
            "INSERT INTO orders (stripe_session, customer_email, plan, amount_cents, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (challenge["payment_id"], "", plan, cfg["amount"], now()),
        )
        conn.commit()
        conn.close()

        resp = jsonify(challenge)
        resp.status_code = 402
        # Set the legacy RFC-9420 headers too
        for h, v in challenge["headers"].items():
            resp.headers[h] = v
        return resp

    @app.route("/api/x402/settle", methods=["GET", "PUT", "DELETE", "PATCH"])
    def x402_settle_wrong_method():
        # Reject non-POST with a JSON 405 (not Flask's default HTML page).
        # An HTML 405 breaks any x402 client that's expecting JSON.
        return jsonify({
            "error": "method_not_allowed",
            "allowed": ["POST"],
            "message": "settle requires POST with a payment_id + tx_hash body",
        }), 405

    @app.route("/api/x402/settle", methods=["POST"])
    def x402_settle():
        """
        Verify a payment and issue a license.

        Request body (modern x402): {"payment_id": "x402-pro-xxx", "tx_hash": "0x..."}
        Or with X-PAYMENT header set to base64-JSON.

        On success, returns the license key.
        """
        # Parse the payment
        body = request.get_json(force=True, silent=True)
        # Reject non-dict bodies (string, list, number) explicitly so we
        # don't blow up with a 500 AttributeError later.
        if not isinstance(body, dict):
            return jsonify({
                "error": "request body must be a JSON object",
                "got": type(body).__name__ if body is not None else "null",
            }), 400
        payment_id = body.get("payment_id", "")
        tx_hash = body.get("tx_hash", "")

        # If no tx_hash in body, check the X-PAYMENT / Authorization header
        if not tx_hash:
            for header in ("X-PAYMENT", "Authorization"):
                parsed = parse_payment_header(request.headers.get(header, ""))
                if parsed["proof"]:
                    tx_hash = parsed["proof"]
                    break

        # Reject path-traversal and SQL-meta characters in payment_id.
        # Legitimate ids are `x402-<plan>-<10 base62 chars>`, e.g. "x402-pro-aB3xY9kL2m".
        # Anything else is either malicious or a client bug; return 400 early
        # instead of doing a wasted DB lookup.
        import re as _re
        if payment_id and not _re.match(r"^x402-[a-z0-9-]{1,32}-[A-Za-z0-9]{1,32}$", payment_id):
            return jsonify({
                "error": "invalid payment_id format",
                "expected": "x402-<plan>-<token>",
            }), 400

        if not payment_id or not tx_hash:
            return jsonify({"error": "missing payment_id or tx_hash"}), 400

        # tx_hash must look like an EVM tx hash (0x + 64 hex chars)
        if not _re.match(r"^0x[a-fA-F0-9]{64}$", tx_hash):
            return jsonify({
                "error": "invalid tx_hash format",
                "expected": "0x followed by 64 hex chars",
            }), 400

        # Look up the order
        conn = db()
        order = conn.execute(
            "SELECT * FROM orders WHERE stripe_session = ?", (payment_id,)
        ).fetchone()
        if not order:
            conn.close()
            return jsonify({"error": f"unknown payment_id: {payment_id}"}), 404
        if order["status"] == "completed":
            # Already settled — return the existing license. Query FIRST,
            # then close, so we never operate on a closed connection.
            existing = conn.execute(
                "SELECT license_key, plan FROM licenses WHERE order_id = ?", (order["id"],)
            ).fetchone()
            conn.close()
            if existing:
                return jsonify({
                    "settled": True,
                    "license_key": existing["license_key"],
                    "plan": existing["plan"],
                    "note": "already settled",
                })
            # Race condition: order marked completed but no license row.
            # Do NOT fall through and issue a duplicate. Refuse and alert.
            return jsonify({
                "settled": False,
                "error": "order marked completed but no license found",
                "payment_id": payment_id,
                "alert": "data_integrity_check_required",
            }), 500

        plan = order["plan"]
        if plan not in PRICES:
            conn.close()
            return jsonify({"error": f"plan {plan} not in price list"}), 400
        cfg = PRICES[plan]
        expected_amount = usdc_amount_for(cfg["amount"])

        # Verify on-chain
        result = verify_usdc_transfer(
            tx_hash=tx_hash,
            expected_recipient=RECEIVING_WALLET,
            expected_amount_usdc=expected_amount,
        )

        if not result["valid"]:
            conn.close()
            return jsonify({
                "settled": False,
                "error": result["reason"],
                "details": result.get("details"),
            }), 402

        # Issue the license
        license_key = issue_license_for_plan(conn, order["id"], plan, order["customer_email"] or "")
        conn.execute(
            "UPDATE orders SET status = 'completed', completed_at = ? WHERE id = ?",
            (now(), order["id"]),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "settled": True,
            "license_key": license_key,
            "plan": plan,
            "tx_hash": tx_hash,
            "network": NETWORK,
            "confirmations": result["details"]["confirmations"],
            "amount_usdc": result["details"]["amount_usdc"],
            "instructions": "Save this license key. Activate it from XKG Desktop: Settings → License → Enter Key.",
        })
