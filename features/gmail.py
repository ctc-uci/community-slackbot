"""
Gmail-filter Slack bot: one monitored inbox; users subscribe to *sender* addresses.
When the monitored inbox receives mail, only users who subscribed to that message's
From address get a DM. E.g. subscribe to email1@x.com → notified only when email1
sends to the monitored inbox; mail from email2 does not notify you.

- /subscribe [sender-email] — notify me when this sender emails the monitored inbox
- Uses Firebase (email_subscriptions collection) with schema: slack_id, email (sender)
"""

import base64
import io
import html
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Max image size to upload to Slack (5 MB); larger attachments are skipped
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_EMAIL = 10

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from slack_sdk import WebClient

from firebase_client import get_firebase_app

# -----------------------------------------------------------------------------
# Config (set in .env)
# -----------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

GMAIL_CREDENTIALS_PATH = os.environ.get(
    "GMAIL_CREDENTIALS_PATH",
    str(PROJECT_ROOT / "gmail-credentials.json"),
)
GMAIL_TOKEN_PATH = os.environ.get(
    "GMAIL_TOKEN_PATH",
    str(PROJECT_ROOT / "gmail-token.json"),
)
GMAIL_HISTORY_PATH = os.environ.get(
    "GMAIL_HISTORY_PATH",
    str(PROJECT_ROOT / "gmail-history-id.txt"),
)
# Single monitored inbox (e.g. ctc-uci@gmail.com)
GMAIL_MONITORED_EMAIL = os.environ.get("GMAIL_MONITORED_EMAIL", "").strip().lower()

# Optional: base URL for OAuth callback (e.g. https://your-app.up.railway.app). If unset, derived from RAILWAY_PUBLIC_DOMAIN.
GMAIL_REDIRECT_BASE = (os.environ.get("GMAIL_REDIRECT_URI") or os.environ.get("GMAIL_REDIRECT_BASE") or "").strip().rstrip("/")

# Gmail API scopes needed for reading mail
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

GMAIL_OAUTH_CALLBACK_PATH = "/gmail/oauth/callback"

# PKCE: store state -> code_verifier between /gmail/oauth and /gmail/oauth/callback
_gmail_oauth_pkce_store: dict[str, str] = {}

# Firebase: email_subscriptions documents = slack_id, email (email = sender to subscribe to)
SUBSCRIPTIONS_COLLECTION = "email_subscriptions"


def _normalize_email(addr: str) -> str:
    """Normalize for comparison: lowercase, strip."""
    return (addr or "").strip().lower()


def get_firestore_subscriptions():
    """Return Firestore collection for email_subscriptions."""
    get_firebase_app()
    from firebase_admin import firestore

    return firestore.client().collection(SUBSCRIPTIONS_COLLECTION)


def add_subscription(slack_id: str, email_address: str) -> bool:
    """Add (slack_id, email) if not already present. Returns True if added."""
    email_address = _normalize_email(email_address)
    if not email_address or "@" not in email_address:
        return False
    col = get_firestore_subscriptions()
    # Avoid duplicate: same user + same email
    for doc in col.where("slack_id", "==", slack_id).where("email", "==", email_address).limit(1).stream():
        return False
    col.add({"slack_id": slack_id, "email": email_address})
    return True


def remove_subscription(slack_id: str, email_address: str) -> bool:
    """Remove one subscription. Returns True if something was removed."""
    email_address = _normalize_email(email_address)
    col = get_firestore_subscriptions()
    for doc in col.where("slack_id", "==", slack_id).where("email", "==", email_address).stream():
        doc.reference.delete()
        return True
    return False


def list_subscriptions(slack_id: str) -> list[str]:
    """Return list of email addresses the user is subscribed to."""
    col = get_firestore_subscriptions()
    return [doc.get("email") for doc in col.where("slack_id", "==", slack_id).stream() if doc.get("email")]


def get_subscribers_for_sender(sender_email: str) -> list[str]:
    """Return slack_ids who subscribed to this sender (normalized)."""
    sender_email = _normalize_email(sender_email)
    col = get_firestore_subscriptions()
    return [doc.get("slack_id") for doc in col.where("email", "==", sender_email).stream() if doc.get("slack_id")]


