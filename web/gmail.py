"""Gmail OAuth routes."""
import html
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
    force = request.args.get("force") == "1"
    auth_url, err = get_gmail_oauth_authorization_url(force=force)
    if err:
        redirect_uri = (base + GMAIL_OAUTH_CALLBACK_PATH) if base else "(not set)"
        return f"Gmail OAuth not available: {err}. redirect_uri: {redirect_uri}", 400
    if not auth_url:
        return (
            "Gmail already authorized. "
            "To force re-authorization, visit <a href='/gmail/oauth?force=1'>/gmail/oauth?force=1</a>."
        )
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
    ok, result = complete_gmail_oauth(full_url)
    if ok:
        token_escaped = html.escape(result)
        return (
            "<html><body style='font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif; max-width: 650px; margin: 40px auto; padding: 20px; line-height: 1.6;'>"
            "<h2 style='color: #1a7f37;'>✅ Gmail Authorized!</h2>"
            "<p>Gmail polling has started for the monitored inbox.</p>"
            "<hr style='border: none; border-top: 1px solid #e1e4e8; margin: 24px 0;'>"
            "<h3>Keep authorized across Railway deploys (recommended):</h3>"
            "<p>To keep Gmail authorized permanently across future git pushes, add this to your <b>Railway Variables</b>:</p>"
            "<p><b>Variable Name:</b> <code>GMAIL_TOKEN_JSON</code></p>"
            "<p><b>Variable Value:</b></p>"
            f"<textarea style='width: 100%; height: 160px; font-family: monospace; font-size: 12px; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box;' readonly onclick='this.select()'>{token_escaped}</textarea>"
            "<p style='font-size: 13px; color: #57606a;'>Once saved in Railway variables, you will never have to re-authorize again!</p>"
            "</body></html>"
        )
    return f"Authorization failed: {result}", 500
