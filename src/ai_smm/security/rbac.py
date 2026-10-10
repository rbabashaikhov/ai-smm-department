"""Role hierarchy.

viewer < editor < admin < owner. The comparison lives here so that no call
site compares role strings by hand, which is where an off-by-one in an
authorisation check usually comes from.
"""
from __future__ import annotations

from ai_smm.db.models import MembershipRole


#: Rank of each role. Higher contains the rights of everything below it.
ROLE_RANK: dict[MembershipRole, int] = {
    MembershipRole.VIEWER: 10,
    MembershipRole.EDITOR: 20,
    MembershipRole.ADMIN: 30,
    MembershipRole.OWNER: 40,
}


def role_rank(role: MembershipRole) -> int:
    return ROLE_RANK[role]


def role_satisfies(role: MembershipRole, minimum: MembershipRole) -> bool:
    """True when `role` is at least `minimum` in the hierarchy."""

    return ROLE_RANK[role] >= ROLE_RANK[minimum]
