"""Matchy configuration and constants."""
import os
from zoneinfo import ZoneInfo

# Switch to "/matchy" when ready for production.
COMMAND = "/matchytest"

CHANNEL_ID = os.environ.get("MATCHY_CHANNEL_ID", "C01FL4VCE1Z")
TIMEZONE = ZoneInfo("America/Los_Angeles")

ADMIN_USER_IDS = frozenset({
    "U0631Q51G04",
    "U07T20PN1GB",
    "U063K9AG40Y",
    "U07T4JEEUSG",
})

WELCOME_MESSAGE = """🎯 *Welcome to your Matchy Meetup!*

This is your weekly match group! Feel free to introduce yourselves and plan your meetup. Have fun! 🎉

*💡 Activity Ideas:*
• ☕ Grab a sweet treat (Omomomo, Mori's, Heytea, etc.)
• 🍕 Grab food together (In-N-Out, Wadaya, Cava, Yintang, Burnt Crumbs, etc.)
• 🏓 Play sports at the ARC (Pickleball, Badminton, Volleyball, etc.)
• 🎮 Play League/Valorant together
• 🛍️ Go shopping (Irvine Spectrum, Thrifting/Bins, etc.)
• 🗺️ Explore a new place (Hiking Trail, Beach, etc.)
• 💬 Just chat and get to know each other!

*Remember:* The goal is to connect and build relationships. Keep it casual and fun! 🤓👍"""

SLACK_HISTORY_DAYS_HINT = 90
