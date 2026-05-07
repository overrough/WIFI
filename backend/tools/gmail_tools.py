"""
Gmail tools — free via OAuth2.

Same Google Cloud project as Calendar (just add Gmail scope).
Uses the same google_credentials.json / google_token.json.

Setup: same as calendar_tools.py — make sure you enable "Gmail API"
in your Google Cloud project as well.

Tools provided:
  • draft_email(to, subject, body)    — create a draft in Gmail
  • send_email(to, subject, body)     — send an email
  • list_emails(max_results, query)   — read inbox / search
  • reply_email(message_id, body)     — reply to a specific email
"""

import base64
import email as email_lib
import logging
import os
from email.mime.text import MIMEText
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "./google_credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "./google_token.json")
# Gmail needs its own scope in addition to Calendar
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _get_gmail_service():
    """Return an authorised Gmail API service object."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError(
            "Google API libraries not installed. Run:\n"
            "  pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"
        )

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise RuntimeError(
                    f"Google credentials not found at {CREDENTIALS_FILE}.\n"
                    "Download OAuth credentials from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _make_message(to: str, subject: str, body: str) -> dict:
    """Create a base64-encoded RFC 2822 message dict."""
    msg = MIMEText(body, "plain")
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


def make_gmail_tools():
    """Return Gmail tools."""

    @tool
    def draft_email(to: str, subject: str, body: str) -> str:
        """
        Create an email draft in Gmail (does NOT send — you review it first).
        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text email body.
        Returns confirmation with a link to the draft.
        """
        try:
            service = _get_gmail_service()
        except RuntimeError as e:
            return str(e)

        message = _make_message(to, subject, body)
        try:
            draft = service.users().drafts().create(
                userId="me", body={"message": message}
            ).execute()
        except Exception as exc:
            logger.error("Draft creation failed: %s", exc)
            return f"Failed to create draft: {exc}"

        draft_id = draft.get("id", "")
        return (
            f"Draft created successfully.\n"
            f"To: {to}\nSubject: {subject}\n"
            f"Draft ID: {draft_id}\n"
            "Open Gmail to review and send."
        )

    @tool
    def send_email(to: str, subject: str, body: str, confirm: bool = False) -> str:
        """
        Send an email immediately via Gmail.

        SAFETY: This is an irreversible external action. By default this tool
        returns a confirmation prompt and does NOT send. Only call with
        confirm=True AFTER Sir has explicitly approved sending in conversation.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text email body.
            confirm: Must be True to actually send. Defaults to False (dry-run).
        """
        from jarvis_core.safety import confirmation_prompt

        if not confirm:
            preview = body if len(body) <= 200 else body[:200] + "…"
            return confirmation_prompt(
                action="send an email to",
                target=to,
                details=f"Subject: {subject}\nBody: {preview}",
            )

        try:
            service = _get_gmail_service()
        except RuntimeError as e:
            return str(e)

        message = _make_message(to, subject, body)
        try:
            sent = service.users().messages().send(
                userId="me", body=message
            ).execute()
        except Exception as exc:
            logger.error("Email send failed: %s", exc)
            return f"Failed to send email: {exc}"

        return f"Email sent to {to}. Message ID: {sent.get('id', '')}"

    @tool
    def list_emails(query: str = "in:inbox is:unread", max_results: int = 10) -> str:
        """
        Search or list emails from Gmail.
        Args:
            query: Gmail search query (default: unread inbox).
                   Examples: "from:boss@company.com", "subject:meeting", "after:2026/04/01"
            max_results: Maximum number of emails to return (default 10).
        Returns a summary of matching emails.
        """
        try:
            service = _get_gmail_service()
        except RuntimeError as e:
            return str(e)

        try:
            results = service.users().messages().list(
                userId="me", q=query, maxResults=max_results
            ).execute()
        except Exception as exc:
            return f"Failed to list emails: {exc}"

        messages = results.get("messages", [])
        if not messages:
            return f"No emails found for query: {query!r}"

        lines = [f"Emails matching {query!r}:"]
        for msg_ref in messages:
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=msg_ref["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                lines.append(
                    f"  • [{headers.get('Date', '')}] {headers.get('Subject', '(no subject)')} "
                    f"— from {headers.get('From', '?')}"
                    f"  (id: {msg_ref['id']})"
                )
            except Exception:
                lines.append(f"  • {msg_ref['id']}")

        return "\n".join(lines)

    @tool
    def reply_email(message_id: str, body: str, confirm: bool = False) -> str:
        """
        Reply to an existing email thread.

        SAFETY: Irreversible external action. Default is dry-run preview.
        Only call with confirm=True after Sir explicitly approves.

        Args:
            message_id: The Gmail message ID to reply to (from list_emails).
            body: Plain-text reply body.
            confirm: Must be True to actually send. Defaults to False (dry-run).
        """
        from jarvis_core.safety import confirmation_prompt

        try:
            service = _get_gmail_service()
        except RuntimeError as e:
            return str(e)

        try:
            original = service.users().messages().get(
                userId="me", id=message_id, format="metadata",
                metadataHeaders=["Subject", "From", "Message-ID", "References"],
            ).execute()
        except Exception as exc:
            return f"Could not fetch original message: {exc}"

        headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}
        thread_id = original.get("threadId", "")
        subject = headers.get("Subject", "")
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        to = headers.get("From", "")

        if not confirm:
            preview = body if len(body) <= 200 else body[:200] + "…"
            return confirmation_prompt(
                action="send a reply to",
                target=to,
                details=f"Subject: {subject}\nBody: {preview}",
            )

        msg = MIMEText(body, "plain")
        msg["to"] = to
        msg["subject"] = subject
        if headers.get("Message-ID"):
            msg["In-Reply-To"] = headers["Message-ID"]
            msg["References"] = headers.get("References", "") + " " + headers["Message-ID"]

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        try:
            service.users().messages().send(
                userId="me", body={"raw": raw, "threadId": thread_id}
            ).execute()
        except Exception as exc:
            return f"Failed to send reply: {exc}"

        return f"Reply sent to {to} in thread {thread_id}."

    return [draft_email, send_email, list_emails, reply_email]
