"""Client-facing timing: clock sync, loss repair and remote movement relays.

The retail client draws other actors by dead-reckoning each MoveCommand for at
most its duration, holds every later move behind a lost reliable datagram,
and takes any ``1b..83`` message from the server as a clock sync. These pin
the server behaviour that keeps that smooth (2026-10-29 netcode audit).
"""

import struct
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from dso_server.persistence.social_state import SocialStateStore
from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.gameplay_protocol import Move, decode_move_batch
from dso_server.runtime.context import (
    NAK_RETRANSMIT_GRACE_SECONDS,
    RELIABLE_RTO_INITIAL_SECONDS,
    RELIABLE_RTO_MIN_SECONDS,
    server_time,
)
from dso_server.runtime.transport import RakNetServer, Session
from dso_server.services.character_server import CharacterServer
from dso_server.services.presence_movement import relayed_player_move
from dso_server.tests.test_pvp_matchmaking import _FakeSocket, _session
from dso_server.tests.test_achievement_sources import player  # noqa: F401 (fixture)


PAYLOAD = b"\x84\x01\x00" + bytes(8)


def _reliable(session, label="t"):
    session.send_reliable_ordered(PAYLOAD, label=label)
    return max(session.pending_reliable)


def test_client_time_sync_is_not_echoed_and_its_lead_is_recorded():
    server = RakNetServer.__new__(RakNetServer)
    server.service_name = "test"
    sock = _FakeSocket()
    session = Session(("127.0.0.1", 41001), sock)
    estimate = server_time() + 500
    payload = (
        b"\x1b" + struct.pack(">Q", 4_462_872)
        + b"\x83" + struct.pack("<Q", estimate)
    )
    server._on_timestamp(session, payload)
    assert sock.sent == []
    assert 450 <= session.client_clock_lead_ms <= 500


def test_retransmission_timeout_follows_the_measured_round_trip():
    session = Session(("127.0.0.1", 41002), _FakeSocket())
    assert session.retransmission_timeout() == RELIABLE_RTO_INITIAL_SECONDS
    for _ in range(20):
        sequence = _reliable(session)
        session.pending_reliable[sequence]["first_sent_at"] -= 0.02
        session.acknowledge([sequence])
    assert RELIABLE_RTO_MIN_SECONDS <= session.retransmission_timeout() < 0.1


def test_nak_inside_the_grace_is_repaired_when_the_grace_ends():
    session = Session(("127.0.0.1", 41003), _FakeSocket())
    sequence = _reliable(session, "lost")
    retries, _ = session.retransmit([sequence])
    assert retries == []
    assert session.pending_reliable[sequence]["nak_at"] is not None
    # Long before the timed retransmission would fire.
    session.pending_reliable[sequence]["sent_at"] -= NAK_RETRANSMIT_GRACE_SECONDS
    retries, _ = session.due_retransmissions()
    assert [label for _seq, _data, label, _attempt in retries] == ["lost"]
    assert session.nak_repairs == 1


def test_timed_retries_back_off_per_attempt():
    session = Session(("127.0.0.1", 41004), _FakeSocket())
    sequence = _reliable(session)
    item = session.pending_reliable[sequence]
    item["sent_at"] -= RELIABLE_RTO_INITIAL_SECONDS
    (retry,), _ = session.due_retransmissions()
    item = session.pending_reliable[retry[0]]
    item["sent_at"] = time.perf_counter() - RELIABLE_RTO_INITIAL_SECONDS * 1.5
    assert session.due_retransmissions()[0] == []
    item["sent_at"] = time.perf_counter() - RELIABLE_RTO_INITIAL_SECONDS * 2.1
    assert len(session.due_retransmissions()[0]) == 1
    assert session.timed_repairs == 2


@pytest.fixture
def world(tmp_path):
    server = CharacterServer.__new__(CharacterServer)
    server.sessions = {}
    server.social_store = SocialStateStore(tmp_path / "social.json")
    server.static_database = None
    sessions = []
    for character_id in (1, 2):
        session = _session(53000 + character_id, character_id, f"P{character_id}")
        session.gameplay_initialization_sent = True
        session.gameplay_presence_ready = True
        session.gameplay_presence_callback = server._handle_presence_event
        session.moves = []
        session.send_unreliable_sequenced = (
            lambda payload, *, label="", skip_if_busy=False, _s=session:
            (_s.moves.append(payload), b"sent")[1]
        )
        server.sessions[session.addr] = session
        sessions.append(session)
    return server, sessions[0], sessions[1]


def _relayed(peer):
    moves = []
    for payload, _bits in peer.moves:
        assert payload[:3] == b"\x84\x5f\x00"
        moves.append((Move(*struct.unpack("<hhhBBB I H", payload[3:18])),
                       struct.unpack("<I", payload[18:22])[0]))
    return moves


