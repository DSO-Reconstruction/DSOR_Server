"""Durable guild and group state for the local development server."""

from __future__ import annotations

import copy
from collections import deque
from dataclasses import asdict, dataclass, field
from functools import wraps
import hmac
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

# ``gui.window.grouping/groupEditWindowDescriptionCreate``: "Group names can
# contain up to 40 characters".  The same bound applies to the edit dialog.
GROUP_NAME_MAX_LENGTH = 40

# Local choices (no authored value exists in the client or static.db4):
# guild names keep the historical 3..32 bound, a guild's message of the day
# is bounded so one officer cannot grow every member's chat batch without
# limit, an ignore list holds at most 100 characters, and an unanswered
# group/guild invitation or join request lapses after five minutes.
GUILD_NAME_MIN_LENGTH = 3
GUILD_NAME_MAX_LENGTH = 32
GUILD_MESSAGE_MAX_LENGTH = 1024
IGNORE_LIST_LIMIT = 100
INVITATION_TTL_SECONDS = 300.0

INVITATION_KINDS = frozenset({"group", "guild", "group-request"})


class GroupFullError(ValueError):
    """Raised when a join would exceed :data:`GROUP_MEMBER_LIMIT`."""


class SocialPermissionError(ValueError):
    """The acting character lacks the rank/leadership the operation needs."""


class SocialTargetError(ValueError):
    """The named character is not a member of the actor's collection."""


class SocialNameError(ValueError):
    """A group or guild name is empty, too long, or otherwise invalid."""


class SocialNameTakenError(ValueError):
    """Another guild already uses the requested name."""


class SocialNoChangeError(ValueError):
    """The request would not change any durable state."""


class SocialContactBlockedError(ValueError):
    """One of the two characters ignores the other."""


class InvitationPendingError(ValueError):
    """The same invitation or join request is still waiting for an answer."""


class InvitationMissingError(ValueError):
    """No live invitation matches the answer (forged, replayed, or expired)."""


