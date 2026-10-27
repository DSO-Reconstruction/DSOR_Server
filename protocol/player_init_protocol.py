"""Typed codecs for the retail player-initialization command batch.

The map server sends a large ``0x85`` message containing independently
generated commands separated by the bit-stream marker ``0xff``.  The classes
in this module preserve command boundaries and every captured field so the
batch can be decoded, edited, and serialized without replaying opaque bytes.
"""

from __future__ import annotations

from dso_server.protocol.command_ids import UNAVAILABLE_COMMAND_ID, command_id
from dso_server.protocol.shop_protocol import OFFER_COMMAND, read_offer_command_body, write_offer_command_body
from collections.abc import Mapping
from dataclasses import dataclass, field
import json

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.currency_protocol import (
    GAME_CURRENCY_CHANGED_COMMAND,
    GameCurrencyChanged,
    read_game_currency_changed_body,
    write_game_currency_changed_body,
)
from dso_server.protocol.gameplay_protocol import (
    ACTORS_ENTER_VICINITY_COMMAND,
    MESSAGE_COMMAND,
    NEW_NPC_COMMAND,
    NPC_INFO_COMMAND,
    PROP_INFO_COMMAND,
    STATUS_EFFECT_COMMAND,
    ActorsEnterVicinity,
    Message,
    MessageArgument,
    PropInfo,
    StatusEffect,
    StatusEffectEntry,
    _read_message_argument,
    _read_status_effect_body,
    _read_new_npc_body,
    _read_npc_info_body,
    _read_prop_info_body,
    _write_status_effect_body,
    _write_new_npc_body,
    _write_npc_info_body,
    _write_message_argument,
    _write_prop_info_body,
)
from dso_server.protocol.new_player_codec import (
    NewPlayerWire,
    read_new_player_body,
    write_new_player_body,
)
from dso_server.protocol.inventory_codec import (
    INVENTORY_INFO_COMMAND,
    InventoryInfo,
    ItemInfo,
    decode_item_info,
    encode_item_info,
    read_inventory_info_body,
    write_inventory_info_body,
)
from dso_server.protocol.membership_protocol import (
    MEMBERSHIP_UPDATE_COMMAND,
    MembershipUpdate,
    read_membership_update_body,
    write_membership_update_body,
)


NEW_PLAYER_COMMAND = command_id("Commands::NewPlayerCommand", 0x001D)
PLAYER_READY_COMMAND = command_id("Commands::PlayerReadyCommand", 0x001F)
NOTIFICATION_COMMAND = command_id("Commands::NotificationCommand", 0x0088)
PLAYER_QUERY_RESULT_COMMAND = command_id("Commands::PlayerQueryResultCommand", 0x00B5)
EVENT_PROGRESS_CHANGED_COMMAND = command_id("Commands::EventProgressChangedCommand", 0x00E1)
ACHIEVEMENT_INFO_COMMAND = command_id("Achievements::AchievementInfoCommand", 0x0114)
TALENT_CATEGORY_POINTS_INFO_COMMAND = command_id("Talents::TalentCategoryPointsInfoCommand", 0x011A)
TALENT_SELECTION_INFO_COMMAND = command_id("TalentSelectionInfoCommand", 0x011B)
ITEM_CUSTOMIZATION_INFO_COMMAND = command_id("Commands::ItemCustomizationInfoCommand", 0x0140)
STICKERBOOK_INFO_COMMAND = command_id("Commands::StickerbookInfoCommand", 0x012F)
STICKERBOOK_ITEM_INFO_COMMAND = command_id("Commands::StickerbookItemInfoCommand", 0x0130)
RECIPE_BOOK_INFO_COMMAND = command_id("Commands::RecipeBookInfoCommand", 0x0137)
CURRENCY_CHANGED_COMMAND = command_id("Commands::CurrencyChangedCommand", 0x0080)
PLAYER_LEVEL_UPDATE_COMMAND = command_id("Commands::PlayerLevelUpdateCommand", 0x007C)
XP_CHANGED_COMMAND = command_id("Commands::XPChangedCommand", 0x007D)
SKILL_BOOK_INFO_COMMAND = command_id("SkillBookInfoCommand", 0x0058)
QUICK_SLOTS_INFO_COMMAND = command_id("Commands::QuickSlotsInfoCommand", 0x0050)
QUICKBAR_INFO_COMMAND = command_id("Commands::QuickbarInfoCommand", 0x0052)
CRAFTING_RESULT_COMMAND = command_id("Commands::CraftingResultCommand", 0x005D)
UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND = command_id("Talents::UnlockSkillTalentPreselectionInfoCommand", 0x011D)
QUEST_LOG_INFO_COMMAND = command_id("Commands::QuestLogInfoCommand", 0x008C)
CHAPTER_PROGRESS_COMMAND = command_id("Commands::ChapterProgressCommand", 0x0093)
EVENT_UPDATE_COMMAND = command_id("Commands::EventUpdateCommand", 0x00DD)
DAILY_CHALLENGE_PROGRESS_COMMAND = command_id("Commands::DailyChallengeProgressCommand", 0x0095)
TIMED_CHALLENGES_INFO_COMMAND = command_id("Commands::TimedChallengesInfoCommand", 0x0083)
SPECIAL_OFFER_COMMAND = command_id("SpecialOfferCommand", 0x0084)
TIMED_OFFERS_INFO_COMMAND = command_id("Commands::TimedOffersInfoCommand", 0x0082)
UPDATE_PROGRESS_TIER_SPENDER_CHALLENGES_COMMAND = command_id("Commands::UpdateProgressTierSpenderChallengesCommand", 0x013A)
EVENT_PROGRESS_INFO_COMMAND = command_id("Commands::EventProgressInfoCommand", 0x00E0)
EVENT_PARTICIPATION_INFO_COMMAND = command_id("Commands::EventParticipationInfoCommand", 0x00DF)
VARIATION_INFO_COMMAND = command_id("Commands::VariationInfoCommand", 0x0043)
EXIT_INFO_COMMAND = command_id("Commands::ExitInfoCommand", 0x0044)
# The literals here are the current client's (dro_client64-patch5,
# docs/command_ids.json). release261_192 registers three more commands before
# the Season Pass block, so from there on its ids are three higher
# (KickOutToHubLimitTimeLeftCommand 0x016b, GetWantedThiefInfoResultCommand
# 0x0168): serve it with its own DSO_COMMAND_IDS table.
KICK_OUT_TO_HUB_LIMIT_TIME_LEFT_COMMAND = command_id(
    "Commands::KickOutToHubLimitTimeLeftCommand", 0x0168,
)
# release261_192 registers KillMonsterGetTotalScoresCommand (0x016c there) and
# GetEndlessTowerAllBuffsResultCommand (0x0171); the current client has neither
# (its 0x016c is GetSecondDaySignedAwardCommand), so they are unavailable unless
# a table names them, and the Fractured Planes panel and buff choices are only
# sent when they are (command_available). Never through ``_BODY_WRITERS``,
# whose 0x016c entry emits the award body.
KILL_MONSTER_GET_TOTAL_SCORES_COMMAND = command_id(
    "Commands::KillMonsterGetTotalScoresCommand", UNAVAILABLE_COMMAND_ID,
)
GET_SELF_ENDLESS_TOWER_DATA_COMMAND = command_id(
    "Commands::GetSelfEndlessTowerDataCommand", 0x016B,
)
GET_ENDLESS_TOWER_ALL_BUFFS_RESULT_COMMAND = command_id(
    "Commands::GetEndlessTowerAllBuffsResultCommand", UNAVAILABLE_COMMAND_ID,
)


def command_available(command: int) -> bool:
    """Whether the client build being served defines this command."""

    return command != UNAVAILABLE_COMMAND_ID
ACTIVITY_SIGNIN_CALENDAR_COMMAND = 0x014B
ACTIVITY_EXTRA_RECORD_COMMAND = 0x014A
USER_HATCH_EGGS_INFO_COMMAND = command_id("Commands::UserHatchEggsInfoCommand", 0x015B)
SEASON_PASS_REWARDS_INFO_RESULT_COMMAND = command_id("Commands::GetSeasonPassRewardsInfoResultCommand", 0x015F)
SEASON_PASS_MISSIONS_INFO_RESULT_COMMAND = command_id("Commands::GetSeasonPassMissionsInfoResultCommand", 0x0163)
GET_SECOND_DAY_SIGNED_AWARD_COMMAND = command_id("Commands::GetSecondDaySignedAwardCommand", 0x016C)
OBSTACLE_UPDATE_COMMAND = command_id("Commands::ObstacleUpdateCommand", 0x0072)
COMMAND_0173 = 0x0173

# The 2026-09-09 retail capture contains both commands, but the exact local
# dro_client64.exe used by this project cannot accept either in a server batch.
# 0x0173 has no generated registration.  0x016c is a client request type and
# produces ``invalid command ending in multi command 364`` when sent by the
# server.  Keep both codecs so captures remain fully reversible, but omit them
# from packets targeting this client build.
#
# 0x015b reproduces the same 0x016c symptom on this build (``invalid command
# ending in multi command 347``). Persisted incubations therefore travel in
# their own batch after PlayerReady (services/seasonal_activities.py), which
# cannot take the initialization batch down with it. This client
# build's 0x0083 decoder expects four maps: daily, bonus, tiered spender, and
# comeback daily. The older checked-in capture has only three, so the reader
# accepts that legacy form while the writer emits the current four-map form.
LOCAL_CLIENT_UNSUPPORTED_INIT_COMMANDS = frozenset({
    GET_SECOND_DAY_SIGNED_AWARD_COMMAND,
    COMMAND_0173,
    USER_HATCH_EGGS_INFO_COMMAND,
})


@dataclass(frozen=True)
class ServerCommand:
    command_id: int
    value: object
    actor_id: int | None


@dataclass(frozen=True)
class KickOutToHubLimitTimeLeft:
    """Native floor timer; zero selects the panel's Level completed phase.

    PlanarFissureInfoWidget::SetTimeLeft (0x1405b7f2c) calls the phase-three
    setter on zero; it is also the boss-clear notification for this panel.
    """

    seconds: int


def _read_kick_out_to_hub_limit_time_left_body(
    reader: BitReader,
) -> KickOutToHubLimitTimeLeft:
    return KickOutToHubLimitTimeLeft(reader.read_uint32_le())


def _write_kick_out_to_hub_limit_time_left_body(
    writer: BitWriter, value: KickOutToHubLimitTimeLeft,
) -> None:
    writer.write_uint32_le(max(0, int(value.seconds)))