# -----------------------------------------------------------------------------
# Gmail API auth (OAuth2; one token for the single monitored inbox)
# -----------------------------------------------------------------------------
def _gmail_oauth_callback_base() -> str | None:
    """Return base URL for OAuth callback (e.g. https://myapp.up.railway.app) or None if not configured."""
    if GMAIL_REDIRECT_BASE:
        return GMAIL_REDIRECT_BASE
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}"
    return None


def _get_gmail_flow(redirect_uri: str, code_verifier: str | None = None):
    """Build InstalledAppFlow with the given redirect_uri. Optional code_verifier for PKCE callback."""
    gmail_creds_json = os.environ.get("GMAIL_CREDENTIALS_JSON", "").strip()
    kwargs = {}
    if code_verifier is not None:
        kwargs["code_verifier"] = code_verifier
        kwargs["autogenerate_code_verifier"] = False
    if gmail_creds_json:
        client_config = json.loads(gmail_creds_json)
        flow = InstalledAppFlow.from_client_config(client_config, GMAIL_SCOPES, **kwargs)
    else:
        if not Path(GMAIL_CREDENTIALS_PATH).exists():
            raise FileNotFoundError("Gmail credentials required (file or GMAIL_CREDENTIALS_JSON).")
        flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_PATH, GMAIL_SCOPES, **kwargs)
    flow.redirect_uri = redirect_uri
    return flow


def get_gmail_oauth_authorization_url() -> tuple[str | None, str | None]:
    """Return (authorization_url, None) for HTTP redirect, or (None, error_message). Uses callback base if set."""
    if Path(GMAIL_TOKEN_PATH).exists():
        return (None, None)
    base = _gmail_oauth_callback_base()
    if not base:
        return (None, "Set GMAIL_REDIRECT_URI or RAILWAY_PUBLIC_DOMAIN for OAuth callback.")
    callback_url = base + GMAIL_OAUTH_CALLBACK_PATH
    logger.info("Gmail OAuth: using redirect_uri=%s (add this exact URI in Google Console)", callback_url)
    try:
        flow = _get_gmail_flow(callback_url)
        auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
        if state and getattr(flow, "code_verifier", None):
            _gmail_oauth_pkce_store[state] = flow.code_verifier
        return (auth_url, None)
    except Exception as e:
        return (None, str(e))


def complete_gmail_oauth(callback_full_url: str) -> tuple[bool, str]:
    """Exchange callback URL for token and save. Returns (True, "") or (False, error_message)."""
    base = _gmail_oauth_callback_base()
    if not base:
        return (False, "GMAIL_REDIRECT_URI or RAILWAY_PUBLIC_DOMAIN not set.")
    callback_url = base + GMAIL_OAUTH_CALLBACK_PATH
    state = None
    if "state=" in callback_full_url:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(callback_full_url)
        qs = parse_qs(parsed.query)
        state = (qs.get("state") or [None])[0]
    code_verifier = _gmail_oauth_pkce_store.pop(state, None) if state else None
    if not code_verifier:
        return (False, "Missing or expired PKCE state. Visit /gmail/oauth again and complete the flow in one go.")
    try:
        flow = _get_gmail_flow(callback_url, code_verifier=code_verifier)
        flow.fetch_token(authorization_response=callback_full_url)
        creds = flow.credentials
        token_path = Path(GMAIL_TOKEN_PATH)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        logger.info("Gmail: token saved via OAuth callback to %s", token_path)
        return (True, "")
    except Exception as e:
        logger.exception("Gmail: OAuth callback exchange failed")
        return (False, str(e))


