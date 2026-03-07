"""
Minimal HTTP server for Gmail OAuth callback (e.g. on Railway).
Serves GET /gmail/oauth (redirect to Google) and GET /gmail/oauth/callback (exchange code, save token).
Run in a background thread so Slack Socket Mode can run in the main thread.
"""
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# Lazy import to avoid loading gmail before env is ready
def _get_gmail_module():
    from features import gmail
    return gmail


class OAuthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quiet by default; use print for OAuth events
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parsed.query

        if path == "/gmail/oauth" or path == "/gmail/oauth/debug":
            gmail = _get_gmail_module()
            base = gmail._gmail_oauth_callback_base()
            redirect_uri = (base + gmail.GMAIL_OAUTH_CALLBACK_PATH) if base else "(not set)"
            if path == "/gmail/oauth/debug":
                self._send(200, f"redirect_uri sent to Google: {redirect_uri}. Add this exact URL in Google Cloud Console → Credentials → your OAuth client → Authorized redirect URIs.")
                return
            auth_url, err = gmail.get_gmail_oauth_authorization_url()
            if err:
                self._send(400, f"Gmail OAuth not available: {err}. redirect_uri we use: {redirect_uri}")
                return
            if not auth_url:
                self._send(200, "Gmail already authorized. Token file exists.")
                return
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()
            return

        if path == "/gmail/oauth/callback":
            gmail = _get_gmail_module()
            base = gmail._gmail_oauth_callback_base()
            if not base:
                self._send(500, "GMAIL_REDIRECT_URI or RAILWAY_PUBLIC_DOMAIN not set.")
                return
            full_url = base + (self.path if self.path.startswith("/") else "/" + self.path)
            ok, err = gmail.complete_gmail_oauth(full_url)
            if ok:
                self._send(200, "Gmail authorized. Token saved. You can close this tab.")
                self.server.shutdown()  # Stop OAuth server once token is obtained
            else:
                self._send(500, f"Authorization failed: {err}")
            return

        self.send_response(404)
        self.end_headers()

    def _send(self, code: int, body: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = f"<!DOCTYPE html><html><body><p>{body}</p></body></html>"
        self.wfile.write(html.encode("utf-8"))


def run_oauth_server(port: int | None = None):
    port = port or int(os.environ.get("PORT", "3000"))
    server = HTTPServer(("", port), OAuthHandler)
    print(f"[OAuth] HTTP server listening on port {port} (e.g. /gmail/oauth)")
    server.serve_forever()
