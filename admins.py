"""Single admin list shared by every feature (Matchy, Spottings, ...).

Add a Slack member ID here (Profile -> ⋮ -> Copy member ID) and restart the bot.
"""

ADMIN_USER_IDS = frozenset({
    "U0631Q51G04",
    "U07T20PN1GB",
    "U063K9AG40Y",
    "U07T4JEEUSG",
    "U09N3KJ5KTN",
    "U07T07QHQ7P",
})


def is_admin(user_id: str) -> bool:
    return user_id in ADMIN_USER_IDS