def get_gmail_credentials():
    """Load or refresh Gmail OAuth2 credentials for the monitored inbox."""
    token_path = Path(GMAIL_TOKEN_PATH)
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            gmail_creds_json = os.environ.get("GMAIL_CREDENTIALS_JSON", "").strip()
            if not gmail_creds_json and not Path(GMAIL_CREDENTIALS_PATH).exists():
                raise FileNotFoundError(
                    f"Gmail OAuth credentials not found at {GMAIL_CREDENTIALS_PATH}. "
                    "Set GMAIL_CREDENTIALS_JSON (JSON one-liner) or add the credentials file."
                )
            if gmail_creds_json:
                client_config = json.loads(gmail_creds_json)
                flow = InstalledAppFlow.from_client_config(client_config, GMAIL_SCOPES)
            else:
                flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_PATH, GMAIL_SCOPES)
            login_hint = GMAIL_MONITORED_EMAIL or "the monitored Gmail account"

            # OAuth callback URL configured (e.g. Railway): user will visit /gmail/oauth in browser
            if _gmail_oauth_callback_base():
                raise FileNotFoundError(
                    "Gmail token missing. Visit this app's /gmail/oauth URL in your browser to authorize "
                    f"(e.g. https://your-app.up.railway.app/gmail/oauth). Token path: {token_path}"
                )

            # Headless: no browser; set redirect_uri so the auth URL is valid, then prompt for paste-back
            if os.environ.get("GMAIL_HEADLESS") or os.environ.get("HEADLESS"):
                flow.redirect_uri = "http://localhost"
                auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
                print(f"[Gmail] Token missing. Headless mode: no browser will open.")
                print(f"[Gmail] Sign in with {login_hint} by opening this URL in a browser:")
                print(auth_url)
                print()
                print("[Gmail] After signing in, the browser will go to localhost (nothing is running there).")
                print("[Gmail] Copy the *entire URL* from the address bar (e.g. http://localhost/?state=...&code=...) and paste it below.")
                try:
                    redirect_url = input("[Gmail] Paste the redirect URL here (or press Enter to exit): ").strip()
                except EOFError:
                    redirect_url = ""
                if not redirect_url:
                    raise FileNotFoundError(
                        "Gmail token required. Run again and paste the redirect URL after signing in, "
                        f"or copy the token file from a machine where you completed the flow. Token path: {token_path}"
                    )
                try:
                    # redirect_uri is http://localhost; oauthlib requires HTTPS unless we allow insecure transport
                    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
                    try:
                        flow.fetch_token(authorization_response=redirect_url)
                    finally:
                        os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
                    creds = flow.credentials
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to exchange the redirect URL for a token: {e}. "
                        "Check that you pasted the full URL from the browser (including ?state=...&code=...)."
                    ) from e
                token_path.parent.mkdir(parents=True, exist_ok=True)
                with open(token_path, "w") as f:
                    f.write(creds.to_json())
                print(f"[Gmail] Token saved to {token_path}")
                return creds

            auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
            print(f"[Gmail] Token missing. Sign in with: {login_hint}")
            print(f"[Gmail] Authorization URL: {auth_url}")
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return creds


def get_gmail_service():
    """Build Gmail API service for the monitored inbox."""
    creds = get_gmail_credentials()
    return build("gmail", "v1", credentials=creds)


def _load_history_id() -> str | None:
    path = Path(GMAIL_HISTORY_PATH)
    if path.exists():
        return path.read_text().strip() or None
    return None