@pytest.mark.parametrize("scalar,duration", [(0, 0), (166, 18)])
def test_standing_players_are_relayed_as_a_hard_stop(world, scalar, duration):
    server, source, peer = world
    source.gameplay_last_move = Move(128, 0, 256, scalar, 64, 64, 1000, 20)
    server._handle_presence_event(source, "move")
    ((move, actor_id),) = _relayed(peer)
    assert actor_id == source.gameplay_remote_actor_id
    assert (move.scalar_byte, move.duration_ticks) == (scalar, duration)


def test_refused_move_correction_reaches_peers(world):
    server, source, peer = world
    stopped = Move(128, 0, 256, 0, 64, 64, 1001, 0)
    server._handle_presence_event(source, ("move_correction", stopped))
    assert _relayed(peer) == [(stopped, source.gameplay_remote_actor_id)]
    assert source.moves == []


def _batched(session):
    """Decode the tick batches a session received as (actor id, Move)."""

    out = []
    for payload, bits in session.moves:
        out.append([
            (command.actor_id, command.value)
            for command in decode_move_batch(payload, bits).commands
        ])
    session.moves.clear()
    return out


def _at(x_metres, *, scalar=0, tick=1000):
    return Move(round(x_metres * 128), 0, 0, scalar, 64, 64, tick, 20)


def test_tick_relay_batches_the_echo_with_every_due_player(world):
    server, viewer, peer = world
    viewer.gameplay_last_move = _at(0.0)
    peer.gameplay_last_move = _at(5.0, scalar=166)
    assert server._publish_presence_movement(2000) == 2
    ((echo, relayed),) = _batched(viewer)
    assert echo == (LOCAL_PLAYER_ACTOR_ID, viewer.gameplay_last_move)
    assert relayed[0] == peer.gameplay_remote_actor_id
    assert relayed[1].duration_ticks == 18
    assert viewer.gameplay_move_echo_deferred
    # Nothing new: neither the echo nor the moving peer is repeated.
    assert server._publish_presence_movement(2001) == 0
    # A near moving peer is relayed on every new move.
    peer.gameplay_last_move = _at(5.2, scalar=166, tick=1001)
    server._publish_presence_movement(2002)
    assert [actor for actor, _ in _batched(viewer)[0]] == [
        peer.gameplay_remote_actor_id
    ]


def test_tick_relay_refreshes_standing_and_far_players_on_a_schedule(world):
    server, viewer, peer = world
    viewer.gameplay_last_move = _at(0.0)
    peer.gameplay_last_move = _at(5.0)
    server._publish_presence_movement(100)
    _batched(viewer)
    sent = []
    for tick in range(101, 108):
        server._publish_presence_movement(tick)
        if any(actor == peer.gameplay_remote_actor_id
               for batch in _batched(viewer) for actor, _ in batch):
            sent.append(tick)
    assert sent == [103, 106]
    # Beyond the activity radius players are still refreshed, more slowly:
    # at once when they move, then every 12 ticks while they stand.
    peer.gameplay_last_move = _at(100.0)
    server._publish_presence_movement(108)
    assert _batched(viewer)
    refreshed = []
    for tick in range(109, 145):
        server._publish_presence_movement(tick)
        if _batched(viewer):
            refreshed.append(tick)
    assert refreshed == [120, 132, 144]
    # Out of relay range: nothing at all.
    peer.gameplay_last_move = _at(200.0)
    for tick in range(145, 170):
        server._publish_presence_movement(tick)
    assert all(
        actor != peer.gameplay_remote_actor_id
        for batch in _batched(viewer) for actor, _ in batch
    )


def test_wrapped_zero_speed_byte_is_relayed_at_the_measured_speed():
    # 0.8 m per tick (20 m/s) along -z, i.e. direction byte 128, but the
    # client sent speed byte 0: (4.0 * 64) & 0xFF.
    previous = Move(0, 0, 0, 0, 128, 64, 1000, 20)
    current = Move(0, 0, round(-0.8 * 128), 0, 128, 64, 1001, 20)
    relayed = relayed_player_move(current, previous)
    assert relayed.scalar_byte == 255
    assert relayed.yaw_byte == 128
    assert relayed.duration_ticks == 18
    # Standing still, or a blink far beyond any run speed, stays a hard stop.
    assert relayed_player_move(previous, previous).duration_ticks == 0
    blink = Move(0, 0, round(-10.0 * 128), 0, 128, 64, 1001, 20)
    assert relayed_player_move(blink, previous).scalar_byte == 0
    # A wrong heading byte is replaced by the measured one.
    assert relayed_player_move(replace(current, yaw_byte=0), previous).yaw_byte == 128


def test_tick_relay_measures_wrapped_moves_between_ticks(world):
    server, viewer, peer = world
    viewer.gameplay_last_move = _at(0.0)
    peer.gameplay_last_move = Move(640, 0, 0, 0, 128, 64, 1000, 20)
    server._publish_presence_movement(10)
    _batched(viewer)
    peer.gameplay_last_move = Move(640, 0, round(-0.8 * 128), 0, 128, 64, 1001, 20)
    server._publish_presence_movement(11)
    ((relayed,),) = _batched(viewer)
    assert relayed[1].scalar_byte == 255


