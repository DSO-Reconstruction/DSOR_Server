"""Quest command codecs (docs/QUEST_PROTOCOL.md).

Client-to-server commands are ``0x8B``, the ``u16`` id, then the decoder's
fields and nothing else.  Server-to-client commands are batched with the
shared ``ServerCommand`` machinery in ``player_init_protocol`` and end with
the player's actor id.  ``QuestLogInfo`` and ``ChapterProgress`` already
have codecs there; this module adds the rest and the retail-verified
constants.
"""

from __future__ import annotations

from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.command_ids import command_id
from dso_server.protocol.player_init_protocol import (
    _BODY_READERS,
    _BODY_WRITERS,
    ChapterProgress,
    ChapterProgressEntry,
    QuestLogEntry,
    QuestLogInfo,
    ServerCommand,
    encode_server_command_batch,
)


QUEST_COMMAND = command_id("Commands::QuestCommand", 0x008A)
QUEST_UPDATE_COMMAND = command_id("Commands::QuestUpdateCommand", 0x008B)
QUEST_LOG_INFO_COMMAND = command_id("Commands::QuestLogInfoCommand", 0x008C)
QUEST_MONSTER_INDICATION_REQUEST_COMMAND = command_id(
    "Commands::QuestMonsterIndicationRequestCommand", 0x008D
)
QUEST_MONSTER_INDICATION_INFO_COMMAND = command_id(
    "Commands::QuestMonsterIndicationInfoCommand", 0x008E
)
QUEST_TRIGGER_INDICATION_REQUEST_COMMAND = command_id(
    "Commands::QuestTriggerIndicationRequestCommand", 0x008F
)
QUEST_TRIGGER_INDICATION_INFO_COMMAND = command_id(
    "Commands::QuestTriggerIndicationInfoCommand", 0x0090
)
QUEST_DISABLE_COMMAND = command_id("Commands::QuestDisableCommand", 0x0091)
CHAPTER_UNLOCKED_COMMAND = command_id("Commands::ChapterUnlockedCommand", 0x0092)
CHAPTER_PROGRESS_COMMAND = command_id("Commands::ChapterProgressCommand", 0x0093)
TALK_COMMAND = command_id("Commands::TalkCommand", 0x0067)
DELIVER_COMMAND = command_id("Commands::DeliverCommand", 0x0068)
PAY_COMMAND = command_id("Commands::PayCommand", 0x0069)
ENCOUNTER_COMMAND = command_id("Commands::EncounterCommand", 0x0066)

# QuestCommand.operation (three client senders, docs §2)
QUEST_OPERATION_ACCEPT = 0
QUEST_OPERATION_TURN_IN = 1
QUEST_OPERATION_CANCEL = 2

# QuestUpdate.subject (docs §3)
QUEST_SUBJECT_TASK_PROGRESS = 0
QUEST_SUBJECT_TASK_FINISHED = 1
QUEST_SUBJECT_QUEST_STATE = 2
QUEST_SUBJECT_INTERACTION = 3

# QuestChange::Param keys observed on the wire
PARAM_QUEST_ID = 0          # string
PARAM_TASK_INDEX = 1        # int
PARAM_QUEST_STATE = 2       # int
PARAM_TASK_PROGRESS = 3     # int
PARAM_TASK_TARGET = 4       # int
PARAM_TIMESTAMP = 6         # string

# NPCInfo.interactions[].field_08 (docs §1).  0 is the NPC's quest dialog:
# an offer, or the goal dialog once the quest's encounter task is open or
# the quest is finished.  1 marks the NPC as the target of an open encounter
# task; the client walks the player into it and sends Encounter/NPCRequest.
# 2 is a talk-task dialog (its id is echoed in the flags list).
NPC_QUEST_DIALOG = 0
NPC_TASK_TARGET = 1
NPC_TALK_DIALOG = 2
NPC_DELIVERY_DIALOG = 3
NPC_PAY_DIALOG = 4
# World/main-story indication; other quest types use QuestDefinition.quest_type_id.
# Kept as a compatibility constant for captured tutorial records.
QUEST_INDICATION = 6

CLIENT_MESSAGE_ID = 0x8B


@dataclass(frozen=True)
class QuestCommand:
    quest_id: str
    operation: int
    argument: int = 0


@dataclass(frozen=True)
class Talk:
    npc_actor_id: int
    dialog_id: str


@dataclass(frozen=True)
class Deliver:
    npc_actor_id: int
    barter_id: str


@dataclass(frozen=True)
class Pay:
    npc_actor_id: int
    pay_id: str


@dataclass(frozen=True)
class QuestIndicationRequest:
    quest_id: str
    task_index: int