def _save_history_id(history_id: str) -> None:
    path = Path(GMAIL_HISTORY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(history_id)


def _parse_message_sender(msg) -> str | None:
    """Extract From address (single) for sender-based subscription matching."""
    headers = msg.get("payload", {}).get("headers") or []
    from_header = next((h["value"] for h in headers if h.get("name", "").lower() == "from"), "")
    if not from_header or "@" not in from_header:
        return None
    # "Name <a@b.com>" or "a@b.com"
    if "<" in from_header and ">" in from_header:
        m = re.search(r"<([^>]+)>", from_header)
        if m:
            return _normalize_email(m.group(1))
    return _normalize_email(from_header)


def _get_header(msg, name: str) -> str:
    headers = msg.get("payload", {}).get("headers") or []
    return next((h["value"] for h in headers if h.get("name", "").lower() == name.lower()), "")


def _slack_escape(s: str) -> str:
    """Escape & < > for Slack mrkdwn so entities and angle brackets display literally."""
    if not s:
        return s
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _collect_image_parts(parts: list, service, message_id: str) -> list[tuple[bytes, str, str]]:
    """Recursively collect image parts from MIME structure. Returns list of (bytes, filename, mime_type)."""
    result = []
    for part in parts or []:
        if part.get("parts"):
            result.extend(_collect_image_parts(part["parts"], service, message_id))
            continue
        mime = (part.get("mimeType") or "").lower()
        if not mime.startswith("image/"):
            continue
        body = part.get("body") or {}
        raw = None
        if body.get("data"):
            try:
                raw = base64.urlsafe_b64decode(body["data"].encode("utf-8"))
            except Exception:
                continue
        elif body.get("attachmentId"):
            try:
                att = (
                    service.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=body["attachmentId"])
                    .execute()
                )
                if att.get("data"):
                    raw = base64.urlsafe_b64decode(att["data"].encode("utf-8"))
            except Exception:
                continue
        if raw is None:
            continue
        filename = (part.get("filename") or "").strip() or _image_filename_from_mime(mime)
        result.append((raw, filename, mime))
    return result


def _image_filename_from_mime(mime: str) -> str:
    """Default filename for image mime type."""
    ext = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(
        mime.split(";")[0].strip(), "png"
    )
    return f"image.{ext}"


def _get_email_images(service, message_id: str, msg) -> list[tuple[bytes, str, str]]:
    """Return list of (bytes, filename, mime_type) for all images in the message."""
    payload = msg.get("payload") or {}
    parts = payload.get("parts")
    if not parts:
        body = payload.get("body") or {}
        mime = (payload.get("mimeType") or "").lower()
        if mime.startswith("image/") and (body.get("data") or body.get("attachmentId")):
            # Single-part image message
            return _collect_image_parts([payload], service, message_id)
    return _collect_image_parts(parts or [], service, message_id)


