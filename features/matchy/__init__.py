"""
Matchy: weekly opt-in pairing + channel participation leaderboard.

  config         — constants, channel, admins
  store          — Firestore roster (matchyData/members)
  matching       — group assignment algorithm
  generation     — weekly run + Slack group DMs
  participation  — @mention leaderboard (matchy collection)
  roster         — opt-in, pause, skip, overrides
  handlers       — Bolt command + events
"""
from features.matchy.config import COMMAND as MATCHY_COMMAND
from features.matchy.handlers import register_handlers as register_matchy_handlers

__all__ = ["MATCHY_COMMAND", "register_matchy_handlers"]
