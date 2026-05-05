"""
Date/time tool — always timezone-aware per the user's profile.
"""

from datetime import datetime

import pytz
from langchain_core.tools import tool

from core.config import get_settings

settings = get_settings()


def make_datetime_tools():
    @tool
    def get_datetime() -> str:
        """
        Get the current date and time in the user's timezone.
        Always call this when you need to know what time or day it is.
        """
        tz = pytz.timezone(settings.jarvis_timezone)
        now = datetime.now(tz)
        return now.strftime(
            "Current date: %A, %d %B %Y\nCurrent time: %H:%M (%Z)\nISO: %Y-%m-%dT%H:%M:%S%z"
        )

    return [get_datetime]