def encode_kill_monster_get_total_scores(
    score: int, *, actor_id: int,
) -> tuple[bytes, int]:
    """Push the running Endless Tower / Fractured Planes kill score.

    ``Commands::KillMonsterGetTotalScoresCommand``'s decoder (0x14097a09c)
    reads one u32 into ``[command + 0x20]``, and the inherited-actor slot every
    Endless Tower command shares (vtable +0x48, 0x140a2297c) then reads a
    trailing u32 into ``[command + 0x18]``.  Both fields are mandatory: the
    earlier one-field shape let the actor reader swallow the ``0xff``
    separator, which is what the client logged as
    ``could not decode command's additional server data (ID: '364')``.

    The handler (0x1401cf938) forwards ``[+0x20]`` to
    ``UI::DrasaUIManager`` (0x14040f268), which lazily loads and shows
    PlanarFissureInfoWidget and then drives its bar and ``%0.f%%`` label
    (0x1405b7fd4/0x1405b8080).  Showing the widget is also what stamps the
    panel's phase-one state and score target, so the first command both
    activates the panel and replaces its authored ``Text`` placeholder.
    """

    if type(score) is not int or not 0 <= score <= 0x7FFFFFFF:
        raise ValueError("kill score must be a non-negative int32")
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(
        KILL_MONSTER_GET_TOTAL_SCORES_COMMAND.to_bytes(2, "little")
    )
    writer.write_uint32_le(score)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_endless_tower_buffs(
    resources: tuple[str, ...], *, actor_id: int,
) -> tuple[bytes, int]:
    """Native 0x171: u32 count, u16-length StringAtom entries, actor ID.

    Verified in rel261_192: vtable 0x14117c310, decoder 0x140979484,
    StringAtom-array reader 0x14095dc2c. The portal displays the first
    ActNumber entries from ClientActivityManager +0xc0 (0x1405b2c08).
    """

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(GET_ENDLESS_TOWER_ALL_BUFFS_RESULT_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(len(resources))
    for resource in resources:
        writer.write_string(resource)
    writer.write_uint32_le(actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def encode_endless_tower_progress(
    cleared_floor: int, claimed_floor: int, *, actor_id: int,
) -> tuple[bytes, int]:
    """Refresh the native Fractured Planes floor selection.

    GetSelfEndlessTowerDataCommand reads three u32s (0x140979364):
    highest cleared floor, ranking score, highest rewarded floor. Its handler
    stores them in ClientRankingManager +0x1a0/+0x1a4/+0x1a8. The map-access
    widget selects cleared_floor + 1 (0x1405b5be8). A trailing actor is read
    by the inherited decoder, just like KillMonsterGetTotalScoresCommand.
    No leaderboard score is published by this local progression update.
    """

    if not 0 <= claimed_floor <= cleared_floor <= 100:
        raise ValueError("invalid Endless Tower floor progress")
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(GET_SELF_ENDLESS_TOWER_DATA_COMMAND.to_bytes(2, "little"))
    for value in (cleared_floor, 0, claimed_floor, actor_id):
        writer.write_uint32_le(value)
    writer.write_uint8(0xFF)
    return writer.finish()


@dataclass(frozen=True)
class CraftingResult:
    """Typed body of generated ``CraftingResultCommand`` (0x005d)."""

    status: int
    items: tuple[ItemInfo, ...]
    result_type: int = 3
    item_relations: tuple[tuple[int, int], ...] = ()
    field_70: int = 0


@dataclass(frozen=True)
class PlayerQueryResult:
    query_id: int
    results: tuple[object, ...]


@dataclass(frozen=True)
class Achievement:
    resource_id: str
    values: tuple[int, ...]


@dataclass(frozen=True)
class AchievementInfo:
    achievements: tuple[Achievement, ...]


@dataclass(frozen=True)
class NotificationEntry:
    kind: int
    instance_guid: bytes
    values: tuple[int, int, int]
    payload: str
    timestamp: str


@dataclass(frozen=True)
class Notification:
    notification_type: int
    entries: tuple[NotificationEntry, ...]


def make_notification_entry(
    kind: int,
    instance_guid: bytes,
    values: tuple[int, int, int],
    payload: Mapping[str, object] | str,
    timestamp: str = "0000-00-00 00:00:00",
) -> NotificationEntry:
    """Build one retail-shaped notification entry.

    Daily rewards and live social invitations deliberately share this helper
    so their GUID, three-value tuple, JSON payload, and timestamp are encoded
    through the same proven ``NotificationCommand`` path.
    """

    guid = bytes(instance_guid)
    if len(guid) != 16:
        raise ValueError("notification instance GUID must contain 16 bytes")
    if len(values) != 3:
        raise ValueError("notification values must contain three integers")
    if isinstance(payload, str):
        encoded_payload = payload
    else:
        encoded_payload = json.dumps(payload, separators=(",", ":"))
    return NotificationEntry(
        int(kind),
        guid,
        tuple(int(value) & 0xFFFFFFFF for value in values),
        encoded_payload,
        str(timestamp),
    )


def _write_crafting_result_body(
    writer: BitWriter, value: CraftingResult,
) -> None:
    writer.write_uint8(int(value.status) & 0xFF)
    writer.write_uint32_le(len(value.items))
    for item in value.items:
        encode_item_info(writer, item)
    writer.write_uint8(int(value.result_type) & 0xFF)
    writer.write_uint32_le(len(value.item_relations))
    for first, second in value.item_relations:
        writer.write_uint32_le(first)
        writer.write_uint32_le(second)
    writer.write_uint32_le(value.field_70)


def _read_crafting_result_body(reader: BitReader) -> CraftingResult:
    status = reader.read_uint8()
    item_count = reader.read_uint32_le()
    if item_count > 1024:
        raise ValueError("CraftingResult contains too many result items")
    items = tuple(decode_item_info(reader) for _ in range(item_count))
    result_type = reader.read_uint8()
    relation_count = reader.read_uint32_le()
    if relation_count > 1024:
        raise ValueError("CraftingResult contains too many item relations")
    item_relations = tuple(
        (reader.read_uint32_le(), reader.read_uint32_le())
        for _ in range(relation_count)
    )
    return CraftingResult(
        status,
        items,
        result_type,
        item_relations,
        reader.read_uint32_le(),
    )


@dataclass(frozen=True)
class Command0173:
    """Wire-complete command absent from the older generated type catalog."""

    field_20: int
    field_24: int


@dataclass(frozen=True)
class EventProgressChanged:
    values: tuple[int, int, int, int, int]


# ``TalentCategoryPointsInfo`` does not order its four progression values the
# way ``TalentSelectionInfo`` orders its category array. Wire slot ``i`` holds
# the progression of category ``TALENT_CATEGORY_POINTS_WIRE_ORDER[i]``, so the
# wire runs Skill, Fame, Knowledge, Group.
#
# The 2026-09-09 retail capture pins this down. It belongs to a fully
# progressed character, and this is the only assignment of its three category
# values that puts each one at or above its own ``_Template_TalentXPLevel``
# maximum: 22317796 against Fame's 15617525, 4351515 against Knowledge's
# 3622960, and 1924958 against Group's 1924643. Reading the array in category
# order instead makes the client show its Group progression as Knowledge.
#
# ``values`` stays in category order everywhere in the server; the codec
# permutes on the way in and out.
TALENT_CATEGORY_POINTS_WIRE_ORDER = (0, 3, 1, 2)
TALENT_CATEGORY_POINTS_ALL = 0xFF


@dataclass(frozen=True)
class TalentCategoryPointsInfo:
    category: int
    values: tuple[int, int, int, int]


@dataclass(frozen=True)
class TalentSelectionEntry:
    field_00: int
    field_04: int
    resource_id: str


@dataclass(frozen=True)
class TalentCategorySelection:
    # The containing array has four fixed entries whose indexes are the
    # TalentCategory IDs.  This byte is the selected preset/preselection for
    # that implicit category; it is not another category ID.
    preselection: int
    selected_skills: tuple[TalentSelectionEntry, ...]
    selected_talents: tuple[TalentSelectionEntry, ...]


@dataclass(frozen=True)
class TalentSelectionInfo:
    """Complete snapshot (branch 0) or skill-category refresh (branch 3)."""

    selection_type: int
    categories: tuple[TalentCategorySelection, ...] = ()
    preselection: int | None = None
    selections: tuple[TalentSelectionEntry, ...] = ()


@dataclass(frozen=True)
class ItemCustomization:
    item_id: str
    skin_id: str | None


@dataclass(frozen=True)
class ItemCustomizationInfo:
    customizations: tuple[ItemCustomization, ...]


@dataclass(frozen=True)
class StickerbookEntry:
    field_14: int
    field_18: int
    resource_id: str


@dataclass(frozen=True)
class StickerbookInfo:
    field_20: int
    entries: tuple[StickerbookEntry | None, ...]


@dataclass(frozen=True)
class StickerbookItemInfo:
    operation_type: int
    entry: StickerbookEntry | None
    related_resource_id: str = ""


@dataclass(frozen=True)
class RecipeBookEntry:
    recipe_id: str
    value: str | None


@dataclass(frozen=True)
class RecipeBookInfo:
    recipes: tuple[RecipeBookEntry, ...]


@dataclass(frozen=True)
class CurrencyChanged:
    """Fixed legacy currency snapshot used by command 0x0080.

    The generated wire fields predate the keyed game-currency wallet.  Keep
    their positional representation for compatibility, while exposing the
    balances through their verified names so application code does not
    confuse Badge of Honor, Draken, and Glyphs of Power.
    """

    currency_type: int
    amount: int
    values: tuple[int, int, int]
    delta: int
    notify: bool
    wallet_base: int | None = field(default=None, compare=False, repr=False)

    @property
    def andermant(self) -> int:
        return self.currency_type

    @property
    def gold(self) -> int:
        return self.amount

    @property
    def badge_of_honor(self) -> int:
        return self.values[0]

    @property
    def legacy_progression(self) -> int:
        return self.values[1]

    @property
    def draken(self) -> int:
        return self.values[2]

    @property
    def glyph_of_power(self) -> int:
        return self.delta


@dataclass(frozen=True)
class PlayerLevelUpdate:
    """Fields consumed by ClientGameWorld::HandlePlayerLevelUpdateCommand.

    ``level`` is copied directly to ``GamePlayer + 0x68``.  The remaining
    values refresh the four adjacent progression/title fields and mirror the
    zero-valued local NewPlayer state.
    """

    level: int
    field_24: int
    field_28: int
    field_30: int
    field_34: int


@dataclass(frozen=True)
class XPChanged:
    '''One normal-character XP snapshot consumed by the live client.

    The command replaces the complete XP bar state. ``level_up`` controls
    the client-side level-up transition and attribute refresh.
    '''

    xp: int
    level: int
    level_base_xp: int
    next_level_base_xp: int
    level_up: bool


@dataclass(frozen=True)
class SkillBookEntry:
    """One SkillBookInfo entry in generated wire order.

    ``flags`` is ``(unlocked, hidden, new)``.  The generated client serializer
    writes the underlying C++ entry bytes in the non-struct order [1], [2],
    [0], so treating this tuple as the in-memory order silently swaps the
    meaning of every flag.
    """

    skill_id: int
    flags: tuple[bool, bool, bool]


@dataclass(frozen=True)
class SkillBookInfo:
    skills: tuple[SkillBookEntry, ...]


@dataclass(frozen=True)
class QuickSlotInfo:
    kind: int
    resource_id: str | None


@dataclass(frozen=True)
class QuickSlotsInfo:
    quickbars: tuple[tuple[QuickSlotInfo, ...], ...]


@dataclass(frozen=True)
class QuickbarInfo:
    quickbar_amount: int
    locked_quickbars: int


@dataclass(frozen=True)
class UnlockSkillTalentPreselectionInfo:
    unlocked_count: int


@dataclass(frozen=True)
class QuestLogEntry:
    quest_id: str
    field_08: int
    state: int
    field_10: int
    progress: tuple[int, ...]
    field_28: int
    tracking_id: str


@dataclass(frozen=True)
class QuestLogInfo:
    quests: tuple[QuestLogEntry, ...]


@dataclass(frozen=True)
class ChapterProgressEntry:
    chapter_id: str
    field_08: int
    field_0a: int


@dataclass(frozen=True)
class ChapterProgress:
    active: tuple[ChapterProgressEntry, ...]
    completed: tuple[ChapterProgressEntry, ...]


@dataclass(frozen=True)
class EventUpdateEntry:
    event_id: int
    event_name: str
    flags: tuple[bool, bool, bool, bool]
    starts_at: tuple[int, int, int, int, int, int] | None
    ends_at: tuple[int, int, int, int, int, int] | None
    tags: tuple[str, ...]


@dataclass(frozen=True)
class EventUpdate:
    events: tuple[EventUpdateEntry, ...]


@dataclass(frozen=True)
class TimedChallenge:
    challenge_id: str
    schedule_id: str
    state: int
    progress: int
    timestamp: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class TimedChallengesInfo:
    """Timed-challenge maps, in native wire order.

    weekly/monthly are legacy field names; these are not calendar periods.
    comeback_daily is the fourth map required by the installed client.
    """

    server_time: int
    daily: tuple[TimedChallenge, ...]
    weekly: tuple[TimedChallenge, ...]
    monthly: tuple[TimedChallenge, ...]
    # None preserves the older captured three-map form on decode/encode.
    # Server-generated messages must pass an explicit tuple, even when empty.
    comeback_daily: tuple[TimedChallenge, ...] | None = None


@dataclass(frozen=True)
class OfferStringFloat:
    resource_id: str
    value: float


@dataclass(frozen=True)
class OfferDetails:
    resource_id: str
    field_58: int
    field_5c: int
    field_50: str
    field_74: int
    field_78: int
    field_94: int
    field_98: int
    values_168: tuple[OfferStringFloat, ...]
    values_190: tuple[OfferStringFloat, ...]
    field_c0: str
    field_c8: str
    field_80: int
    field_84: int
    field_88: bool
    field_8b: bool


@dataclass(frozen=True)
class OfferGroupEntry:
    offer: OfferDetails
    amount: int


@dataclass(frozen=True)
class OfferGroup:
    resource_id: str
    field_90: int
    offers: tuple[OfferGroupEntry, ...]


@dataclass(frozen=True)
class SpecialOffer:
    primary: OfferDetails | None
    secondary: OfferDetails | None
    featured: OfferGroup | None
    field_c0: bool
    field_c4: str
    groups_40: tuple[OfferGroup, ...]
    groups_60: tuple[OfferGroup, ...]
    groups_80: tuple[OfferGroup, ...]
    groups_a0: tuple[OfferGroup, ...]


@dataclass(frozen=True)
class TimedOffer:
    offer_id: str
    field_08: int
    field_0c: int
    timestamp: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class TimedOffersInfo:
    server_time: int
    offers: tuple[TimedOffer, ...]


@dataclass(frozen=True)
class UpdateProgressTierSpenderChallenges:
    """Premium-currency spend credited to the spender challenge manager."""

    amount: int


@dataclass(frozen=True)
class EventProgressInfoEntry:
    event_id: int
    values: tuple[int, int, int, int]


@dataclass(frozen=True)
class EventProgressInfo:
    events: tuple[EventProgressInfoEntry, ...]


@dataclass(frozen=True)
class EventParticipation:
    event_id: int
    participating: bool


@dataclass(frozen=True)
class EventParticipationInfo:
    events: tuple[EventParticipation, ...]


@dataclass(frozen=True)
class VariationInfoEntry:
    instance_guid: bytes
    event_set_row: int = -1


@dataclass(frozen=True)
class VariationInfo:
    entries: tuple[VariationInfoEntry, ...]


@dataclass(frozen=True)
class ExitInfoEntry:
    exit_id: str
    map_id: str
    field_10: int
    instance_guid: bytes


@dataclass(frozen=True)
class ExitInfo:
    exits: tuple[ExitInfoEntry, ...]


@dataclass(frozen=True)
class ActivitySigninDay:
    values: tuple[int, int, int, int]


@dataclass(frozen=True)
class ActivitySigninCalendar:
    days: tuple[ActivitySigninDay, ...]


@dataclass(frozen=True)
class ActivityExtraRecordEntry:
    record_id: int
    resource_id: str


@dataclass(frozen=True)
class ActivityExtraRecord:
    records: tuple[ActivityExtraRecordEntry, ...]


@dataclass(frozen=True)
class HatchEggRecord:
    egg_id: int
    values: tuple[int, int, int, int, int, int, int, int]


@dataclass(frozen=True)
class UserHatchEggsInfo:
    counters: tuple[tuple[int, int], ...]
    eggs: tuple[HatchEggRecord, ...]


@dataclass(frozen=True)
class SeasonPassRewardsInfoResult:
    field_20: int
    field_24: int
    pass_types: tuple[int, ...]
    claimed_awards: tuple[int, ...]


@dataclass(frozen=True)
class SeasonPassMissionsInfoResult:
    missions: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Fieldless:
    pass


@dataclass(frozen=True)
class SecondDaySignedAwardRequest:
    """Three zero-valued request context words present in the retail wire."""

    values: tuple[int, int, int]


@dataclass(frozen=True)
class ObstacleState:
    obstacle_id: str
    state: int


@dataclass(frozen=True)
class ObstacleUpdate:
    obstacles: tuple[ObstacleState, ...]


def _read_blob16(reader: BitReader) -> bytes:
    return reader.read_bytes(int.from_bytes(reader.read_bytes(2), "little"))


def _write_blob16(writer: BitWriter, value: bytes) -> None:
    if len(value) > 0xFFFF:
        raise ValueError("binary value exceeds the uint16 wire length")
    writer.write_bytes(len(value).to_bytes(2, "little"))
    writer.write_bytes(value)


def _read_message_body(reader: BitReader) -> Message:
    message_type = reader.read_uint8()
    arguments = [
        _read_message_argument(reader)
        for _ in range(reader.read_uint32_le())
    ]
    return Message(message_type, tuple(arguments))


def _write_message_body(writer: BitWriter, value: Message) -> None:
    writer.write_uint8(value.message_type)
    writer.write_uint32_le(len(value.arguments))
    for argument in value.arguments:
        _write_message_argument(writer, argument)


def _read_player_query_result_body(reader: BitReader) -> PlayerQueryResult:
    query_id = reader.read_uint32_le()
    count = reader.read_uint32_le()
    if count:
        raise ValueError("non-empty PlayerQueryResult branch is not yet modeled")
    return PlayerQueryResult(query_id, ())


def _write_player_query_result_body(
    writer: BitWriter, value: PlayerQueryResult
) -> None:
    if value.results:
        raise ValueError("non-empty PlayerQueryResult branch is not yet modeled")
    writer.write_uint32_le(value.query_id)
    writer.write_uint32_le(0)


def _read_achievement_info_body(reader: BitReader) -> AchievementInfo:
    achievements = []
    for _ in range(reader.read_uint32_le()):
        achievements.append(
            Achievement(
                reader.read_string(),
                tuple(reader.read_uint32_le() for _ in range(9)),
            )
        )
    return AchievementInfo(tuple(achievements))


def _write_achievement_info_body(
    writer: BitWriter, value: AchievementInfo
) -> None:
    writer.write_uint32_le(len(value.achievements))
    for achievement in value.achievements:
        if len(achievement.values) != 9:
            raise ValueError("Achievement requires nine uint32 values")
        writer.write_string(achievement.resource_id)
        for number in achievement.values:
            writer.write_uint32_le(number)


def _read_notification_body(reader: BitReader) -> Notification:
    notification_type = reader.read_uint8()
    if notification_type != 1:
        raise ValueError(
            f"captured Notification union branch {notification_type} is not modeled"
        )
    entries = []
    for _ in range(int.from_bytes(reader.read_bytes(2), "little")):
        entries.append(
            NotificationEntry(
                reader.read_uint8(),
                _read_blob16(reader),
                tuple(reader.read_uint32_le() for _ in range(3)),
                reader.read_string(),
                reader.read_string(),
            )
        )
    return Notification(notification_type, tuple(entries))


def _write_notification_body(writer: BitWriter, value: Notification) -> None:
    if value.notification_type != 1:
        raise ValueError(
            f"captured Notification union branch {value.notification_type} is not modeled"
        )
    if len(value.entries) > 0xFFFF:
        raise ValueError("Notification contains too many entries")
    writer.write_uint8(value.notification_type)
    writer.write_bytes(len(value.entries).to_bytes(2, "little"))
    for entry in value.entries:
        if len(entry.values) != 3:
            raise ValueError("NotificationEntry requires three uint32 values")
        writer.write_uint8(entry.kind)
        _write_blob16(writer, entry.instance_guid)
        for number in entry.values:
            writer.write_uint32_le(number)
        writer.write_string(entry.payload)
        writer.write_string(entry.timestamp)


def _read_command_0173_body(reader: BitReader) -> Command0173:
    return Command0173(reader.read_uint32_le(), reader.read_uint32_le())


def _write_command_0173_body(writer: BitWriter, value: Command0173) -> None:
    writer.write_uint32_le(value.field_20)
    writer.write_uint32_le(value.field_24)


def _read_event_progress_changed_body(
    reader: BitReader,
) -> EventProgressChanged:
    return EventProgressChanged(
        tuple(reader.read_uint32_le() for _ in range(5))
    )


def _write_event_progress_changed_body(
    writer: BitWriter, value: EventProgressChanged
) -> None:
    if len(value.values) != 5:
        raise ValueError("EventProgressChanged requires five uint32 values")
    for number in value.values:
        writer.write_uint32_le(number)


def _talent_points_category_to_wire(category: int) -> int:
    """Map a category id onto its slot in the progression array."""

    category = int(category)
    if category == TALENT_CATEGORY_POINTS_ALL:
        return TALENT_CATEGORY_POINTS_ALL
    try:
        return TALENT_CATEGORY_POINTS_WIRE_ORDER.index(category)
    except ValueError:
        raise ValueError(
            f"talent category {category} is outside [0, 4)"
        ) from None


def _talent_points_category_from_wire(category: int) -> int:
    category = int(category)
    if category == TALENT_CATEGORY_POINTS_ALL:
        return TALENT_CATEGORY_POINTS_ALL
    if not 0 <= category < len(TALENT_CATEGORY_POINTS_WIRE_ORDER):
        raise ValueError(f"talent category slot {category} is outside [0, 4)")
    return TALENT_CATEGORY_POINTS_WIRE_ORDER[category]


def _read_talent_category_points_info_body(
    reader: BitReader,
) -> TalentCategoryPointsInfo:
    category = _talent_points_category_from_wire(reader.read_uint8())
    wire = tuple(reader.read_uint32_le() for _ in range(4))
    values = [0, 0, 0, 0]
    for slot, category_id in enumerate(TALENT_CATEGORY_POINTS_WIRE_ORDER):
        values[category_id] = wire[slot]
    return TalentCategoryPointsInfo(category, tuple(values))


def _write_talent_category_points_info_body(
    writer: BitWriter, value: TalentCategoryPointsInfo
) -> None:
    if len(value.values) != 4:
        raise ValueError("TalentCategoryPointsInfo requires four uint32 values")
    writer.write_uint8(_talent_points_category_to_wire(value.category))
    for category_id in TALENT_CATEGORY_POINTS_WIRE_ORDER:
        writer.write_uint32_le(value.values[category_id])


def _read_talent_selection_info_body(reader: BitReader) -> TalentSelectionInfo:
    selection_type = reader.read_uint8()
    def read_entry() -> TalentSelectionEntry:
        return TalentSelectionEntry(
            int.from_bytes(reader.read_bytes(2), "little"),
            int.from_bytes(reader.read_bytes(2), "little"),
            reader.read_string(),
        )
    if selection_type == 3:
        preselection = reader.read_uint8()
        selections = tuple(
            read_entry() for _ in range(reader.read_uint8())
        )
        return TalentSelectionInfo(
            selection_type,
            preselection=preselection,
            selections=selections,
        )
    if selection_type != 0:
        raise ValueError(
            f"captured TalentSelectionInfo union branch {selection_type} is not modeled"
        )
    categories = []
    for _ in range(4):
        preselection = reader.read_uint8()
        skills = tuple(read_entry() for _ in range(reader.read_uint8()))
        talents = tuple(read_entry() for _ in range(reader.read_uint8()))
        categories.append(
            TalentCategorySelection(preselection, skills, talents)
        )
    return TalentSelectionInfo(selection_type, tuple(categories))


def _write_talent_selection_info_body(
    writer: BitWriter, value: TalentSelectionInfo
) -> None:
    def write_entry(entry: TalentSelectionEntry) -> None:
        if not 0 <= int(entry.field_00) <= 0xFFFF:
            raise ValueError("talent selection rank must fit uint16")
        if not 0 <= int(entry.field_04) <= 0xFFFF:
            raise ValueError("talent selection field_04 must fit uint16")
        if not entry.resource_id:
            raise ValueError("talent selection resource id must not be empty")
        writer.write_bytes(int(entry.field_00).to_bytes(2, "little"))
        writer.write_bytes(int(entry.field_04).to_bytes(2, "little"))
        writer.write_string(entry.resource_id)
    if value.selection_type == 3:
        if value.categories:
            raise ValueError(
                "TalentSelectionInfo skill refresh cannot contain categories"
            )
        if value.preselection is None or not 0 <= value.preselection < 8:
            raise ValueError(
                "skill talent preselection must be inside [0, 8)"
            )
        if len(value.selections) > 0xFF:
            raise ValueError("too many skill talent selections")
        writer.write_uint8(value.selection_type)
        writer.write_uint8(value.preselection)
        writer.write_uint8(len(value.selections))
        for entry in value.selections:
            write_entry(entry)
        return
    if value.selection_type != 0 or len(value.categories) != 4:
        raise ValueError("TalentSelectionInfo snapshot requires four categories")
    writer.write_uint8(value.selection_type)
    # PlayerTalentModule::GetMaxPresets(category), recovered from the client.
    max_presets = (8, 4, 4, 1)
    for category_id, category in enumerate(value.categories):
        if not 0 <= category.preselection < max_presets[category_id]:
            raise ValueError(
                f"talent category {category_id} preselection "
                f"{category.preselection} is outside "
                f"[0, {max_presets[category_id]})"
            )
        if len(category.selected_skills) > 0xFF:
            raise ValueError("too many selected skills")
        if len(category.selected_talents) > 0xFF:
            raise ValueError("too many selected talents")
        writer.write_uint8(category.preselection)
        writer.write_uint8(len(category.selected_skills))
        for entry in category.selected_skills:
            write_entry(entry)
        writer.write_uint8(len(category.selected_talents))
        for entry in category.selected_talents:
            write_entry(entry)


def _read_item_customization_info_body(
    reader: BitReader,
) -> ItemCustomizationInfo:
    values = []
    for _ in range(reader.read_uint32_le()):
        item_id = reader.read_string()
        skin_id = reader.read_string() if reader.read_bool() else None
        values.append(ItemCustomization(item_id, skin_id))
    return ItemCustomizationInfo(tuple(values))


def _write_item_customization_info_body(
    writer: BitWriter, value: ItemCustomizationInfo
) -> None:
    writer.write_uint32_le(len(value.customizations))
    for customization in value.customizations:
        writer.write_string(customization.item_id)
        writer.write_bool(customization.skin_id is not None)
        if customization.skin_id is not None:
            writer.write_string(customization.skin_id)


def _read_stickerbook_info_body(reader: BitReader) -> StickerbookInfo:
    field_20 = reader.read_uint32_le()
    entries = []
    for _ in range(reader.read_uint32_le()):
        entries.append(
            StickerbookEntry(
                reader.read_uint32_le(),
                reader.read_uint32_le(),
                reader.read_string(),
            )
            if reader.read_bool()
            else None
        )
    return StickerbookInfo(field_20, tuple(entries))


def _write_stickerbook_info_body(writer: BitWriter, value: StickerbookInfo) -> None:
    writer.write_uint32_le(value.field_20)
    writer.write_uint32_le(len(value.entries))
    for entry in value.entries:
        writer.write_bool(entry is not None)
        if entry is not None:
            writer.write_uint32_le(entry.field_14)
            writer.write_uint32_le(entry.field_18)
            writer.write_string(entry.resource_id)


def _read_stickerbook_item_info_body(
    reader: BitReader,
) -> StickerbookItemInfo:
    operation_type = reader.read_uint32_le()
    entry = None
    if reader.read_bool():
        entry = StickerbookEntry(
            reader.read_uint32_le(),
            reader.read_uint32_le(),
            reader.read_string(),
        )
    return StickerbookItemInfo(
        operation_type,
        entry,
        reader.read_string(),
    )


def _write_stickerbook_item_info_body(
    writer: BitWriter, value: StickerbookItemInfo
) -> None:
    writer.write_uint32_le(value.operation_type)
    writer.write_bool(value.entry is not None)
    if value.entry is not None:
        writer.write_uint32_le(value.entry.field_14)
        writer.write_uint32_le(value.entry.field_18)
        writer.write_string(value.entry.resource_id)
    # Generated StickerbookItemInfo owns a second Util::String at +0x30.
    # It is empty for operation 1 (unlock), but is still serialized. Omitting
    # its uint16 length shifts the following actor id by 16 bits and makes the
    # client display the entry while keeping it unusable.
    writer.write_string(value.related_resource_id)


def _read_recipe_book_info_body(reader: BitReader) -> RecipeBookInfo:
    recipes = []
    for _ in range(reader.read_uint32_le()):
        recipe_id = reader.read_string()
        value = reader.read_string() if reader.read_bool() else None
        recipes.append(RecipeBookEntry(recipe_id, value))
    return RecipeBookInfo(tuple(recipes))


def _write_recipe_book_info_body(writer: BitWriter, value: RecipeBookInfo) -> None:
    writer.write_uint32_le(len(value.recipes))
    for recipe in value.recipes:
        writer.write_string(recipe.recipe_id)
        writer.write_bool(recipe.value is not None)
        if recipe.value is not None:
            writer.write_string(recipe.value)


def _read_currency_changed_body(reader: BitReader) -> CurrencyChanged:
    return CurrencyChanged(
        reader.read_uint32_le(),
        reader.read_uint64_le(),
        tuple(reader.read_uint32_le() for _ in range(3)),
        reader.read_uint64_le(),
        reader.read_bool(),
    )


def _write_currency_changed_body(writer: BitWriter, value: CurrencyChanged) -> None:
    if len(value.values) != 3:
        raise ValueError("CurrencyChanged requires three uint32 values")
    writer.write_uint32_le(value.currency_type)
    writer.write_uint64_le(value.amount)
    for number in value.values:
        writer.write_uint32_le(number)
    writer.write_uint64_le(value.delta)
    writer.write_bool(value.notify)


def _read_player_level_update_body(reader: BitReader) -> PlayerLevelUpdate:
    return PlayerLevelUpdate(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint64_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
    )


def _write_player_level_update_body(
    writer: BitWriter, value: PlayerLevelUpdate
) -> None:
    writer.write_uint32_le(value.level)
    writer.write_uint32_le(value.field_24)
    writer.write_uint64_le(value.field_28)
    writer.write_uint32_le(value.field_30)
    writer.write_uint32_le(value.field_34)


def _read_xp_changed_body(reader: BitReader) -> XPChanged:
    return XPChanged(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_bool(),
    )


def _write_xp_changed_body(writer: BitWriter, value: XPChanged) -> None:
    writer.write_uint32_le(value.xp)
    writer.write_uint32_le(value.level)
    writer.write_uint32_le(value.level_base_xp)
    writer.write_uint32_le(value.next_level_base_xp)
    writer.write_bool(value.level_up)


def _read_skill_book_info_body(reader: BitReader) -> SkillBookInfo:
    return SkillBookInfo(
        tuple(
            SkillBookEntry(
                reader.read_uint32_le(),
                tuple(reader.read_bool() for _ in range(3)),
            )
            for _ in range(reader.read_uint8())
        )
    )


def _write_skill_book_info_body(writer: BitWriter, value: SkillBookInfo) -> None:
    if len(value.skills) >= 0xFF:
        raise ValueError("SkillBookInfo supports at most 254 skills")
    writer.write_uint8(len(value.skills))
    for skill in value.skills:
        if len(skill.flags) != 3:
            raise ValueError("SkillBookEntry requires three flags")
        writer.write_uint32_le(skill.skill_id)
        for flag in skill.flags:
            writer.write_bool(flag)


def _read_quick_slots_info_body(reader: BitReader) -> QuickSlotsInfo:
    quickbars = []
    for _ in range(reader.read_uint32_le()):
        slots = []
        for _ in range(reader.read_uint32_le()):
            kind = reader.read_uint32_le()
            slots.append(
                QuickSlotInfo(
                    kind,
                    None if kind == 0xFFFFFFFF else reader.read_string(),
                )
            )
        quickbars.append(tuple(slots))
    return QuickSlotsInfo(tuple(quickbars))


def _write_quick_slots_info_body(writer: BitWriter, value: QuickSlotsInfo) -> None:
    writer.write_uint32_le(len(value.quickbars))
    for quickbar in value.quickbars:
        writer.write_uint32_le(len(quickbar))
        for slot in quickbar:
            writer.write_uint32_le(slot.kind)
            if slot.kind == 0xFFFFFFFF:
                if slot.resource_id is not None:
                    raise ValueError("empty QuickSlotInfo cannot contain a resource")
            else:
                if slot.resource_id is None:
                    raise ValueError("populated QuickSlotInfo needs a resource")
                writer.write_string(slot.resource_id)


def _read_quickbar_info_body(reader: BitReader) -> QuickbarInfo:
    return QuickbarInfo(reader.read_uint32_le(), reader.read_uint32_le())


def _write_quickbar_info_body(writer: BitWriter, value: QuickbarInfo) -> None:
    writer.write_uint32_le(value.quickbar_amount)
    writer.write_uint32_le(value.locked_quickbars)


def _read_unlock_skill_talent_preselection_info_body(
    reader: BitReader,
) -> UnlockSkillTalentPreselectionInfo:
    return UnlockSkillTalentPreselectionInfo(reader.read_uint32_le())


def _write_unlock_skill_talent_preselection_info_body(
    writer: BitWriter, value: UnlockSkillTalentPreselectionInfo
) -> None:
    writer.write_uint32_le(value.unlocked_count)


def _read_quest_log_info_body(reader: BitReader) -> QuestLogInfo:
    quests = []
    for _ in range(reader.read_uint32_le()):
        quests.append(
            QuestLogEntry(
                reader.read_string(),
                reader.read_uint32_le(),
                reader.read_uint8(),
                int.from_bytes(reader.read_bytes(2), "little"),
                tuple(
                    reader.read_uint32_le()
                    for _ in range(reader.read_uint32_le())
                ),
                reader.read_uint32_le(),
                reader.read_string(),
            )
        )
    return QuestLogInfo(tuple(quests))


def _write_quest_log_info_body(writer: BitWriter, value: QuestLogInfo) -> None:
    writer.write_uint32_le(len(value.quests))
    for quest in value.quests:
        writer.write_string(quest.quest_id)
        writer.write_uint32_le(quest.field_08)
        writer.write_uint8(quest.state)
        writer.write_bytes(int(quest.field_10).to_bytes(2, "little"))
        writer.write_uint32_le(len(quest.progress))
        for number in quest.progress:
            writer.write_uint32_le(number)
        writer.write_uint32_le(quest.field_28)
        writer.write_string(quest.tracking_id)


def _read_chapter_progress_entries(
    reader: BitReader,
) -> tuple[ChapterProgressEntry, ...]:
    return tuple(
        ChapterProgressEntry(
            reader.read_string(),
            int.from_bytes(reader.read_bytes(2), "little"),
            int.from_bytes(reader.read_bytes(2), "little"),
        )
        for _ in range(reader.read_uint32_le())
    )


def _write_chapter_progress_entries(
    writer: BitWriter, values: tuple[ChapterProgressEntry, ...]
) -> None:
    writer.write_uint32_le(len(values))
    for value in values:
        writer.write_string(value.chapter_id)
        writer.write_bytes(int(value.field_08).to_bytes(2, "little"))
        writer.write_bytes(int(value.field_0a).to_bytes(2, "little"))


def _read_chapter_progress_body(reader: BitReader) -> ChapterProgress:
    return ChapterProgress(
        _read_chapter_progress_entries(reader),
        _read_chapter_progress_entries(reader),
    )


def _write_chapter_progress_body(writer: BitWriter, value: ChapterProgress) -> None:
    _write_chapter_progress_entries(writer, value.active)
    _write_chapter_progress_entries(writer, value.completed)


def _read_event_update_body(reader: BitReader) -> EventUpdate:
    events = []
    for _ in range(reader.read_uint32_le()):
        event_id = reader.read_uint32_le()
        event_name = reader.read_string()
        flags = tuple(reader.read_bool() for _ in range(4))
        starts_at = ends_at = None
        tags = ()
        if not flags[1]:
            starts_at = tuple(reader.read_uint32_le() for _ in range(6))
            ends_at = tuple(reader.read_uint32_le() for _ in range(6))
            tags = tuple(
                reader.read_string() for _ in range(reader.read_uint32_le())
            )
        events.append(
            EventUpdateEntry(
                event_id, event_name, flags, starts_at, ends_at, tags
            )
        )
    return EventUpdate(tuple(events))


def _write_event_update_body(writer: BitWriter, value: EventUpdate) -> None:
    writer.write_uint32_le(len(value.events))
    for event in value.events:
        if len(event.flags) != 4:
            raise ValueError("EventUpdateEntry requires four flags")
        writer.write_uint32_le(event.event_id)
        writer.write_string(event.event_name)
        for flag in event.flags:
            writer.write_bool(flag)
        if not event.flags[1]:
            if event.starts_at is None or len(event.starts_at) != 6:
                raise ValueError("active EventUpdateEntry requires starts_at")
            if event.ends_at is None or len(event.ends_at) != 6:
                raise ValueError("active EventUpdateEntry requires ends_at")
            for number in event.starts_at + event.ends_at:
                writer.write_uint32_le(number)
            writer.write_uint32_le(len(event.tags))
            for tag in event.tags:
                writer.write_string(tag)


def encode_event_update(value: EventUpdate) -> tuple[bytes, int]:
    """Serialize a standalone actorless EventUpdateCommand (0x00dd)."""

    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(EVENT_UPDATE_COMMAND.to_bytes(2, "little"))
    _write_event_update_body(writer, value)
    return writer.finish()


def decode_event_update(payload: bytes, bit_length: int) -> EventUpdate:
    """Decode every EventUpdate field into an editable value object."""

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x84:
        raise ValueError("EventUpdate is not a server application message")
    if int.from_bytes(reader.read_bytes(2), "little") != EVENT_UPDATE_COMMAND:
        raise ValueError("EventUpdate has the wrong command ID")
    value = _read_event_update_body(reader)
    if reader.remaining:
        raise ValueError("EventUpdate has unaccounted trailing bits")
    return value


def _read_timed_challenges(
    reader: BitReader, *, count: int | None = None,
) -> tuple[TimedChallenge, ...]:
    if count is None:
        count = reader.read_uint32_le()
    return tuple(
        TimedChallenge(
            reader.read_string(),
            reader.read_string(),
            reader.read_uint8(),
            reader.read_uint32_le(),
            tuple(reader.read_uint32_le() for _ in range(6)),
        )
        for _ in range(count)
    )


def _write_timed_challenges(
    writer: BitWriter, values: tuple[TimedChallenge, ...]
) -> None:
    writer.write_uint32_le(len(values))
    for value in values:
        if len(value.timestamp) != 6:
            raise ValueError("TimedChallenge timestamp requires six values")
        writer.write_string(value.challenge_id)
        writer.write_string(value.schedule_id)
        writer.write_uint8(value.state)
        writer.write_uint32_le(value.progress)
        for number in value.timestamp:
            writer.write_uint32_le(number)


def _write_daily_challenge_progress_body(writer, value):
    # Native TimedChallengeInfo without the outer map key (0x140a53f2c).
    writer.write_string(value.challenge_id)
    writer.write_uint8(value.state)
    writer.write_uint32_le(value.progress)
    for part in value.timestamp:
        writer.write_uint32_le(part)


def _read_daily_challenge_progress_body(reader):
    identity = reader.read_string()
    return TimedChallenge(identity, identity, reader.read_uint8(), reader.read_uint32_le(),
                          tuple(reader.read_uint32_le() for _ in range(6)))


def _read_timed_challenges_info_body(reader: BitReader) -> TimedChallengesInfo:
    server_time = reader.read_uint64_le()
    daily = _read_timed_challenges(reader)
    weekly = _read_timed_challenges(reader)
    monthly = _read_timed_challenges(reader)

    # A 2026-09 retail capture ends after the three legacy maps. The installed
    # client reads a fourth comeback-daily map before the actor context. Try
    # that map, but rewind on malformed/truncated legacy tails so old captures
    # remain decodable. Counts are bounded before allocating or iterating.
    comeback_position = reader.position
    try:
        count = reader.read_uint32_le()
        if count > 4096:
            raise ValueError(f"unreasonable timed-challenge count {count}")
        comeback_daily = _read_timed_challenges(reader, count=count)
    except (ValueError, IndexError):
        reader.position = comeback_position
        comeback_daily = None

    return TimedChallengesInfo(
        server_time, daily, weekly, monthly, comeback_daily,
    )


def _write_timed_challenges_info_body(
    writer: BitWriter, value: TimedChallengesInfo
) -> None:
    writer.write_uint64_le(value.server_time)
    _write_timed_challenges(writer, value.daily)
    _write_timed_challenges(writer, value.weekly)
    _write_timed_challenges(writer, value.monthly)
    if value.comeback_daily is not None:
        _write_timed_challenges(writer, value.comeback_daily)


def _read_offer_string_floats(reader: BitReader) -> tuple[OfferStringFloat, ...]:
    return tuple(
        OfferStringFloat(reader.read_string(), reader.read_float())
        for _ in range(reader.read_uint32_le())
    )


def _write_offer_string_floats(
    writer: BitWriter, values: tuple[OfferStringFloat, ...]
) -> None:
    writer.write_uint32_le(len(values))
    for value in values:
        writer.write_string(value.resource_id)
        writer.write_float(value.value)


def _read_offer_details(reader: BitReader) -> OfferDetails:
    return OfferDetails(
        reader.read_string(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_string(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        int.from_bytes(reader.read_bytes(2), "little"),
        _read_offer_string_floats(reader),
        _read_offer_string_floats(reader),
        reader.read_string(),
        reader.read_string(),
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        reader.read_bool(),
        reader.read_bool(),
    )


def _write_offer_details(writer: BitWriter, value: OfferDetails) -> None:
    writer.write_string(value.resource_id)
    writer.write_uint32_le(value.field_58)
    writer.write_uint32_le(value.field_5c)
    writer.write_string(value.field_50)
    writer.write_uint32_le(value.field_74)
    writer.write_uint32_le(value.field_78)
    writer.write_uint32_le(value.field_94)
    writer.write_bytes(int(value.field_98).to_bytes(2, "little"))
    _write_offer_string_floats(writer, value.values_168)
    _write_offer_string_floats(writer, value.values_190)
    writer.write_string(value.field_c0)
    writer.write_string(value.field_c8)
    writer.write_uint32_le(value.field_80)
    writer.write_uint32_le(value.field_84)
    writer.write_bool(value.field_88)
    writer.write_bool(value.field_8b)


def _read_offer_group(reader: BitReader) -> OfferGroup:
    resource_id = reader.read_string()
    field_90 = reader.read_uint32_le()
    offers = tuple(
        OfferGroupEntry(_read_offer_details(reader), reader.read_uint32_le())
        for _ in range(reader.read_uint32_le())
    )
    return OfferGroup(resource_id, field_90, offers)


def _write_offer_group(writer: BitWriter, value: OfferGroup) -> None:
    writer.write_string(value.resource_id)
    writer.write_uint32_le(value.field_90)
    writer.write_uint32_le(len(value.offers))
    for entry in value.offers:
        _write_offer_details(writer, entry.offer)
        writer.write_uint32_le(entry.amount)


def _read_offer_groups(reader: BitReader) -> tuple[OfferGroup, ...]:
    return tuple(_read_offer_group(reader) for _ in range(reader.read_uint32_le()))


def _write_offer_groups(writer: BitWriter, values: tuple[OfferGroup, ...]) -> None:
    writer.write_uint32_le(len(values))
    for value in values:
        _write_offer_group(writer, value)


def _read_special_offer_body(reader: BitReader) -> SpecialOffer:
    primary = _read_offer_details(reader) if reader.read_bool() else None
    secondary = _read_offer_details(reader) if reader.read_bool() else None
    featured = _read_offer_group(reader) if reader.read_bool() else None
    field_c0 = reader.read_bool()
    field_c4 = reader.read_string()
    return SpecialOffer(
        primary,
        secondary,
        featured,
        field_c0,
        field_c4,
        _read_offer_groups(reader),
        _read_offer_groups(reader),
        _read_offer_groups(reader),
        _read_offer_groups(reader),
    )


def _write_special_offer_body(writer: BitWriter, value: SpecialOffer) -> None:
    writer.write_bool(value.primary is not None)
    if value.primary is not None:
        _write_offer_details(writer, value.primary)
    writer.write_bool(value.secondary is not None)
    if value.secondary is not None:
        _write_offer_details(writer, value.secondary)
    writer.write_bool(value.featured is not None)
    if value.featured is not None:
        _write_offer_group(writer, value.featured)
    writer.write_bool(value.field_c0)
    writer.write_string(value.field_c4)
    _write_offer_groups(writer, value.groups_40)
    _write_offer_groups(writer, value.groups_60)
    _write_offer_groups(writer, value.groups_80)
    _write_offer_groups(writer, value.groups_a0)


def _read_timed_offers_info_body(reader: BitReader) -> TimedOffersInfo:
    server_time = reader.read_uint64_le()
    offers = tuple(
        TimedOffer(
            reader.read_string(),
            reader.read_uint32_le(),
            reader.read_uint32_le(),
            tuple(reader.read_uint32_le() for _ in range(6)),
        )
        for _ in range(reader.read_uint32_le())
    )
    return TimedOffersInfo(server_time, offers)


def _write_timed_offers_info_body(writer: BitWriter, value: TimedOffersInfo) -> None:
    writer.write_uint64_le(value.server_time)
    writer.write_uint32_le(len(value.offers))
    for offer in value.offers:
        if len(offer.timestamp) != 6:
            raise ValueError("TimedOffer timestamp requires six values")
        writer.write_string(offer.offer_id)
        writer.write_uint32_le(offer.field_08)
        writer.write_uint32_le(offer.field_0c)
        for number in offer.timestamp:
            writer.write_uint32_le(number)


def _read_update_progress_tier_spender_challenges_body(
    reader: BitReader,
) -> UpdateProgressTierSpenderChallenges:
    # Generated serializer 0x140945e6c contains one uint32 at object +0x20.
    return UpdateProgressTierSpenderChallenges(reader.read_uint32_le())


def _write_update_progress_tier_spender_challenges_body(
    writer: BitWriter, value: UpdateProgressTierSpenderChallenges
) -> None:
    writer.write_uint32_le(value.amount)


def _read_event_progress_info_body(reader: BitReader) -> EventProgressInfo:
    return EventProgressInfo(
        tuple(
            EventProgressInfoEntry(
                reader.read_uint32_le(),
                tuple(reader.read_uint32_le() for _ in range(4)),
            )
            for _ in range(reader.read_uint32_le())
        )
    )


def _write_event_progress_info_body(
    writer: BitWriter, value: EventProgressInfo
) -> None:
    writer.write_uint32_le(len(value.events))
    for event in value.events:
        if len(event.values) != 4:
            raise ValueError("EventProgressInfoEntry requires four values")
        writer.write_uint32_le(event.event_id)
        for number in event.values:
            writer.write_uint32_le(number)


def _read_event_participation_info_body(
    reader: BitReader,
) -> EventParticipationInfo:
    return EventParticipationInfo(
        tuple(
            EventParticipation(reader.read_uint32_le(), reader.read_bool())
            for _ in range(reader.read_uint32_le())
        )
    )


def _write_event_participation_info_body(
    writer: BitWriter, value: EventParticipationInfo
) -> None:
    writer.write_uint32_le(len(value.events))
    for event in value.events:
        writer.write_uint32_le(event.event_id)
        writer.write_bool(event.participating)


def _read_variation_info_body(reader: BitReader) -> VariationInfo:
    count = reader.read_uint32_le()
    if count > 1_000_000:
        raise ValueError("VariationInfo entry count is unreasonable")
    entries = []
    for _ in range(count):
        guid = _read_blob16(reader)
        if len(guid) != 16:
            raise ValueError("VariationInfo requires a 16-byte instance GUID")
        event_set_row = reader.read_uint32_le()
        if event_set_row >= 0x80000000:
            event_set_row -= 0x100000000
        entries.append(VariationInfoEntry(guid, event_set_row))
    return VariationInfo(tuple(entries))


def _write_variation_info_body(writer: BitWriter, value: VariationInfo) -> None:
    # release261_192: array writer 0x14095f6d8; record writer 0x140a96a90.
    # GUID blob followed by EventSetRow; the in-memory identity transform
    # and ParentNodeIndex are not serialized by this command.
    writer.write_uint32_le(len(value.entries))
    for entry in value.entries:
        if len(entry.instance_guid) != 16 or not any(entry.instance_guid):
            raise ValueError("VariationInfo requires a nonempty 16-byte GUID")
        if not -(1 << 31) <= entry.event_set_row < (1 << 31):
            raise ValueError("VariationInfo event set row must fit int32")
        _write_blob16(writer, entry.instance_guid)
        writer.write_uint32_le(entry.event_set_row & 0xFFFFFFFF)


def _read_exit_info_body(reader: BitReader) -> ExitInfo:
    return ExitInfo(
        tuple(
            ExitInfoEntry(
                reader.read_string(),
                reader.read_string(),
                reader.read_uint32_le(),
                _read_blob16(reader),
            )
            for _ in range(reader.read_uint32_le())
        )
    )


def _write_exit_info_body(writer: BitWriter, value: ExitInfo) -> None:
    writer.write_uint32_le(len(value.exits))
    for exit_info in value.exits:
        writer.write_string(exit_info.exit_id)
        writer.write_string(exit_info.map_id)
        writer.write_uint32_le(exit_info.field_10)
        _write_blob16(writer, exit_info.instance_guid)


def _read_actors_enter_vicinity_body(
    reader: BitReader,
) -> ActorsEnterVicinity:
    count = reader.read_uint32_le()
    if count > 1_000_000:
        raise ValueError("ActorsEnterVicinity actor count is unreasonable")
    return ActorsEnterVicinity(tuple(
        reader.read_uint32_le() for _ in range(count)
    ))


def _write_actors_enter_vicinity_body(
    writer: BitWriter, value: ActorsEnterVicinity,
) -> None:
    writer.write_uint32_le(len(value.actor_ids))
    for actor_id in value.actor_ids:
        writer.write_uint32_le(actor_id)


def _read_activity_signin_calendar_body(
    reader: BitReader,
) -> ActivitySigninCalendar:
    return ActivitySigninCalendar(
        tuple(
            ActivitySigninDay(
                tuple(reader.read_uint32_le() for _ in range(4))
            )
            for _ in range(reader.read_uint32_le())
        )
    )


def _write_activity_signin_calendar_body(
    writer: BitWriter, value: ActivitySigninCalendar
) -> None:
    writer.write_uint32_le(len(value.days))
    for day in value.days:
        if len(day.values) != 4:
            raise ValueError("ActivitySigninDay requires four values")
        for number in day.values:
            writer.write_uint32_le(number)


def _read_activity_extra_record_body(reader: BitReader) -> ActivityExtraRecord:
    return ActivityExtraRecord(
        tuple(
            ActivityExtraRecordEntry(
                reader.read_uint32_le(), reader.read_string()
            )
            for _ in range(reader.read_uint32_le())
        )
    )


def _write_activity_extra_record_body(
    writer: BitWriter, value: ActivityExtraRecord
) -> None:
    writer.write_uint32_le(len(value.records))
    for record in value.records:
        writer.write_uint32_le(record.record_id)
        writer.write_string(record.resource_id)


def _read_user_hatch_eggs_info_body(reader: BitReader) -> UserHatchEggsInfo:
    counters = tuple(
        (reader.read_uint32_le(), reader.read_uint32_le())
        for _ in range(reader.read_uint32_le())
    )
    eggs = tuple(
        HatchEggRecord(
            reader.read_uint32_le(),
            tuple(reader.read_uint32_le() for _ in range(8)),
        )
        for _ in range(reader.read_uint32_le())
    )
    return UserHatchEggsInfo(counters, eggs)


def _write_user_hatch_eggs_info_body(
    writer: BitWriter, value: UserHatchEggsInfo
) -> None:
    writer.write_uint32_le(len(value.counters))
    for key, number in value.counters:
        writer.write_uint32_le(key)
        writer.write_uint32_le(number)
    writer.write_uint32_le(len(value.eggs))
    for egg in value.eggs:
        if len(egg.values) != 8:
            raise ValueError("HatchEggRecord requires eight values")
        writer.write_uint32_le(egg.egg_id)
        for number in egg.values:
            writer.write_uint32_le(number)


def _read_u32_array(reader: BitReader) -> tuple[int, ...]:
    return tuple(reader.read_uint32_le() for _ in range(reader.read_uint32_le()))


def _write_u32_array(writer: BitWriter, values: tuple[int, ...]) -> None:
    writer.write_uint32_le(len(values))
    for value in values:
        writer.write_uint32_le(value)


def _read_season_pass_rewards_info_result_body(
    reader: BitReader,
) -> SeasonPassRewardsInfoResult:
    return SeasonPassRewardsInfoResult(
        reader.read_uint32_le(),
        reader.read_uint32_le(),
        _read_u32_array(reader),
        _read_u32_array(reader),
    )


def _write_season_pass_rewards_info_result_body(
    writer: BitWriter, value: SeasonPassRewardsInfoResult
) -> None:
    writer.write_uint32_le(value.field_20)
    writer.write_uint32_le(value.field_24)
    _write_u32_array(writer, value.pass_types)
    _write_u32_array(writer, value.claimed_awards)


def _read_season_pass_missions_info_result_body(
    reader: BitReader,
) -> SeasonPassMissionsInfoResult:
    return SeasonPassMissionsInfoResult(tuple(
        (reader.read_uint32_le(), reader.read_uint32_le())
        for _ in range(reader.read_uint32_le())
    ))


def _write_season_pass_missions_info_result_body(
    writer: BitWriter, value: SeasonPassMissionsInfoResult
) -> None:
    writer.write_uint32_le(len(value.missions))
    for mission_id, progress in value.missions:
        writer.write_uint32_le(mission_id)
        writer.write_uint32_le(progress)


def _read_fieldless_body(reader: BitReader) -> Fieldless:
    return Fieldless()


def _write_fieldless_body(writer: BitWriter, value: Fieldless) -> None:
    if not isinstance(value, Fieldless):
        raise TypeError("fieldless command requires Fieldless")


def _read_second_day_signed_award_body(
    reader: BitReader,
) -> SecondDaySignedAwardRequest:
    return SecondDaySignedAwardRequest(
        tuple(reader.read_uint32_le() for _ in range(3))
    )


def _write_second_day_signed_award_body(
    writer: BitWriter, value: SecondDaySignedAwardRequest
) -> None:
    if len(value.values) != 3:
        raise ValueError("SecondDaySignedAwardRequest requires three values")
    for number in value.values:
        writer.write_uint32_le(number)


def _read_obstacle_update_body(reader: BitReader) -> ObstacleUpdate:
    return ObstacleUpdate(
        tuple(
            ObstacleState(reader.read_string(), reader.read_uint32_le())
            for _ in range(reader.read_uint32_le())
        )
    )


def _write_obstacle_update_body(writer: BitWriter, value: ObstacleUpdate) -> None:
    writer.write_uint32_le(len(value.obstacles))
    for obstacle in value.obstacles:
        writer.write_string(obstacle.obstacle_id)
        writer.write_uint32_le(obstacle.state)


_BODY_READERS = {
    KICK_OUT_TO_HUB_LIMIT_TIME_LEFT_COMMAND:
        _read_kick_out_to_hub_limit_time_left_body,
    MEMBERSHIP_UPDATE_COMMAND: read_membership_update_body,
    MESSAGE_COMMAND: _read_message_body,
    NOTIFICATION_COMMAND: _read_notification_body,
    PLAYER_QUERY_RESULT_COMMAND: _read_player_query_result_body,
    EVENT_PROGRESS_CHANGED_COMMAND: _read_event_progress_changed_body,
    ACHIEVEMENT_INFO_COMMAND: _read_achievement_info_body,
    TALENT_CATEGORY_POINTS_INFO_COMMAND: _read_talent_category_points_info_body,
    TALENT_SELECTION_INFO_COMMAND: _read_talent_selection_info_body,
    COMMAND_0173: _read_command_0173_body,
    STATUS_EFFECT_COMMAND: _read_status_effect_body,
    ITEM_CUSTOMIZATION_INFO_COMMAND: _read_item_customization_info_body,
    INVENTORY_INFO_COMMAND: read_inventory_info_body,
    CRAFTING_RESULT_COMMAND: _read_crafting_result_body,
    STICKERBOOK_INFO_COMMAND: _read_stickerbook_info_body,
    STICKERBOOK_ITEM_INFO_COMMAND: _read_stickerbook_item_info_body,
    RECIPE_BOOK_INFO_COMMAND: _read_recipe_book_info_body,
    CURRENCY_CHANGED_COMMAND: _read_currency_changed_body,
    GAME_CURRENCY_CHANGED_COMMAND: read_game_currency_changed_body,
    PLAYER_LEVEL_UPDATE_COMMAND: _read_player_level_update_body,
    XP_CHANGED_COMMAND: _read_xp_changed_body,
    SKILL_BOOK_INFO_COMMAND: _read_skill_book_info_body,
    QUICK_SLOTS_INFO_COMMAND: _read_quick_slots_info_body,
    QUICKBAR_INFO_COMMAND: _read_quickbar_info_body,
    UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND:
        _read_unlock_skill_talent_preselection_info_body,
    QUEST_LOG_INFO_COMMAND: _read_quest_log_info_body,
    CHAPTER_PROGRESS_COMMAND: _read_chapter_progress_body,
    EVENT_UPDATE_COMMAND: _read_event_update_body,
    DAILY_CHALLENGE_PROGRESS_COMMAND: _read_daily_challenge_progress_body,
    TIMED_CHALLENGES_INFO_COMMAND: _read_timed_challenges_info_body,
    SPECIAL_OFFER_COMMAND: _read_special_offer_body,
    TIMED_OFFERS_INFO_COMMAND: _read_timed_offers_info_body,
    OFFER_COMMAND: read_offer_command_body,
    UPDATE_PROGRESS_TIER_SPENDER_CHALLENGES_COMMAND:
        _read_update_progress_tier_spender_challenges_body,
    EVENT_PROGRESS_INFO_COMMAND: _read_event_progress_info_body,
    EVENT_PARTICIPATION_INFO_COMMAND: _read_event_participation_info_body,
    VARIATION_INFO_COMMAND: _read_variation_info_body,
    EXIT_INFO_COMMAND: _read_exit_info_body,
    ACTIVITY_SIGNIN_CALENDAR_COMMAND: _read_activity_signin_calendar_body,
    ACTIVITY_EXTRA_RECORD_COMMAND: _read_activity_extra_record_body,
    USER_HATCH_EGGS_INFO_COMMAND: _read_user_hatch_eggs_info_body,
    SEASON_PASS_REWARDS_INFO_RESULT_COMMAND:
        _read_season_pass_rewards_info_result_body,
    SEASON_PASS_MISSIONS_INFO_RESULT_COMMAND:
        _read_season_pass_missions_info_result_body,
    GET_SECOND_DAY_SIGNED_AWARD_COMMAND: _read_second_day_signed_award_body,
    OBSTACLE_UPDATE_COMMAND: _read_obstacle_update_body,
    PLAYER_READY_COMMAND: _read_fieldless_body,
    NEW_NPC_COMMAND: _read_new_npc_body,
    NPC_INFO_COMMAND: _read_npc_info_body,
    PROP_INFO_COMMAND: _read_prop_info_body,
    ACTORS_ENTER_VICINITY_COMMAND: _read_actors_enter_vicinity_body,
}

_BODY_WRITERS = {
    KICK_OUT_TO_HUB_LIMIT_TIME_LEFT_COMMAND:
        _write_kick_out_to_hub_limit_time_left_body,
    MEMBERSHIP_UPDATE_COMMAND: write_membership_update_body,
    MESSAGE_COMMAND: _write_message_body,
    NOTIFICATION_COMMAND: _write_notification_body,
    PLAYER_QUERY_RESULT_COMMAND: _write_player_query_result_body,
    EVENT_PROGRESS_CHANGED_COMMAND: _write_event_progress_changed_body,
    ACHIEVEMENT_INFO_COMMAND: _write_achievement_info_body,
    TALENT_CATEGORY_POINTS_INFO_COMMAND: _write_talent_category_points_info_body,
    TALENT_SELECTION_INFO_COMMAND: _write_talent_selection_info_body,
    COMMAND_0173: _write_command_0173_body,
    STATUS_EFFECT_COMMAND: _write_status_effect_body,
    ITEM_CUSTOMIZATION_INFO_COMMAND: _write_item_customization_info_body,
    INVENTORY_INFO_COMMAND: write_inventory_info_body,
    CRAFTING_RESULT_COMMAND: _write_crafting_result_body,
    STICKERBOOK_INFO_COMMAND: _write_stickerbook_info_body,
    STICKERBOOK_ITEM_INFO_COMMAND: _write_stickerbook_item_info_body,
    RECIPE_BOOK_INFO_COMMAND: _write_recipe_book_info_body,
    CURRENCY_CHANGED_COMMAND: _write_currency_changed_body,
    GAME_CURRENCY_CHANGED_COMMAND: write_game_currency_changed_body,
    PLAYER_LEVEL_UPDATE_COMMAND: _write_player_level_update_body,
    XP_CHANGED_COMMAND: _write_xp_changed_body,
    SKILL_BOOK_INFO_COMMAND: _write_skill_book_info_body,
    QUICK_SLOTS_INFO_COMMAND: _write_quick_slots_info_body,
    QUICKBAR_INFO_COMMAND: _write_quickbar_info_body,
    UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND:
        _write_unlock_skill_talent_preselection_info_body,
    QUEST_LOG_INFO_COMMAND: _write_quest_log_info_body,
    CHAPTER_PROGRESS_COMMAND: _write_chapter_progress_body,
    EVENT_UPDATE_COMMAND: _write_event_update_body,
    DAILY_CHALLENGE_PROGRESS_COMMAND: _write_daily_challenge_progress_body,
    TIMED_CHALLENGES_INFO_COMMAND: _write_timed_challenges_info_body,
    SPECIAL_OFFER_COMMAND: _write_special_offer_body,
    TIMED_OFFERS_INFO_COMMAND: _write_timed_offers_info_body,
    OFFER_COMMAND: write_offer_command_body,
    UPDATE_PROGRESS_TIER_SPENDER_CHALLENGES_COMMAND:
        _write_update_progress_tier_spender_challenges_body,
    EVENT_PROGRESS_INFO_COMMAND: _write_event_progress_info_body,
    EVENT_PARTICIPATION_INFO_COMMAND: _write_event_participation_info_body,
    VARIATION_INFO_COMMAND: _write_variation_info_body,
    EXIT_INFO_COMMAND: _write_exit_info_body,
    ACTIVITY_SIGNIN_CALENDAR_COMMAND: _write_activity_signin_calendar_body,
    ACTIVITY_EXTRA_RECORD_COMMAND: _write_activity_extra_record_body,
    USER_HATCH_EGGS_INFO_COMMAND: _write_user_hatch_eggs_info_body,
    SEASON_PASS_REWARDS_INFO_RESULT_COMMAND:
        _write_season_pass_rewards_info_result_body,
    SEASON_PASS_MISSIONS_INFO_RESULT_COMMAND:
        _write_season_pass_missions_info_result_body,
    GET_SECOND_DAY_SIGNED_AWARD_COMMAND: _write_second_day_signed_award_body,
    OBSTACLE_UPDATE_COMMAND: _write_obstacle_update_body,
    PLAYER_READY_COMMAND: _write_fieldless_body,
    NEW_NPC_COMMAND: _write_new_npc_body,
    NPC_INFO_COMMAND: _write_npc_info_body,
    PROP_INFO_COMMAND: _write_prop_info_body,
    ACTORS_ENTER_VICINITY_COMMAND: _write_actors_enter_vicinity_body,
}

_ACTORLESS_COMMANDS = {
    PLAYER_QUERY_RESULT_COMMAND,
    EVENT_UPDATE_COMMAND,
    COMMAND_0173,
}
_EMBEDDED_ACTOR_COMMANDS = {NEW_NPC_COMMAND, NPC_INFO_COMMAND}


def decode_player_init_prefix(
    payload: bytes, bit_length: int
) -> tuple[tuple[ServerCommand, ...], int]:
    """Decode all currently modeled commands at the front of the retail batch.

    The returned bit position is deliberately exposed while the remaining
    command layouts are being recovered; it must equal ``bit_length`` before
    this codec is considered complete.
    """

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("player initialization is not a 0x85 command batch")
    if int.from_bytes(reader.read_bytes(2), "little") != NEW_PLAYER_COMMAND:
        raise ValueError("player initialization does not begin with NewPlayer")
    commands = [
        ServerCommand(NEW_PLAYER_COMMAND, read_new_player_body(reader), None)
    ]
    while reader.remaining:
        marker_position = reader.position
        if reader.read_uint8() != 0xFF:
            raise ValueError(f"missing command marker at bit {marker_position}")
        if not reader.remaining:
            break
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        body_reader = _BODY_READERS.get(command_id)
        if body_reader is None:
            reader.position = marker_position
            break
        value = body_reader(reader)
        # These observed generated wrappers carry no server actor context.
        if command_id in _ACTORLESS_COMMANDS:
            actor_id = None
        elif command_id in _EMBEDDED_ACTOR_COMMANDS:
            actor_id = value.actor_id
        else:
            actor_id = reader.read_uint32_le()
        commands.append(ServerCommand(command_id, value, actor_id))
    return tuple(commands), reader.position


def encode_player_init_prefix(
    commands: tuple[ServerCommand, ...], *, include_terminator: bool = False
) -> tuple[bytes, int]:
    if not commands or commands[0].command_id != NEW_PLAYER_COMMAND:
        raise ValueError("player initialization must begin with NewPlayer")
    if not isinstance(commands[0].value, NewPlayerWire):
        raise TypeError("first command value must be NewPlayerWire")
    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(NEW_PLAYER_COMMAND.to_bytes(2, "little"))
    write_new_player_body(writer, commands[0].value)
    for command in commands[1:]:
        body_writer = _BODY_WRITERS.get(command.command_id)
        if body_writer is None:
            raise ValueError(f"unmodeled command 0x{command.command_id:04x}")
        writer.write_uint8(0xFF)
        writer.write_bytes(command.command_id.to_bytes(2, "little"))
        body_writer(writer, command.value)
        if command.command_id not in (
            _ACTORLESS_COMMANDS | _EMBEDDED_ACTOR_COMMANDS
        ):
            if command.actor_id is None:
                raise ValueError(
                    f"command 0x{command.command_id:04x} requires actor context"
                )
            writer.write_uint32_le(command.actor_id)
    if include_terminator:
        writer.write_uint8(0xFF)
    return writer.finish()


def encode_server_command_batch(
    commands: tuple[ServerCommand, ...], *, include_terminator: bool = True
) -> tuple[bytes, int]:
    """Serialize an arbitrary ordered server-command batch.

    Unlike :func:`encode_player_init_prefix`, this accepts any modeled command
    as the first entry.  Vendor purchases use it for the retail
    ``0x013a/0x0054/0x0080/0x0054/0x0052/0x0080`` response topology.
    """

    if not commands:
        raise ValueError("server command batch cannot be empty")
    writer = BitWriter()
    writer.write_uint8(0x85)
    for index, command in enumerate(commands):
        body_writer = _BODY_WRITERS.get(command.command_id)
        if body_writer is None:
            raise ValueError(f"unmodeled command 0x{command.command_id:04x}")
        if index:
            writer.write_uint8(0xFF)
        writer.write_bytes(command.command_id.to_bytes(2, "little"))
        body_writer(writer, command.value)
        if command.command_id not in (
            _ACTORLESS_COMMANDS | _EMBEDDED_ACTOR_COMMANDS
        ):
            if command.actor_id is None:
                raise ValueError(
                    f"command 0x{command.command_id:04x} requires actor context"
                )
            writer.write_uint32_le(command.actor_id)
    if include_terminator:
        writer.write_uint8(0xFF)
    return writer.finish()


def decode_server_command_batch(
    payload: bytes, bit_length: int
) -> tuple[ServerCommand, ...]:
    """Decode a complete arbitrary ``0x85`` batch of modeled commands."""

    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x85:
        raise ValueError("server command batch does not begin with 0x85")
    commands = []
    first = True
    while reader.remaining:
        if not first:
            marker_position = reader.position
            if reader.read_uint8() != 0xFF:
                raise ValueError(
                    f"missing command marker at bit {marker_position}"
                )
            if not reader.remaining:
                break
        first = False
        command_id = int.from_bytes(reader.read_bytes(2), "little")
        body_reader = _BODY_READERS.get(command_id)
        if body_reader is None:
            raise ValueError(f"unmodeled command 0x{command_id:04x}")
        value = body_reader(reader)
        if command_id in _ACTORLESS_COMMANDS:
            actor_id = None
        elif command_id in _EMBEDDED_ACTOR_COMMANDS:
            actor_id = value.actor_id
        else:
            actor_id = reader.read_uint32_le()
        commands.append(ServerCommand(command_id, value, actor_id))
    return tuple(commands)


def build_local_player_init_commands(
    new_player: NewPlayerWire,
    inventory: InventoryInfo,
    map_entity_commands: tuple[ServerCommand, ...],
    *,
    exit_info: ExitInfo,
    actor_id: int,
    skill_book_ids: tuple[int, ...] = (),
    quickbar_skill_slots: tuple[str | None, ...] = (),
    quickbar_stickerbook_ids: tuple[str, ...] = (),
    quick_slots_info: QuickSlotsInfo | None = None,
    stickerbook_entries: tuple[StickerbookEntry | None, ...] = (),
    recipe_book: RecipeBookInfo | None = None,
    achievements: AchievementInfo | None = None,
    persistent_status_effects: tuple[StatusEffectEntry, ...] = (),
    talent_categories: tuple[TalentCategorySelection, ...] | None = None,
    talent_category_points: tuple[int, int, int, int] | None = None,
    unlocked_skill_presets: int = 0,
    prop_info: PropInfo | None = None,
    notifications: tuple[NotificationEntry, ...] = (),
    activity_signin_calendar: ActivitySigninCalendar | None = None,
    item_customizations: tuple[ItemCustomization, ...] = (),
    membership_update: MembershipUpdate | None = None,
    event_update: EventUpdate | None = None,
    event_progress_info: EventProgressInfo | None = None,
    event_participation_info: EventParticipationInfo | None = None,
    season_pass_info: SeasonPassRewardsInfoResult | None = None,
    quest_log: QuestLogInfo | None = None,
    chapter_progress: ChapterProgress | None = None,
    timed_challenges: TimedChallengesInfo | None = None,
    special_offer: SpecialOffer | None = None,
    skill_book: SkillBookInfo | None = None,
    hatch_eggs_info: UserHatchEggsInfo | None = None,
) -> tuple[ServerCommand, ...]:
    """Build the complete retail initialization topology from local state.

    The original city message contains 117 generated commands. Empty local
    collections are still emitted because each command initializes a distinct
    client-side manager; omitting one is observably different from sending an
    empty generated array. NewNPC/NPCInfo pairs are supplied by the selected
    map database, whose NPC count varies by map.
    """

    actor_id = int(actor_id)
    if new_player.actor_id != actor_id:
        raise ValueError("NewPlayer actor does not match initialization actor")
    npc_commands = tuple(
        command for command in map_entity_commands
        if command.command_id != VARIATION_INFO_COMMAND
    )
    has_variation_info = len(npc_commands) != len(map_entity_commands)
    native_exit_guids = {
        bytes(entry.instance_guid)
        for command in map_entity_commands
        if command.command_id == VARIATION_INFO_COMMAND
        for entry in command.value.entries
    }
    initial_exit_info = ExitInfo(tuple(
        entry for entry in exit_info.exits
        if bytes(entry.instance_guid) not in native_exit_guids
    ))
    if len(npc_commands) % 2:
        raise ValueError(
            "local initialization requires complete NewNPC/NPCInfo pairs"
        )
    for index, command in enumerate(npc_commands):
        expected = NEW_NPC_COMMAND if index % 2 == 0 else NPC_INFO_COMMAND
        if command.command_id != expected:
            raise ValueError(
                f"map entity command {index} must be 0x{expected:04x}"
            )

    empty_achievement = ServerCommand(
        ACHIEVEMENT_INFO_COMMAND, AchievementInfo(()), actor_id
    )
    empty_notification = ServerCommand(
        NOTIFICATION_COMMAND, Notification(1, ()), 0
    )
    login_notification = ServerCommand(
        NOTIFICATION_COMMAND, Notification(1, tuple(notifications)), 0
    )
    if quick_slots_info is None:
        empty_slot = QuickSlotInfo(0xFFFFFFFF, None)
        if len(quickbar_skill_slots) > 8:
            raise ValueError(
                "local quickbar supports at most eight skill slots"
            )
        mount_slot_start = 8
        local_quickbar = [
            (
                QuickSlotInfo(0, str(resource_id))
                if resource_id is not None
                else empty_slot
            )
            for resource_id in quickbar_skill_slots
        ]
        # The retail bar places its first sticker-book favorite at zero-based
        # slot 8. Preserve that position when saved skill positions contain
        # gaps or do not fill the first eight slots.
        local_quickbar.extend(
            empty_slot
            for _ in range(mount_slot_start - len(local_quickbar))
        )
        # Slots 8..10 are stickerbook shortcuts (Mount: 8, Pet/Mercenary: 9, Emote: 10).
        mount_id = next((r for r in (quickbar_stickerbook_ids or ()) if "mount" in str(r)), None)
        mercenary_id = next((r for r in (quickbar_stickerbook_ids or ()) if "mercenary" in str(r) or "pet" in str(r)), None)
        local_quickbar.append(QuickSlotInfo(2, str(mount_id)) if mount_id else empty_slot)
        local_quickbar.append(QuickSlotInfo(2, str(mercenary_id)) if mercenary_id else empty_slot)
        local_quickbar.append(empty_slot)
        # Consumable and widget slots:
        # Slot 11: QuickSkillResource (Key D)
        local_quickbar.append(empty_slot)
        # Slot 12: QuickSkillResource2 (Key U)
        local_quickbar.append(empty_slot)
        # Slot 13: QuickHealth (Key H)
        local_quickbar.append(
            QuickSlotInfo(1, "potion_combined_health_rarity_05")
        )
        # Slot 14: QuickHealth2 (Key R)
        local_quickbar.append(empty_slot)
        # Slots 15..16: Extra widget slots
        local_quickbar.extend(empty_slot for _ in range(2))
        if len(local_quickbar) != 17:
            raise AssertionError("invalid local quickbar layout")
        quick_slots_info = QuickSlotsInfo((tuple(local_quickbar),))
    else:
        if not 1 <= len(quick_slots_info.quickbars) <= 10:
            raise ValueError("saved quick slots require between 1 and 10 bars")
        for quickbar in quick_slots_info.quickbars:
            if len(quickbar) != 17:
                raise ValueError("every saved quickbar requires 17 slots")
            for slot in quickbar:
                if (
                    slot.kind == 0xFFFFFFFF
                    and slot.resource_id is not None
                ):
                    raise ValueError(
                        "empty saved quick slot carries a resource id"
                    )
                if (
                    slot.kind != 0xFFFFFFFF
                    and not slot.resource_id
                ):
                    raise ValueError(
                        "occupied saved quick slot has no resource id"
                    )
    if skill_book is None:
        # Persisted entries are already read. Leaving `new` set makes the
        # client's next XPChanged event add the same skills to QuickSlots.
        skill_book = SkillBookInfo(tuple(
            SkillBookEntry(int(skill_id), (True, False, False))
            for skill_id in dict.fromkeys(skill_book_ids)
        ))
    # Category 0 is the class-skill progression.  Retail mirrors the current
    # normal XP from NewPlayer (+0x118) into TalentCategoryPointsInfo; sending
    # zero here makes the Skills & Talents manager rebuild a level-100
    # character from an empty progression snapshot and leaves only the low
    # tier skills enabled.  Categories 1 and 3 also have authoritative values
    # in NewPlayer.  Group progression (category 2) has no corresponding
    # generated NewPlayer field, so a fresh local character starts it at zero.
    if talent_category_points is None:
        talent_category_points = (
            int(new_player.fields_110[2]),
            int(new_player.player_state_scalars[17]),
            0,
            int(new_player.fields_110[6]),
        )
    elif len(talent_category_points) != 4:
        raise ValueError("talent_category_points requires four values")
    if talent_categories is None:
        talent_categories = (
            TalentCategorySelection(1, (), ()),
            TalentCategorySelection(0, (), ()),
            TalentCategorySelection(0, (), ()),
            TalentCategorySelection(0, (), ()),
        )
    elif len(talent_categories) != 4:
        raise ValueError("local talent snapshot requires four categories")
    if prop_info is None:
        prop_info = PropInfo(())
    if activity_signin_calendar is None:
        activity_signin_calendar = ActivitySigninCalendar(())
    if recipe_book is None:
        recipe_book = RecipeBookInfo(())
    if achievements is None:
        achievements = AchievementInfo(())
    if event_update is None:
        event_update = EventUpdate(())
    if event_progress_info is None:
        event_progress_info = EventProgressInfo(())
    if event_participation_info is None:
        event_participation_info = EventParticipationInfo(())
    if season_pass_info is None:
        season_pass_info = SeasonPassRewardsInfoResult(0, 0, (), ())
    aggregate_achievement = ServerCommand(
        ACHIEVEMENT_INFO_COMMAND, achievements, actor_id
    )

    commands = [
        ServerCommand(NEW_PLAYER_COMMAND, new_player, None),
    ]
    if membership_update is not None:
        commands.append(
            ServerCommand(
                MEMBERSHIP_UPDATE_COMMAND,
                membership_update,
                actor_id,
            )
        )
    commands.extend([
        ServerCommand(
            MESSAGE_COMMAND,
            Message(10, (MessageArgument(1, 0),)),
            actor_id,
        ),
    ])
    # Retail sends fifty single-achievement notifications before its aggregate
    # snapshot.  Preserve those command boundaries even for a fresh account.
    commands.extend(empty_achievement for _ in range(50))
    commands.extend(
        (
            ServerCommand(
                PLAYER_QUERY_RESULT_COMMAND, PlayerQueryResult(0, ()), None
            ),
            empty_notification,
            empty_notification,
            empty_notification,
            empty_notification,
            ServerCommand(COMMAND_0173, Command0173(0, 0), None),
            ServerCommand(
                EVENT_PROGRESS_CHANGED_COMMAND,
                EventProgressChanged((0, 0, 0xFFFFFFFF, 0xFFFFFFFF, 0)),
                actor_id,
            ),
            aggregate_achievement,
            ServerCommand(
                STATUS_EFFECT_COMMAND, StatusEffect(True, ()), actor_id
            ),
            ServerCommand(
                TALENT_CATEGORY_POINTS_INFO_COMMAND,
                TalentCategoryPointsInfo(0xFF, talent_category_points),
                0,
            ),
            ServerCommand(
                TALENT_SELECTION_INFO_COMMAND,
                TalentSelectionInfo(0, talent_categories),
                0,
            ),
            ServerCommand(
                ITEM_CUSTOMIZATION_INFO_COMMAND,
                ItemCustomizationInfo(tuple(item_customizations)),
                actor_id,
            ),
            ServerCommand(INVENTORY_INFO_COMMAND, inventory, actor_id),
            ServerCommand(
                STICKERBOOK_INFO_COMMAND,
                StickerbookInfo(0, tuple(stickerbook_entries)),
                actor_id,
            ),
            ServerCommand(
                RECIPE_BOOK_INFO_COMMAND, recipe_book, actor_id
            ),
            # These six generated values duplicate NewPlayer's account-state
            # fields in the retail stream.  Keep both commands internally
            # consistent; premium currency is the separate character-list
            # field stored in the player database.
            ServerCommand(
                CURRENCY_CHANGED_COMMAND,
                CurrencyChanged(
                    new_player.fields_170[6],
                    new_player.field_190,
                    new_player.fields_198,
                    new_player.field_1a8,
                    True,
                ),
                actor_id,
            ),
            ServerCommand(SKILL_BOOK_INFO_COMMAND, skill_book, actor_id),
            ServerCommand(
                QUICK_SLOTS_INFO_COMMAND,
                quick_slots_info,
                actor_id,
            ),
            ServerCommand(
                QUICKBAR_INFO_COMMAND, QuickbarInfo(1, 0), actor_id
            ),
            ServerCommand(
                UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND,
                UnlockSkillTalentPreselectionInfo(
                    max(0, int(unlocked_skill_presets))
                ),
                0,
            ),
            ServerCommand(
                QUEST_LOG_INFO_COMMAND,
                quest_log if quest_log is not None else QuestLogInfo(()),
                actor_id,
            ),
            ServerCommand(
                CHAPTER_PROGRESS_COMMAND,
                chapter_progress if chapter_progress is not None
                else ChapterProgress((), ()),
                actor_id,
            ),
            ServerCommand(
                TIMED_CHALLENGES_INFO_COMMAND,
                timed_challenges if timed_challenges is not None
                else TimedChallengesInfo(0, (), (), (), ()),
                actor_id,
            ),
            ServerCommand(
                SPECIAL_OFFER_COMMAND,
                special_offer if special_offer is not None
                else SpecialOffer(None, None, None, False, "", (), (), (), ()),
                0,
            ),
            ServerCommand(
                TIMED_OFFERS_INFO_COMMAND, TimedOffersInfo(0, ()), actor_id
            ),
            ServerCommand(
                EVENT_PROGRESS_INFO_COMMAND, event_progress_info, actor_id
            ),
            ServerCommand(
                EVENT_PARTICIPATION_INFO_COMMAND,
                event_participation_info,
                actor_id,
            ),
            # Retail publishes login rewards in the later notification
            # snapshot immediately before ExitInfo. The four earlier
            # notification managers are independent event/sale snapshots and
            # must remain empty locally; repeating the login entry in each
            # one produces duplicate login-reward icons.
            login_notification,
            # Native exits in ``native_exit_guids`` do not exist in the scene
            # yet. Sending them before EventUpdate makes UpdateExitState log a
            # missing-entity warning. They are refreshed below after
            # VariationInfo has created them.
            ServerCommand(EXIT_INFO_COMMAND, initial_exit_info, 0),
        )
    )
    event_bound_variations = []
    for command in map_entity_commands:
        if command.command_id in {NEW_NPC_COMMAND, NPC_INFO_COMMAND}:
            # Let EventUpdate materialize the authored map actors before
            # attaching the network NPC actors to those scene entities.
            continue
        if command.command_id != VARIATION_INFO_COMMAND:
            commands.append(command)
            continue
        unconditional = tuple(
            entry for entry in command.value.entries if entry.event_set_row < 0
        )
        event_bound = tuple(
            entry for entry in command.value.entries if entry.event_set_row >= 0
        )
        if unconditional:
            commands.append(ServerCommand(
                VARIATION_INFO_COMMAND, VariationInfo(unconditional), command.actor_id,
            ))
        if event_bound:
            event_bound_variations.append(ServerCommand(
                VARIATION_INFO_COMMAND, VariationInfo(event_bound), command.actor_id,
            ))
    # The client materializes map-scene entities when it processes the map's
    # EventUpdate. Publish ExitInfo first so variation-specific exits are in
    # the client's Name/GUID cache, and register map NPCs before the event can
    # activate their scene sets.
    commands.append(ServerCommand(EVENT_UPDATE_COMMAND, event_update, None))
    # The client remembers processed variation GUIDs. Create event-bound
    # entities only after their event is active so initialization cannot
    # remove them and leave later creation requests permanently ignored.
    commands.extend(npc_commands)
    commands.extend(event_bound_variations)
    if has_variation_info:
        # VariationInfo creates missing native entities. Refresh their exit
        # labels/state after creation, using the cache primed above.
        commands.append(ServerCommand(EXIT_INFO_COMMAND, exit_info, 0))
    commands.extend(
        (
            ServerCommand(PROP_INFO_COMMAND, prop_info, actor_id),
            ServerCommand(
                ACTIVITY_SIGNIN_CALENDAR_COMMAND,
                activity_signin_calendar,
                0,
            ),
            ServerCommand(
                ACTIVITY_EXTRA_RECORD_COMMAND, ActivityExtraRecord(()), 0
            ),
            ServerCommand(
                USER_HATCH_EGGS_INFO_COMMAND,
                hatch_eggs_info if hatch_eggs_info is not None
                else UserHatchEggsInfo((), ()),
                actor_id,
            ),
            ServerCommand(
                SEASON_PASS_REWARDS_INFO_RESULT_COMMAND,
                season_pass_info,
                0,
            ),
            ServerCommand(
                GET_SECOND_DAY_SIGNED_AWARD_COMMAND,
                SecondDaySignedAwardRequest((0, 0, 0)),
                0,
            ),
            ServerCommand(OBSTACLE_UPDATE_COMMAND, ObstacleUpdate(()), 0),
            ServerCommand(
                STATUS_EFFECT_COMMAND,
                StatusEffect(False, tuple(persistent_status_effects)),
                actor_id,
            ),
            ServerCommand(PLAYER_READY_COMMAND, Fieldless(), actor_id),
        )
    )
    expected_command_count = (
        91 + len(map_entity_commands)
        + int(membership_update is not None)
        + int(has_variation_info)
    )
    if len(commands) != expected_command_count:
        raise AssertionError(
            f"local initialization topology has {len(commands)} commands; "
            f"expected {expected_command_count}"
        )
    return tuple(commands)


def local_client_player_init_commands(
    commands: tuple[ServerCommand, ...],
) -> tuple[ServerCommand, ...]:
    """Return the initialization stream supported by the local client build.

    This is intentionally a compatibility projection, not a deletion from the
    reconstructed retail protocol.  ``decode_player_init_prefix`` and
    ``encode_player_init_prefix`` still decode and reproduce all 117 captured
    commands bit-for-bit, including commands 0x016c and 0x0173.
    """

    compatible = tuple(
        command
        for command in commands
        if command.command_id not in LOCAL_CLIENT_UNSUPPORTED_INIT_COMMANDS
    )
    removed = len(commands) - len(compatible)
    if removed != len(LOCAL_CLIENT_UNSUPPORTED_INIT_COMMANDS):
        raise ValueError(
            "unexpected unsupported local-client init command count; "
            f"removed {removed}"
        )
    return compatible
