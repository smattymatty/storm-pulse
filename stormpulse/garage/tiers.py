"""The account-key tier vocabulary, declared once.

Two wire vocabularies exist deliberately and must not be merged: the
provisioning/rotation/enforcement commands speak ``all | rw | ro`` (a key's
tier ceiling), while attach speaks ``ro | rw | owner`` (the grant it widens
a key by). Both resolve to the same (read, write, owner) permission triple;
this module is the one place either mapping or pattern may be edited.
"""

from __future__ import annotations

# Wire patterns for the tier params (kept in sync with the dicts below).
TIER_PATTERN = r"(?:all|rw|ro)"
ATTACH_TIER_PATTERN = r"(?:ro|rw|owner)"

# (read, write, owner) per key tier.
TIER_PERMS: dict[str, tuple[bool, bool, bool]] = {
    "all": (True, True, True),
    "rw": (True, True, False),
    "ro": (True, False, False),
}

# (read, write, owner) per attach grant tier.
ATTACH_TIER_PERMS: dict[str, tuple[bool, bool, bool]] = {
    "ro": (True, False, False),
    "rw": (True, True, False),
    "owner": (True, True, True),
}
