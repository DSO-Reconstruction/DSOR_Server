"""Durable guild and group state for the local development server."""

from __future__ import annotations

import copy
from collections import deque
from dataclasses import asdict, dataclass, field
from functools import wraps
import json
import os
from pathlib import Path
import tempfile
import threading
import time
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


def _durable_mutation(method):
    """Keep existing durable-return semantics without holding the social lock."""
    @wraps(method)
    def invoke(self, *args, **kwargs):
        if self.lock._is_owned():
            raise RuntimeError("social mutations must run without the caller holding the social lock")
        with self.lock:
            if self._closed:
                raise RuntimeError("social state store is closed")
            result = method(self, *args, **kwargs)
        self.flush()
        return result
    return invoke


class SocialStateStore:
    def __init__(self, path: str | Path = DEFAULT_SOCIAL_STATE_PATH):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.guilds: dict[int, Guild] = {}
        self.groups: dict[int, Group] = {}
        self.buddies: dict[int, set[int]] = {}
        self.next_guild_id = 50_000_001
        self.next_group_id = 60_000_001
        self._guild_memberships: dict[int, int] = {}
        self._group_memberships: dict[int, int] = {}
        self._guild_names: set[str] = set()
        self._condition = threading.Condition()
        self._queued_revision = 0
        self._written_revision = 0
        self._thread: threading.Thread | None = None
        self._last_error: Exception | None = None
        self._closed = False
        self._stop = False
        self.warnings: deque[str] = deque(maxlen=32)
        self._load()
        self._guild_memberships = {
            member.character_id: guild.guild_id
            for guild in self.guilds.values() for member in guild.members
        }
        self._group_memberships = {
            member.character_id: group.group_id
            for group in self.groups.values() for member in group.members
        }
        self._guild_names = {guild.name.casefold() for guild in self.guilds.values()}

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
        raw_buddies = document.get("buddies", {})
        if isinstance(raw_buddies, dict):
            for owner_id, buddy_ids in raw_buddies.items():
                owner = int(owner_id)
                self.buddies[owner] = {
                    int(buddy_id)
                    for buddy_id in buddy_ids
                    if int(buddy_id) != owner
                }
        if repaired:
            self._queue_write_locked()
            self.flush()

    def _queue_write_locked(self) -> None:
        # One revision counter represents all pending work. A mutation burst
        # cannot build an unbounded queue of whole-world JSON snapshots.
        with self._condition:
            self._queued_revision += 1
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._writer_loop, name="social-state-writer", daemon=True
                )
                self._thread.start()
            self._condition.notify_all()

    def _write_snapshot(self, snapshot) -> None:
        next_guild_id, next_group_id, guilds, groups, buddies = snapshot
        document = {
            "version": STATE_VERSION,
            "next_guild_id": next_guild_id,
            "next_group_id": next_group_id,
            "guilds": [asdict(value) for value in guilds],
            "groups": [asdict(value) for value in groups],
            "buddies": {
                str(owner_id): sorted(buddy_ids)
                for owner_id, buddy_ids in buddies if buddy_ids
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as output:
                temporary = Path(output.name)
                json.dump(document, output, ensure_ascii=False, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _writer_loop(self) -> None:
        while True:
            with self._condition:
                while self._written_revision >= self._queued_revision:
                    if self._stop or not self._condition.wait(timeout=1.0):
                        # Idle stores should not retain a daemon forever (also
                        # useful for short-lived tooling and test instances).
                        self._thread = None
                        return
                self._condition.wait(timeout=0.01)
            # Mutations replace changed entities/sets. These shallow references
            # are immutable snapshots, so asdict/JSON/fsync need no social lock.
            with self.lock:
                snapshot = (
                    self.next_guild_id, self.next_group_id,
                    tuple(self.guilds.values()), tuple(self.groups.values()),
                    tuple(self.buddies.items()),
                )
                with self._condition:
                    revision = self._queued_revision
            try:
                self._write_snapshot(snapshot)
            except Exception as exc:
                with self._condition:
                    self._last_error = exc
                    self.warnings.append(f"social state write failed: {exc}")
                    self._condition.notify_all()
                    self._condition.wait(timeout=0.1)
                continue
            with self._condition:
                self._written_revision = max(self._written_revision, revision)
                self._last_error = None
                self._condition.notify_all()

    def flush(self, timeout: float = 15.0) -> None:
        """Wait for queued mutations, raising if persistence cannot catch up."""
        if self.lock._is_owned():
            raise RuntimeError("social flush must run without holding the social lock")
        deadline = time.monotonic() + max(0.01, float(timeout))
        with self._condition:
            target = self._queued_revision
            while self._written_revision < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out flushing social state: {self._last_error}"
                    ) from self._last_error
                self._condition.wait(remaining)

    def close(self, timeout: float = 15.0) -> None:
        """Drain writes before shutdown; retain pending work on timeout."""
        with self.lock:
            self._closed = True
        self.flush(timeout)
        with self._condition:
            self._stop = True
            thread = self._thread
            self._condition.notify_all()
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise TimeoutError("timed out stopping social state writer")

    def guild_for(self, character_id: int) -> Guild | None:
        with self.lock:
            guild = self.guilds.get(self._guild_memberships.get(int(character_id)))
            return copy.deepcopy(guild) if guild is not None else None

    @_durable_mutation
    def create_guild(
        self, name: str, description: str, member: SocialMember
    ) -> Guild:
        cleaned = name.strip()
        if not 3 <= len(cleaned) <= 32:
            raise ValueError("guild name must contain 3..32 characters")
        with self.lock:
            if self.guild_for(member.character_id):
                raise ValueError("character is already in a guild")
            if cleaned.casefold() in self._guild_names:
                raise ValueError("guild name is already in use")
            member = copy.deepcopy(member)
            member.rank = GUILD_RANK_LEADER
            member.can_invite = True
            guild = Guild(
                self.next_guild_id, cleaned, description.strip(), [member]
            )
            self.next_guild_id += 1
            self.guilds[guild.guild_id] = guild
            self._guild_names.add(guild.name.casefold())
            self._guild_memberships[member.character_id] = guild.guild_id
            self._queue_write_locked()
            return copy.deepcopy(guild)

    @_durable_mutation
    def leave_guild(self, character_id: int) -> Guild | None:
        with self.lock:
            guild = self.guild_for(character_id)
            if guild is None:
                return None
            departing = next(m for m in guild.members if m.character_id == character_id)
            guild.members = [
                m for m in guild.members if m.character_id != character_id
            ]
            self._guild_memberships.pop(int(character_id), None)
            if not guild.members:
                del self.guilds[guild.guild_id]
                self._guild_names.discard(guild.name.casefold())
                self._queue_write_locked()
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
            self.guilds[guild.guild_id] = guild
            self._queue_write_locked()
            return copy.deepcopy(guild)

    def group_for(self, character_id: int) -> Group | None:
        with self.lock:
            group = self.groups.get(self._group_memberships.get(int(character_id)))
            return copy.deepcopy(group) if group is not None else None

    def buddies_for(self, character_id: int) -> tuple[int, ...]:
        with self.lock:
            return tuple(sorted(self.buddies.get(int(character_id), ())))

    @_durable_mutation
    def add_buddy(self, character_id: int, buddy_character_id: int) -> None:
        owner = int(character_id)
        buddy = int(buddy_character_id)
        if owner == buddy:
            raise ValueError("a character cannot add itself as a buddy")
        with self.lock:
            buddies = set(self.buddies.get(owner, ()))
            if buddy in buddies:
                raise ValueError("character is already a buddy")
            buddies.add(buddy)
            self.buddies[owner] = buddies
            self._queue_write_locked()

    @_durable_mutation
    def remove_buddy(self, character_id: int, buddy_character_id: int) -> bool:
        owner = int(character_id)
        buddy = int(buddy_character_id)
        with self.lock:
            buddies = set(self.buddies.get(owner, ()))
            if not buddies or buddy not in buddies:
                return False
            buddies.remove(buddy)
            self.buddies[owner] = buddies
            if not buddies:
                self.buddies.pop(owner, None)
            self._queue_write_locked()
            return True

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

    @_durable_mutation
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
            self._group_memberships[member.character_id] = group.group_id
            self._queue_write_locked()
            return copy.deepcopy(group)

    @_durable_mutation
    def add_group_member(
        self,
        group_id: int,
        inviter_character_id: int,
        member: SocialMember,
    ) -> Group:
        with self.lock:
            group = copy.deepcopy(self.groups.get(int(group_id)))
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
            self.groups[group.group_id] = group
            self._group_memberships[joined.character_id] = group.group_id
            self._queue_write_locked()
            return copy.deepcopy(group)

    @_durable_mutation
    def add_guild_member(
        self,
        guild_id: int,
        inviter_character_id: int,
        member: SocialMember,
    ) -> Guild:
        with self.lock:
            guild = copy.deepcopy(self.guilds.get(int(guild_id)))
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
            self.guilds[guild.guild_id] = guild
            self._guild_memberships[joined.character_id] = guild.guild_id
            self._queue_write_locked()
            return copy.deepcopy(guild)

    @_durable_mutation
    def leave_group(self, character_id: int) -> Group | None:
        with self.lock:
            group = self.group_for(character_id)
            if group is None:
                return None
            group.members = [
                m for m in group.members if m.character_id != character_id
            ]
            self._group_memberships.pop(int(character_id), None)
            if not group.members:
                del self.groups[group.group_id]
                self._queue_write_locked()
                return None
            if group.owner_character_id == character_id:
                successor = min(
                    group.members, key=lambda m: (m.joined_at, m.character_id)
                )
                group.owner_character_id = successor.character_id
                successor.can_invite = True
            self.groups[group.group_id] = group
            self._queue_write_locked()
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