def test_admin_speed_headroom_stays_below_the_wrapping_ratio():
    from dso_server.services.admin_effects import CLIENT_MAX_SENT_SPEED_RATIO

    assert int(CLIENT_MAX_SENT_SPEED_RATIO * 64) & 0xFF == 254


def test_collecting_a_large_unrelated_amount_does_not_rescan_per_unit():
    # Gold pickups feed their amount as the collect count. One pile once
    # re-scanned every active quest thousands of times (~1 s per pickup).
    from dso_server.gameplay.quest_content import (
        QuestProgress, apply_quest_event, load_quest_catalog,
    )
    from dso_server.paths import DEFAULT_QUESTS_PATH

    quest = load_quest_catalog(DEFAULT_QUESTS_PATH).quest(
        "a0002-WayToGrimmagstone-02-n"
    )
    progress = QuestProgress(quest.quest_id)
    started = time.perf_counter()
    result = apply_quest_event(quest, progress, "collect", "money_vc", count=1_000_000)
    assert time.perf_counter() - started < 0.05
    assert result.changes == () and progress.tokens == {}
    # A matching amount still credits every unit, as one event per unit did.
    wood = "quest_a0002_WayToGrimmagstone_02_wood"
    batched = apply_quest_event(quest, progress, "collect", wood, count=3)
    single = QuestProgress(quest.quest_id)
    for _ in range(3):
        apply_quest_event(quest, single, "collect", wood)
    assert batched.changes[0].progress == 3
    assert progress.tokens == single.tokens


def test_kills_are_credited_in_one_batch_with_their_own_difficulty(player):
    # Per kill, achievements took the mutation gate, deep-copied the state
    # and saved synchronously; area fights spent most hit-worker time there.
    server, session, _saves = player
    server._kill_progress_batching = True
    session.gameplay_map_difficulty = 7
    boss = (SimpleNamespace(template_id="boss_grimmag"),)
    server._achievement_on_monsters_killed(session, boss)
    server._achievement_on_monsters_killed(session, boss)
    assert len(session.gameplay_pending_kill_progress) in (0, 2)
    # Leaving the difficulty before the batch runs must not change the credit.
    session.gameplay_map_difficulty = 0
    server._flush_monster_kill_progress(session)
    assert session.gameplay_achievements["pw_difficulty_09_defeatboss"].progress == 2
    assert not session.gameplay_pending_kill_progress
    assert not session.gameplay_kill_progress_flush_scheduled


@pytest.fixture
def progression():
    from dso_server.runtime import progression_queue

    queue = progression_queue.ProgressionQueue(name="test-progression")
    queue.start()
    progression_queue.install(queue)
    try:
        yield queue
    finally:
        progression_queue.install(None)
        queue.stop(timeout=2.0)


def _drain(queue, timeout=2.0):
    deadline = time.perf_counter() + timeout
    while queue.backlog() and time.perf_counter() < deadline:
        time.sleep(0.005)
    time.sleep(0.02)


def test_progression_queue_runs_in_order_and_skips_stale_jobs(progression):
    ran = []
    progression.submit(ran.append, (1,))
    progression.submit(ran.append, (2,), guard=lambda: False)
    progression.submit(lambda: 1 / 0)
    progression.submit(ran.append, (3,))
    _drain(progression)
    assert ran == [1, 3]
    assert (progression.skipped, progression.failed) == (1, 1)
    progression.stop()
    assert not progression.submit(ran.append, (4,))


def test_kills_leave_the_hit_path_and_saves_coalesce(progression):
    from dso_server.services.base import CharacterService

    session = _session(57001, 1, "K")
    session.gameplay_combat_state = object()
    events = []
    session.gameplay_presence_callback = lambda s, event: events.append(event)
    CharacterService._notify_monsters_killed(session, (SimpleNamespace(template_id="x"),))
    _drain(progression)
    assert events and events[0][0] == "monsters_killed"
    # A kill whose player has since left the instance is not credited there.
    events.clear()
    CharacterService._notify_monsters_killed(session, (SimpleNamespace(template_id="x"),))
    session.gameplay_combat_state = object()
    _drain(progression)
    assert events == [] or progression.skipped >= 1

    saves = []
    session.gameplay_persist_callback = lambda s, reason, flush: saves.append((reason, flush))
    # Hold the worker so both requests land before it runs.
    import threading as _threading

    release = _threading.Event()
    progression.submit(release.wait, (2.0,))
    for reason in ("world loot pickup", "quest drop event"):
        CharacterService._request_account_save(session, reason)
    release.set()
    _drain(progression)
    assert saves == [("quest drop event", False)]
    # A map exit flushes its own snapshot; a capture queued before it is dropped.
    session.gameplay_exit_transition_started = True
    CharacterService._request_account_save(session, "world loot pickup")
    _drain(progression)
    assert saves == [("quest drop event", False)]
    # Durable saves never wait in the queue.
    session.gameplay_exit_transition_started = False
    CharacterService._request_account_save(session, "trade", flush=True)
    assert saves[-1] == ("trade", True)