def _get_plain_text_from_payload(payload: dict) -> str:
    """Extract plain text from message payload (single part or walk parts). Returns decoded string."""
    if not payload:
        return ""
    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    if body.get("data") and mime == "text/plain":
        try:
            return base64.urlsafe_b64decode(body["data"].encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            pass
    for part in payload.get("parts") or []:
        if part.get("parts"):
            text = _get_plain_text_from_payload(part)
            if text:
                return text
        mime = (part.get("mimeType") or "").lower()
        if mime != "text/plain":
            continue
        b = part.get("body") or {}
        if not b.get("data"):
            continue
        try:
            return base64.urlsafe_b64decode(b["data"].encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            continue
    return ""


def _is_quoted_reply_start(line: str, lines: list[str], i: int) -> bool:
    """True if this line starts the quoted reply block. Strips leading '>' so nested '> On ... wrote:' is detected."""
    stripped = re.sub(r"^>+\s*", "", line.strip()).strip()
    if re.match(r"^On\s+.+wrote\s*:\s*$", stripped, re.IGNORECASE):
        return True
    if re.match(r"^On\s+.+,.+wrote\s*:\s*$", stripped, re.IGNORECASE):
        return True
    if re.match(r"^-{2,}\s*(Original Message|Forwarded message)", stripped, re.IGNORECASE):
        return True
    if re.match(r"^_{3,}\s*$", stripped):
        return True
    if stripped.startswith("From:") and i > 0 and any(
        lines[j].strip().startswith("Sent:") for j in range(max(0, i - 2), min(len(lines), i + 3))
    ):
        return True
    if re.search(r"\bwrote\s*:\s*$", stripped):
        return True
    return False


def _split_reply_and_quoted(text: str) -> tuple[str, str]:
    """Split body into (new_reply_text, quoted_part). Either may be empty."""
    if not text or not text.strip():
        return ("", "")
    lines = text.split("\n")
    reply_lines: list[str] = []
    quoted_start = None
    for i, line in enumerate(lines):
        if _is_quoted_reply_start(line, lines, i):
            quoted_start = i
            break
        reply_lines.append(line)
    new_reply = "\n".join(reply_lines).strip()
    quoted = "\n".join(lines[quoted_start:]).strip() if quoted_start is not None else ""
    return (new_reply, quoted)


def _split_quoted_into_replies(quoted: str) -> list[tuple[str, list[str]]]:
    """Split quoted thread into [(header, body_lines), ...]. Header is 'On ... wrote:'; body lines are stripped of leading '>'."""
    if not quoted or not quoted.strip():
        return []
    lines = quoted.strip().split("\n")
    segments: list[tuple[str, list[str]]] = []
    current_header: str | None = None
    current_body: list[str] = []
    for i, line in enumerate(lines):
        if _is_quoted_reply_start(line, lines, i):
            if current_header is not None:
                segments.append((current_header, current_body))
            # Store header without leading '>' so nested replies don't show extra blockquote on date
            current_header = re.sub(r"^>+\s*", "", line.strip()).strip()
            current_body = []
        else:
            # Strip leading ">" and spaces from quoted lines
            stripped = line.strip()
            if stripped.startswith(">"):
                stripped = re.sub(r"^>+\s*", "", stripped)
            if current_header is not None and stripped:
                current_body.append(stripped)
    if current_header is not None:
        segments.append((current_header, current_body))
    return segments


def _format_quoted_replies(quoted: str, max_chars: int = 1200) -> str:
    """Format quoted thread: each reply (date + body) as one blockquote so the bar spans header to text."""
    segments = _split_quoted_into_replies(quoted)
    if not segments:
        return ""
    sep = "\n\n──────────────\n\n"
    out: list[str] = []
    n = 0
    for header, body_lines in segments:
        header_escaped = _slack_escape(header)
        body_escaped = [_slack_escape(ln) for ln in body_lines if ln.strip()]
        # One blockquote per reply: prefix every line with "> " so the bar spans date through reply text
        lines = [f"*{header_escaped}*"] + body_escaped if body_escaped else [f"*{header_escaped}*"]
        block = "\n".join("> " + line for line in lines)
        if n + len(block) + len(sep) > max_chars:
            remaining = max(0, max_chars - n - 25)
            if remaining > 0 and block:
                out.append(block[:remaining] + "…")
            break
        out.append(block)
        n += len(block) + len(sep)
    return sep.join(out)


def _get_email_slack_parts(msg) -> tuple[str, str, str, str | None]:
    """Return (subject_escaped, from_escaped, body_mrkdwn, quoted_mrkdwn_or_none). Body is new reply or full snippet; quoted is blockquoted thread or None."""
    subject = html.unescape(_get_header(msg, "Subject") or "(no subject)")
    from_addr = html.unescape(_get_header(msg, "From") or "")
    subject_escaped = _slack_escape(subject)
    from_escaped = _slack_escape(from_addr)
    payload = msg.get("payload") or {}
    plain = _get_plain_text_from_payload(payload)
    new_reply = ""
    quoted = ""
    if plain:
        new_reply, quoted = _split_reply_and_quoted(plain)
    if not new_reply and not quoted:
        plain = msg.get("snippet") or ""
        decoded = html.unescape(plain)
        body = _slack_escape(decoded[:500])
        if len(decoded) > 500:
            body += "…"
        return (subject_escaped, from_escaped, body, None)
    body_parts: list[str] = []
    if new_reply:
        decoded = html.unescape(new_reply)
        reply_snippet = _slack_escape(decoded[:500])
        if len(decoded) > 500:
            reply_snippet += "…"
        body_parts.append(reply_snippet)
    body_mrkdwn = "\n\n".join(body_parts) if body_parts else ""
    quoted_mrkdwn = _format_quoted_replies(quoted) if quoted else None
    return (subject_escaped, from_escaped, body_mrkdwn, quoted_mrkdwn)


def _format_email_for_slack(msg) -> str:
    """Build a short Slack-friendly summary string (legacy). New reply first; quoted thread in blockquote."""
    subject_escaped, from_escaped, body_mrkdwn, quoted_mrkdwn = _get_email_slack_parts(msg)
    parts = [f"*Subject:* {subject_escaped}\n*From:* {from_escaped}\n*Snippet:* {body_mrkdwn}"]
    if quoted_mrkdwn:
        parts.append(quoted_mrkdwn)
    return "\n\n".join(parts)


def _process_new_message(service, message_id: str, slack_client: WebClient) -> None:
    """Fetch one message, get sender (From); notify only users subscribed to that sender."""
    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    except Exception:
        return
    sender = _parse_message_sender(msg)
    if not sender:
        return
    slack_ids = get_subscribers_for_sender(sender)
    if not slack_ids:
        return
    raw_images = _get_email_images(service, message_id, msg)
    # Limit size and count so Slack uploads don't timeout or hit limits
    images = [(b, fn, m) for b, fn, m in raw_images if len(b) <= MAX_IMAGE_BYTES][:MAX_IMAGES_PER_EMAIL]
    if len(raw_images) > len(images):
        logger.info("Gmail: using %s of %s image(s) (max %s MB each, max %s per email)", len(images), len(raw_images), MAX_IMAGE_BYTES // (1024 * 1024), MAX_IMAGES_PER_EMAIL)

    subject_escaped, from_escaped, body_mrkdwn, quoted_mrkdwn = _get_email_slack_parts(msg)
    # Blocks: header, subject/from, main body, then divider for space before "Previous replies" attachment
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"You're subscribed to *{_slack_escape(sender)}*. New email to the monitored inbox."}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Subject:* {subject_escaped}\n*From:* {from_escaped}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body_mrkdwn or "_No body_"}},
        {"type": "divider"},
    ]
    attachments: list[dict] = []
    if quoted_mrkdwn:
        # Legacy attachments get real "Show more" collapse; Block Kit section expand does not
        attachments.append({
            "title": "Previous replies",
            "text": quoted_mrkdwn,
            "mrkdwn_in": ["text"],
        })
    fallback_text = f"You're subscribed to {sender}. New email to the monitored inbox.\n\nSubject: {subject_escaped}\nFrom: {from_escaped}"

    for slack_id in slack_ids:
        try:
            dm = slack_client.conversations_open(users=[slack_id])
            channel_id = dm.get("channel", {}).get("id")
            if channel_id:
                payload: dict = {
                    "channel": channel_id,
                    "text": fallback_text,
                    "blocks": blocks,
                }
                if attachments:
                    payload["attachments"] = attachments
                slack_client.chat_postMessage(**payload)
                for img_bytes, filename, mime in images:
                    try:
                        # Slack SDK can require a real file path in some environments; use a temp file for reliability
                        with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix or ".png", delete=False) as tmp:
                            tmp.write(img_bytes)
                            tmp.flush()
                            tmp_path = tmp.name
                        try:
                            slack_client.files_upload_v2(
                                channel=channel_id,
                                file=tmp_path,
                                title=filename,
                            )
                            logger.info("Gmail: uploaded image %s to Slack DM for %s", filename, slack_id)
                        finally:
                            Path(tmp_path).unlink(missing_ok=True)
                    except Exception as e:
                        logger.warning(
                            "Gmail: failed to upload image to Slack for %s: %s",
                            slack_id, e,
                        )
        except Exception as e:
            logger.warning("Gmail: failed to send DM to %s: %s", slack_id, e)


