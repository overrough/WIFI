"""
Google Calendar tools — free via OAuth2.

Setup (one-time, ~5 minutes):
  1. Go to console.cloud.google.com → New project → Enable "Google Calendar API"
  2. OAuth consent screen → External → add your email as test user
  3. Credentials → Create OAuth client ID → Desktop app → download JSON
  4. Save as google_credentials.json in the backend/ directory
  5. Set GOOGLE_CREDENTIALS_FILE=./google_credentials.json in .env
  6. First run will open a browser to authorize — token saved to google_token.json

Tools provided:
  • list_calendar_events(days_ahead)  — what's on my calendar?
  • add_calendar_event(...)           — create a new event
  • find_free_slots(date, duration_minutes) — when am I free?
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "./google_credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "./google_token.json")
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_calendar_service():
    """Return an authorised Google Calendar API service object."""
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
                    "Download OAuth credentials from Google Cloud Console and save as google_credentials.json."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def _parse_event(event: dict) -> dict:
    """Normalise a Google Calendar event to a simple dict."""
    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event.get("id", ""),
        "title": event.get("summary", "(No title)"),
        "start": start.get("dateTime", start.get("date", "")),
        "end": end.get("dateTime", end.get("date", "")),
        "location": event.get("location", ""),
        "description": event.get("description", "")[:200],
        "link": event.get("htmlLink", ""),
    }


def make_calendar_tools():
    """Return Google Calendar tools."""

    @tool
    def list_calendar_events(days_ahead: int = 7) -> str:
        """
        List upcoming events from Google Calendar.
        Args:
            days_ahead: How many days ahead to look (default 7).
        Returns a summary of upcoming events.
        """
        try:
            service = _get_calendar_service()
        except RuntimeError as e:
            return str(e)

        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days_ahead)

        try:
            result = service.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                maxResults=20,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
        except Exception as exc:
            logger.error("Calendar list failed: %s", exc)
            return f"Failed to fetch calendar: {exc}"

        events = [_parse_event(e) for e in result.get("items", [])]
        if not events:
            return f"No events in the next {days_ahead} days."

        lines = [f"Upcoming events (next {days_ahead} days):"]
        for e in events:
            lines.append(f"  • {e['title']}  —  {e['start']}")
            if e["location"]:
                lines.append(f"    Location: {e['location']}")
        return "\n".join(lines)

    @tool
    def add_calendar_event(
        title: str,
        start_datetime: str,
        end_datetime: str,
        description: str = "",
        location: str = "",
    ) -> str:
        """
        Create a new event in Google Calendar.
        Args:
            title: Event title / summary.
            start_datetime: ISO 8601 format, e.g. "2026-04-15T14:00:00+05:30"
            end_datetime: ISO 8601 format, e.g. "2026-04-15T15:00:00+05:30"
            description: Optional event description or notes.
            location: Optional location string.
        Returns confirmation with a link to the event.
        """
        try:
            service = _get_calendar_service()
        except RuntimeError as e:
            return str(e)

        body = {
            "summary": title,
            "description": description,
            "location": location,
            "start": {"dateTime": start_datetime, "timeZone": "Asia/Kolkata"},
            "end": {"dateTime": end_datetime, "timeZone": "Asia/Kolkata"},
        }

        try:
            event = service.events().insert(calendarId="primary", body=body).execute()
        except Exception as exc:
            logger.error("Calendar insert failed: %s", exc)
            return f"Failed to create event: {exc}"

        return f"Event created: '{title}'\nLink: {event.get('htmlLink', '')}"

    @tool
    def find_free_slots(date: str, duration_minutes: int = 60) -> str:
        """
        Find free time slots on a given date.
        Args:
            date: Date string in YYYY-MM-DD format.
            duration_minutes: Desired meeting length in minutes (default 60).
        Returns a list of available time windows during working hours (9am–6pm).
        """
        try:
            service = _get_calendar_service()
        except RuntimeError as e:
            return str(e)

        try:
            day_start = datetime.fromisoformat(f"{date}T09:00:00+05:30")
            day_end = datetime.fromisoformat(f"{date}T18:00:00+05:30")
        except ValueError:
            return f"Invalid date format: {date}. Use YYYY-MM-DD."

        try:
            result = service.freebusy().query(body={
                "timeMin": day_start.isoformat(),
                "timeMax": day_end.isoformat(),
                "items": [{"id": "primary"}],
            }).execute()
        except Exception as exc:
            return f"Failed to check availability: {exc}"

        busy_periods = result.get("calendars", {}).get("primary", {}).get("busy", [])
        busy = [
            (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
            for b in busy_periods
        ]
        busy.sort()

        free_slots = []
        cursor = day_start
        for bstart, bend in busy:
            if cursor < bstart:
                gap = (bstart - cursor).seconds // 60
                if gap >= duration_minutes:
                    free_slots.append(f"  {cursor.strftime('%H:%M')} – {bstart.strftime('%H:%M')} ({gap} min free)")
            cursor = max(cursor, bend)

        if cursor < day_end:
            gap = (day_end - cursor).seconds // 60
            if gap >= duration_minutes:
                free_slots.append(f"  {cursor.strftime('%H:%M')} – {day_end.strftime('%H:%M')} ({gap} min free)")

        if not free_slots:
            return f"No free slots of {duration_minutes}+ minutes on {date}."
        return f"Free slots on {date} (working hours):\n" + "\n".join(free_slots)

    return [list_calendar_events, add_calendar_event, find_free_slots]