def test_item_rolls_are_identical_from_the_cache():
    from itertools import islice
    from dso_server.gameplay import item_random

    for seed in (7, 4_000_000_000):
        uncached = list(islice(item_random._generate_item_rolls(seed), 70))
        assert list(islice(item_random.deterministic_item_rolls(seed), 70)) == uncached
        assert list(islice(item_random.deterministic_item_rolls(seed), 3)) == uncached[:3]


def test_kill_xp_and_daily_progress_saves_are_coalesced():
    from dso_server.runtime.context import COALESCED_ACCOUNT_SAVE_REASONS

    assert "monster-kill-xp" in COALESCED_ACCOUNT_SAVE_REASONS
    assert "Daily challenge progress and rewards" in COALESCED_ACCOUNT_SAVE_REASONS


def test_meta_notifications_use_their_own_ordering_channel():
    from dso_server.runtime.transport import META_RELIABLE_CHANNEL, reliable_channel_for

    assert META_RELIABLE_CHANNEL == 1
    for label in ("daily-challenge-progress", "daily-challenges", "achievement-kill",
                  "achievement-kill-achievement-kill-split-1/2"):
        assert reliable_channel_for(label) == 1, label
    for label in ("achievement-reward", "daily-challenge-rewards", "monster-skill",
                  "world-loot-spawn", "quest-progress", "reliable", ""):
        assert reliable_channel_for(label) == 0, label


def test_a_lost_meta_message_does_not_hold_back_world_traffic():
    sender = Session(("127.0.0.1", 41101), _FakeSocket())
    receiver = Session(("127.0.0.1", 41102), _FakeSocket())
    sender.send_reliable_ordered(PAYLOAD, label="daily-challenge-progress")   # lost
    sender.send_reliable_ordered(PAYLOAD, label="achievement-kill")
    sender.send_reliable_ordered(PAYLOAD, label="monster-skill")
    frames = [RakNetServer._parse_frames(packet)[0] for packet, _ in sender.sock.sent]
    assert [f["order_channel"] for f in frames] == [1, 1, 0]
    assert [f["order_idx"] for f in frames] == [0, 1, 0]
    assert sender.fb.order_idx == 1
    # Channel 0 delivers at once; channel 1 waits for its own gap only.
    assert RakNetServer._queue_or_deliver_inbound_order_locked(receiver, frames[2])
    assert not RakNetServer._queue_or_deliver_inbound_order_locked(receiver, frames[1])
    assert len(RakNetServer._queue_or_deliver_inbound_order_locked(receiver, frames[0])) == 2


def test_split_meta_messages_keep_one_channel():
    sender = Session(("127.0.0.1", 41103), _FakeSocket())
    big = b"\x85" + bytes(3000)
    sender.send_reliable_ordered_split(big, label_prefix="achievement-kill-achievement-kill")
    frames = [RakNetServer._parse_frames(packet)[0] for packet, _ in sender.sock.sent]
    assert len(frames) >= 3
    assert {f["order_channel"] for f in frames} == {1}
    assert {f["order_idx"] for f in frames} == {0}
    assert sender.fb.order_idx == 0


def test_skipped_client_datagrams_are_nakked_at_once():
    server = RakNetServer.__new__(RakNetServer)
    sock = _FakeSocket()
    session = Session(("127.0.0.1", 41104), sock)

    def datagram(sequence):
        server._handle_frameset(session, b"\x84" + sequence.to_bytes(3, "little"))
        sent = [data for data, _ in sock.sent if data[:1] == b"\xa0"]
        sock.sent.clear()
        return sent

    assert datagram(10) == []
    assert datagram(11) == []
    (nak,) = datagram(14)
    assert RakNetServer._parse_ack_sequences(nak) == [12, 13]
    # The late datagram fills its own gap: no NAK, and no repeat later.
    assert datagram(12) == []
    assert datagram(12) == []
    (single,) = datagram(16)
    assert RakNetServer._parse_ack_sequences(single) == [15]
    assert session.inbound_naks_sent == 2


def test_raw_relay_steps_aside_while_the_tick_relay_runs(world):
    server, source, peer = world
    server._presence_movement_publisher_running = True
    source.gameplay_last_move = _at(1.0, scalar=166)
    server._handle_presence_event(source, "move")
    assert peer.moves == []


def _queue_server():
    server = CharacterServer.__new__(CharacterServer)
    server.is_session_current = lambda _session: True
    session = _session(54001, 1, "Q")
    # Keep queued entries in place: no drain runs during the test.
    session.gameplay_skill_dispatch_scheduled_token = -1
    session.gameplay_skill_dispatch_token_reserved_at = time.perf_counter()
    session.gameplay_skill_dispatch_last_progress_at = time.perf_counter()
    return server, session


