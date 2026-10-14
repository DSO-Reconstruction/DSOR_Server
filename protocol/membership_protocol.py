"""Typed Deluxe/Premium membership protocol and expiry helpers.

``MembershipUpdateCommand`` is generated command ``0x0024``. The retail
serializer writes the membership enum, one Nebula ``CalendarTime`` and an
active bit; the player actor context follows the body in the normal generated
server-command wrapper.
"""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass
from datetime import datetime, timezone

from dso_server.protocol.character_selection import BitReader, BitWriter


MEMBERSHIP_UPDATE_COMMAND = command_id("Commands::MembershipUpdateCommand", 0x0024)
NO_MEMBERSHIP = 0
PREMIUM_MEMBERSHIP = 1
DELUXE_MEMBERSHIP = 2

DEFAULT_DELUXE_YEARS = 5


def add_calendar_years(value: datetime, years: int) -> datetime:
    """Add whole calendar years, clamping 29 February to 28 February."""

    try:
        return value.replace(year=value.year + int(years))
    except ValueError:
        return value.replace(
            year=value.year + int(years),
            month=2,
            day=28,
        )


def default_deluxe_expiry(
    now: datetime | None = None,
    *,
    years: int = DEFAULT_DELUXE_YEARS,
) -> str:
    """Return the persisted UTC expiry for a newly granted Deluxe status."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    return add_calendar_years(current, years).isoformat()


def parse_membership_expiry(value: str) -> datetime:
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def membership_is_active(
    membership_type: int, active: bool, expires_at: str,
    *, now: datetime | None = None,
) -> bool:
    """Both paid tiers grant bags only until their UTC expiry."""

    if not active or membership_type not in (PREMIUM_MEMBERSHIP, DELUXE_MEMBERSHIP):
        return False
    try:
        expiry = parse_membership_expiry(expires_at)
    except (TypeError, ValueError):
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expiry > current


@dataclass(frozen=True)
class CalendarTime:
    """Nebula CalendarTime's eight in-memory integer fields."""

    year: int
    month: int
    weekday: int
    day: int
    hour: int
    minute: int
    second: int
    millisecond: int

    @classmethod
    def from_datetime(cls, value: datetime) -> "CalendarTime":
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        normalized = normalized.astimezone(timezone.utc)
        return cls(
            year=normalized.year,
            month=normalized.month,
            weekday=normalized.weekday(),
            day=normalized.day,
            hour=normalized.hour,
            minute=normalized.minute,
            second=normalized.second,
            millisecond=normalized.microsecond // 1000,
        )


@dataclass(frozen=True)
class MembershipUpdate:
    membership_type: int
    expires_at: CalendarTime
    active: bool


def membership_update_from_expiry(
    expires_at: str,
    *,
    membership_type: int = DELUXE_MEMBERSHIP,
    active: bool = True,
) -> MembershipUpdate:
    return MembershipUpdate(
        int(membership_type),
        CalendarTime.from_datetime(parse_membership_expiry(expires_at)),
        bool(active),
    )


def read_membership_update_body(reader: BitReader) -> MembershipUpdate:
    membership_type = reader.read_uint32_le()
    # The generated serializer order differs from CalendarTime's memory
    # layout: year, day, hour, minute, second, millisecond, weekday, month.
    year = reader.read_uint32_le()
    day = reader.read_uint32_le()
    hour = reader.read_uint32_le()
    minute = reader.read_uint32_le()
    second = reader.read_uint32_le()
    millisecond = reader.read_uint32_le()
    weekday = reader.read_uint32_le()
    month = reader.read_uint32_le()
    return MembershipUpdate(
        membership_type,
        CalendarTime(
            year,
            month,
            weekday,
            day,
            hour,
            minute,
            second,
            millisecond,
        ),
        reader.read_bool(),
    )


def write_membership_update_body(
    writer: BitWriter,
    value: MembershipUpdate,
) -> None:
    if value.membership_type not in {
        NO_MEMBERSHIP,
        PREMIUM_MEMBERSHIP,
        DELUXE_MEMBERSHIP,
    }:
        raise ValueError("invalid membership type")
    expires_at = value.expires_at
    if not (
        1 <= expires_at.month <= 12
        and 1 <= expires_at.day <= 31
        and 0 <= expires_at.hour <= 23
        and 0 <= expires_at.minute <= 59
        and 0 <= expires_at.second <= 59
        and 0 <= expires_at.millisecond <= 999
    ):
        raise ValueError("invalid membership CalendarTime")
    writer.write_uint32_le(value.membership_type)
    for number in (
        expires_at.year,
        expires_at.day,
        expires_at.hour,
        expires_at.minute,
        expires_at.second,
        expires_at.millisecond,
        expires_at.weekday,
        expires_at.month,
    ):
        writer.write_uint32_le(number)
    writer.write_bool(value.active)
