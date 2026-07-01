#!/usr/bin/env python3
"""
Tiny path-aware reverse proxy for the Tailscale funnel.

Routes:
  /api/x402/*  → 127.0.0.1:8095   (xkg-stripe public crypto API)
  /api/checkout → 127.0.0.1:8095   (xkg-stripe checkout)
  /api/webhook → 127.0.0.1:8095    (stripe webhook)
  /api/admin/* → 127.0.0.1:8095    (admin API)
  /api/license/* → 127.0.0.1:8095  (license verify/activate)
  /*           → 127.0.0.1:18789  (openclaw gateway — for tailnet web access)

Listens on 0.0.0.0:8089 and is intended to be exposed via `tailscale funnel 8089`.
"""
import http.server
import socketserver
import urllib.request
import urllib.error
from urllib.parse import urlparse

# Backend map: path-prefix → backend (host, port)
# Order matters: most specific match wins.
# - /pay/*  → xkg-payments (new multi-processor: card, x402, LS, PayPal, Amazon)
# - /api/x402/*  → xkg-stripe (x402 crypto payments)
# - /api/.well-known/x402 → xkg-stripe (x402 discovery)
# - /api/checkout, /api/webhook, /api/license/*, /api/support/* → xkg-stripe
# - /api/admin/licenses, /api/admin/orders, /api/admin/tickets, /api/admin/stats → xkg-stripe
# - /api/* (everything else) → cluster-hub (task management)
# - /* → openclaw gateway
# Public funnel prefixes. Both /xkg-pay/* and /api/pay/* map to :8765.
# /xkg-pay is the canonical public path; /api/pay is an alias used by the
# Cloudflare Worker at seele.agency/api/pay/*.
XKG_PAYMENTS_PATHS = ("/xkg-pay/", "/api/pay/")
XKG_PAYMENTS_BACKEND = ("127.0.0.1", 8765)
XKG_STRIPE_PATHS = [
    "/api/x402/",
    "/api/.well-known/x402",
    "/api/checkout",
    "/api/webhook",
    "/api/license/",
    "/api/support/",
    "/api/admin/licenses",
    "/api/admin/orders",
    "/api/admin/tickets",
    "/api/admin/stats",
]
CLUSTER_HUB_BACKEND = ("127.0.0.1", 8090)
XKG_STRIPE_BACKEND = ("127.0.0.1", 8095)
OPENCLAW_BACKEND = ("127.0.0.1", 18789)


def _rewrite_for_payments(path: str, prefix: str = "/xkg-pay/") -> str:
    # Strip the public prefix; keep the rest of the path as-is for xkg-payments.
    # /xkg-pay/v1/checkout → /v1/checkout
    # /api/pay/v1/checkout → /v1/checkout
    if path.startswith(prefix):
        return "/" + path[len(prefix):]
    return path


def pick_backend(path):
    # Returns (host, port, upstream_path).
    # xkg-payments is checked FIRST so /xkg-pay/* and /api/pay/* never fall through.
    for prefix in XKG_PAYMENTS_PATHS:
        if path.startswith(prefix):
            host, port = XKG_PAYMENTS_BACKEND
            return host, port, _rewrite_for_payments(path, prefix)
    return _ORIGINAL_PICK(path)


def _ORIGINAL_PICK(path):
    # Returns (host, port, path). Path is unchanged for these backends.
    for stripe_path in XKG_STRIPE_PATHS:
        if path == stripe_path or path.startswith(stripe_path):
            host, port = XKG_STRIPE_BACKEND
            return host, port, path
    if path.startswith("/api/"):
        host, port = CLUSTER_HUB_BACKEND
        return host, port, path
    host, port = OPENCLAW_BACKEND
    return host, port, path


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    # Silence per-request access logs (we log via the parent)
    def log_message(self, fmt, *args):
        pass

    def _proxy(self, method):
        host, port, upstream_path = pick_backend(self.path)
        target = f"http://{host}:{port}{upstream_path}"
        # Copy headers (skip hop-by-hop)
        hop_by_hop = {
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailers", "transfer-encoding", "upgrade", "host",
        }
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in hop_by_hop:
                continue
            headers[k] = v
        # Preserve original Host so Flask/other backends see the public hostname
        # (helps with CORS and redirects)
        headers["Host"] = self.headers.get("Host", f"{host}:{port}")
        # Read body if present
        body = None
        if "Content-Length" in self.headers:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 0:
                    body = self.rfile.read(length)
            except (ValueError, OSError):
                pass
        req = urllib.request.Request(target, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                # Copy response headers (skip hop-by-hop)
                for k, v in resp.getheaders():
                    if k.lower() in hop_by_hop:
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            try:
                payload = e.read()
            except Exception:
                payload = b""
            self.send_response(e.code)
            for k, v in (e.headers.items() if e.headers else []):
                if k.lower() in hop_by_hop:
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            msg = f"proxy error: {e!r}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_GET(self):     self._proxy("GET")
    def do_POST(self):    self._proxy("POST")
    def do_PUT(self):     self._proxy("PUT")
    def do_DELETE(self):  self._proxy("DELETE")
    def do_PATCH(self):   self._proxy("PATCH")
    def do_OPTIONS(self): self._proxy("OPTIONS")
    def do_HEAD(self):    self._proxy("HEAD")


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    port = 8089
    print(f"[reverse_proxy] listening on 0.0.0.0:{port} → x402/checkout/license/support/admin(sales):8095, /api/*:8090, /*:18789")
    with ThreadedServer(("0.0.0.0", port), ProxyHandler) as srv:
        srv.serve_forever()