def _queued(session):
    return [entry[2] for entry in session.gameplay_skill_dispatch_queue]


def _move_payload(tick):
    return b"\x8b\x5f\x00" + _at(1.0, tick=tick).encode_body()


def test_moves_overtake_queued_actor_requests_only():
    server, session = _queue_server()
    actor_request = b"\x8b\x1c\x00" + bytes(8)
    cast = b"\x8b" + (0x0060).to_bytes(2, "little") + bytes(8)
    server._enqueue_gameplay_skill_command(session, 0x001C, actor_request)
    server._enqueue_gameplay_skill_command(session, 0x001C, actor_request)
    server._enqueue_gameplay_skill_command(session, 0x005F, _move_payload(1))
    assert _queued(session) == [0x005F, 0x001C, 0x001C]
    # A newer move replaces the one waiting ahead of the requests.
    server._enqueue_gameplay_skill_command(session, 0x001C, actor_request)
    server._enqueue_gameplay_skill_command(session, 0x005F, _move_payload(2))
    assert _queued(session) == [0x005F, 0x001C, 0x001C, 0x001C]
    assert session.gameplay_skill_dispatch_queue[0][3] == _move_payload(2)
    # Any other command stays an ordering boundary.
    server._enqueue_gameplay_skill_command(session, 0x0060, cast)
    server._enqueue_gameplay_skill_command(session, 0x001C, actor_request)
    server._enqueue_gameplay_skill_command(session, 0x005F, _move_payload(3))
    assert _queued(session) == [
        0x005F, 0x001C, 0x001C, 0x001C, 0x0060, 0x005F, 0x001C,
    ]


def test_sticker_index_resolves_items_tiers_and_ids_like_the_scan():
    import sqlite3

    from dso_server.gameplay import stickerbook_state
    from dso_server.paths import DEFAULT_STATIC_DB_PATH

    with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True) as database:
        rows = database.execute(
            "SELECT Id, Items, RarityUpgradeItems FROM _Template_StickerbookItem "
            "WHERE IsLive<>0 AND RarityUpgradeItems LIKE '%:%' LIMIT 1"
        ).fetchall()
    assert rows
    sticker_id, items, upgrades = rows[0]
    index = stickerbook_state._sticker_rows_by_identifier(DEFAULT_STATIC_DB_PATH)
    base = stickerbook_state._split(items)[0]
    tier = stickerbook_state._rarity_upgrade_stages(items, upgrades)[1][0]
    assert index[base]["Id"] == index[tier]["Id"] == index[str(sticker_id)]["Id"] == sticker_id
    definition = stickerbook_state.load_sticker_definition(DEFAULT_STATIC_DB_PATH, tier)
    assert definition is None or definition.resource_id == tier
    assert stickerbook_state.load_sticker_definition(DEFAULT_STATIC_DB_PATH, "no-such-sticker") is None


def test_sticker_unlock_counts_the_collection_off_the_input_path(player, progression):
    from unittest.mock import patch

    from dso_server.protocol.gameplay_protocol import (
        AddStickerbookFromInventoryRequest, encode_add_stickerbook_from_inventory,
    )
    from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo

    server, session, _saves = player
    session.gameplay_inventory = InventoryInfo(
        items=[ItemInfo(item_id=51, template_id="pet_goat", amount=1)],
        storage_58={51: 0},
    )
    session.gameplay_next_item_id = 52
    payload, bits = encode_add_stickerbook_from_inventory(AddStickerbookFromInventoryRequest(51))
    session.current_message_bit_length = bits
    import threading as _threading

    release = _threading.Event()
    progression.submit(release.wait, (2.0,))
    with patch.object(CharacterServer, "_persist_gameplay_session", return_value=False):
        server._handle_add_stickerbook_from_inventory(session, payload)
        assert "stickerbook_25_pets_01" not in session.gameplay_achievements
        release.set()
        _drain(progression)
    assert session.gameplay_achievements["stickerbook_25_pets_01"].progress == 1


class _Roll:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


def test_health_globes_drop_from_most_kills_and_always_from_elites():
    from dso_server.protocol.world_loot_protocol import _health_drops

    monster = SimpleNamespace(rank_kind="monster", health_event_loot_lists="0.0,healthglobe_01")
    guardian = SimpleNamespace(rank_kind="guardian", health_event_loot_lists="0.0,healthglobe_20")
    minion = SimpleNamespace(rank_kind="monster", health_event_loot_lists="")
    assert _health_drops(monster, _Roll(0.59)) == ["healthglobe_singleTarget"]
    assert _health_drops(monster, _Roll(0.61)) == []
    assert _health_drops(guardian, _Roll(0.999)) == ["healthglobe_singleTarget"]
    assert _health_drops(minion, _Roll(0.0)) == []


