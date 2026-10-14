"""Durable guild and group state for the local development server."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import threading
from datetime import datetime, timezone
from dso_server.paths import DEFAULT_SOCIAL_STATE_PATH


from dso_server.protocol.social_protocol import (
    GUILD_RANK_LEADER,
    GUILD_RANK_MEMBER,
    GUILD_RANK_OFFICER,
)


# Retail groups hold five characters. Capacity has to be enforced here rather
# than at invitation time: invitations are asynchronous notifications, so a
# group with one free slot can legitimately hand out several and only the
# first acceptance can be honoured.
GROUP_MEMBER_LIMIT = 5


class GroupFullError(ValueError):
    """Raised when a join would exceed :data:`GROUP_MEMBER_LIMIT`."""


# Persisted files written before the guild member's class and rank bytes were
# told apart store the ranks under the old, mirrored enum (1=leader,
# 2=officer).  Version 2 stores the client's own enum.
STATE_VERSION = 2
_LEGACY_GUILD_RANKS = {
    0: GUILD_RANK_MEMBER,
    1: GUILD_RANK_LEADER,
    2: GUILD_RANK_OFFICER,
}


@dataclass
class SocialMember:
    character_id: int
    account_id: int
    name: str
    class_id: int
    level: int
    joined_at: str
    rank: int = GUILD_RANK_MEMBER
    can_invite: bool = False


@dataclass
class Guild:
    guild_id: int
    name: str
    description: str
    members: list[SocialMember] = field(default_factory=list)


@dataclass
class Group:
    group_id: int
    name: str
    description: str
    map_id: str
    owner_character_id: int
    listed: bool = True
    private: bool = False
    members: list[SocialMember] = field(default_factory=list)


class SocialStateStore:
    def __init__(self, path: str | Path = DEFAULT_SOCIAL_STATE_PATH):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.guilds: dict[int, Guild] = {}
        self.groups: dict[int, Group] = {}
        self.next_guild_id = 50_000_001
        self.next_group_id = 60_000_001
        self._load()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _load(self) -> None:
        if not self.path.is_file():
            return
        document = json.loads(self.path.read_text(encoding="utf-8"))
        repaired = False
        version = int(document.get("version", 1))
        self.next_guild_id = int(document.get("next_guild_id", self.next_guild_id))
        self.next_group_id = int(document.get("next_group_id", self.next_group_id))
        for raw in document.get("guilds", ()):
            guild = Guild(
                int(raw["guild_id"]),
                str(raw["name"]),
                str(raw.get("description", "")),
                [SocialMember(**member) for member in raw.get("members", ())],
            )
            if version < STATE_VERSION:
                for member in guild.members:
                    member.rank = _LEGACY_GUILD_RANKS.get(
                        member.rank, GUILD_RANK_MEMBER
                    )
                repaired = True
            # Early local builds used rank 0 for the leader and later wrote
            # values outside the enum. Repair either shape into the client's
            # enum (0=member, 1=officer, 2=leader).
            if guild.members and not any(
                member.rank == GUILD_RANK_LEADER for member in guild.members
            ):
                leader = min(
                    guild.members,
                    key=lambda member: (
                        member.joined_at,
                        member.character_id,
                    ),
                )
                leader.rank = GUILD_RANK_LEADER
                leader.can_invite = True
                repaired = True
            for member in guild.members:
                if member.rank not in {
                    GUILD_RANK_MEMBER,
                    GUILD_RANK_LEADER,
                    GUILD_RANK_OFFICER,
                }:
                    member.rank = GUILD_RANK_MEMBER
                    member.can_invite = False
                    repaired = True
            self.guilds[guild.guild_id] = guild
        for raw in document.get("groups", ()):
            group = Group(
                int(raw["group_id"]),
                str(raw["name"]),
                str(raw.get("description", "")),
                str(raw.get("map_id", "")),
                int(raw["owner_character_id"]),
                bool(raw.get("listed", True)),
                bool(raw.get("private", False)),
                [SocialMember(**member) for member in raw.get("members", ())],
            )
            # Early builds mistook FoundationRequest's first string (the
            # creator's character name) for a group description.  The client
            # then rendered a possessive fallback such as ``test's Group``.
            owner = next(
                (
                    member
                    for member in group.members
                    if member.character_id == group.owner_character_id
                ),
                None,
            )
            if owner is not None and group.description == owner.name:
                group.description = ""
                repaired = True
            self.groups[group.group_id] = group
        if repaired:
            self.flush()

    def flush(self) -> None:
        with self.lock:
            document = {
                "version": STATE_VERSION,
                "next_guild_id": self.next_guild_id,
                "next_group_id": self.next_group_id,
                "guilds": [asdict(value) for value in self.guilds.values()],
                "groups": [asdict(value) for value in self.groups.values()],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    def guild_for(self, character_id: int) -> Guild | None:
        with self.lock:
            for guild in self.guilds.values():
                if any(m.character_id == character_id for m in guild.members):
                    return copy.deepcopy(guild)
        return None

    def create_guild(
        self, name: str, description: str, member: SocialMember
    ) -> Guild:
        cleaned = name.strip()
        if not 3 <= len(cleaned) <= 32:
            raise ValueError("guild name must contain 3..32 characters")
        with self.lock:
            if self.guild_for(member.character_id):
                raise ValueError("character is already in a guild")
            if any(g.name.casefold() == cleaned.casefold() for g in self.guilds.values()):
                raise ValueError("guild name is already in use")
            member = copy.deepcopy(member)
            member.rank = GUILD_RANK_LEADER
            member.can_invite = True
            guild = Guild(
                self.next_guild_id, cleaned, description.strip(), [member]
            )
            self.next_guild_id += 1
            self.guilds[guild.guild_id] = guild
            self.flush()
            return copy.deepcopy(guild)

    def leave_guild(self, character_id: int) -> Guild | None:
        with self.lock:
            guild = next(
                (
                    value
                    for value in self.guilds.values()
                    if any(m.character_id == character_id for m in value.members)
                ),
                None,
            )
            if guild is None:
                return None
            departing = next(m for m in guild.members if m.character_id == character_id)
            guild.members = [
                m for m in guild.members if m.character_id != character_id
            ]
            if not guild.members:
                del self.guilds[guild.guild_id]
                self.flush()
                return None
            if departing.rank == GUILD_RANK_LEADER:
                successor = min(
                    guild.members,
                    key=lambda m: (
                        0 if m.rank == GUILD_RANK_OFFICER else 1,
                        m.joined_at,
                        m.character_id,
                    ),
                )
                successor.rank = GUILD_RANK_LEADER
                successor.can_invite = True
            self.flush()
            return copy.deepcopy(guild)

    def group_for(self, character_id: int) -> Group | None:
        with self.lock:
            for group in self.groups.values():
                if any(m.character_id == character_id for m in group.members):
                    return copy.deepcopy(group)
        return None

    def group_by_id(self, group_id: int) -> Group | None:
        """Return a detached roster for group finder inspection."""

        with self.lock:
            group = self.groups.get(int(group_id))
            return copy.deepcopy(group) if group is not None else None

    def groups_in_map(self, map_id: str) -> tuple[Group, ...]:
        with self.lock:
            return tuple(
                copy.deepcopy(group)
                for group in self.groups.values()
                if group.listed and (not map_id or group.map_id == map_id)
            )

    def create_group(
        self, name: str, description: str, map_id: str, member: SocialMember
    ) -> Group:
        cleaned = name.strip()
        if not 1 <= len(cleaned) <= 32:
            raise ValueError("group name must contain 1..32 characters")
        with self.lock:
            if self.group_for(member.character_id):
                raise ValueError("character is already in a group")
            member = copy.deepcopy(member)
            member.can_invite = True
            group = Group(
                self.next_group_id,
                cleaned,
                description.strip(),
                map_id,
                member.character_id,
                True,
                False,
                [member],
            )
            self.next_group_id += 1
            self.groups[group.group_id] = group
            self.flush()
            return copy.deepcopy(group)

    def add_group_member(
        self,
        group_id: int,
        inviter_character_id: int,
        member: SocialMember,
    ) -> Group:
        with self.lock:
            group = self.groups.get(int(group_id))
            if group is None:
                raise ValueError("group no longer exists")
            inviter = next(
                (
                    value
                    for value in group.members
                    if value.character_id == int(inviter_character_id)
                ),
                None,
            )
            if inviter is None or (
                group.owner_character_id != inviter.character_id
                and not inviter.can_invite
            ):
                raise ValueError("character cannot invite to this group")
            existing = self.group_for(member.character_id)
            if existing is not None:
                raise ValueError("character is already in a group")
            if len(group.members) >= GROUP_MEMBER_LIMIT:
                raise GroupFullError(
                    f"group {group.group_id} already holds "
                    f"{GROUP_MEMBER_LIMIT} members"
                )
            joined = copy.deepcopy(member)
            joined.can_invite = False
            group.members.append(joined)
            self.flush()
            return copy.deepcopy(group)

    def add_guild_member(
        self,
        guild_id: int,
        inviter_character_id: int,
        member: SocialMember,
    ) -> Guild:
        with self.lock:
            guild = self.guilds.get(int(guild_id))
            if guild is None:
                raise ValueError("guild no longer exists")
            inviter = next(
                (
                    value
                    for value in guild.members
                    if value.character_id == int(inviter_character_id)
                ),
                None,
            )
            if inviter is None or not (
                inviter.can_invite
                or inviter.rank in {
                    GUILD_RANK_LEADER,
                    GUILD_RANK_OFFICER,
                }
            ):
                raise ValueError("character cannot invite to this guild")
            existing = self.guild_for(member.character_id)
            if existing is not None:
                raise ValueError("character is already in a guild")
            joined = copy.deepcopy(member)
            joined.rank = GUILD_RANK_MEMBER
            joined.can_invite = False
            guild.members.append(joined)
            self.flush()
            return copy.deepcopy(guild)

    def leave_group(self, character_id: int) -> Group | None:
        with self.lock:
            group = next(
                (
                    value
                    for value in self.groups.values()
                    if any(m.character_id == character_id for m in value.members)
                ),
                None,
            )
            if group is None:
                return None
            group.members = [
                m for m in group.members if m.character_id != character_id
            ]
            if not group.members:
                del self.groups[group.group_id]
                self.flush()
                return None
            if group.owner_character_id == character_id:
                successor = min(
                    group.members, key=lambda m: (m.joined_at, m.character_id)
                )
                group.owner_character_id = successor.character_id
                successor.can_invite = True
            self.flush()
            return copy.deepcopy(group)


def make_social_member(
    character_id: int,
    name: str,
    class_id: int,
    level: int,
    *,
    account_id: int = 777_777,
) -> SocialMember:
    return SocialMember(
        int(character_id),
        int(account_id),
        str(name),
        int(class_id),
        int(level),
        SocialStateStore._now(),
    )
