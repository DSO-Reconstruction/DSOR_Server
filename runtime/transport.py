"""RakNet framing, sessions, retransmission, and login transport."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, MutableMapping
from typing import TypeVar
import traceback

from dso_server.runtime.context import *
from dso_server.runtime.gameplay_scheduler import (
    GameplayDeadlineHandle,
    GameplayDeadlineScheduler,
)
from dso_server.runtime.concurrency import (
    InstrumentedLock,
    SESSION_FLOW_DOMAIN,
    assert_no_state_locks_held,
)


# Direct Session construction is useful in protocol tests and small tooling.
# Share one lazy scheduler there as well; otherwise every short-lived test
# session would retain its own pair of idle daemon workers.
_STANDALONE_GAMEPLAY_DEADLINE_SCHEDULER = GameplayDeadlineScheduler(
    name="standalone-gameplay-deadline",
    worker_count=2,
)


_SessionValue = TypeVar("_SessionValue")


_U24_MASK = (1 << 24) - 1
_U24_HALF_RANGE = 1 << 23
_INBOUND_RELIABLE_WINDOW = 8192
_INBOUND_DATAGRAM_HISTORY = 8192
_INBOUND_ORDER_WINDOW = 4096
_INBOUND_SPLIT_LIMIT = 64
_INBOUND_SPLIT_FRAGMENT_LIMIT = 4096
# A frameset header (4 bytes) carries reliable-ordered frames, each behind a
# 10-byte header.
_FRAMESET_HEADER_BYTES = 4
_RELIABLE_ORDERED_HEADER_BYTES = 10
_UNSPLIT_RELIABLE_FRAMING_BYTES = (
    _FRAMESET_HEADER_BYTES + _RELIABLE_ORDERED_HEADER_BYTES
)
# The installed client drops an oversized datagram without acknowledging it
# (docs/GAME_EVENTS.md: a 2,703-byte visibility batch stalled every later
# ordered reply). Split fragments are 1,232 bytes on the wire -- FRAG_SIZE of
# data plus 24 bytes of headers -- and unsplit reliable datagrams are held to
# the same size.
RELIABLE_DATAGRAM_LIMIT = FRAG_SIZE + 24


class SessionRegistry(MutableMapping[tuple, _SessionValue]):
    """Lock-protected identity registry with snapshot iteration.

    Network, retransmission, autosave, and shutdown threads all observe this
    table. Returning snapshots from the collection methods prevents callers
    from iterating a live ``dict`` while another thread replaces or detaches a
    session. Factories and lifecycle side effects deliberately run outside the
    registry lock.
    """

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._entries: dict[tuple, _SessionValue] = {}

    def __getitem__(self, key: tuple) -> _SessionValue:
        with self._lock:
            return self._entries[key]

    def __setitem__(self, key: tuple, value: _SessionValue) -> None:
        with self._lock:
            self._entries[key] = value

    def __delitem__(self, key: tuple) -> None:
        with self._lock:
            del self._entries[key]

    def __iter__(self) -> Iterator[tuple]:
        return iter(self.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._entries

    def get(self, key: tuple, default=None):
        with self._lock:
            return self._entries.get(key, default)

    def pop(self, key: tuple, default=...):
        with self._lock:
            if default is ...:
                return self._entries.pop(key)
            return self._entries.pop(key, default)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def keys(self) -> tuple[tuple, ...]:
        with self._lock:
            return tuple(self._entries.keys())

    def values(self) -> tuple[_SessionValue, ...]:
        with self._lock:
            return tuple(self._entries.values())

    def items(self) -> tuple[tuple[tuple, _SessionValue], ...]:
        with self._lock:
            return tuple(self._entries.items())

    def snapshot(self) -> tuple[_SessionValue, ...]:
        return self.values()

    def get_or_create(
        self,
        key: tuple,
        factory: Callable[[], _SessionValue],
    ) -> _SessionValue:
        # Session construction must never happen while the registry is held.
        with self._lock:
            current = self._entries.get(key)
        if current is not None:
            return current
        candidate = factory()
        with self._lock:
            return self._entries.setdefault(key, candidate)

    def replace_if_current(
        self,
        key: tuple,
        expected: _SessionValue,
        replacement: _SessionValue,
    ) -> bool:
        with self._lock:
            if self._entries.get(key) is not expected:
                return False
            self._entries[key] = replacement
            return True

    def detach(self, key: tuple, expected: _SessionValue) -> bool:
        with self._lock:
            if self._entries.get(key) is not expected:
                return False
            del self._entries[key]
            return True

    def is_current(self, key: tuple, expected: _SessionValue) -> bool:
        with self._lock:
            return self._entries.get(key) is expected

    def drain(self) -> tuple[_SessionValue, ...]:
        with self._lock:
            sessions = tuple(self._entries.values())
            self._entries.clear()
            return sessions


class FrameBuilder:
    def __init__(self):
        self.frame_seq    = 0
        self.reliable_idx = 0
        self.sequence_idx = 0
        self.order_idx    = 0
        self.compound_id  = 0
        self.lock = threading.Lock()

    def _seq(self):
        s = self.frame_seq; self.frame_seq += 1; return s

    def _rel(self):
        r = self.reliable_idx; self.reliable_idx += 1; return r

    def _ord(self):
        o = self.order_idx; self.order_idx += 1
        # RakNet restarts the sequencing index whenever it assigns an ordering
        # index, and the official server's captures follow it: (order 8, seq
        # 1..8), (9, 0), (10, 0..11). The client sorts frames buffered behind
        # a missing ordered frame by ``hole * 2**20 + sequencing index``. A
        # counter that never restarts passes 2**20 after several hours on one
        # map; the next lost ordered frame then leaves a stale sequenced frame
        # at the head of that heap, and ordered delivery stops for good while
        # every datagram is still acknowledged.
        self.sequence_idx = 0
        return o

    @staticmethod
    def _payload_spec(payload):
        if (
                isinstance(payload, tuple)
                and len(payload) == 2
                and isinstance(payload[0], bytes)
                and isinstance(payload[1], int)):
            data, bit_length = payload
        else:
            data = payload
            bit_length = len(data) * 8
        if not isinstance(data, bytes):
            raise TypeError('RakNet payload must be bytes')
        if bit_length < 0 or (bit_length + 7) // 8 != len(data):
            raise ValueError(
                f'bit length {bit_length} does not match {len(data)} payload bytes'
            )
        return data, bit_length

    def _frame_unreliable(self, payload) -> bytes:
        payload, bit_length = self._payload_spec(payload)
        return b'\x00' + struct.pack('>H', bit_length) + payload

    @staticmethod
    def _frame_reliable_ordered_with_indices(
            payload: bytes, reliable_idx: int, order_idx: int,
            bit_length: int | None = None) -> bytes:
        if bit_length is None:
            bit_length = len(payload) * 8
        return (b'\x60'
                + struct.pack('>H', bit_length)
                + struct.pack('<I', reliable_idx)[:3]
                + struct.pack('<I', order_idx)[:3]
                + b'\x00'
                + payload)

    def _frame_reliable_ordered(self, payload) -> bytes:
        payload, bit_length = self._payload_spec(payload)
        with self.lock:
            reliable_idx = self._rel()
            order_idx = self._ord()
        return self._frame_reliable_ordered_with_indices(
            payload, reliable_idx, order_idx, bit_length)

    def _frameset(self, *frames: bytes) -> bytes:
        with self.lock:
            seq = self._seq()
        return b'\x84' + struct.pack('<I', seq)[:3] + b''.join(frames)

    def unreliable(self, payload) -> bytes:
        return self._frameset(self._frame_unreliable(payload))

    def unreliable_sequenced(self, payload) -> bytes:
        """Build RakNet reliability-1 traffic without consuming order index.

        The captured character service sends its timestamped 0x83 message on
        the current ordering index, then uses that same index for the reliable
        split character response.
        """
        payload, bit_length = self._payload_spec(payload)
        with self.lock:
            sequence_idx = self.sequence_idx
            self.sequence_idx += 1
            order_idx = self.order_idx
            frame_seq = self._seq()
        frame = (
            b'\x20'
            + struct.pack('>H', bit_length)
            + struct.pack('<I', sequence_idx)[:3]
            + struct.pack('<I', order_idx)[:3]
            + b'\x00'
            + payload
        )
        return b'\x84' + struct.pack('<I', frame_seq)[:3] + frame

    def reliable_ordered(self, payload) -> bytes:
        return self._frameset(self._frame_reliable_ordered(payload))

    def retransmit_frameset(self, packet: bytes) -> tuple[bytes, int]:
        """Put reliable frames in a fresh RakNet datagram.

        RakNet acknowledges datagram sequence numbers, but deduplicates and
        orders the encapsulated frames by their reliable/order indexes.  A
        retry must therefore retain everything after the four-byte frameset
        header while allocating a new datagram sequence number.  Re-sending
        the original header can be discarded as an already observed datagram
        and leaves a later ordered command, such as SwitchMap, blocked.
        """

        if len(packet) < 4 or not 0x80 <= packet[0] <= 0x8F:
            raise ValueError("retransmission requires a RakNet frameset")
        with self.lock:
            sequence = self._seq()
        return (
            packet[:1]
            + struct.pack('<I', sequence)[:3]
            + packet[4:],
            sequence,
        )

    def multi_reliable_ordered(self, *payloads) -> bytes:
        return self._frameset(*[self._frame_reliable_ordered(p) for p in payloads])

    def wire_first_delivery_second(self, wire_first, delivery_first) -> bytes:
        """Reproduce the login server's deliberately reversed order indexes.

        The auth-complete frame is physically first in the datagram, but its
        order index is one higher. RakNet therefore delivers the redirect to
        the application before auth-complete, exactly as in session 57601.
        """
        wire_payload, wire_bits = self._payload_spec(wire_first)
        delivery_payload, delivery_bits = self._payload_spec(delivery_first)
        with self.lock:
            wire_reliable = self._rel()
            delivery_reliable = self._rel()
            delivery_order = self._ord()
            wire_order = self._ord()
            seq = self._seq()

        wire_frame = self._frame_reliable_ordered_with_indices(
            wire_payload, wire_reliable, wire_order, wire_bits)
        delivery_frame = self._frame_reliable_ordered_with_indices(
            delivery_payload, delivery_reliable, delivery_order, delivery_bits)
        return b'\x84' + struct.pack('<I', seq)[:3] + wire_frame + delivery_frame

    def send_split(
            self, big_payload: bytes, sock, addr, trailing_payloads=(),
            pace_seconds=0, send_callback=None, label_prefix='split',
            total_bit_length=None):
        chunks = [big_payload[i:i+FRAG_SIZE] for i in range(0, len(big_payload), FRAG_SIZE)]
        self.send_split_parts(
            chunks,
            sock,
            addr,
            trailing_payloads,
            pace_seconds,
            send_callback,
            label_prefix,
            total_bit_length,
        )

    def send_split_parts(
            self, chunks, sock, addr, trailing_payloads=(), pace_seconds=0,
            send_callback=None, label_prefix='split', total_bit_length=None):
        chunks = list(chunks)
        # Validate before allocating: once the fragments are on the wire, a
        # trailer that fails to frame leaves the message incomplete, and its
        # ordering index then holds the client's stream forever.
        trailing_specs = tuple(
            self._payload_spec(payload) for payload in trailing_payloads
        )
        for trailing_payload, _trailing_bits in trailing_specs:
            if (
                len(trailing_payload) + _UNSPLIT_RELIABLE_FRAMING_BYTES
                > RELIABLE_DATAGRAM_LIMIT
            ):
                raise ValueError(
                    f'{len(trailing_payload)}-byte trailing payload exceeds '
                    f'the {RELIABLE_DATAGRAM_LIMIT}-byte datagram budget'
                )
        if not chunks:
            return
        payload_bytes = sum(len(chunk) for chunk in chunks)
        if total_bit_length is None:
            total_bit_length = payload_bytes * 8
        if (total_bit_length + 7) // 8 != payload_bytes:
            raise ValueError(
                f'bit length {total_bit_length} does not match split payload '
                f'of {payload_bytes} bytes'
            )
        total  = len(chunks)
        with self.lock:
            cid     = self.compound_id
            self.compound_id = (self.compound_id + 1) & 0xffff
            ord_idx = struct.pack('<I', self._ord())[:3]

        for idx, chunk in enumerate(chunks):
            preceding_bytes = sum(len(part) for part in chunks[:idx])
            chunk_bits = min(
                len(chunk) * 8,
                total_bit_length - preceding_bytes * 8,
            )
            with self.lock:
                seq     = self._seq()
                rel_idx = struct.pack('<I', self._rel())[:3]

            frame = (b'\x70'                           # reliable ordered | split
                     + struct.pack('>H', chunk_bits)
                     + rel_idx
                     + ord_idx
                     + b'\x00'
                     + struct.pack('>I', total)         # compound_size
                     + struct.pack('>H', cid)           # compound_id
                     + struct.pack('>I', idx)           # fragment index
                     + chunk)

            datagrams = [b'\x84' + struct.pack('<I', seq)[:3] + frame]
            labels = [f'{label_prefix}-{idx + 1}/{total}']
            # The official 2192 trace bundles cmd 0x001b after the final
            # character-selection fragment in the same FrameSet/datagram.
            # Only while it fits: a full final fragment plus a trailer is a
            # datagram the client drops without acknowledging. RakNet starts
            # a new datagram there, and so does this.
            if idx == total - 1:
                for trailing_payload, trailing_bits in trailing_specs:
                    with self.lock:
                        trailing_rel = self._rel()
                        trailing_order = self._ord()
                    trailing_frame = self._frame_reliable_ordered_with_indices(
                        trailing_payload, trailing_rel, trailing_order,
                        trailing_bits)
                    if (
                        len(datagrams[-1]) + len(trailing_frame)
                        > RELIABLE_DATAGRAM_LIMIT
                    ):
                        with self.lock:
                            trailing_seq = self._seq()
                        datagrams.append(
                            b'\x84' + struct.pack('<I', trailing_seq)[:3]
                        )
                        labels.append(f'{label_prefix}-trailing')
                    datagrams[-1] += trailing_frame
            for pkt, label in zip(datagrams, labels):
                if send_callback:
                    send_callback(pkt, label)
                else:
                    try:
                        sock.sendto(pkt, addr)
                    except OSError:
                        return
            if pace_seconds and idx != total - 1:
                time.sleep(pace_seconds)

class _OutboundLock:
    """One session's send serialization, with contention reporting.

    Ordering-index allocation, reliable registration and the socket write must
    be one transaction, so this lock is unavoidable. What is avoidable is
    being unable to see it: the map simulation thread acquires every viewer's
    outbound lock in turn, so one client holding its own lock for a long time
    stalls monster updates for everybody on that map. That failure is silent
    with a plain ``threading.Lock`` -- no owner, no timeout, no diagnostic.
    """

    __slots__ = (
        "_lock", "_addr", "_holder", "_acquired_at",
        "_last_wait_report_at", "_last_hold_report_at",
    )

    def __init__(self, addr) -> None:
        self._lock = threading.Lock()
        self._addr = addr
        self._holder = None
        self._acquired_at = 0.0
        # Rate limited separately: a long hold and the wait it caused are one
        # event seen from two sides, and the wait names the stalled thread.
        # Sharing one limiter lets the hold report hide the victim.
        self._last_wait_report_at = 0.0
        self._last_hold_report_at = 0.0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(False):
            self._note_acquired()
            return True
        if not blocking:
            return False
        holder = self._holder
        started = time.perf_counter()
        acquired = self._lock.acquire(True, timeout)
        if not acquired:
            return False
        waited = time.perf_counter() - started
        self._note_acquired()
        if waited >= OUTBOUND_CONTENTION_LOG_SECONDS:
            self._last_wait_report_at = self._report(
                self._last_wait_report_at,
                f"waited {waited * 1000.0:.0f}ms for the outbound lock of "
                f"{self._addr}; previous holder={holder!r}",
            )
        return True

    def release(self) -> None:
        holder = self._holder
        held = time.perf_counter() - self._acquired_at
        self._holder = None
        self._lock.release()
        if held >= OUTBOUND_CONTENTION_LOG_SECONDS:
            self._last_hold_report_at = self._report(
                self._last_hold_report_at,
                f"held the outbound lock of {self._addr} for "
                f"{held * 1000.0:.0f}ms on {holder!r}",
            )

    def _note_acquired(self) -> None:
        self._holder = threading.current_thread().name
        self._acquired_at = time.perf_counter()

    def _report(self, last_reported_at: float, message: str) -> float:
        """Emit at most one report of this kind per second; return the clock."""

        now = time.perf_counter()
        if now - last_reported_at < 1.0:
            return last_reported_at
        print(f"[RakNet] {threading.current_thread().name!r} {message}")
        return now

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback_object) -> None:
        self.release()


class Session:
    @property
    def gameplay_currency_state(self):
        value = getattr(self, "_gameplay_currency_state", None)
        store = getattr(self, "gameplay_currency_store", None)
        character = getattr(self, "gameplay_character", None)
        if value is not None and store is not None and character is not None:
            return store.currency_for_character(character.character_id, value)
        return value

    @gameplay_currency_state.setter
    def gameplay_currency_state(self, value):
        store = getattr(self, "gameplay_currency_store", None)
        character = getattr(self, "gameplay_character", None)
        if value is not None and store is not None and character is not None:
            value = store.commit_currency(character.character_id, value)
        self._gameplay_currency_state = value

    # Bumped on every ``gameplay_map`` assignment, from anywhere. The presence
    # layer keeps a map -> sessions index so a move broadcast does not scan
    # every session on the server; it uses this counter to know when that index
    # went stale. Hooking the attribute rather than each assignment site means
    # the index cannot desync -- including from code (and tests) that set
    # ``gameplay_map`` directly.
    map_generation = 0

    @property
    def current_message_bit_length(self):
        """Exact bit length for the command executing on the current thread.

        Gameplay skill ingress is drained by deadline workers while the UDP
        receive thread continues decoding frames. A thread-local value keeps
        one worker from borrowing or overwriting another frame's bit length.
        """

        return getattr(self._message_context, "bit_length", None)

    @current_message_bit_length.setter
    def current_message_bit_length(self, value) -> None:
        self._message_context.bit_length = value

    @property
    def gameplay_map(self):
        return self._gameplay_map

    @gameplay_map.setter
    def gameplay_map(self, value) -> None:
        if value != self._gameplay_map:
            Session.map_generation += 1
        self._gameplay_map = value

    def __init__(
            self, addr, sock,
            gameplay_deadline_scheduler: GameplayDeadlineScheduler | None = None,
            gameplay_ingress_scheduler: GameplayDeadlineScheduler | None = None):
        self.addr = addr
        self.sock = sock
        self.gameplay_deadline_scheduler = (
            gameplay_deadline_scheduler
            if gameplay_deadline_scheduler is not None
            else _STANDALONE_GAMEPLAY_DEADLINE_SCHEDULER
        )
        # Production gives latency-sensitive movement/casts their own worker
        # pool.  Direct Session construction keeps the historical scheduler
        # fallback so protocol tests and small tools do not leak another pool.
        self.gameplay_ingress_scheduler = (
            gameplay_ingress_scheduler
            if gameplay_ingress_scheduler is not None
            else self.gameplay_deadline_scheduler
        )
        self.fb   = FrameBuilder()
        self.connected = False
        # Retirement can be requested concurrently by the receive,
        # retransmission, and shutdown threads.  This lock protects only the
        # one-way lifecycle claim; it is never held while callbacks or any
        # other session lock run.
        self.retirement_lock = threading.Lock()
        self.retired = False
        self.map_bootstrap_sent = False
        self.character_data_sent = False
        self.bulk_data_sent = False
        self.post_selection_config_sent = False
        self.post_selection_client_ready = False
        self.post_selection_character_request = False
        self.post_selection_tail_sent = False
        self.pre_character_sync_sent = False
        self.split_packets = {}
        # RakNet retransmits a reliable encapsulated frame with a fresh outer
        # datagram number but the same reliable/order indexes.  Keep inbound
        # reliability state independently from gameplay state so duplicate
        # casts are rejected and ordered commands are held until every earlier
        # command has arrived.
        self.inbound_lock = threading.Lock()
        self.inbound_datagram_seen = set()
        self.inbound_datagram_history = deque()
        self.inbound_reliable_base = 0
        self.inbound_reliable_out_of_order = set()
        self.inbound_order_expected = {}
        self.inbound_order_pending = {}
        self.inbound_sequence_next = {}
        self.inbound_duplicate_datagrams = 0
        self.inbound_duplicate_reliable_frames = 0
        self.inbound_order_window_drops = 0
        self.inbound_split_rejections = 0
        self._message_context = threading.local()
        # Exact RakNet frame length for the application message currently
        # being dispatched. Generated command inheritance markers are
        # bit-aligned and cannot be distinguished from byte padding after the
        # frame length has been discarded.
        self.current_message_bit_length = None
        # Hot gameplay commands (skills and movement) are ordered per session
        # but execute away from the sole UDP receive thread. Movement includes
        # vicinity/loot reconciliation and must never delay ACK or ping input.
        # The queue has its own narrow lock: no gameplay handler, network send,
        # or scheduler lifecycle call runs while it is held.
        self.gameplay_skill_dispatch_lock = threading.Lock()
        self.gameplay_skill_dispatch_queue = deque()
        self.gameplay_skill_dispatch_context = None
        self.gameplay_skill_dispatch_generation = 0
        self.gameplay_skill_dispatch_next_token = 0
        self.gameplay_skill_dispatch_scheduled_token = None
        self.gameplay_skill_dispatch_overflow_count = 0
        self.gameplay_skill_dispatch_last_overflow_log_at = 0.0
        self.gameplay_skill_dispatch_token_reserved_at = 0.0
        self.gameplay_skill_dispatch_last_progress_at = time.perf_counter()
        self.gameplay_skill_dispatch_last_stall_log_at = 0.0
        self.gameplay_skill_dispatch_last_slow_log_at = 0.0
        self.gameplay_skill_dispatch_running_since = 0.0
        self.gameplay_skill_dispatch_worker_ident = None
        self.gameplay_skill_dispatch_current_command = None
        self.flow_lock = InstrumentedLock(
            SESSION_FLOW_DOMAIN,
            f"session[{addr[0]}:{addr[1]}]",
        )
        self.pending_reliable = {}
        self.pending_lock = threading.Lock()
        # Frame/order allocation, reliable registration, and the UDP write
        # must be one transaction. Gameplay deadline workers send from many
        # threads; allocating order N and then being descheduled before
        # ``sendto`` allowed another worker to put N+1 on the wire first.
        # RakNet correctly held every later ordered command behind that gap,
        # which looked like a multi-second combat freeze on the client.
        # Instrumented because the map's single simulation thread has to take
        # this lock once per viewer to publish monster movement. A long hold
        # by any one client's own traffic therefore stalls the whole map's
        # simulation, and a plain lock reports nothing when it does.
        self.outbound_lock = _OutboundLock(addr)
        self.skipped_unreliable_sends = 0
        self.reliable_backlog_peak = 0
        self.reliable_backpressure_active = False
        self.reliable_backpressure_started_at = 0.0
        self.last_reliable_ack_latency_seconds = 0.0
        self.maximum_reliable_ack_latency_seconds = 0.0
        self.last_slow_socket_send_log_at = 0.0
        self.last_stuck_reliable_log_at = 0.0
        self.oversized_reliable_logged_at = {}
        now = time.perf_counter()
        self.last_received_at = now
        self.last_send_at = now
        self.last_reliable_backpressure_log_at = 0.0
        self.post_selection_tail_timer = None
        self.post_selection_completion_timer = None
        self.selected_character_id = None
        self.character_generation_active = False
        self.character_generation_ocp_sent = False
        self.gameplay_mode = False
        self._gameplay_map = None
        self.gameplay_map_access_id = None
        self.gameplay_map_access_group_id = None
        self.gameplay_map_difficulty = 0
        self.gameplay_scaling_difficulty = 0
        self.gameplay_monster_level = None
        self.gameplay_character = None
        self.gameplay_template_row_index = None
        self.gameplay_spawn_position = None
        self.gameplay_route_spawn_position = None
        self.gameplay_coordinate_frame = None
        self.gameplay_map_exits = ()
        # Shared per-map-instance scripted event state and this player's
        # trigger-volume occupancy (see services/map_events.py).
        self.gameplay_map_event_runtime = None
        self.gameplay_trigger_volumes_inside = set()
        self.gameplay_trigger_volumes_last_position = None
        self.gameplay_last_visited_town = ""
        # Actual character discoveries, independent of development travel unlocks.
        self.gameplay_unlocked_maps = set()
        self.gameplay_admin_all_maps = False
        self.gameplay_gm_invulnerable = False
        self.gameplay_respawn_allowed = False
        self.gameplay_death_pending = False
        self.gameplay_respawn_in_progress = False
        self.gameplay_revive_movement_until = 0.0
        self.gameplay_respawn_protection_instance_id = None
        self.gameplay_exit_inside_guids = set()
        self.gameplay_exit_distances = {}
        self.gameplay_exit_callback = None
        self.gameplay_exit_transition_started = False
        # Inventory item actions (including socket insertion) and a map
        # handoff must not snapshot different sides of the same mutation.
        self.gameplay_inventory_action_lock = threading.RLock()
        # Switch-character countdown outside hubs (services/map_transition.py)
        self.gameplay_logout_timer = None
        self.gameplay_logout_generation = 0
        self.gameplay_ready_sent = False
        self.gameplay_initialization_sent = False
        # ``gameplay_initialization_sent`` claims the one-shot initialization
        # path before the large local-player batch is assembled.  It therefore
        # must not also mean that this socket is safe to expose to remote
        # presence.  Other map occupants can tick while that batch is in
        # flight; sending their GamePlayer-owned actors before NewPlayer has
        # been consumed makes the client dereference a null local GamePlayer.
        self.gameplay_presence_ready = False
        self.gameplay_initialization_timer = None
        self.gameplay_client_command_counts = {}
        self.gameplay_last_move = None
        # Movement validation budget and one-shot movement-skill path.
        self.gameplay_move_budget = 0.0
        self.gameplay_movement_allowance = None
        self.gameplay_move_correction_at = 0.0
        self.gameplay_client_command_state = {}
        self.gameplay_monster_encounter = None
        self.gameplay_combat_state = None
        self.gameplay_skill_book_ids = frozenset()
        self.gameplay_vicinity_announced = False
        self.gameplay_pending_monster_movements = {}
        self.gameplay_pending_monster_movement_combat = None
        self.gameplay_announced_monster_actor_ids = set()
        self.gameplay_announced_npc_actor_ids = set()
        self.gameplay_requested_actor_ids = set()
        self.gameplay_monsters_spawned = False
        self.gameplay_spawned_monster_actor_ids = set()
        # Every actor id this client has actually been sent NewMonster for on
        # the current map. ``gameplay_spawned_monster_actor_ids`` is a
        # reservation set that several paths add to before they encode, so it
        # cannot answer "did the client already create this actor?" on its
        # own. This ledger can, and the client asserts hard on a duplicate
        # registration: RegisterActor() rejects the id, NewMonster() fails to
        # set the monster up, and the half-built actor trips an effect-module
        # assertion a frame later.
        self.gameplay_new_monster_sent_actor_ids = set()
        # Last transform actually submitted to this client's socket for each
        # registered monster. Shared combat positions advance even when an
        # unreliable movement frame is skipped behind another outbound write;
        # collision must use what this viewer was told, not that server-only
        # position.
        self.gameplay_delivered_monster_positions = {}
        self.gameplay_no_target_cast_streak = 0
        self.gameplay_last_monster_resync_at = 0.0
        self.gameplay_last_tier_spender_shuffle_at = 0.0
        self.gameplay_last_tier_spender_shuffle_backpressure_log_at = 0.0
        self.gameplay_summon_actor_ids = set()
        # Native NewLocationEffect/DiscardLocationEffect registrations are
        # client-local just like monster and item registrations.
        self.gameplay_location_effect_ids = set()
        # Status instances installed solely to select talent/item-specific
        # remote spell VFX. Keyed by (source actor id, instance id) so a
        # repeated cast never re-creates an already-live client effect.
        self.gameplay_remote_visual_status_effects = {}
        self.gameplay_chase_timer = None
        self.gameplay_chase_generation = 0
        self.gameplay_chase_last_at = None
        self.gameplay_chase_server_tick = 0
        self.gameplay_chase_clock_anchor = None
        self.gameplay_last_heal_server_tick = 0
        self.gameplay_last_monster_attack_log_at = 0.0
        self.gameplay_projectile_timers = {}
        self.gameplay_mount_cast = None
        self.gameplay_mount_ride = None
        self.gameplay_mount_cast_timer = None
        self.gameplay_mount_generation = 0
        self.gameplay_stickerbook_entries = {}
        self.gameplay_recipe_book_resources = ()
        self.gameplay_active_costume = ""
        self.gameplay_active_pet = ""
        self.gameplay_active_mercenary = ""
        self.gameplay_mercenary_restore_pending = False
        self.gameplay_mercenary_status_effect = None
        self.gameplay_mercenary_aura_status_effect = None
        self.gameplay_mercenary_aura_status_effects = ()
        self.gameplay_active_decorations = {}  # exclusive group -> (resource, instance)
        self.gameplay_active_costume_effect_instance_id = None
        self.gameplay_active_pet_effect_instance_id = None
        self.gameplay_next_status_effect_instance_id = 0x00020000
        self.gameplay_persistent_status_effects = {}
        # Finite persistent effects (consumable buffs/potions) use wall-clock
        # expiry so their remaining duration survives map loads and relogs.
        self.gameplay_timed_status_effect_expiries = {}
        # Buffs granted by active game events; excluded from persistence.
        self.gameplay_event_status_effect_instance_ids = frozenset()
        self.gameplay_fractured_planes_status_effect_instance_ids = frozenset()
        # Map-scoped effects (arena ``pvp_char_modifier``) installed on entry
        # and removed on the way out; they are never written to the save.
        self.gameplay_map_status_effect_instance_ids = frozenset()
        self.gameplay_item_set_effects = {}
        self.gameplay_item_set_evaluation = None
        self.gameplay_socket_effects = {}
        self.gameplay_socket_effect_evaluation = None
        # Per-rule cooldown/state keys.  The dictionary is bounded by the
        # equipped sockets (ten jewel + ten rune slots), not world entities.
        self.gameplay_socket_event_state = {}
        self.gameplay_runtime_status_effects = {}
        self.gameplay_victim_status_effect_instances = {}
        self.gameplay_talent_categories = tuple(
            (
                TalentCategorySelection(
                    ACTIVE_SKILL_TALENT_PRESELECTION, (), ()
                ),
                TalentCategorySelection(0, (), ()),
                TalentCategorySelection(0, (), ()),
                TalentCategorySelection(0, (), ()),
            )
        )
        self.gameplay_skill_talent_presets = {
            ACTIVE_SKILL_TALENT_PRESELECTION: ()
        }
        self.gameplay_category_talent_presets = {
            1: {0: ()},
            2: {0: ()},
            3: {0: ()},
        }
        self.gameplay_talent_category_points = (0, 0, 0, 0)
        # Skill presets 5..8 bought with Andermant (0..4).
        self.gameplay_unlocked_skill_presets = 0
        self.gameplay_wisdom_maximum_xp = 0
        self.gameplay_talent_effect_instance_ids = {}
        self.gameplay_inventory = None
        self.gameplay_currency_state = None
        self.gameplay_admin_booking_ids: tuple[int, ...] = ()
        self.gameplay_game_currencies = {}
        self.gameplay_currency_ic = 0
        self.gameplay_next_item_id = RUNTIME_ITEM_ID_BASE
        self.gameplay_world_loot = {}
        # GameWorld/ItemManager registrations are local to each client.  Keep
        # the authoritative map loot separate from the subset that has
        # actually been announced to this connection so re-entering vicinity
        # always has a matching DiscardItem -> NewItem lifecycle.
        self.gameplay_announced_world_item_ids = set()
        self.gameplay_world_loot_allocator = None
        self.gameplay_quickbar_info = QuickbarInfo(1, 0)
        self.gameplay_quick_slots = None
        self.gameplay_quick_slots_init_echo_until = 0.0
        self.gameplay_membership_type = DELUXE_MEMBERSHIP
        self.gameplay_membership_expires_at = default_deluxe_expiry()
        self.gameplay_membership_active = True
        self.gameplay_membership_status_effect_ids = frozenset()
        self.gameplay_premium_bag_timer = None
        self.gameplay_premium_bag_deadline = None
        self.gameplay_vendor_buyback = ()
        self.gameplay_open_shop_id = None
        self.gameplay_open_shop_actor_id = None
        self.gameplay_open_shop_offers = None
        self.gameplay_interaction_npc_shop_id = None
        self.gameplay_timed_offer_purchases = {}
        self.gameplay_event_progress = {}
        self.gameplay_season_pass = {}
        self.gameplay_wanted_thief = None
        self.gameplay_daily_challenges = None
        self.gameplay_tier_spender_challenges = ()
        self.gameplay_event_participation = {}
        self.gameplay_pending_travel_item = None
        # Immutable process-cache reference; never serialized.
        self.gameplay_event_catalog = None
        self.gameplay_event_progress_callback = None
        self.gameplay_pvp_enabled = False
        # Career PvP totals, restored from the account document on entry.
        self.gameplay_honor_points = 0
        self.gameplay_pvp_matches_won = 0
        self.gameplay_pvp_matches_lost = 0
        self.gameplay_pvp_kills = 0
        self.gameplay_pvp_deaths = 0
        # Matchmaking binds these while the player is queued, holding an
        # accept dialog, or inside a battleground. ``gameplay_match`` is the
        # shared ``PVPMatch``; ``gameplay_match_team`` is this session's side.
        # They survive the map transfer into the arena by being re-armed from
        # the match registry, not by being carried on the socket.
        self.gameplay_match_queue_entry = None
        self.gameplay_match = None
        self.gameplay_match_team = None
        self.gameplay_match_callback = None
        self.gameplay_title_type = 0
        self.gameplay_achievement_title = ""
        self.gameplay_pending_pvp_hits = {}
        self.gameplay_persist_callback = None
        # Coalescing clock for non-durable account snapshots. Combat spends
        # skill resource on every cast, and each snapshot detaches the whole
        # account. Reasons that only reflect regenerating runtime state are
        # rate limited here; anything the player would notice losing passes
        # ``flush=True`` and bypasses this entirely.
        self.gameplay_last_coalesced_save_at = 0.0
        self.gameplay_coalesced_saves_skipped = 0
        # ``(cache key, disabled ActorFeature names)``. Keyed by the combat
        # state plus the sorted live effect-id tuple, so any add/remove/expiry
        # naturally misses. See ``_player_disabled_actor_features``.
        self.gameplay_disabled_feature_cache = None
        self.gameplay_presence_callback = None
        self.gameplay_remote_actor_id = None
        # A map-transfer source socket can remain connected while its
        # replacement is already initializing.  Exclude that departed
        # presence immediately so it cannot suppress or duplicate the
        # replacement's NewRemotePlayer exchange.
        self.gameplay_presence_departed = False
        self.chat_user_id = None
        self.chat_session_id = None
        # Set once the chat hello's session GUID resolves in the account
        # registry; ``None`` on services which do not authenticate.
        self.chat_account_id = None
        # The account this connection proved it owns, resolved from the
        # session GUID the client puts in its 0x8a hello. ``None`` means the
        # connection is unidentified, which is normal without a registry.
        self.account_id = None
        self.account_username = None
        self.account_character_ids = ()
        # The login session the hello's GUID resolved to. It outlives this
        # UDP session across map transfers and is rotated on relog, which is
        # what "once per login session" announcements key on.
        self.login_session_id = None
        self.chat_user_name = None
        self.chat_admin_name_color = False
        self.chat_is_admin = False
        self.chat_membership_timer = None
        self.chat_joined_channels = set()
        self.chat_social_rooms = {}

    def gameplay_deadline_timer(
            self, interval: float, function, args=None,
            kwargs=None) -> GameplayDeadlineHandle:
        """Create a Timer-compatible handle on this transport's scheduler."""

        return self.gameplay_deadline_scheduler.timer(
            interval,
            function,
            args=args,
            kwargs=kwargs,
        )

    def gameplay_ingress_timer(
            self, interval: float, function, args=None,
            kwargs=None) -> GameplayDeadlineHandle:
        """Create a timer on the movement/skill-only scheduler."""

        return self.gameplay_ingress_scheduler.timer(
            interval,
            function,
            args=args,
            kwargs=kwargs,
        )

    def _send_locked(
            self, data: bytes, *, track: bool, label: str) -> tuple[bool, float]:
        """Register and transmit one already-framed datagram.

        The caller owns ``outbound_lock``. Return whether reliable
        backpressure began and the duration of the socket call.
        """

        backpressure_began = False
        if track and len(data) >= 4 and 0x80 <= data[0] <= 0x8f:
            sequence = int.from_bytes(data[1:4], 'little')
            queued_at = time.perf_counter()
            with self.pending_lock:
                self.pending_reliable[sequence] = {
                    'data': data,
                    'label': label,
                    'sent_at': queued_at,
                    # First transmission. ``sent_at`` is reset by every retry
                    # and dict order is lost when an item is re-keyed under
                    # its new datagram sequence, so neither survives as an age.
                    # The oldest frame is the one holding the receiver's
                    # ordered stream, so it is the one worth re-sending first.
                    'queued_at': queued_at,
                    'attempts': 1,
                }
                backlog = len(self.pending_reliable)
                self.reliable_backlog_peak = max(
                    int(self.reliable_backlog_peak), backlog
                )
                if (
                    backlog >= RELIABLE_BACKLOG_SOFT_LIMIT
                    and not self.reliable_backpressure_active
                ):
                    self.reliable_backpressure_active = True
                    self.reliable_backpressure_started_at = time.perf_counter()
                    backpressure_began = True
        self.last_send_at = time.perf_counter()
        send_started = time.perf_counter()
        try:
            self.sock.sendto(data, self.addr)
        except OSError:
            pass
        return backpressure_began, time.perf_counter() - send_started

    def _report_send_health(
            self, *, label: str, backpressure_began: bool,
            socket_seconds: float) -> None:
        now = time.perf_counter()
        if backpressure_began:
            print(
                f"[RakNet] reliable backlog entered backpressure for "
                f"{self.addr} at {self.reliable_backlog_size()} pending; "
                f"trigger={label!r}"
            )
        if (
            socket_seconds >= 0.040
            and now - self.last_slow_socket_send_log_at >= 2.0
        ):
            self.last_slow_socket_send_log_at = now
            print(
                f"[RakNet] slow UDP send to {self.addr}: "
                f"{socket_seconds * 1000.0:.1f}ms; label={label!r}; "
                f"backlog={self.reliable_backlog_size()}"
            )

    def send(self, data: bytes, *, track: bool = False, label: str = 'reliable'):
        assert_no_state_locks_held("Session.send")
        with self.outbound_lock:
            backpressure_began, socket_seconds = self._send_locked(
                data, track=track, label=label
            )
        self._report_send_health(
            label=label,
            backpressure_began=backpressure_began,
            socket_seconds=socket_seconds,
        )

    def send_reliable_ordered(
            self, payload, *, track: bool = True,
            label: str = 'reliable', skip_if_busy: bool = False) -> bytes | None:
        """Allocate and transmit one reliable-ordered frame atomically.

        Concurrent producers must use this method instead of separately
        calling ``fb.reliable_ordered`` and ``send``. The latter leaves a
        scheduling window between order-index allocation and transmission.
        With skip_if_busy, None means nothing was allocated or sent; the caller
        must retain its state for a later retry.
        """

        assert_no_state_locks_held("Session.send_reliable_ordered")
        # Decide before allocating: an order index taken for an unsplit frame
        # and then abandoned would itself stall the ordered stream.
        body, bit_length = FrameBuilder._payload_spec(payload)
        if len(body) + _UNSPLIT_RELIABLE_FRAMING_BYTES > RELIABLE_DATAGRAM_LIMIT:
            return self._send_oversized_reliable_ordered(
                ((body, bit_length),), track=track, label=label,
                skip_if_busy=skip_if_busy,
            )
        if not self.outbound_lock.acquire(blocking=not skip_if_busy):
            return None
        try:
            data = self.fb.reliable_ordered(payload)
            backpressure_began, socket_seconds = self._send_locked(
                data, track=track, label=label
            )
        finally:
            self.outbound_lock.release()
        self._report_send_health(
            label=label,
            backpressure_began=backpressure_began,
            socket_seconds=socket_seconds,
        )
        return data

    def _send_oversized_reliable_ordered(
            self, specs, *, track: bool, label: str,
            skip_if_busy: bool = False) -> bytes | None:
        """Send reliable-ordered messages too large for one datagram.

        The client drops an oversized datagram without acknowledging it, so
        the retransmit loop re-sends it forever and every later ordered packet
        waits behind it -- monsters, loot and replies all freeze for that
        client while the server keeps running. Callers that build
        variable-size batches (a pack starting its attacks in one tick) cannot
        all know the budget, so the transport enforces it: ``specs`` -- the
        ``(body, bit_length)`` of each message, in order -- are packed into as
        few datagrams as fit, and a message no datagram can hold is
        fragmented.
        """

        send_health = []

        def send_fragment(data: bytes, fragment_label: str) -> None:
            send_health.append(
                (
                    data,
                    fragment_label,
                    *self._send_locked(
                        data, track=track, label=fragment_label
                    ),
                )
            )

        if not self.outbound_lock.acquire(blocking=not skip_if_busy):
            return None
        try:
            batch = []
            batch_bytes = _FRAMESET_HEADER_BYTES
            for body, bit_length in specs:
                frame_bytes = _RELIABLE_ORDERED_HEADER_BYTES + len(body)
                if batch and batch_bytes + frame_bytes > RELIABLE_DATAGRAM_LIMIT:
                    send_fragment(self.fb.multi_reliable_ordered(*batch), label)
                    batch, batch_bytes = [], _FRAMESET_HEADER_BYTES
                if (
                    _FRAMESET_HEADER_BYTES + frame_bytes
                    > RELIABLE_DATAGRAM_LIMIT
                ):
                    self.fb.send_split(
                        body,
                        self.sock,
                        self.addr,
                        send_callback=send_fragment,
                        label_prefix=label,
                        total_bit_length=bit_length,
                    )
                    continue
                batch.append((body, bit_length))
                batch_bytes += frame_bytes
            if batch:
                send_fragment(self.fb.multi_reliable_ordered(*batch), label)
        finally:
            self.outbound_lock.release()
        for _data, fragment_label, backpressure_began, socket_seconds in (
            send_health
        ):
            self._report_send_health(
                label=fragment_label,
                backpressure_began=backpressure_began,
                socket_seconds=socket_seconds,
            )
        now = time.perf_counter()
        if now - self.oversized_reliable_logged_at.get(label, -60.0) >= 30.0:
            self.oversized_reliable_logged_at[label] = now
            print(
                f"[RakNet] split oversized reliable {label!r} for "
                f"{self.addr}: {len(specs)} message(s) of "
                f"{sum(len(body) for body, _bits in specs)} bytes exceed the "
                f"{RELIABLE_DATAGRAM_LIMIT}-byte datagram budget; "
                f"sent as {len(send_health)} datagrams"
            )
        return send_health[-1][0] if send_health else None

    def send_multi_reliable_ordered(
            self, *payloads, track: bool = True,
            label: str = 'reliable') -> bytes:
        """Allocate and send one multi-frame ordered datagram atomically.

        Every payload is validated before an ordering index is taken: an
        index allocated for the first frame of a batch that then fails to
        frame is never sent, and the client holds every later ordered packet
        waiting for it. Frames that do not fit one datagram together go out
        through the oversized path instead, in the same order.
        """

        assert_no_state_locks_held("Session.send_multi_reliable_ordered")
        specs = tuple(FrameBuilder._payload_spec(payload) for payload in payloads)
        if _FRAMESET_HEADER_BYTES + sum(
            _RELIABLE_ORDERED_HEADER_BYTES + len(body) for body, _bits in specs
        ) > RELIABLE_DATAGRAM_LIMIT:
            return self._send_oversized_reliable_ordered(
                specs, track=track, label=label,
            )
        with self.outbound_lock:
            data = self.fb.multi_reliable_ordered(*specs)
            backpressure_began, socket_seconds = self._send_locked(
                data, track=track, label=label
            )
        self._report_send_health(
            label=label,
            backpressure_began=backpressure_began,
            socket_seconds=socket_seconds,
        )
        return data

    def send_wire_first_delivery_second(
            self, wire_first, delivery_first, *, track: bool = True,
            label: str = 'reliable') -> bytes:
        """Send the login handoff's reverse-index pair atomically."""

        assert_no_state_locks_held(
            "Session.send_wire_first_delivery_second"
        )
        with self.outbound_lock:
            data = self.fb.wire_first_delivery_second(
                wire_first, delivery_first
            )
            backpressure_began, socket_seconds = self._send_locked(
                data, track=track, label=label
            )
        self._report_send_health(
            label=label,
            backpressure_began=backpressure_began,
            socket_seconds=socket_seconds,
        )
        return data

    def send_unreliable_sequenced(
            self, payload, *, label: str = 'unreliable-sequenced',
            skip_if_busy: bool = False) -> bytes | None:
        """Preserve sequenced/current-order allocation through transmission.

        ``skip_if_busy`` is for replaceable state only. The map's simulation
        thread publishes monster movement to every viewer in turn, so waiting
        on one client's outbound lock stalls monster updates for everybody on
        that map. A superseded movement frame is worth less than the tick, and
        the next tick carries the newer position anyway.
        """

        assert_no_state_locks_held("Session.send_unreliable_sequenced")
        if not self.outbound_lock.acquire(blocking=not skip_if_busy):
            self.skipped_unreliable_sends += 1
            return None
        try:
            data = self.fb.unreliable_sequenced(payload)
            backpressure_began, socket_seconds = self._send_locked(
                data, track=False, label=label
            )
        finally:
            self.outbound_lock.release()
        self._report_send_health(
            label=label,
            backpressure_began=backpressure_began,
            socket_seconds=socket_seconds,
        )
        return data

    def send_reliable_ordered_split(
            self, payload_or_parts, *, trailing_payloads=(),
            pace_seconds: float = 0, label_prefix: str = 'split',
            total_bit_length: int | None = None,
            track: bool = True) -> None:
        '''Allocate and transmit one fragmented ordered message atomically.

        payload_or_parts accepts either a complete bytes payload or an
        iterable of pre-sized fragments. The outbound lock deliberately spans
        compound/order/reliable allocation, optional pacing, and every UDP
        write. Otherwise a concurrent ordinary reliable command can put a
        later order index on the wire between fragments and make RakNet hold
        the receiver's ordered stream until the missing fragment arrives.

        The fragment callback calls _send_locked directly: routing it through
        send would recursively acquire the non-reentrant outbound lock. Health
        reporting runs after the transaction so diagnostics do not extend the
        critical section.
        '''

        assert_no_state_locks_held('Session.send_reliable_ordered_split')
        send_health = []

        def send_fragment(data: bytes, label: str) -> None:
            backpressure_began, socket_seconds = self._send_locked(
                data,
                track=track,
                label=label,
            )
            send_health.append((label, backpressure_began, socket_seconds))

        with self.outbound_lock:
            if isinstance(payload_or_parts, bytes):
                self.fb.send_split(
                    payload_or_parts,
                    self.sock,
                    self.addr,
                    trailing_payloads=trailing_payloads,
                    pace_seconds=pace_seconds,
                    send_callback=send_fragment,
                    label_prefix=label_prefix,
                    total_bit_length=total_bit_length,
                )
            else:
                self.fb.send_split_parts(
                    payload_or_parts,
                    self.sock,
                    self.addr,
                    trailing_payloads=trailing_payloads,
                    pace_seconds=pace_seconds,
                    send_callback=send_fragment,
                    label_prefix=label_prefix,
                    total_bit_length=total_bit_length,
                )

        for label, backpressure_began, socket_seconds in send_health:
            self._report_send_health(
                label=label,
                backpressure_began=backpressure_began,
                socket_seconds=socket_seconds,
            )

    def acknowledge(self, sequences):
        acknowledged = []
        recovered = None
        now = time.perf_counter()
        with self.pending_lock:
            for sequence in sequences:
                item = self.pending_reliable.pop(sequence, None)
                if item:
                    acknowledged.append((sequence, item['label']))
                    latency = max(0.0, now - float(item['sent_at']))
                    self.last_reliable_ack_latency_seconds = latency
                    self.maximum_reliable_ack_latency_seconds = max(
                        float(self.maximum_reliable_ack_latency_seconds),
                        latency,
                    )
            backlog = len(self.pending_reliable)
            if (
                self.reliable_backpressure_active
                and backlog < max(1, RELIABLE_BACKLOG_SOFT_LIMIT // 2)
            ):
                recovered = (
                    max(0.0, now - self.reliable_backpressure_started_at),
                    backlog,
                    self.reliable_backlog_peak,
                    self.maximum_reliable_ack_latency_seconds,
                )
                self.reliable_backpressure_active = False
                self.reliable_backpressure_started_at = 0.0
                self.reliable_backlog_peak = backlog
                self.maximum_reliable_ack_latency_seconds = 0.0
        if recovered is not None:
            duration, backlog, peak, maximum_ack = recovered
            print(
                f"[RakNet] reliable backlog recovered for {self.addr} after "
                f"{duration:.3f}s; pending={backlog}, peak={peak}, "
                f"max_ack={maximum_ack * 1000.0:.1f}ms"
            )
        return acknowledged

    def reliable_backlog_size(self) -> int:
        with self.pending_lock:
            return len(self.pending_reliable)

    def oldest_unacknowledged_reliable(self, now: float):
        """Return ``(label, age, datagram bytes, attempts)`` or ``None``.

        The oldest pending frame is the one the receiver's ordered stream is
        waiting for; everything sent after it is held behind it.
        """

        with self.pending_lock:
            oldest = min(
                self.pending_reliable.values(),
                key=lambda item: item.get('queued_at', now),
                default=None,
            )
            if oldest is None:
                return None
            return (
                oldest.get('label'),
                max(0.0, now - oldest.get('queued_at', now)),
                len(oldest.get('data') or b''),
                int(oldest.get('attempts', 1)),
            )

    def pending_reliable_count_for_label(self, label: str) -> int:
        """Count unacknowledged reliable datagrams for one response type."""

        with self.pending_lock:
            return sum(
                1 for item in self.pending_reliable.values()
                if item.get('label') == label
            )

    def retransmit(
            self, sequences, interval=NAK_RETRANSMIT_GRACE_SECONDS,
            max_attempts=RELIABLE_MAX_ATTEMPTS):
        '''Retransmit reliable frames without creating ordering gaps.'''
        retransmissions = []
        dropped = []
        now = time.perf_counter()
        with self.pending_lock:
            for sequence in sequences:
                item = self.pending_reliable.get(sequence)
                if not item:
                    continue
                # A NAK for a freshly transmitted replacement can be received
                # before that replacement reaches the socket.  Keep its
                # sequence mapping intact so a late ACK can still retire it.
                if now - item['sent_at'] < interval:
                    continue
                del self.pending_reliable[sequence]
                packet, retry_sequence = self.fb.retransmit_frameset(
                    item['data']
                )
                item['data'] = packet
                item['sent_at'] = now
                item['attempts'] += 1
                self.pending_reliable[retry_sequence] = item
                retransmissions.append(
                    (
                        retry_sequence,
                        packet,
                        item['label'],
                        item['attempts'],
                    )
                )
        return retransmissions, dropped

    def due_retransmissions(
            self, interval=0.25, max_attempts=RELIABLE_MAX_ATTEMPTS,
            burst_limit=RELIABLE_RETRANSMIT_BURST_LIMIT):
        '''Retry overdue reliable frames until ACK or session retirement.

        Sends the oldest overdue frames first and at most ``burst_limit`` of
        them. RakNet delivery is head-of-line blocked: until the earliest
        missing ordering index arrives the receiver holds every later ordered
        command and every sequenced update stamped past the gap, so nothing
        after it can be consumed however many times it is re-sent. Emptying
        the whole backlog in one pass therefore recovers no sooner, and floods
        the same receive window that dropped the traffic to begin with.
        '''

        retransmissions = []
        dropped = []
        now = time.perf_counter()
        with self.pending_lock:
            overdue = sorted(
                (
                    (item['queued_at'], sequence)
                    for sequence, item in self.pending_reliable.items()
                    if now - item['sent_at'] >= interval
                ),
                key=lambda entry: entry[0],
            )
            if burst_limit is not None and burst_limit > 0:
                overdue = overdue[:burst_limit]
            for _queued_at, sequence in overdue:
                item = self.pending_reliable.pop(sequence, None)
                if item is None:
                    continue
                packet, retry_sequence = self.fb.retransmit_frameset(
                    item['data']
                )
                item['data'] = packet
                item['sent_at'] = now
                item['attempts'] += 1
                self.pending_reliable[retry_sequence] = item
                retransmissions.append(
                    (
                        retry_sequence,
                        packet,
                        item['label'],
                        item['attempts'],
                    )
                )
        return retransmissions, dropped

    def ack(self, seq: int):
        self.send(b'\xc0' + b'\x00\x01' + b'\x01' + struct.pack('<I', seq)[:3])

UNSPECIFIED_HOSTS = ("", "0.0.0.0", "::")


def default_advertised_host(bind_host: str) -> str:
    """The address clients are told to dial for a service bound to ``bind_host``.

    A wildcard bind is reachable, but "0.0.0.0:2190" in a SwitchMapCommand is
    not something a client can connect to. Loopback is the only safe guess
    without operator input; the application overrides it from
    ``DSO_PUBLIC_HOST`` for a published server.
    """

    if bind_host in UNSPECIFIED_HOSTS:
        return "127.0.0.1"
    return bind_host


class RakNetServer:
    def __init__(
            self, host: str, port: int, service_name: str,
            *, gameplay_deadline_workers: int = 2,
            gameplay_ingress_workers: int = 0,
            gameplay_initialization_workers: int = 0):
        self.host         = host
        # What redirects and SwitchMapCommands name; the bind address is
        # not always routable from the client's side.
        self.advertised_host = default_advertised_host(host)
        self.port         = port
        self.service_name = service_name
        self._session_registry_lock = threading.Lock()
        self.sessions: SessionRegistry[Session] = SessionRegistry(
            self._session_registry_lock
        )
        self.sock = None
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_udp_reset_log_at = 0.0
        # Optional ``callable(client_ip) -> bool`` consulted before a RakNet
        # handshake starts. ``None`` accepts every address, which is what the
        # local development setup and every existing test expect.
        self.connection_filter = None
        # When set, a hello that does not resolve to a live login session
        # ends the connection. Off, an unidentified client is served exactly
        # as it was before accounts existed: the whole configured roster.
        self.require_account_identity = False
        self._last_rejection_log_at = {}
        self.gameplay_deadline_scheduler = GameplayDeadlineScheduler(
            name=f"{service_name}-gameplay-deadline",
            worker_count=gameplay_deadline_workers,
        )
        # Most RakNet services do not dispatch gameplay input.  A zero count
        # aliases the regular scheduler and avoids idle threads; the gameplay
        # character service explicitly enables the isolated pool.
        self.gameplay_ingress_scheduler = self.gameplay_deadline_scheduler
        if int(gameplay_ingress_workers) > 0:
            self.gameplay_ingress_scheduler = GameplayDeadlineScheduler(
                name=f"{service_name}-gameplay-ingress",
                worker_count=int(gameplay_ingress_workers),
            )
        # Local-player initialization takes hundreds of milliseconds to
        # seconds and used to run on the receive thread, stalling every
        # client's datagrams meanwhile.  The gameplay service gives it its own
        # worker; services that never initialize players alias the regular
        # scheduler so no idle thread is created.
        self.gameplay_initialization_scheduler = self.gameplay_deadline_scheduler
        if int(gameplay_initialization_workers) > 0:
            self.gameplay_initialization_scheduler = GameplayDeadlineScheduler(
                name=f"{service_name}-gameplay-initialization",
                worker_count=int(gameplay_initialization_workers),
            )

    def start(self):
        if self.sock is not None:
            raise RuntimeError(f"{self.service_name} is already running")
        self._stop_event.clear()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # On Windows SO_REUSEADDR lets another server silently take over the
        # same UDP endpoint, mixing services from old and new processes.
        address_option = (
            socket.SO_EXCLUSIVEADDRUSE if os.name == "nt"
            else socket.SO_REUSEADDR
        )
        self.sock.setsockopt(socket.SOL_SOCKET, address_option, 1)
        self._size_socket_buffers()
        try:
            self.sock.bind((self.host, self.port))
        except OSError:
            self.sock.close()
            self.sock = None
            raise
        self.gameplay_deadline_scheduler.start()
        if self.gameplay_ingress_scheduler is not self.gameplay_deadline_scheduler:
            self.gameplay_ingress_scheduler.start()
        if (
            self.gameplay_initialization_scheduler
            is not self.gameplay_deadline_scheduler
        ):
            self.gameplay_initialization_scheduler.start()
        self._threads = [
            threading.Thread(
                target=self._loop,
                daemon=True,
                name=f"{self.service_name}-receive",
            ),
            threading.Thread(
                target=self._retransmit_loop,
                daemon=True,
                name=f"{self.service_name}-retransmit",
            ),
        ]
        for thread in self._threads:
            thread.start()
        print(f"[{self.service_name}] :{self.port}")

    def _size_socket_buffers(self) -> None:
        """Raise the UDP buffers above the platform default.

        Windows defaults both directions to 64 KiB, which is about fifty
        full-size datagrams. A cast that fans Hit/StatusEffect/Kill out to
        several viewers, or a monster batch burst, can exceed that faster than
        the peer drains it: the send buffer then blocks ``sendto`` (measured at
        91 ms on loopback) and an overrun receive buffer discards datagrams
        outright. A discarded *reliable* datagram is the expensive case,
        because RakNet holds every later ordered command -- and every
        sequenced monster movement stamped past the gap -- until the
        retransmission arrives.

        Failures are non-fatal: the socket keeps whatever the OS granted.
        """

        for option, name, requested in (
            (socket.SO_RCVBUF, "DSO_UDP_RECEIVE_BUFFER_BYTES", 1 << 20),
            (socket.SO_SNDBUF, "DSO_UDP_SEND_BUFFER_BYTES", 1 << 20),
        ):
            raw = os.environ.get(name)
            try:
                wanted = int(raw) if raw else requested
            except (TypeError, ValueError):
                print(
                    f"[{self.service_name}] ignoring invalid {name}={raw!r}; "
                    f"using {requested}"
                )
                wanted = requested
            if wanted <= 0:
                continue
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, option, wanted)
                granted = self.sock.getsockopt(socket.SOL_SOCKET, option)
            except OSError as exc:
                print(
                    f"[{self.service_name}] could not size UDP buffer "
                    f"{name}: {exc}"
                )
                continue
            if granted < wanted:
                print(
                    f"[{self.service_name}] UDP buffer {name}: requested "
                    f"{wanted} bytes, granted {granted}"
                )

    def session_snapshot(self) -> tuple[Session, ...]:
        """Return one immutable registry view without exposing its lock."""

        return self.sessions.snapshot()

    def is_session_current(self, session: Session) -> bool:
        """Whether ``session`` is still the installed endpoint generation."""

        return self.sessions.is_current(session.addr, session)

    def _detach_session(self, session: Session) -> bool:
        return self.sessions.detach(session.addr, session)

    def _replace_session(
        self, expected: Session, replacement: Session
    ) -> bool:
        return self.sessions.replace_if_current(
            expected.addr, expected, replacement
        )

    def _detach_all_sessions(self) -> tuple[Session, ...]:
        return self.sessions.drain()

    def _new_session(self, addr) -> Session:
        return Session(
            addr,
            self.sock,
            self.gameplay_deadline_scheduler,
            self.gameplay_ingress_scheduler,
        )

    def stop(self):
        """Stop receiving, cancel session timers, and release the UDP port."""

        self._stop_event.set()
        sock, self.sock = self.sock, None
        if sock is not None:
            sock.close()
        # Detach the complete generation set in one registry operation. Every
        # callback, timer cancellation, persistence request, and pending-frame
        # cleanup below therefore runs without holding the registry lock.
        for session in self._detach_all_sessions():
            self._retire_session(
                session,
                reason="server shutdown",
                remove=False,
            )
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)
        self._threads.clear()
        if (
            self.gameplay_initialization_scheduler
            is not self.gameplay_deadline_scheduler
        ):
            self.gameplay_initialization_scheduler.shutdown(
                wait=True,
                cancel_pending=True,
            )
        if self.gameplay_ingress_scheduler is not self.gameplay_deadline_scheduler:
            self.gameplay_ingress_scheduler.shutdown(
                wait=True,
                cancel_pending=True,
            )
        self.gameplay_deadline_scheduler.shutdown(
            wait=True,
            cancel_pending=True,
        )

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                sock = self.sock
                if sock is None:
                    return
                data, addr = sock.recvfrom(4096)
            except ConnectionResetError as exc:
                # Windows reports an ICMP "port unreachable" generated by a
                # client that closed its UDP socket as WSAECONNRESET on the
                # server's next recvfrom().  It is a per-datagram client
                # failure, not a listening-socket failure.  Exiting here
                # leaves the port bound but permanently kills this service's
                # receive thread, which looks exactly like a server hang.
                if self._stop_event.is_set() or self.sock is not sock:
                    return
                now = time.perf_counter()
                if now - self._last_udp_reset_log_at >= 5.0:
                    self._last_udp_reset_log_at = now
                    print(
                        f"[{self.service_name}] ignored client UDP reset "
                        f"while receiving; stale sessions will be retired: "
                        f"{exc}"
                    )
                continue
            except OSError as exc:
                if not self._stop_event.is_set() and self.sock is sock:
                    print(
                        f"[{self.service_name}] receive loop stopped after "
                        f"socket error: {exc}"
                    )
                return
            dispatch_started = time.perf_counter()
            try:
                # RakNet datagram and reliable-order indices are sequential.
                # An unbounded thread per UDP packet both reordered handlers
                # and allowed a movement burst to exhaust the process.
                self._dispatch(data, addr)
            except Exception as exc:
                print(
                    f"[{self.service_name}] packet handler failed for "
                    f"{addr}: {type(exc).__name__}: {exc}"
                )
                traceback.print_exc()
            finally:
                # Process/ACK this datagram before administrative application work.
                controls = getattr(self, "admin_commands", None)
                if controls is not None:
                    controls.drain_one()
                dispatch_seconds = time.perf_counter() - dispatch_started
                if dispatch_seconds >= 0.040:
                    print(
                        f"[{self.service_name}] slow UDP dispatch for {addr}: "
                        f"{dispatch_seconds * 1000.0:.1f}ms; "
                        f"packet=0x{data[0]:02x}; bytes={len(data)}"
                    )

    def _retransmit_loop(self):
        failures: dict[int, tuple[int, float, float]] = {}
        snapshot_failures = 0
        while not self._stop_event.is_set():
            now = time.perf_counter()
            try:
                sessions = self.session_snapshot()
            except Exception as exc:
                snapshot_failures += 1
                retry_delay = min(
                    2.0,
                    0.1 * (2 ** min(snapshot_failures - 1, 5)),
                )
                print(
                    f"[{self.service_name}] retransmit registry snapshot "
                    f"failed: {type(exc).__name__}: {exc}; retrying in "
                    f"{retry_delay:.2f}s"
                )
                traceback.print_exc()
                if self._stop_event.wait(retry_delay):
                    return
                continue
            snapshot_failures = 0

            active_session_ids = {id(session) for session in sessions}
            for session_id in tuple(failures):
                if session_id not in active_session_ids:
                    failures.pop(session_id, None)

            for session in sessions:
                if self._stop_event.is_set():
                    return
                session_id = id(session)
                failure = failures.get(session_id)
                if failure is not None and now < failure[1]:
                    continue
                try:
                    self._process_retransmit_session(session, now)
                except Exception as exc:
                    previous_count, _retry_at, last_logged_at = failures.get(
                        session_id,
                        (0, 0.0, 0.0),
                    )
                    failure_count = previous_count + 1
                    retry_delay = min(
                        2.0,
                        0.05 * (2 ** min(failure_count - 1, 6)),
                    )
                    failures[session_id] = (
                        failure_count,
                        now + retry_delay,
                        last_logged_at,
                    )
                    # Log the first failure in full, then rate-limit a broken
                    # endpoint so recovery itself cannot self-DDoS the server.
                    if failure_count == 1 or now - last_logged_at >= 5.0:
                        print(
                            f"[{self.service_name}] retransmit session failed "
                            f"for {getattr(session, 'addr', '<unknown>')}: "
                            f"{type(exc).__name__}: {exc}; retrying in "
                            f"{retry_delay:.2f}s"
                        )
                        traceback.print_exc()
                        failures[session_id] = (
                            failure_count,
                            now + retry_delay,
                            now,
                        )
                    continue
                failures.pop(session_id, None)
            self._stop_event.wait(0.05)

    def _process_retransmit_session(
        self,
        session: Session,
        now: float,
    ) -> None:
        """Run one endpoint's maintenance without owning the worker loop."""

        if self._session_reliably_stale(session, now):
            self._retire_session(
                session,
                reason="reliable receive timeout",
                remove=True,
            )
            return
        retransmissions, dropped = session.due_retransmissions()
        for sequence, packet, label, attempt in retransmissions:
            session.send(packet)
            if attempt == RELIABLE_MAX_ATTEMPTS + 1:
                print(
                    f'[{self.service_name}] reliable {label} '
                    f'exceeded {RELIABLE_MAX_ATTEMPTS} attempts; '
                    'continuing retries to preserve ordered delivery'
                )
            if VERBOSE_PACKET_LOGGING:
                print(
                    f"[{self.service_name}] retransmit {label} "
                    f"seq={sequence} attempt={attempt}"
                )
        if dropped:
            print(
                f"[{self.service_name}] gave up {len(dropped)} "
                "reliable datagram(s) after "
                f"{RELIABLE_MAX_ATTEMPTS} attempts; labels="
                f"{tuple(label for _sequence, label in dropped[:5])}"
            )
        self._report_stuck_reliable(session, now)

    def _report_stuck_reliable(self, session: Session, now: float) -> None:
        """Name a reliable frame a still-talking client never acknowledges.

        Only a silent client is retired; one that keeps sending while refusing
        a single datagram is retried forever, and its ordered stream holds
        every later monster update, hit and reply behind that frame. The
        server looks healthy throughout, so say which frame it is.
        """

        if now - session.last_stuck_reliable_log_at < 10.0:
            return
        oldest = session.oldest_unacknowledged_reliable(now)
        if oldest is None:
            return
        label, age, size, attempts = oldest
        if (
            age < RELIABLE_STUCK_WARNING_SECONDS
            or now - float(session.last_received_at)
            >= RELIABLE_STUCK_WARNING_SECONDS
        ):
            return
        session.last_stuck_reliable_log_at = now
        print(
            f"[{self.service_name}] reliable {label!r} ({size} bytes, "
            f"{attempts} attempts) unacknowledged for {age:.1f}s although "
            f"{session.addr} is still sending; that client holds every "
            f"later ordered packet behind it"
        )

    @staticmethod
    def _session_reliably_stale(
            session: Session, now: float | None = None) -> bool:
        if not session.connected:
            return False
        with session.pending_lock:
            has_pending_reliable = bool(session.pending_reliable)
        if not has_pending_reliable:
            return False
        current = time.perf_counter() if now is None else float(now)
        return (
            current - float(session.last_received_at)
            >= RELIABLE_SESSION_IDLE_TIMEOUT_SECONDS
        )

    @staticmethod
    def _cancel_session_activity(session: Session) -> None:
        with session.gameplay_skill_dispatch_lock:
            session.gameplay_skill_dispatch_generation += 1
            session.gameplay_skill_dispatch_queue.clear()
            session.gameplay_skill_dispatch_scheduled_token = None
        with session.flow_lock:
            session.gameplay_chase_generation += 1
            session.gameplay_mount_generation += 1
            timers = [
                session.gameplay_chase_timer,
                session.gameplay_mount_cast_timer,
                session.gameplay_initialization_timer,
                session.post_selection_tail_timer,
                session.post_selection_completion_timer,
                session.chat_membership_timer,
                session.gameplay_premium_bag_timer,
                *session.gameplay_projectile_timers.values(),
            ]
            session.gameplay_chase_timer = None
            session.gameplay_mount_cast_timer = None
            session.gameplay_initialization_timer = None
            session.post_selection_tail_timer = None
            session.post_selection_completion_timer = None
            session.chat_membership_timer = None
            session.gameplay_premium_bag_timer = None
            session.gameplay_premium_bag_deadline = None
            session.gameplay_projectile_timers.clear()
        for timer in timers:
            if timer is not None:
                timer.cancel()

    def drop_session(self, session: Session, *, reason: str) -> bool:
        """Disconnect a client outright and retire its session.

        Used for requests a stock client cannot produce -- naming a character
        the account does not own, for instance. Those are tampering, not user
        error, so the connection ends rather than the request being refused.
        """

        print(
            f"[{self.service_name}] dropping {session.addr}: {reason}"
        )
        try:
            # RakNet ID_DISCONNECTION_NOTIFICATION, so the client leaves
            # cleanly instead of waiting out its timeout. A connected peer
            # ignores a bare 0x15, so it travels in a frameset, unreliable so
            # that an ordered stream stalled behind a missing frame -- often
            # the very reason for the drop -- cannot hold it back.
            session.send(session.fb.unreliable(b'\x15'))
        except OSError as exc:
            print(
                f"[{self.service_name}] could not notify {session.addr} of "
                f"disconnect: {exc}"
            )
        return self._retire_session(session, reason=reason, remove=True)

    def _retire_session(
            self, session: Session, *, reason: str, remove: bool) -> bool:
        """Stop one abandoned connection without stopping the UDP service."""

        # Identity-based detachment cannot remove a replacement which reused
        # the same UDP endpoint. It must happen before any callback so callback
        # code can safely reconnect, query the registry, or trigger retirement
        # again without seeing this generation as active.
        self._detach_session(session)
        with session.retirement_lock:
            if session.retired:
                return False
            session.retired = True
            was_connected = session.connected
            session.connected = False
            presence_callback = session.gameplay_presence_callback
            persist_callback = session.gameplay_persist_callback

        if was_connected and presence_callback is not None:
            try:
                presence_callback(session, 'disconnect')
            except Exception as exc:
                print(
                    f"[{self.service_name}] presence cleanup failed for "
                    f"{session.addr}: {type(exc).__name__}: {exc}"
                )
        if was_connected and persist_callback is not None:
            try:
                persist_callback(session, reason, True)
            except Exception as exc:
                print(
                    f"[{self.service_name}] persistence cleanup failed for "
                    f"{session.addr}: {type(exc).__name__}: {exc}"
                )
        self._cancel_session_activity(session)
        with session.pending_lock:
            session.pending_reliable.clear()
        print(
            f"[{self.service_name}] retired session {session.addr}: {reason}"
        )
        return True

    def _session(self, addr) -> Session:
        return self.sessions.get_or_create(
            addr, lambda: self._new_session(addr)
        )

    def _dispatch(self, data: bytes, addr):
        if not data: return
        pid = data[0]
        if pid in (0x01, 0x02, 0x05, 0x07):
            s = self._session(addr)
        else:
            # Only offline messages open a session. A frameset, ACK or NAK
            # from an endpoint without one belongs to a connection this server
            # already retired: dropped, timed out, or from before a restart.
            # Re-creating a session for it acknowledged the client's datagrams
            # and answered its pings indefinitely, so its RakNet connection
            # never timed out and the player was left in a world nothing
            # updated. Ignored, the client's own timeout reports the loss.
            s = self.sessions.get(addr)
            if s is None:
                return
        s.last_received_at = time.perf_counter()

        # A post-selection map connection may reuse the character connection's
        # UDP source port.  OpenConnectionRequest1 starts a fresh RakNet session,
        # so do not carry reliable/order counters across that reconnect.
        if pid == 0x05 and s.connected:
            replacement = self._new_session(addr)
            replacement.last_received_at = time.perf_counter()
            if self._replace_session(s, replacement):
                # Replacement is already visible before retirement callbacks;
                # the old generation can never detach the new one by address.
                self._retire_session(
                    s, reason='connection replaced', remove=False
                )
                s = replacement
            else:
                # A timeout or shutdown raced the reconnect. Resolve the
                # registry's authoritative generation instead of overwriting
                # whatever that competing lifecycle action installed.
                s = self._session(addr)

        if pid in (0x01, 0x02):
            self._reply_unconnected_ping(s, data)
        elif pid == 0x05:
            self._reply_open1(s, data)
        elif pid == 0x07:
            self._reply_open2(s, data)
        elif pid == 0xc0:
            self._handle_ack(s, data)
        elif pid == 0xa0:
            self._handle_nak(s, data)
        elif 0x80 <= pid <= 0x8f:
            self._handle_frameset(s, data)

    @staticmethod
    def _parse_ack_sequences(data: bytes):
        if len(data) < 3:
            return []
        record_count = int.from_bytes(data[1:3], 'big')
        sequences = []
        offset = 3
        for _ in range(record_count):
            if offset + 4 > len(data):
                break
            singleton = data[offset] != 0
            offset += 1
            start = int.from_bytes(data[offset:offset + 3], 'little')
            offset += 3
            if singleton:
                sequences.append(start)
                continue
            if offset + 3 > len(data):
                break
            end = int.from_bytes(data[offset:offset + 3], 'little')
            offset += 3
            if end >= start and end - start <= 4096:
                sequences.extend(range(start, end + 1))
        return sequences

    def _handle_ack(self, s: Session, data: bytes):
        for sequence, label in s.acknowledge(self._parse_ack_sequences(data)):
            if label.endswith("-switch-map") or label == "switch-character":
                print(
                    f"[{self.service_name}] acknowledged {label} datagram "
                    f"seq={sequence} from {s.addr}; awaiting map handoff"
                )
            elif VERBOSE_PACKET_LOGGING:
                print(
                    f"[{self.service_name}] ACK {label} seq={sequence}"
                )

    def _handle_nak(self, s: Session, data: bytes):
        retransmissions, dropped = s.retransmit(
            self._parse_ack_sequences(data)
        )
        for sequence, packet, label, attempt in retransmissions:
            s.send(packet)
            if attempt == RELIABLE_MAX_ATTEMPTS + 1:
                print(
                    f'[{self.service_name}] reliable {label} exceeded '
                    f'{RELIABLE_MAX_ATTEMPTS} NAK attempts; continuing '
                    'retries to preserve ordered delivery'
                )
            if VERBOSE_PACKET_LOGGING:
                print(
                    f"[{self.service_name}] NAK retransmit {label} "
                    f"seq={sequence} attempt={attempt}"
                )
        if dropped:
            print(
                f"[{self.service_name}] gave up {len(dropped)} NAK "
                "datagram(s) after "
                f"{RELIABLE_MAX_ATTEMPTS} attempts; labels="
                f"{tuple(label for _sequence, label in dropped[:5])}"
            )

    def _reply_unconnected_ping(self, s: Session, data: bytes):
        ping_time = data[1:9] if len(data) >= 9 else bytes(8)
        message = self.service_name.encode('utf-8')
        s.send(b'\x1c'
               + ping_time
               + SERVER_GUID
               + RAKNET_MAGIC
               + struct.pack('>H', len(message))
               + message)

    def _connection_is_authorized(self, client_ip: str) -> bool:
        """Whether an unauthenticated address may open a RakNet session."""

        connection_filter = self.connection_filter
        if connection_filter is None:
            return True
        try:
            allowed = bool(connection_filter(client_ip))
        except Exception as exc:
            # An account service that is down must not silently lock every
            # player out of a running game server.
            print(
                f"[{self.service_name}] connection filter failed for "
                f"{client_ip}: {type(exc).__name__}: {exc}; allowing"
            )
            return True
        if not allowed:
            now = time.perf_counter()
            if now - self._last_rejection_log_at.get(client_ip, 0.0) > 5.0:
                self._last_rejection_log_at[client_ip] = now
                print(
                    f"[{self.service_name}] refused {client_ip}: no active "
                    "login session for that address"
                )
        return allowed

    def _reply_open1(self, s: Session, data: bytes):
        if not self._connection_is_authorized(s.addr[0]):
            return
        discovered_mtu = max(576, min(1492, len(data) + 28))
        s.send(b'\x06'
               + RAKNET_MAGIC
               + SERVER_GUID
               + b'\x00'        # no security
               + struct.pack('>H', discovered_mtu))

    def _reply_open2(self, s: Session, data: bytes):
        mtu = struct.unpack('>H', data[-10:-8])[0] if len(data) >= 10 else 1400
        s.send(b'\x08'
               + RAKNET_MAGIC
               + SERVER_GUID
               + pack_addr(s.addr[0], s.addr[1])
               + struct.pack('>H', mtu)
               + b'\x00')

    @staticmethod
    def _u24_forward_distance(index: int, expected: int) -> int:
        return (int(index) - int(expected)) & _U24_MASK

    @staticmethod
    def _accept_inbound_datagram_locked(s: Session, sequence: int) -> bool:
        """Deduplicate one outer RakNet datagram without blocking on gaps."""

        sequence &= _U24_MASK
        if sequence in s.inbound_datagram_seen:
            s.inbound_duplicate_datagrams += 1
            return False
        s.inbound_datagram_seen.add(sequence)
        s.inbound_datagram_history.append(sequence)
        while len(s.inbound_datagram_history) > _INBOUND_DATAGRAM_HISTORY:
            expired = s.inbound_datagram_history.popleft()
            s.inbound_datagram_seen.discard(expired)
        return True

    @classmethod
    def _accept_inbound_reliable_locked(
            cls, s: Session, reliable_index: int | None) -> bool:
        """Mirror RakNet's uint24 reliable-message duplicate window."""

        if reliable_index is None:
            return False
        reliable_index &= _U24_MASK
        expected = int(s.inbound_reliable_base) & _U24_MASK
        distance = cls._u24_forward_distance(reliable_index, expected)
        if distance >= _U24_HALF_RANGE:
            s.inbound_duplicate_reliable_frames += 1
            return False
        if distance > _INBOUND_RELIABLE_WINDOW:
            # A legitimate peer cannot advance this far while more than eight
            # thousand reliable messages remain unreceived.  Rejecting the
            # frame also bounds memory under malformed or hostile input.
            s.inbound_order_window_drops += 1
            return False
        if distance:
            if reliable_index in s.inbound_reliable_out_of_order:
                s.inbound_duplicate_reliable_frames += 1
                return False
            s.inbound_reliable_out_of_order.add(reliable_index)
            return True

        expected = (expected + 1) & _U24_MASK
        while expected in s.inbound_reliable_out_of_order:
            s.inbound_reliable_out_of_order.remove(expected)
            expected = (expected + 1) & _U24_MASK
        s.inbound_reliable_base = expected
        return True

    @classmethod
    def _accept_inbound_sequence_locked(
            cls, s: Session, channel: int, frame: dict
    ) -> list[tuple[bytes, int]]:
        sequence = frame.get('sequence_idx')
        if sequence is None:
            return []
        sequence &= _U24_MASK
        expected = int(s.inbound_sequence_next.get(channel, 0)) & _U24_MASK
        if cls._u24_forward_distance(sequence, expected) >= _U24_HALF_RANGE:
            return []
        s.inbound_sequence_next[channel] = (sequence + 1) & _U24_MASK
        return [(frame['payload'], frame['bit_length'])]

    @classmethod
    def _queue_or_deliver_inbound_order_locked(
            cls, s: Session, frame: dict
    ) -> list[tuple[bytes, int]]:
        reliability = int(frame['reliability'])
        channel = frame.get('order_channel')
        order_index = frame.get('order_idx')
        if (
                channel is None or not 0 <= int(channel) < 32
                or order_index is None):
            return []
        channel = int(channel)
        order_index = int(order_index) & _U24_MASK
        expected = int(s.inbound_order_expected.get(channel, 0)) & _U24_MASK
        distance = cls._u24_forward_distance(order_index, expected)
        if distance >= _U24_HALF_RANGE:
            return []
        if distance > _INBOUND_ORDER_WINDOW:
            s.inbound_order_window_drops += 1
            return []
        is_sequenced = reliability in (1, 4)

        if distance:
            channel_pending = s.inbound_order_pending.setdefault(channel, {})
            if (
                    order_index not in channel_pending
                    and len(channel_pending) >= _INBOUND_ORDER_WINDOW):
                s.inbound_order_window_drops += 1
                return []
            bucket = channel_pending.setdefault(order_index, {})
            if is_sequenced:
                previous = bucket.get('sequenced')
                if previous is None:
                    bucket['sequenced'] = frame
                else:
                    previous_sequence = int(
                        previous.get('sequence_idx') or 0
                    ) & _U24_MASK
                    sequence = int(frame.get('sequence_idx') or 0) & _U24_MASK
                    newer = cls._u24_forward_distance(
                        sequence, previous_sequence
                    )
                    if 0 < newer < _U24_HALF_RANGE:
                        bucket['sequenced'] = frame
            else:
                bucket.setdefault('ordered', frame)
            return []

        if is_sequenced:
            return cls._accept_inbound_sequence_locked(s, channel, frame)

        deliveries = [(frame['payload'], frame['bit_length'])]
        expected = (expected + 1) & _U24_MASK
        s.inbound_order_expected[channel] = expected
        s.inbound_sequence_next[channel] = 0
        channel_pending = s.inbound_order_pending.get(channel)
        while channel_pending:
            bucket = channel_pending.pop(expected, None)
            if bucket is None:
                break
            sequenced = bucket.get('sequenced')
            if sequenced is not None:
                deliveries.extend(
                    cls._accept_inbound_sequence_locked(
                        s, channel, sequenced
                    )
                )
            ordered = bucket.get('ordered')
            if ordered is None:
                break
            deliveries.append(
                (ordered['payload'], ordered['bit_length'])
            )
            expected = (expected + 1) & _U24_MASK
            s.inbound_order_expected[channel] = expected
            s.inbound_sequence_next[channel] = 0
        if channel_pending == {}:
            s.inbound_order_pending.pop(channel, None)
        return deliveries

    @staticmethod
    def _reassemble_inbound_split_locked(
            s: Session, frame: dict) -> dict | None:
        split = frame.get('split')
        if not split:
            return frame
        total = int(split.get('total', 0))
        index = int(split.get('idx', -1))
        if (
                total <= 0
                or total > _INBOUND_SPLIT_FRAGMENT_LIMIT
                or index < 0
                or index >= total):
            s.inbound_split_rejections += 1
            return None
        key = (
            int(split.get('id', 0)),
            total,
            int(frame['reliability']),
            frame.get('order_channel'),
            frame.get('order_idx'),
        )
        if (
                key not in s.split_packets
                and len(s.split_packets) >= _INBOUND_SPLIT_LIMIT):
            s.split_packets.pop(next(iter(s.split_packets)))
            s.inbound_split_rejections += 1
        fragments = s.split_packets.setdefault(key, {})
        if index in fragments:
            return None
        fragments[index] = (frame['payload'], frame['bit_length'])
        if len(fragments) != total:
            return None
        try:
            payload = b''.join(
                fragments[fragment_index][0]
                for fragment_index in range(total)
            )
            bit_length = sum(
                fragments[fragment_index][1]
                for fragment_index in range(total)
            )
        except KeyError:
            return None
        finally:
            s.split_packets.pop(key, None)
        assembled = dict(frame)
        assembled['payload'] = payload
        assembled['bit_length'] = bit_length
        assembled['split'] = None
        return assembled

    @classmethod
    def _prepare_inbound_frame_locked(
            cls, s: Session, frame: dict) -> list[tuple[bytes, int]]:
        reliability = int(frame['reliability'])
        if reliability in (2, 3, 4, 6, 7):
            if not cls._accept_inbound_reliable_locked(
                    s, frame.get('reliable_idx')):
                return []
        frame = cls._reassemble_inbound_split_locked(s, frame)
        if frame is None:
            return []
        if reliability in (1, 3, 4, 7):
            return cls._queue_or_deliver_inbound_order_locked(s, frame)
        return [(frame['payload'], frame['bit_length'])]

    def _handle_frameset(self, s: Session, data: bytes):
        seq = int.from_bytes(data[1:4], 'little')
        # ACK immediately, including repeated datagrams.  A lost ACK must not
        # make a retransmission execute the same application command again.
        s.ack(seq)
        deliveries = []
        with s.inbound_lock:
            if not self._accept_inbound_datagram_locked(s, seq):
                return
            for frame in self._parse_frames(data):
                deliveries.extend(
                    self._prepare_inbound_frame_locked(s, frame)
                )
        # No application callback is allowed to run under inbound_lock.  The
        # callback may acquire gameplay locks, schedule work, or send replies.
        for payload, bit_length in deliveries:
            if payload:
                s.current_message_bit_length = bit_length
                try:
                    self._on_message(s, payload[0], payload)
                finally:
                    s.current_message_bit_length = None

    @staticmethod
    def _parse_frames(data: bytes) -> list:
        frames = []
        offset = 4
        while offset + 3 <= len(data):
            flags   = data[offset]; offset += 1
            bit_len = int.from_bytes(data[offset:offset+2], 'big'); offset += 2
            byte_len = (bit_len + 7) // 8
            rel      = (flags >> 5) & 7
            has_split = bool(flags & 0x10)

            reliable_idx = None
            sequence_idx = None
            order_idx = None
            order_channel = None

            if rel in (2, 3, 4, 6, 7):
                if offset + 3 > len(data): break
                reliable_idx = int.from_bytes(data[offset:offset+3], 'little')
                offset += 3
            if rel in (1, 4):
                if offset + 3 > len(data): break
                sequence_idx = int.from_bytes(data[offset:offset+3], 'little')
                offset += 3
            if rel in (1, 3, 4, 7):
                if offset + 4 > len(data): break
                order_idx = int.from_bytes(data[offset:offset+3], 'little')
                order_channel = data[offset+3]
                offset += 4
            split_info = None
            if has_split:
                if offset + 10 > len(data): break
                split_info = {
                    'total': int.from_bytes(data[offset:offset+4], 'big'),
                    'id':    int.from_bytes(data[offset+4:offset+6], 'big'),
                    'idx':   int.from_bytes(data[offset+6:offset+10], 'big'),
                }
                offset += 10
            if offset + byte_len > len(data): break
            payload = data[offset:offset+byte_len]
            frames.append({
                'id': payload[0] if payload else None,
                'payload': payload,
                'bit_length': bit_len,
                'split': split_info,
                'reliability': rel,
                'reliable_idx': reliable_idx,
                'sequence_idx': sequence_idx,
                'order_idx': order_idx,
                'order_channel': order_channel,
            })
            offset += byte_len
        return frames

    def _on_message(self, s: Session, msg_id: int, payload: bytes):
        if VERBOSE_PACKET_LOGGING:
            print(
                f"[{self.service_name}] {s.addr[0]}:{s.addr[1]} -> "
                f"0x{msg_id:02x} ({len(payload)} bytes)"
            )
        if msg_id == 0x09:   # Connection Request
            self._send_conn_accepted(s, payload)
        elif msg_id == 0x13: # New Incoming Connection
            s.connected = True
            self._on_connected(s)
        elif msg_id == 0x00: # Connected Ping
            self._send_pong(s, payload)
        elif msg_id == 0x8a: # Client Hello
            self._identify_account(s, payload)
            if self.require_account_identity and s.account_id is None:
                # The address gate only proves that someone at this address
                # logged in. Without a session of its own this client would
                # otherwise be served every account's characters.
                self.drop_session(
                    s,
                    reason=(
                        "hello carried no live login session; login "
                        "enforcement refuses unidentified clients"
                    ),
                )
                return
            self._on_hello(s, payload)
        elif msg_id == 0x8d: # Data request
            self._on_data_request(s)
        elif msg_id == 0x8b: # Character operation
            self._on_char_op(s, payload)
        elif msg_id == 0x1b: # Keepalive
            self._on_timestamp(s, payload)
        elif msg_id == 0x15: # Disconnect
            self._retire_session(
                s,
                reason='client disconnect',
                remove=True,
            )

    def _send_conn_accepted(self, s: Session, request_payload: bytes):
        incoming_timestamp = request_payload[9:17] if len(request_payload) >= 17 else bytes(8)
        payload = (b'\x10'
                   + pack_addr(s.addr[0], s.addr[1])
                   + b'\x00\x00'
                   + pack_addr('255.255.255.255', 0) * 10
                   + incoming_timestamp
                   + struct.pack('>Q', server_time()))
        s.send_reliable_ordered(payload)

    def _send_pong(self, s: Session, payload: bytes):
        client_ts = payload[1:9] if len(payload) >= 9 else bytes(8)
        pong = b'\x03' + client_ts + struct.pack('>Q', server_time())
        s.send(s.fb.unreliable(pong))

    def _on_timestamp(self, s: Session, payload: bytes):
        # ID_TIMESTAMP wraps the application packet after its 64-bit clock.
        # The working server echoes its 0x83 keepalive wrapper unchanged.
        if len(payload) >= 10 and payload[9] == 0x83:
            s.send(s.fb.unreliable(payload))

    def _on_connected(self, s: Session):
        svc = self.service_name.encode()
        s.send_reliable_ordered(
            b'\x82' + struct.pack('<H', len(svc)) + svc
        )

    def _identify_account(self, s: Session, payload: bytes):
        """Bind the connection to the account whose session it presented.

        The client repeats its session GUID in the hello it sends to every
        service, which is the only per-connection identity the protocol
        offers. Services without a registry stay unidentified, exactly as
        they were before accounts existed.
        """

        registry = getattr(self, "account_registry", None)
        if registry is None or s.account_id is not None:
            return None
        try:
            record = registry.find_session(payload)
        except Exception as exc:
            print(
                f"[{self.service_name}] account lookup failed for {s.addr}: "
                f"{type(exc).__name__}: {exc}"
            )
            return None
        if record is None:
            return None
        s.account_id = record.account_id
        s.account_username = record.username
        s.account_character_ids = tuple(record.character_ids)
        s.login_session_id = record.session_id
        print(
            f"[{self.service_name}] {s.addr[0]} identified as "
            f"{record.username!r} (account {record.account_id})"
        )
        return record

    # Hooks for subclasses
    def _on_hello(self, s: Session, payload: bytes): pass
    def _on_data_request(self, s: Session): pass
    def _on_char_op(self, s: Session, payload: bytes): pass

class LoginServer(RakNetServer):
    def __init__(self, host: str, char_server_addr: str, port: int = 2190):
        super().__init__(host, port, 'DrasaOnlineLoginServer')
        self.char_server_addr = char_server_addr  # "ip:port"

    def _on_hello(self, s: Session, payload: bytes):
        redirect = switch_map_payload(self.char_server_addr)
        # 0x88 (1B) + 0x84(redirect) dans le même FrameSet
        # Session 57601 puts 0x88 first on the wire with order index 3, then
        # the redirect with order index 2. RakNet delivers the lower index first.
        s.send_wire_first_delivery_second(b'\x88', redirect)