def test_hangmans_pride_heals_its_rank_per_final_blow():
    import threading
    from unittest.mock import patch

    from dso_server.paths import DEFAULT_STATIC_DB_PATH
    from dso_server.protocol.talent_protocol import TalentDatabase

    talents = TalentDatabase(DEFAULT_STATIC_DB_PATH)
    trigger = talents.status_effect_wire_id("trait_wisdom_all_hpPerKill_trigger")
    stack = talents.status_effect_wire_id("trait_wisdom_all_hpPerKill_stackeffect")
    health = {7: [1000, 20000]}

    def heal(actor_id, amount):
        previous, maximum = health[actor_id]
        health[actor_id][0] = min(maximum, previous + amount)
        return health[actor_id][0], maximum, health[actor_id][0] - previous

    combat = SimpleNamespace(
        encounter=SimpleNamespace(static_database_path=DEFAULT_STATIC_DB_PATH),
        lock=threading.Lock(), player_actor_id=7, heal_player_amount=heal,
    )
    session = SimpleNamespace(
        gameplay_combat_state=combat, gameplay_character=SimpleNamespace(character_id=1),
        gameplay_inventory=SimpleNamespace(health_points=1000.0),
        gameplay_presence_callback=None, flow_lock=threading.Lock(),
        gameplay_chase_server_tick=100, gameplay_last_heal_server_tick=0,
    )
    # Rank 10: the base trigger plus nine stacks of +165.
    sources = (SimpleNamespace(effect_id=trigger, field_18=0),
               SimpleNamespace(effect_id=stack, field_18=8))
    kills = (SimpleNamespace(killed=True), SimpleNamespace(killed=False),
             SimpleNamespace(killed=True))
    with patch.object(CharacterServer, "_active_skill_modifier_sources", return_value=sources), \
            patch.object(CharacterServer, "_send_player_heal_transition") as sent:
        assert CharacterServer._apply_player_kill_healing(session, kills, label="t") == 2 * 1650
        assert CharacterServer._apply_player_kill_healing(session, kills[1:2], label="t") == 0
    assert health[7][0] == 1000 + 3300 and session.gameplay_inventory.health_points == 4300.0
    assert sent.call_args.kwargs["recovered"] == 3300
    with patch.object(CharacterServer, "_active_skill_modifier_sources", return_value=()):
        assert CharacterServer._apply_player_kill_healing(session, kills, label="t") == 0


def test_skill_geometry_is_reused_for_frozen_sources_but_not_mutable_ones():
    from dso_server.gameplay.player_combat_stats import CombatModifier
    from dso_server.tests.test_local_combat_state import LocalCombatStateTests

    fixture = LocalCombatStateTests()
    fixture.setUp()
    try:
        state = fixture.state
        geometry = state._load_player_skill_geometry(1849)
        scope = geometry.resource_id
        near = (CombatModifier("SkillRange", ("2.0", "absolute", scope)),)
        far = (CombatModifier("SkillRange", ("4.0", "absolute", scope)),)
        first = state._modified_player_skill_geometry(geometry, near)
        assert first != geometry
        assert state._modified_player_skill_geometry(geometry, near) is first
        assert len(state._modified_player_geometry_identity_cache) == 1
        assert state._modified_player_skill_geometry(geometry, far) != first
        # A mutable carrier is keyed by content every time, never by identity.
        mutable = SimpleNamespace(kind="SkillRange", arguments=["2.0", "absolute", scope])
        assert state._modified_player_skill_geometry(geometry, (mutable,)) == first
        mutable.arguments[0] = "4.0"
        assert state._modified_player_skill_geometry(geometry, (mutable,)) == (
            state._modified_player_skill_geometry(geometry, far)
        )
        assert len(state._modified_player_geometry_identity_cache) == 2
    finally:
        fixture.doCleanups()


def test_conditional_sources_follow_low_health_through_the_memo():
    from unittest.mock import patch

    from dso_server.gameplay.player_combat_stats import CombatModifier

    plain = CombatModifier("SkillDamage", ("0.1", "relative", "all"))
    gated = CombatModifier("SkillDamage", ("0.2", "relative", "all", "ON:HitLowHealth"))
    sources = (plain, gated)
    for low, expected in ((False, (plain,)), (True, (plain, gated)), (False, (plain,))):
        with patch.object(CharacterServer, "_player_is_low_health", return_value=low):
            for _repeat in range(2):
                assert CharacterServer._conditional_skill_modifier_sources(None, sources) == expected


def test_combat_signatures_are_memoized_only_for_frozen_sources():
    from dso_server.gameplay import player_combat_stats

    frozen = player_combat_stats.CombatModifier("SkillDamage", ("0.1", "relative", "all"))
    first = player_combat_stats._combat_modifier_signature(frozen)
    assert player_combat_stats._combat_modifier_signature(frozen) is first
    mutable = SimpleNamespace(kind="SkillDamage", arguments=["0.1", "relative", "all"])
    before = player_combat_stats._combat_modifier_signature(mutable)
    mutable.arguments[0] = "0.3"
    assert player_combat_stats._combat_modifier_signature(mutable) != before