@dataclass(frozen=True)
class QuestUpdate:
    subject: int
    ints: tuple[tuple[int, int], ...] = ()
    strs: tuple[tuple[int, str], ...] = ()

    @classmethod
    def task_progress(
        cls, quest_id: str, task_index: int, progress: int, target: int,
        *, finished: bool,
    ) -> "QuestUpdate":
        return cls(
            QUEST_SUBJECT_TASK_FINISHED if finished else QUEST_SUBJECT_TASK_PROGRESS,
            (
                (PARAM_TASK_INDEX, int(task_index)),
                (PARAM_TASK_PROGRESS, int(progress)),
                (PARAM_TASK_TARGET, int(target)),
            ),
            ((PARAM_QUEST_ID, str(quest_id)),),
        )

    @classmethod
    def quest_state(cls, quest_id: str, state: int, timestamp: str) -> "QuestUpdate":
        return cls(
            QUEST_SUBJECT_QUEST_STATE,
            ((PARAM_QUEST_STATE, int(state)),),
            ((PARAM_QUEST_ID, str(quest_id)), (PARAM_TIMESTAMP, str(timestamp))),
        )

    @classmethod
    def interaction(cls) -> "QuestUpdate":
        return cls(QUEST_SUBJECT_INTERACTION)


@dataclass(frozen=True)
class QuestMonsterIndication:
    actor_id: int
    template_id: str
    position: tuple[float, float, float]


@dataclass(frozen=True)
class QuestMonsterIndicationInfo:
    tick: int
    quest_id: str
    task_index: int
    monsters: tuple[QuestMonsterIndication, ...]


@dataclass(frozen=True)
class QuestTriggerIndication:
    trigger_id: str
    position: tuple[float, float, float]


@dataclass(frozen=True)
class QuestTriggerIndicationInfo:
    quest_id: str
    task_index: int
    triggers: tuple[QuestTriggerIndication, ...]


@dataclass(frozen=True)
class ChapterUnlocked:
    chapter_id: str


# ---------------------------------------------------------------------------
# client -> server
# ---------------------------------------------------------------------------

def _client_reader(payload: bytes, command: int, bit_length: int | None) -> BitReader:
    reader = BitReader(payload, len(payload) * 8 if bit_length is None else bit_length)
    if reader.read_uint8() != CLIENT_MESSAGE_ID:
        raise ValueError("not a client gameplay command")
    actual = int.from_bytes(reader.read_bytes(2), "little")
    if actual != command:
        raise ValueError(f"expected command 0x{command:04x}, got 0x{actual:04x}")
    return reader


def _finish(reader: BitReader, name: str) -> None:
    if reader.remaining >= 8:
        raise ValueError(f"{name} has {reader.remaining} unread bits")


def decode_quest_command(payload: bytes, bit_length: int | None = None) -> QuestCommand:
    reader = _client_reader(payload, QUEST_COMMAND, bit_length)
    value = QuestCommand(reader.read_string(), reader.read_uint8(), reader.read_uint8())
    _finish(reader, "QuestCommand")
    return value


def encode_quest_command(value: QuestCommand) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(CLIENT_MESSAGE_ID)
    writer.write_bytes(QUEST_COMMAND.to_bytes(2, "little"))
    writer.write_string(value.quest_id)
    writer.write_uint8(value.operation)
    writer.write_uint8(value.argument)
    return writer.finish()


def decode_talk(payload: bytes, bit_length: int | None = None) -> Talk:
    reader = _client_reader(payload, TALK_COMMAND, bit_length)
    value = Talk(reader.read_uint32_le(), reader.read_string())
    _finish(reader, "TalkCommand")
    return value


