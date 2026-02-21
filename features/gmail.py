"""
Gmail-filter Slack bot: subscribe to email addresses; when the monitored inbox
receives mail to a subscribed address, forward a summary to all subscribers via Slack DM.

- /subscribe [email] — add an email to your subscriptions (or open modal to add/list)
- Uses Firebase (email_subscriptions collection) with schema: slack_id, email
- Gmail API polls the configured inbox; matching messages are sent to subscribers.
"""

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
# Config (set in .env; you complete credentials)
# -----------------------------------------------------------------------------
GMAIL_CREDENTIALS_PATH = os.environ.get(
    "GMAIL_CREDENTIALS_PATH",
    str(Path(__file__).resolve().parent.parent / "gmail-credentials.json"),
)
GMAIL_TOKEN_PATH = os.environ.get(
    "GMAIL_TOKEN_PATH",
    str(Path(__file__).resolve().parent.parent / "gmail-token.json"),
)
GMAIL_HISTORY_PATH = os.environ.get(
    "GMAIL_HISTORY_PATH",
    str(Path(__file__).resolve().parent.parent / "gmail-history-id.txt"),
)
# Email address of the Gmail account to monitor (e.g. ctc-uci@gmail.com)
GMAIL_MONITORED_EMAIL = os.environ.get("GMAIL_MONITORED_EMAIL", "")

# Gmail API scopes needed for reading mail
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Firebase collection for subscriptions: document fields slack_id, email
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


def get_subscribers_for_email(email_address: str) -> list[str]:
    """Return list of slack_ids subscribed to this (normalized) address."""
    email_address = _normalize_email(email_address)
    col = get_firestore_subscriptions()
    return [doc.get("slack_id") for doc in col.where("email", "==", email_address).stream() if doc.get("slack_id")]


# -----------------------------------------------------------------------------
# Gmail API auth (OAuth2; you run once to produce gmail-token.json)
# -----------------------------------------------------------------------------
def get_gmail_credentials():
    """Load or refresh Gmail OAuth2 credentials."""
    creds = None
    if Path(GMAIL_TOKEN_PATH).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
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
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def get_gmail_service():
    """Build Gmail API service."""
    creds = get_gmail_credentials()
    return build("gmail", "v1", credentials=creds)


def _load_history_id() -> str | None:
    if Path(GMAIL_HISTORY_PATH).exists():
        return Path(GMAIL_HISTORY_PATH).read_text().strip() or None
    return None


def _save_history_id(history_id: str) -> None:
    Path(GMAIL_HISTORY_PATH).write_text(history_id)


def _parse_message_recipients(msg) -> list[str]:
    """Extract To (and optionally Cc) addresses from Gmail message for matching."""
    headers = msg.get("payload", {}).get("headers") or []
    to_header = next((h["value"] for h in headers if h.get("name", "").lower() == "to"), "")
    cc_header = next((h["value"] for h in headers if h.get("name", "").lower() == "cc"), "")
    addresses = []
    for raw in (to_header, cc_header):
        # Simple parse: "Name <a@b.com>, c@d.com"
        for part in re.split(r",|\s", raw):
            part = part.strip()
            if not part:
                continue
            if "<" in part and ">" in part:
                m = re.search(r"<([^>]+)>", part)
                if m:
                    addresses.append(_normalize_email(m.group(1)))
            elif "@" in part:
                addresses.append(_normalize_email(part))
    return [a for a in addresses if a and "@" in a]


def _get_header(msg, name: str) -> str:
    headers = msg.get("payload", {}).get("headers") or []
    return next((h["value"] for h in headers if h.get("name", "").lower() == name.lower()), "")


def _format_email_for_slack(msg) -> str:
    """Build a short Slack-friendly summary of the message."""
    subject = _get_header(msg, "Subject") or "(no subject)"
    from_addr = _get_header(msg, "From")
    raw_snippet = (msg.get("snippet") or "")
    snippet = raw_snippet.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:500]
    if len(raw_snippet) > 500:
        snippet += "…"
    return f"*Subject:* {subject}\n*From:* {from_addr}\n*Snippet:* {snippet}"


def _process_new_message(service, message_id: str, slack_client: WebClient) -> None:
    """Fetch one message, determine recipients, notify subscribers."""
    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    except Exception:
        return
    recipients = _parse_message_recipients(msg)
    if not recipients:
        return
    text = _format_email_for_slack(msg)
    notified = set()
    for addr in recipients:
        for slack_id in get_subscribers_for_email(addr):
            if slack_id in notified:
                continue
            notified.add(slack_id)
            try:
                dm = slack_client.conversations_open(users=[slack_id])
                channel_id = dm.get("channel", {}).get("id")
                if channel_id:
                    slack_client.chat_postMessage(
                        channel=channel_id,
                        text=f"You're subscribed to *{addr}*. New email:\n\n{text}",
                    )
            except Exception:
                pass


def _poll_gmail_loop():
    """Background thread: poll Gmail history and notify Slack subscribers."""
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
                # Full sync: get current historyId from profile and recent messages
                profile = service.users().getProfile(userId="me").execute()
                new_history_id = profile.get("historyId")
                if new_history_id:
                    _save_history_id(str(new_history_id))
                # Optionally process recent messages on first run (skip to avoid spam)
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
        except Exception:
            pass
        time.sleep(30)


def register_gmail_handlers(app):
    """Register /subscribe (and /unsubscribe) and start Gmail polling."""

    @app.command("/subscribe")
    def cmd_subscribe(ack, body, client, logger):
        try:
            ack()
            user_id = body["user_id"]
            text = (body.get("text") or "").strip()
            if not text:
                # No email provided: show current subscriptions and hint
                subs = list_subscriptions(user_id)
                if not subs:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="You have no email subscriptions. Use `/subscribe email@example.com` to add one.",
                    )
                else:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="Your subscriptions:\n• " + "\n• ".join(subs) + "\n\nAdd: `/subscribe email@example.com`",
                    )
                return
            added = add_subscription(user_id, text)
            if added:
                client.chat_postEphemeral(
                    channel=body["channel_id"],
                    user=user_id,
                    text=f"Subscribed to *{text}*. You'll get Slack DMs when the monitored inbox receives mail to this address.",
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
                        text="You have no subscriptions. Use `/unsubscribe email@example.com` to remove one.",
                    )
                else:
                    client.chat_postEphemeral(
                        channel=body["channel_id"],
                        user=user_id,
                        text="Your subscriptions:\n• " + "\n• ".join(subs) + "\n\nRemove: `/unsubscribe email@example.com`",
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

    # Start Gmail polling only if config is set
    if GMAIL_MONITORED_EMAIL and Path(GMAIL_CREDENTIALS_PATH).exists():
        t = threading.Thread(target=_poll_gmail_loop, daemon=True)
        t.start()
