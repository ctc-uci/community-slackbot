"""Gmail OAuth routes."""
from flask import Blueprint, redirect, request

bp = Blueprint("gmail", __name__)


@bp.get("/gmail/oauth")
def gmail_oauth():
    from features.gmail import (
        GMAIL_OAUTH_CALLBACK_PATH,
        _gmail_oauth_callback_base,
        get_gmail_oauth_authorization_url,
    )

    base = _gmail_oauth_callback_base()
    auth_url, err = get_gmail_oauth_authorization_url()
    if err:
        redirect_uri = (base + GMAIL_OAUTH_CALLBACK_PATH) if base else "(not set)"
        return f"Gmail OAuth not available: {err}. redirect_uri: {redirect_uri}", 400
    if not auth_url:
        return "Gmail already authorized."
    return redirect(auth_url)


@bp.get("/gmail/oauth/debug")
def gmail_oauth_debug():
    from features.gmail import GMAIL_OAUTH_CALLBACK_PATH, _gmail_oauth_callback_base

    base = _gmail_oauth_callback_base()
    redirect_uri = (base + GMAIL_OAUTH_CALLBACK_PATH) if base else "(not set)"
    return f"redirect_uri: {redirect_uri}. Add this exact URL in Google Cloud Console."


@bp.get("/gmail/oauth/callback")
def gmail_oauth_callback():
    from features.gmail import _gmail_oauth_callback_base, complete_gmail_oauth

    base = _gmail_oauth_callback_base()
    if not base:
        return "GMAIL_REDIRECT_URI or RAILWAY_PUBLIC_DOMAIN not set.", 500
    qs = request.query_string.decode()
    full_url = base + request.path + ("?" + qs if qs else "")
    ok, err = complete_gmail_oauth(full_url)
    if ok:
        return "Gmail authorized. Token saved. You can close this tab."
    return f"Authorization failed: {err}", 500