@dataclass(frozen=True)
class PendingInvitation:
    """One server-issued invitation capability.

    The ledger is keyed by ``(kind, source, target, collective_id)``: for
    ``group``/``guild`` the source is the inviter and the target the invited
    character; for ``group-request`` the source is the character asking to
    join and the target the group leader who must answer.  ``token`` is the
    opaque value delivered in the client notification; ``leave_current``
    records a join request sent with "leave my current group" confirmed.
    """

    token: str
    expires_at: float
    leave_current: bool = False


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
    message_of_the_day: str = ""


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
        # owner character id -> {ignored character id: name when ignored}.
        self.ignored: dict[int, dict[int, str]] = {}
        # Invitations are session-era capabilities, never restored after restart.
        self._invitations: dict[tuple[str, int, int, int], PendingInvitation] = {}
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
                str(raw.get("message_of_the_day", "")),
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
        raw_ignored = document.get("ignored", {})
        if isinstance(raw_ignored, dict):
            for owner, entries in raw_ignored.items():
                if not isinstance(entries, dict):
                    continue
                kept = {
                    int(target): str(name) for target, name in entries.items()
                    if int(target) != int(owner)
                }
                if kept:
                    self.ignored[int(owner)] = kept
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
        next_guild_id, next_group_id, guilds, groups, buddies, ignored = snapshot
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
            "ignored": {str(owner): entries for owner, entries in ignored if entries},
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
                    tuple(self.ignored.items()),
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

    def _validate_guild_foundation_locked(self, character_id: int, name: str) -> str:
        cleaned = str(name).strip()
        if not GUILD_NAME_MIN_LENGTH <= len(cleaned) <= GUILD_NAME_MAX_LENGTH:
            raise ValueError("guild name must contain 3..32 characters")
        if self._guild_memberships.get(int(character_id)) in self.guilds:
            raise ValueError("character is already in a guild")
        if cleaned.casefold() in self._guild_names:
            raise ValueError("guild name is already in use")
        return cleaned

    def validate_guild_foundation(self, character_id: int, name: str) -> str:
        """Check a foundation without changing state; return the name.

        Lets the caller charge the authored foundation fee only for a
        request :meth:`create_guild` would accept (it repeats the check).
        """

        with self.lock:
            return self._validate_guild_foundation_locked(character_id, name)

    @_durable_mutation
    def create_guild(
        self, name: str, description: str, member: SocialMember
    ) -> Guild:
        with self.lock:
            cleaned = self._validate_guild_foundation_locked(
                member.character_id, name
            )
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
        character_id = int(character_id)
        with self.lock:
            guild = self.guild_for(character_id)
            if guild is None:
                return None
            departing = next(m for m in guild.members if m.character_id == character_id)
            guild.members = [
                m for m in guild.members if m.character_id != character_id
            ]
            self._guild_memberships.pop(character_id, None)
            self._drop_invitations_locked(
                lambda key: key[0] == "guild" and key[1] == character_id
                and key[3] == guild.guild_id
            )
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
            # ClientSocialManager refuses a buddy on the own ignore list
            # (gui.window.social/cannotBeBuddyIfIgnored); the server also
            # refuses a request towards someone who ignores the requester.
            if self.social_contact_blocked(owner, buddy):
                raise SocialContactBlockedError(
                    "an ignored character cannot become a buddy"
                )
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

    # -- ignored players ---------------------------------------------------

    def ignored_for(self, character_id: int) -> tuple[tuple[int, str], ...]:
        """Return ``(character_id, name)`` pairs the owner ignores."""

        with self.lock:
            return tuple(sorted(self.ignored.get(int(character_id), {}).items()))

    def ignores(self, owner: int, target: int) -> bool:
        with self.lock:
            return int(target) in self.ignored.get(int(owner), {})

    def social_contact_blocked(self, first: int, second: int) -> bool:
        """True when either character ignores the other."""

        with self.lock:
            return self.ignores(first, second) or self.ignores(second, first)

    @_durable_mutation
    def set_ignored(
        self, owner: int, target: int, name: str, *, remove: bool = False
    ) -> bool:
        """Add or remove one ignored character; return whether it changed.

        Mirrors ClientSocialManager::AddPlayerToIgnored (0x14026fa4c): a
        character cannot ignore itself (``cannotIgnoreSelf``) or a character
        on its own buddy list (``cannotIgnoreBuddy``).  Ignoring someone
        withdraws every pending invitation/join request between the two.
        """

        owner, target = int(owner), int(target)
        if owner == target:
            raise ValueError("a character cannot ignore itself")
        current = self.ignored.get(owner, {})
        if remove:
            if target not in current:
                return False
            entries = {key: value for key, value in current.items() if key != target}
        else:
            if target in current:
                return False
            if target in self.buddies.get(owner, ()):
                raise SocialContactBlockedError("a buddy cannot be ignored")
            if len(current) >= IGNORE_LIST_LIMIT:
                raise ValueError(
                    f"an ignore list holds at most {IGNORE_LIST_LIMIT} characters"
                )
            entries = {**current, target: str(name)}
        if entries:
            self.ignored[owner] = entries
        else:
            self.ignored.pop(owner, None)
        if not remove:
            self._drop_invitations_locked(
                lambda key: {key[1], key[2]} == {owner, target}
            )
        self._queue_write_locked()
        return True

    # -- invitation ledger ---------------------------------------------------

    def _drop_invitations_locked(self, predicate) -> None:
        self._invitations = {
            key: value for key, value in self._invitations.items()
            if not predicate(key)
        }

    def _purge_expired_invitations_locked(self, clock: float) -> None:
        self._invitations = {
            key: value for key, value in self._invitations.items()
            if value.expires_at > clock
        }

    def _authorized_inviter(self, kind: str, collective_id: int, inviter: int):
        """Return the live collection when ``inviter`` may admit members.

        Groups admit through the leader or a member granted ``can_invite``;
        guilds through officers, the leader, or ``can_invite`` members (the
        same rule ``add_group_member``/``add_guild_member`` enforce).
        """

        collective = (self.groups if kind == "group" else self.guilds).get(
            int(collective_id)
        )
        if collective is None:
            raise InvitationMissingError("social collection no longer exists")
        member = next(
            (m for m in collective.members if m.character_id == int(inviter)),
            None,
        )
        allowed = member is not None and (
            member.can_invite
            or (
                collective.owner_character_id == int(inviter)
                if kind == "group"
                else member.rank in {GUILD_RANK_LEADER, GUILD_RANK_OFFICER}
            )
        )
        if not allowed:
            raise SocialPermissionError("character cannot invite to this collection")
        return collective

    @staticmethod
    def _invitation_roles(kind: str, source: int, target: int):
        """Return ``(collection kind, admitting member, joining member)``."""

        if kind == "group-request":
            return "group", int(target), int(source)
        return kind, int(source), int(target)

    def pending_invitation(
        self, kind: str, source: int, target: int, collective_id: int,
        *, now: float | None = None,
    ) -> PendingInvitation | None:
        clock = time.monotonic() if now is None else float(now)
        with self.lock:
            record = self._invitations.get(
                (kind, int(source), int(target), int(collective_id))
            )
            return record if record is not None and record.expires_at > clock else None

    def issue_invitation(
        self, kind: str, source: int, target: int, collective_id: int,
        token: str, *, now: float | None = None, leave_current: bool = False,
    ) -> PendingInvitation:
        """Record a capability the target's answer must later present.

        Raises before anything is delivered when the two characters ignore
        each other, the admitting member lost the right to admit, the
        joining character already belongs to this collection, a group is
        full, or the same invitation is still pending.
        """

        if kind not in INVITATION_KINDS:
            raise ValueError("invalid invitation kind")
        if not str(token):
            raise ValueError("an invitation needs a token")
        source, target = int(source), int(target)
        if source == target:
            raise ValueError("a character cannot invite itself")
        actual_kind, inviter, joining = self._invitation_roles(kind, source, target)
        clock = time.monotonic() if now is None else float(now)
        with self.lock:
            if self._closed:
                raise RuntimeError("social state store is closed")
            if self.social_contact_blocked(source, target):
                raise SocialContactBlockedError("social contact is ignored")
            collective = self._authorized_inviter(actual_kind, collective_id, inviter)
            if any(m.character_id == joining for m in collective.members):
                raise ValueError("character is already a member")
            if actual_kind == "group" and len(collective.members) >= GROUP_MEMBER_LIMIT:
                raise GroupFullError("group is full")
            self._purge_expired_invitations_locked(clock)
            key = (kind, source, target, int(collective_id))
            if key in self._invitations:
                raise InvitationPendingError("the invitation is still pending")
            record = PendingInvitation(
                str(token), clock + INVITATION_TTL_SECONDS, bool(leave_current)
            )
            self._invitations[key] = record
            return record

    @_durable_mutation
    def answer_invitation(
        self, kind: str, source: int, target: int, collective_id: int,
        member: SocialMember | None = None, *, accept: bool,
        token: str | None = None, now: float | None = None,
    ):
        """Consume one issued invitation; on acceptance add the member.

        The caller passes the authenticated answering character as
        ``target`` (``group``/``guild``) or the authenticated leader
        (``group-request``), so a fabricated or replayed answer cannot match
        a ledger entry.  A token, when the client echoes one, must match
        exactly; a mismatch leaves the real invitation in place.  The entry
        is consumed before membership is checked, so one invitation admits
        at most once.  Returns the joined collection, or ``None`` for a
        refusal.
        """

        key = (kind, int(source), int(target), int(collective_id))
        clock = time.monotonic() if now is None else float(now)
        record = self._invitations.get(key)
        if record is None or record.expires_at <= clock:
            self._invitations.pop(key, None)
            raise InvitationMissingError("social invitation is missing or expired")
        if token is not None and not hmac.compare_digest(
            record.token.encode("utf-8"), str(token).encode("utf-8")
        ):
            raise InvitationMissingError("social invitation token does not match")
        del self._invitations[key]
        if not accept:
            return None
        if self.social_contact_blocked(source, target):
            raise SocialContactBlockedError("social contact is ignored")
        actual_kind, inviter, joining = self._invitation_roles(kind, source, target)
        if member is None or int(member.character_id) != joining:
            raise ValueError("invitation recipient does not match")
        self._authorized_inviter(actual_kind, collective_id, inviter)
        if actual_kind == "group":
            current = self._group_memberships.get(joining)
            if current is not None and current != int(collective_id):
                previous = self.groups.get(current)
                # AcceptGroupInvite (0x14026ea94) only accepts while the own
                # group has at most one member; a confirmed join request
                # (confirmGroupLeaveForJoinRequest) leaves any group.
                if previous is not None and (
                    record.leave_current or len(previous.members) <= 1
                ):
                    self._leave_group_locked(joining)
            return type(self).add_group_member.__wrapped__(
                self, collective_id, inviter, member
            )
        return type(self).add_guild_member.__wrapped__(
            self, collective_id, inviter, member
        )

    # -- management ------------------------------------------------------------

    def _collection_for_locked(self, kind: str, character_id: int):
        if kind == "group":
            return self.groups.get(self._group_memberships.get(int(character_id)))
        if kind == "guild":
            return self.guilds.get(self._guild_memberships.get(int(character_id)))
        raise ValueError(f"unsupported social collection {kind!r}")

    @_durable_mutation
    def kick_member(
        self, kind: str, actor: int, target_name: str
    ) -> tuple[int, SocialMember]:
        """Remove a named member; return ``(collection id, removed member)``.

        Groups: only the leader (GroupKickPlayer 0x140274894 requires the
        GroupStatus leader-rights flag).  Guilds: the actor's rank must be
        strictly higher than the target's (GuildKickPlayer 0x1402762a8,
        ``insufficientGuildRights``), so officers remove members and only
        the leader removes officers.  Nobody removes themselves.
        """

        actor = int(actor)
        stored = self._collection_for_locked(kind, actor)
        if stored is None:
            raise SocialPermissionError("character has no social membership")
        collective = copy.deepcopy(stored)
        source = next(m for m in collective.members if m.character_id == actor)
        wanted = str(target_name).strip().casefold()
        target = next(
            (m for m in collective.members if m.name.casefold() == wanted), None
        )
        if target is None:
            raise SocialTargetError("character is not a member")
        if target.character_id == actor:
            raise SocialTargetError("a character cannot remove itself")
        if kind == "group":
            if collective.owner_character_id != actor:
                raise SocialPermissionError("only the group leader can remove members")
            collective_id = collective.group_id
        else:
            if int(source.rank) <= int(target.rank):
                raise SocialPermissionError("guild rank is too low to remove this member")
            collective_id = collective.guild_id
        collective.members = [
            m for m in collective.members if m.character_id != target.character_id
        ]
        if kind == "group":
            self.groups[collective_id] = collective
            self._group_memberships.pop(target.character_id, None)
        else:
            self.guilds[collective_id] = collective
            self._guild_memberships.pop(target.character_id, None)
        # Invitations the removed member issued for this collection are void.
        self._drop_invitations_locked(
            lambda key: key[0] == kind and key[1] == target.character_id
            and key[3] == collective_id
        )
        self._queue_write_locked()
        return collective_id, copy.deepcopy(target)

    def _validate_group_name(self, name: str) -> str:
        cleaned = str(name).strip()
        if not cleaned:
            raise SocialNameError("group name is empty")
        if len(cleaned) > GROUP_NAME_MAX_LENGTH:
            raise SocialNameError(
                f"group name must contain at most {GROUP_NAME_MAX_LENGTH} characters"
            )
        return cleaned

    @_durable_mutation
    def edit_group(self, actor: int, name: str, hidden: bool) -> Group:
        """Rename the leader's group and set its hidden flag.

        A hidden group is private and leaves the group finder
        (``groupEditWindowHideGroupTooltip``).  Unchanged settings raise
        :class:`SocialNoChangeError` without a write.
        """

        stored = self._collection_for_locked("group", actor)
        if stored is None or stored.owner_character_id != int(actor):
            raise SocialPermissionError("only the leader can edit the group")
        cleaned = self._validate_group_name(name)
        hidden = bool(hidden)
        if (stored.name, stored.private, stored.listed) == (cleaned, hidden, not hidden):
            raise SocialNoChangeError("group settings are unchanged")
        group = copy.deepcopy(stored)
        group.name, group.private, group.listed = cleaned, hidden, not hidden
        self.groups[group.group_id] = group
        self._queue_write_locked()
        return copy.deepcopy(group)

    def validate_guild_rename(self, actor: int, name: str) -> str:
        """Check a leader's rename without changing state; return the name.

        Raises :class:`SocialPermissionError`, :class:`SocialNameError`,
        :class:`SocialNoChangeError` or :class:`SocialNameTakenError`.  The
        caller charges the rename cost only after this check passes, and
        :meth:`rename_guild` repeats it atomically.
        """

        with self.lock:
            return self._validate_guild_rename_locked(int(actor), name)[1]

    def _validate_guild_rename_locked(self, actor: int, name: str):
        guild = self._collection_for_locked("guild", actor)
        if guild is None or not any(
            m.character_id == actor and m.rank == GUILD_RANK_LEADER
            for m in guild.members
        ):
            raise SocialPermissionError("only the guild leader can rename the guild")
        cleaned = str(name).strip()
        if not GUILD_NAME_MIN_LENGTH <= len(cleaned) <= GUILD_NAME_MAX_LENGTH:
            raise SocialNameError(
                f"guild name must contain {GUILD_NAME_MIN_LENGTH}.."
                f"{GUILD_NAME_MAX_LENGTH} characters"
            )
        if cleaned == guild.name:
            raise SocialNoChangeError("guild name is unchanged")
        if (
            cleaned.casefold() != guild.name.casefold()
            and cleaned.casefold() in self._guild_names
        ):
            raise SocialNameTakenError("guild name is already in use")
        return guild, cleaned

    @_durable_mutation
    def rename_guild(self, actor: int, name: str) -> Guild:
        stored, cleaned = self._validate_guild_rename_locked(int(actor), name)
        guild = copy.deepcopy(stored)
        self._guild_names.discard(guild.name.casefold())
        guild.name = cleaned
        self._guild_names.add(cleaned.casefold())
        self.guilds[guild.guild_id] = guild
        self._queue_write_locked()
        return copy.deepcopy(guild)

    @_durable_mutation
    def change_guild_message(self, actor: int, message: str) -> Guild:
        """Set (or clear) the guild's message of the day.

        Officers and the leader may change it; an empty message deletes it
        (the client's ``/dmotd`` sends an empty change).  An unchanged
        message raises :class:`SocialNoChangeError` without a write.
        """

        stored = self._collection_for_locked("guild", actor)
        if stored is None or not any(
            m.character_id == int(actor)
            and m.rank in {GUILD_RANK_LEADER, GUILD_RANK_OFFICER}
            for m in stored.members
        ):
            raise SocialPermissionError("guild officer permission required")
        message = str(message).strip()
        if len(message) > GUILD_MESSAGE_MAX_LENGTH:
            raise ValueError("guild message is too long")
        if message == stored.message_of_the_day:
            raise SocialNoChangeError("guild message is unchanged")
        guild = copy.deepcopy(stored)
        guild.message_of_the_day = message
        self.guilds[guild.guild_id] = guild
        self._queue_write_locked()
        return copy.deepcopy(guild)

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
        cleaned = self._validate_group_name(name)
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
        return self._leave_group_locked(character_id)

    def _leave_group_locked(self, character_id: int) -> Group | None:
        """Remove one member (caller holds the lock and queues persistence).

        The longest-standing remaining member inherits leadership and the
        right to invite; the last member dissolves the group.
        """

        character_id = int(character_id)
        stored = self.groups.get(self._group_memberships.get(character_id))
        if stored is None:
            return None
        group = copy.deepcopy(stored)
        group.members = [
            m for m in group.members if m.character_id != character_id
        ]
        self._group_memberships.pop(character_id, None)
        self._drop_invitations_locked(
            lambda key: key[0] == "group" and key[1] == character_id
            and key[3] == group.group_id
        )
        if not group.members:
            del self.groups[group.group_id]
            self._drop_invitations_locked(
                lambda key: key[0] in {"group", "group-request"}
                and key[3] == group.group_id
            )
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
