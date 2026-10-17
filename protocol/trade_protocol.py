"""Trade between players: a DSOR addition, absent from every game client.

The game has no player trade (no command, window or text in the 2018 or the
release261 client).  The DSOR client (DSOR_Client crates/proto
src/commands/trade.rs) adds it with these command ids, far above the game's
own (release261 registers up to 368), so no game client ever sends them.  A
game client must never receive ``TradeState`` either: the server only sends
it to a session that announced itself with ``TradeHello``.

Client -> server (0x8B, u16 id, body):

* 0x0F00 TradeHello: u8 version (1).
* 0x0F01 TradeRequest: string target character name.
* 0x0F02 TradeAnswer: u8 accept.
* 0x0F03 TradeOffer: u32 gold, u32 count, count x (u32 item id, u32 amount).
* 0x0F04 TradeReady: u8 ready.
* 0x0F05 TradeCancel: u8 0.

Server -> client (0x84, u16 0x0F10, body, no actor, no terminator):
``TradeState``: u8 phase (0 closed, 1 invited, 2 requested, 3 open, 4 done),
string partner, u32 my gold, u32 count + (u32 id, u32 amount) mine, u8 my
ready, u32 their gold, u32 count + (u32 id, string template, u32 amount, u8
rarity) theirs, u8 their ready, string message.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.social_protocol import _client_reader, _finish_byte_rounded

TRADE_HELLO_COMMAND = 0x0F00
TRADE_REQUEST_COMMAND = 0x0F01
TRADE_ANSWER_COMMAND = 0x0F02
TRADE_OFFER_COMMAND = 0x0F03
TRADE_READY_COMMAND = 0x0F04
TRADE_CANCEL_COMMAND = 0x0F05
TRADE_STATE_COMMAND = 0x0F10
TRADE_CLIENT_COMMANDS = frozenset(range(TRADE_HELLO_COMMAND, TRADE_CANCEL_COMMAND + 1))

PHASE_CLOSED, PHASE_INVITED, PHASE_REQUESTED, PHASE_OPEN, PHASE_DONE = range(5)
# The most items one side may offer (the client's window has 12 places).
TRADE_MAX_ITEMS = 12


@dataclass(frozen=True)
class TradeItem:
    item_id: int
    template_id: str
    amount: int
    rarity: int


@dataclass(frozen=True)
class TradeState:
    phase: int
    partner: str = ""
    my_gold: int = 0
    my_items: tuple[tuple[int, int], ...] = ()
    my_ready: bool = False
    their_gold: int = 0
    their_items: tuple[TradeItem, ...] = ()
    their_ready: bool = False
    message: str = ""


@dataclass(frozen=True)
class TradeOfferRequest:
    gold: int
    items: tuple[tuple[int, int], ...] = field(default_factory=tuple)


def command_id_of(payload: bytes) -> int:
    if len(payload) < 3 or payload[0] != 0x8B:
        raise ValueError("expected a client application command")
    return int.from_bytes(payload[1:3], "little")


def _u8_body(payload: bytes, command_id: int, label: str) -> int:
    reader = _client_reader(payload, command_id)
    value = reader.read_uint8()
    _finish_byte_rounded(reader, label)
    return value


def decode_trade_hello(payload: bytes) -> int:
    return _u8_body(payload, TRADE_HELLO_COMMAND, "TradeHello")


def decode_trade_request(payload: bytes) -> str:
    reader = _client_reader(payload, TRADE_REQUEST_COMMAND)
    target = reader.read_string()
    _finish_byte_rounded(reader, "TradeRequest")
    return target


def decode_trade_answer(payload: bytes) -> bool:
    return _u8_body(payload, TRADE_ANSWER_COMMAND, "TradeAnswer") != 0


def decode_trade_offer(payload: bytes) -> TradeOfferRequest:
    reader = _client_reader(payload, TRADE_OFFER_COMMAND)
    gold = reader.read_uint32_le()
    count = reader.read_uint32_le()
    if count > TRADE_MAX_ITEMS:
        raise ValueError(f"trade offer has {count} items")
    items = tuple((reader.read_uint32_le(), reader.read_uint32_le()) for _ in range(count))
    _finish_byte_rounded(reader, "TradeOffer")
    return TradeOfferRequest(gold, items)


def decode_trade_ready(payload: bytes) -> bool:
    return _u8_body(payload, TRADE_READY_COMMAND, "TradeReady") != 0


def decode_trade_cancel(payload: bytes) -> None:
    _u8_body(payload, TRADE_CANCEL_COMMAND, "TradeCancel")


def encode_trade_state(state: TradeState) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(TRADE_STATE_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(state.phase)
    writer.write_string(state.partner)
    writer.write_uint32_le(state.my_gold)
    writer.write_uint32_le(len(state.my_items))
    for item_id, amount in state.my_items:
        writer.write_uint32_le(item_id)
        writer.write_uint32_le(amount)
    writer.write_uint8(1 if state.my_ready else 0)
    writer.write_uint32_le(state.their_gold)
    writer.write_uint32_le(len(state.their_items))
    for item in state.their_items:
        writer.write_uint32_le(item.item_id)
        writer.write_string(item.template_id)
        writer.write_uint32_le(item.amount)
        writer.write_uint8(item.rarity)
    writer.write_uint8(1 if state.their_ready else 0)
    writer.write_string(state.message)
    return writer.finish()