def _poll_gmail_loop() -> None:
    """Background thread: poll the single monitored Gmail inbox."""
    if not GMAIL_MONITORED_EMAIL:
        return
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return
    slack_client = WebClient(token=token)
    service = None
    token_missing_logged = False
    while True:
        try:
            if service is None:
                service = get_gmail_service()
            start_id = _load_history_id()
            if start_id:
                try:
                    hist = (
                        service.users()
                        .history()
                        .list(userId="me", startHistoryId=start_id, historyTypes=["messageAdded"])
                        .execute()
                    )
                except Exception as e:
                    if getattr(e, "resp", None) and getattr(e.resp, "status", None) == 404:
                        start_id = None
                    else:
                        time.sleep(15)
                        continue
            else:
                hist = None

            if start_id is None or hist is None:
                profile = service.users().getProfile(userId="me").execute()
                new_history_id = profile.get("historyId")
                if new_history_id:
                    _save_history_id(str(new_history_id))
                time.sleep(15)
                continue

            new_history_id = hist.get("historyId")
            if new_history_id:
                _save_history_id(str(new_history_id))
            for rec in hist.get("history", []):
                for msg_added in rec.get("messagesAdded", []):
                    mid = msg_added.get("message", {}).get("id")
                    if mid:
                        _process_new_message(service, mid, slack_client)
        except FileNotFoundError as e:
            if not token_missing_logged:
                logger.info("Gmail: token missing. %s", e)
                token_missing_logged = True
        except Exception as e:
            print(f"[Gmail] Poll error: {e}")
        time.sleep(15)