def encode_talk(value: Talk) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(CLIENT_MESSAGE_ID)
    writer.write_bytes(TALK_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(value.npc_actor_id)
    writer.write_string(value.dialog_id)
    return writer.finish()


def decode_deliver(payload: bytes, bit_length: int | None = None) -> Deliver:
    # Native serializer 0x140940548: u32 NPC actor, then a u16-length string.
    reader = _client_reader(payload, DELIVER_COMMAND, bit_length)
    value = Deliver(reader.read_uint32_le(), reader.read_string())
    _finish(reader, "DeliverCommand")
    return value


def encode_deliver(value: Deliver) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(CLIENT_MESSAGE_ID)
    writer.write_bytes(DELIVER_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(value.npc_actor_id)
    writer.write_string(value.barter_id)
    return writer.finish()


def decode_pay(payload: bytes, bit_length: int | None = None) -> Pay:
    reader = _client_reader(payload, PAY_COMMAND, bit_length)
    value = Pay(reader.read_uint32_le(), reader.read_string())
    _finish(reader, "PayCommand")
    return value


def encode_pay(value: Pay) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(CLIENT_MESSAGE_ID)
    writer.write_bytes(PAY_COMMAND.to_bytes(2, "little"))
    writer.write_uint32_le(value.npc_actor_id)
    writer.write_string(value.pay_id)
    return writer.finish()


def decode_quest_indication_request(
    payload: bytes, command: int, bit_length: int | None = None,
) -> QuestIndicationRequest:
    reader = _client_reader(payload, command, bit_length)
    value = QuestIndicationRequest(reader.read_string(), reader.read_uint32_le())
    _finish(reader, "QuestIndicationRequest")
    return value


def encode_quest_indication_request(
    value: QuestIndicationRequest, command: int,
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(CLIENT_MESSAGE_ID)
    writer.write_bytes(int(command).to_bytes(2, "little"))
    writer.write_string(value.quest_id)
    writer.write_uint32_le(value.task_index)
    return writer.finish()


# ---------------------------------------------------------------------------
# server -> client bodies
# ---------------------------------------------------------------------------

def write_quest_update_body(writer: BitWriter, value: QuestUpdate) -> None:
    writer.write_uint8(value.subject)
    writer.write_uint32_le(len(value.ints))
    for key, number in value.ints:
        writer.write_uint8(key)
        writer.write_uint32_le(int(number) & 0xFFFFFFFF)
    writer.write_uint32_le(len(value.strs))
    for key, text in value.strs:
        writer.write_uint8(key)
        writer.write_string(text)


def read_quest_update_body(reader: BitReader) -> QuestUpdate:
    subject = reader.read_uint8()
    ints = tuple(
        (reader.read_uint8(), reader.read_uint32_le())
        for _ in range(reader.read_uint32_le())
    )
    strs = tuple(
        (reader.read_uint8(), reader.read_string())
        for _ in range(reader.read_uint32_le())
    )
    return QuestUpdate(subject, ints, strs)


def write_quest_monster_indication_info_body(
    writer: BitWriter, value: QuestMonsterIndicationInfo,
) -> None:
    writer.write_uint32_le(value.tick)
    writer.write_string(value.quest_id)
    writer.write_uint32_le(value.task_index)
    writer.write_uint32_le(len(value.monsters))
    for monster in value.monsters:
        writer.write_uint32_le(monster.actor_id)
        writer.write_string(monster.template_id)
        for component in monster.position:
            writer.write_float(component)


def read_quest_monster_indication_info_body(
    reader: BitReader,
) -> QuestMonsterIndicationInfo:
    tick = reader.read_uint32_le()
    quest_id = reader.read_string()
    task_index = reader.read_uint32_le()
    monsters = tuple(
        QuestMonsterIndication(
            reader.read_uint32_le(),
            reader.read_string(),
            tuple(reader.read_float() for _ in range(3)),
        )
        for _ in range(reader.read_uint32_le())
    )
    return QuestMonsterIndicationInfo(tick, quest_id, task_index, monsters)


def write_quest_trigger_indication_info_body(
    writer: BitWriter, value: QuestTriggerIndicationInfo,
) -> None:
    writer.write_string(value.quest_id)
    writer.write_uint32_le(value.task_index)
    writer.write_uint32_le(len(value.triggers))
    for trigger in value.triggers:
        writer.write_string(trigger.trigger_id)
        for component in trigger.position:
            writer.write_float(component)


def read_quest_trigger_indication_info_body(
    reader: BitReader,
) -> QuestTriggerIndicationInfo:
    quest_id = reader.read_string()
    task_index = reader.read_uint32_le()
    triggers = tuple(
        QuestTriggerIndication(
            reader.read_string(), tuple(reader.read_float() for _ in range(3)),
        )
        for _ in range(reader.read_uint32_le())
    )
    return QuestTriggerIndicationInfo(quest_id, task_index, triggers)


def write_chapter_unlocked_body(writer: BitWriter, value: ChapterUnlocked) -> None:
    writer.write_string(value.chapter_id)


def read_chapter_unlocked_body(reader: BitReader) -> ChapterUnlocked:
    return ChapterUnlocked(reader.read_string())


QUEST_BODY_WRITERS = {
    QUEST_UPDATE_COMMAND: write_quest_update_body,
    QUEST_MONSTER_INDICATION_INFO_COMMAND: write_quest_monster_indication_info_body,
    QUEST_TRIGGER_INDICATION_INFO_COMMAND: write_quest_trigger_indication_info_body,
    CHAPTER_UNLOCKED_COMMAND: write_chapter_unlocked_body,
}

QUEST_BODY_READERS = {
    QUEST_UPDATE_COMMAND: read_quest_update_body,
    QUEST_MONSTER_INDICATION_INFO_COMMAND: read_quest_monster_indication_info_body,
    QUEST_TRIGGER_INDICATION_INFO_COMMAND: read_quest_trigger_indication_info_body,
    CHAPTER_UNLOCKED_COMMAND: read_chapter_unlocked_body,
}


# Register the quest bodies with the shared batch encoder/decoder so the
# quest commands can travel in the same 0x85 batches as everything else.
_BODY_WRITERS.update(QUEST_BODY_WRITERS)
_BODY_READERS.update(QUEST_BODY_READERS)


def encode_quest_batch(
    commands: tuple[ServerCommand, ...],
) -> tuple[bytes, int]:
    """One ``0x85`` batch of quest and chapter commands with a terminator."""

    return encode_server_command_batch(commands, include_terminator=True)


def decode_quest_batch(payload: bytes, bit_length: int) -> tuple[ServerCommand, ...]:
    """Decode a batch produced by :func:`encode_quest_batch` (tests).

    Quest batches also carry the NPCInfo refresh, so this is the generic
    modeled-batch decoder with the quest readers registered.
    """

    from dso_server.protocol.player_init_protocol import decode_server_command_batch

    return decode_server_command_batch(payload, bit_length)


def quest_log_entry(
    quest_id: str, state: int, finished_mask: int,
    counters: tuple[int, ...], completed_at: str,
) -> QuestLogEntry:
    """A QuestLogInfo record in the retail shape (docs §4)."""

    return QuestLogEntry(
        quest_id=str(quest_id),
        field_08=0,
        state=int(state),
        field_10=int(finished_mask) & 0xFFFF,
        progress=tuple(int(value) for value in counters),
        field_28=0,
        tracking_id=str(completed_at),
    )


def chapter_progress(
    active: tuple[tuple[str, int, int], ...],
    completed: tuple[tuple[str, int, int], ...],
) -> ChapterProgress:
    return ChapterProgress(
        tuple(ChapterProgressEntry(name, int(a), int(b)) for name, a, b in active),
        tuple(ChapterProgressEntry(name, int(a), int(b)) for name, a, b in completed),
    )


__all__ = [
    "CHAPTER_PROGRESS_COMMAND",
    "CHAPTER_UNLOCKED_COMMAND",
    "ENCOUNTER_COMMAND",
    "NPC_QUEST_DIALOG",
    "NPC_TALK_DIALOG",
    "NPC_TASK_TARGET",
    "PARAM_QUEST_ID",
    "PARAM_QUEST_STATE",
    "PARAM_TASK_INDEX",
    "PARAM_TASK_PROGRESS",
    "PARAM_TASK_TARGET",
    "PARAM_TIMESTAMP",
    "QUEST_COMMAND",
    "QUEST_DISABLE_COMMAND",
    "QUEST_INDICATION",
    "QUEST_LOG_INFO_COMMAND",
    "QUEST_MONSTER_INDICATION_INFO_COMMAND",
    "QUEST_MONSTER_INDICATION_REQUEST_COMMAND",
    "QUEST_OPERATION_ACCEPT",
    "QUEST_OPERATION_CANCEL",
    "QUEST_OPERATION_TURN_IN",
    "QUEST_SUBJECT_INTERACTION",
    "QUEST_SUBJECT_QUEST_STATE",
    "QUEST_SUBJECT_TASK_FINISHED",
    "QUEST_SUBJECT_TASK_PROGRESS",
    "QUEST_TRIGGER_INDICATION_INFO_COMMAND",
    "QUEST_TRIGGER_INDICATION_REQUEST_COMMAND",
    "QUEST_UPDATE_COMMAND",
    "TALK_COMMAND",
    "DELIVER_COMMAND",
    "NPC_DELIVERY_DIALOG",
    "NPC_PAY_DIALOG",
    "PAY_COMMAND",
    "Deliver",
    "Pay",
    "decode_pay",
    "encode_pay",
    "decode_deliver",
    "encode_deliver",
    "ChapterUnlocked",
    "QuestCommand",
    "QuestIndicationRequest",
    "QuestMonsterIndication",
    "QuestMonsterIndicationInfo",
    "QuestTriggerIndication",
    "QuestTriggerIndicationInfo",
    "QuestUpdate",
    "Talk",
    "chapter_progress",
    "decode_quest_batch",
    "decode_quest_command",
    "decode_quest_indication_request",
    "decode_talk",
    "encode_quest_batch",
    "encode_quest_command",
    "encode_quest_indication_request",
    "encode_talk",
    "quest_log_entry",
]