@pytest.fixture
def geometry_state():
    from dataclasses import replace as replace_entry

    from dso_server.paths import DEFAULT_STATIC_DB_PATH
    from dso_server.protocol.talent_protocol import TalentDatabase
    from dso_server.services.combat_resolution import _load_attack_speed_bonus_effects
    from dso_server.tests.test_local_combat_state import LocalCombatStateTests

    fixture = LocalCombatStateTests()
    fixture.setUp()
    state = fixture.state
    talents = TalentDatabase(DEFAULT_STATIC_DB_PATH)
    template = talents.status_effect_entries(
        talents.definition("trait_wisdom_all_hpPerKill"), target_rank=1,
        actor_id=1, first_instance_id=1, server_tick=0,
    )[0]
    instances = iter(range(10, 10 ** 6))

    def entry(effect, stack_index):
        effect_id = state._status_effect_ids[effect] if isinstance(effect, str) else effect
        return replace_entry(template, effect_id=int(effect_id),
                             field_04=next(instances), field_18=stack_index)

    stack, buff = _load_attack_speed_bonus_effects(str(DEFAULT_STATIC_DB_PATH))
    yield SimpleNamespace(state=state, geometry=state._load_player_skill_geometry(1849),
                          entry=entry, stack=stack.effect_id, buff=buff.effect_id)
    fixture.doCleanups()


def test_stacks_that_cannot_reach_geometry_reuse_it_but_relevant_stacks_do_not(geometry_state):
    from dso_server.gameplay.local_combat_state import _STATUS_EXPANSION_WINDOW

    state, geometry, entry = geometry_state.state, geometry_state.geometry, geometry_state.entry
    cache = state._modified_player_geometry_cache

    def resolve(*sources):
        return state._modified_player_skill_geometry(geometry, sources)

    # The attack-speed buff carries only Damage; its stack reaches no geometry.
    assert state._geometry_effect_role(geometry_state.buff) == "inert"
    first = resolve(entry(geometry_state.buff, 0))
    computed = len(cache)
    assert resolve(entry(geometry_state.buff, 7)) is first and len(cache) == computed
    # Every accepted cast at 12.5 attacks/s adds five attack-speed stacks. The
    # stack adds its buff and remover at chance 1.0 with zero values, which
    # min(1, chance * stack) and value * stack leave unchanged.
    assert state._geometry_effect_role(geometry_state.stack) == "stack-invariant"
    filler = tuple(entry(geometry_state.buff, 0) for _ in range(_STATUS_EXPANSION_WINDOW))
    at_four = resolve(*filler, entry(geometry_state.stack, 4))
    computed = len(cache)
    assert resolve(*filler, entry(geometry_state.stack, 9)) is at_four
    assert len(cache) == computed
    cache.clear()
    state._modified_player_geometry_identity_cache.clear()
    assert resolve(*filler, entry(geometry_state.stack, 9)) == at_four
    # A short list keeps the stack in its key: the stack still de-duplicates
    # the expansion, whose window then reaches the appended programs.
    resolve(entry(geometry_state.stack, 4))
    resolve(entry(geometry_state.stack, 9))
    assert len(cache) == 3
    # A stack that scales SkillDamage recomputes and changes the result.
    assert state._geometry_effect_role(state._status_effect_ids["skill_enragingleap_lvl"]) == "stack"
    first = resolve(*filler, entry("skill_enragingleap_lvl", 0))
    assert resolve(*filler, entry("skill_enragingleap_lvl", 4)) != first


def test_geometry_miss_log_names_the_source_that_changed(geometry_state, monkeypatch, capsys):
    from dso_server.gameplay import local_combat_state

    state, geometry, entry = geometry_state.state, geometry_state.geometry, geometry_state.entry
    state._modified_player_skill_geometry(geometry, (entry("skill_enragingleap_lvl", 0),))
    monkeypatch.setattr(local_combat_state._GEOMETRY_MISS_LOG, "previous", {})
    monkeypatch.setattr(local_combat_state, "GEOMETRY_MISS_LOG", True)
    first = state._modified_player_skill_geometry(geometry, (entry("skill_enragingleap_lvl", 4),))
    # An inert buff adds no geometry instruction: the instruction cache answers.
    with_buff = state._modified_player_skill_geometry(
        geometry, (entry("skill_enragingleap_lvl", 4), entry(geometry_state.buff, 0)))
    assert with_buff is first
    state._modified_player_skill_geometry(geometry, (entry("skill_enragingleap_lvl", 9),))
    lines = [line for line in capsys.readouterr().out.splitlines() if "[geometry-miss]" in line]
    assert len(lines) == 2 and all("enragingleap" in line for line in lines)
    assert lines[0].endswith("first resolve of this skill")
    assert lines[1].endswith("skill_enragingleap_lvl stack 5->10")
    monkeypatch.setattr(local_combat_state, "GEOMETRY_MISS_LOG", False)
    state._modified_player_skill_geometry(geometry, (entry("skill_enragingleap_lvl", 12),))
    assert "[geometry-miss]" not in capsys.readouterr().out