def register_gmail_handlers(app):
    """Register /subscribe, /unsubscribe and start Gmail polling (single inbox, subscribe by sender)."""

    @app.command("/subscribe")
    def cmd_subscribe(ack, body, client, logger):
        try:
            ack()
            user_id = body["user_id"]
            text = (body.get("text") or "").strip()
            if not text:
                subs = list_subscriptions(user_id)
                hint = "Subscribe to a *sender*: you'll get DMs when that person emails the monitored inbox. Add: `/subscribe sender@example.com`"
                if GMAIL_MONITORED_EMAIL:
                    hint = f"Monitored inbox: {GMAIL_MONITORED_EMAIL}\n{hint}"
                if not subs:
                    client.chat_postMessage(
                        channel=body["channel_id"],
                        text=f"You have no sender subscriptions. {hint}",
                    )
                else:
                    client.chat_postMessage(
                        channel=body["channel_id"],
                        text="Senders you're subscribed to:\n• " + "\n• ".join(subs) + "\n\n" + hint,
                    )
                return
            added = add_subscription(user_id, text)
            if added:
                client.chat_postMessage(
                    channel=body["channel_id"],
                    text=f"Subscribed to *{text}*. You'll get a DM when this sender emails the monitored inbox.",
                )
            else:
                client.chat_postMessage(
                    channel=body["channel_id"],
                    text=f"You're already subscribed to *{text}*, or the address is invalid.",
                )
        except Exception as e:
            logger.exception("Subscribe failed: %s", e)
            raise

    @app.command("/unsubscribe")
    def cmd_unsubscribe(ack, body, client, logger):
        try:
            ack()
            user_id = body["user_id"]
            text = (body.get("text") or "").strip()
            if not text:
                subs = list_subscriptions(user_id)
                if not subs:
                    client.chat_postMessage(
                        channel=body["channel_id"],
                        text="You have no subscriptions. Use `/unsubscribe sender@example.com` to remove one.",
                    )
                else:
                    client.chat_postMessage(
                        channel=body["channel_id"],
                        text="Senders you're subscribed to:\n• " + "\n• ".join(subs) + "\n\nRemove: `/unsubscribe sender@example.com`",
                    )
                return
            removed = remove_subscription(user_id, text)
            if removed:
                client.chat_postMessage(
                    channel=body["channel_id"],
                    text=f"Unsubscribed from *{text}*.",
                )
            else:
                client.chat_postMessage(
                    channel=body["channel_id"],
                    text=f"You weren't subscribed to *{text}* (or invalid address).",
                )
        except Exception as e:
            logger.exception("Unsubscribe failed: %s", e)
            raise

    has_gmail_creds = bool(os.environ.get("GMAIL_CREDENTIALS_JSON", "").strip()) or Path(GMAIL_CREDENTIALS_PATH).exists()
    if GMAIL_MONITORED_EMAIL and has_gmail_creds:
        t = threading.Thread(target=_poll_gmail_loop, daemon=True)
        t.start()
        print(f"[Gmail] Polling inbox: {GMAIL_MONITORED_EMAIL}")
