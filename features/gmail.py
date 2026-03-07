"""
Gmail-filter Slack bot: one monitored inbox; users subscribe to *sender* addresses.
When the monitored inbox receives mail, only users who subscribed to that message's
From address get a DM. E.g. subscribe to email1@x.com → notified only when email1
sends to the monitored inbox; mail from email2 does not notify you.

- /subscribe [sender-email] — notify me when this sender emails the monitored inbox
- Uses Firebase (email_subscriptions collection) with schema: slack_id, email (sender)
"""

import html
import os
import re
import threading
import time
from pathlib import Path

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

# Gmail API scopes needed for reading mail
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

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
            if not Path(GMAIL_CREDENTIALS_PATH).exists():
                raise FileNotFoundError(
                    f"Gmail OAuth credentials not found at {GMAIL_CREDENTIALS_PATH}. "
                    "Download from Google Cloud Console (OAuth 2.0 Client ID) and run once to generate token."
                )
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_PATH, GMAIL_SCOPES)
            auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
            login_hint = GMAIL_MONITORED_EMAIL or "the monitored Gmail account"
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


def _format_email_for_slack(msg) -> str:
    """Build a short Slack-friendly summary of the message. Decode HTML entities from Gmail first."""
    subject = html.unescape(_get_header(msg, "Subject") or "(no subject)")
    from_addr = html.unescape(_get_header(msg, "From") or "")
    raw_snippet = msg.get("snippet") or ""
    decoded = html.unescape(raw_snippet)
    snippet = _slack_escape(decoded[:500])
    if len(decoded) > 500:
        snippet += "…"
    return f"*Subject:* {_slack_escape(subject)}\n*From:* {_slack_escape(from_addr)}\n*Snippet:* {snippet}"


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
    text = _format_email_for_slack(msg)
    for slack_id in slack_ids:
        try:
            dm = slack_client.conversations_open(users=[slack_id])
            channel_id = dm.get("channel", {}).get("id")
            if channel_id:
                slack_client.chat_postMessage(
                    channel=channel_id,
                    text=f"You're subscribed to *{sender}*. New email to the monitored inbox:\n\n{text}",
                )
        except Exception:
            pass


def _poll_gmail_loop() -> None:
    """Background thread: poll the single monitored Gmail inbox."""
    if not GMAIL_MONITORED_EMAIL:
        return
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return
    slack_client = WebClient(token=token)
    service = None
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
                        time.sleep(60)
                        continue
            else:
                hist = None

            if start_id is None or hist is None:
                profile = service.users().getProfile(userId="me").execute()
                new_history_id = profile.get("historyId")
                if new_history_id:
                    _save_history_id(str(new_history_id))
                time.sleep(60)
                continue

            new_history_id = hist.get("historyId")
            if new_history_id:
                _save_history_id(str(new_history_id))
            for rec in hist.get("history", []):
                for msg_added in rec.get("messagesAdded", []):
                    mid = msg_added.get("message", {}).get("id")
                    if mid:
                        _process_new_message(service, mid, slack_client)
        except FileNotFoundError:
            # Credentials not set up yet
            pass
        except Exception as e:
            print(f"[Gmail] Poll error: {e}")
        time.sleep(30)


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
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text=f"You have no sender subscriptions. {hint}",
                    )
                else:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="Senders you're subscribed to:\n• " + "\n• ".join(subs) + "\n\n" + hint,
                    )
                return
            added = add_subscription(user_id, text)
            if added:
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=f"Subscribed to *{text}*. You'll get a DM when this sender emails the monitored inbox.",
                )
            else:
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
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
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="You have no subscriptions. Use `/unsubscribe sender@example.com` to remove one.",
                    )
                else:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="Senders you're subscribed to:\n• " + "\n• ".join(subs) + "\n\nRemove: `/unsubscribe sender@example.com`",
                    )
                return
            removed = remove_subscription(user_id, text)
            if removed:
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=f"Unsubscribed from *{text}*.",
                )
            else:
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=f"You weren't subscribed to *{text}* (or invalid address).",
                )
        except Exception as e:
            logger.exception("Unsubscribe failed: %s", e)
            raise

    if GMAIL_MONITORED_EMAIL and Path(GMAIL_CREDENTIALS_PATH).exists():
        t = threading.Thread(target=_poll_gmail_loop, daemon=True)
        t.start()
        print(f"[Gmail] Polling inbox: {GMAIL_MONITORED_EMAIL}")