def test_every_kind_the_geometry_resolver_reads_keeps_a_source_in_its_key():
    import inspect
    import re

    from dso_server.gameplay import local_combat_state

    body = inspect.getsource(local_combat_state.LocalCombatState._modified_player_skill_geometry)
    read = set(re.findall(r'normalized_kind\s*[!=]=\s*"([a-z]+)"', body))
    read |= set(re.findall(r'source_kind\.casefold\(\)\s*==\s*"([a-z]+)"', body))
    numeric = re.search(r"numeric_kinds = \{(.*?)\}", body, re.S).group(1)
    read |= set(re.findall(r'"([a-z]+)"', numeric))
    read |= {"actorvariable"}        # timing_actor_variables (cooldown operands)
    assert len(read) > 15
    assert read <= local_combat_state._GEOMETRY_MODIFIER_KINDS, read - local_combat_state._GEOMETRY_MODIFIER_KINDS


def test_procs_that_add_no_geometry_instruction_reuse_it_wherever_they_sit(geometry_state):
    from dso_server.gameplay.local_combat_state import _STATUS_EXPANSION_WINDOW

    state, geometry, entry = geometry_state.state, geometry_state.geometry, geometry_state.entry

    def resolve(*sources):
        return state._modified_player_skill_geometry(geometry, sources)

    # On-hit procs come and go and a re-applied buff moves to the end of the
    # source list; past the expansion window an inert one adds nothing the
    # geometry loop reads, wherever it sits.
    filler = tuple(entry(geometry_state.buff, 0) for _ in range(_STATUS_EXPANSION_WINDOW))
    first = resolve(*filler, entry(geometry_state.stack, 4), entry("talent_bloodmage_self_damage", 0))
    assert resolve(*filler, entry("talent_bloodmage_self_damage", 0), entry(geometry_state.stack, 9)) is first
    assert resolve(*filler, entry(geometry_state.stack, 14)) is first
    state._modified_player_geometry_cache.clear()
    state._modified_player_geometry_identity_cache.clear()
    state._geometry_instruction_cache.clear()
    assert resolve(*filler, entry(geometry_state.stack, 14)) == first
    # A proc that scales a skill modifier still changes it.
    assert resolve(*filler, entry(geometry_state.stack, 4), entry("skill_enragingleap_lvl", 3)) != first


def test_equal_item_programs_stay_one_object_across_stats_misses():
    import json

    from dso_server.gameplay.player_combat_stats import CombatModifier, calculate_player_combat_stats
    from dso_server.paths import DEFAULT_STATIC_DB_PATH
    from dso_server.tools.audit_character_stats import DEFAULT_FIXTURE, character_sources

    inventory, sources, _sockets = character_sources(json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8")))

    def stats(*extra):
        return calculate_player_combat_stats(
            DEFAULT_STATIC_DB_PATH, class_name="mage", level=100,
            inventory=inventory, modifier_sources=tuple(sources) + extra,
        )

    first = stats()
    # A buff change misses the stats cache and materializes every item again.
    second = stats(CombatModifier("Damage", ("0.01", "relative", "Min", "Max")))
    assert second is not first
    pairs = list(zip(first.skill_modifier_sources, second.skill_modifier_sources))
    equal = [a for a, b in pairs if a == b]
    assert len(equal) > 100
    assert all(a is b for a, b in pairs if a == b)


def test_resident_revision_keys_are_memoized_but_the_revision_is_read_live(tmp_path, monkeypatch):
    from dso_server.runtime import resident_content

    path = str(tmp_path / "static.db4")
    key = resident_content._key(path)
    monkeypatch.setitem(resident_content._stats, key, SimpleNamespace(st_mtime_ns=1, st_size=5))
    first = resident_content.revision(path)
    assert resident_content._REVISION_KEYS[path] == key
    # A reload replaces the stat; the memo holds only the normalised key.
    monkeypatch.setitem(resident_content._stats, key, SimpleNamespace(st_mtime_ns=2, st_size=5))
    assert resident_content.revision(path) != first
    monkeypatch.delitem(resident_content._stats, key)
    assert resident_content.revision(path) is None


def test_skills_reading_the_same_actor_variables_share_their_resolution(geometry_state):
    state, geometry, entry = geometry_state.state, geometry_state.geometry, geometry_state.entry
    state._modified_player_skill_geometry(geometry, (entry("skill_enragingleap_lvl", 0),))
    computed = dict(state._geometry_actor_variables)
    # A different stack recomputes the geometry but not its (absent) variables.
    state._modified_player_skill_geometry(geometry, (entry("skill_enragingleap_lvl", 4),))
    assert state._geometry_actor_variables.keys() == computed.keys()
    assert all(state._geometry_actor_variables[key] is value for key, value in computed.items())
