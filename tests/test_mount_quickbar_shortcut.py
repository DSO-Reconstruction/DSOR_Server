"""The last mount ridden goes on O, from the album or from the bag."""
import threading
from types import SimpleNamespace

from dso_server.protocol.player_init_protocol import QuickSlotInfo, QuickSlotsInfo
from dso_server.services.base import CharacterService

EMPTY = QuickSlotInfo(0xFFFFFFFF, None)


def _session(*, album=(), bag=()):
    bars = tuple(tuple(EMPTY for _ in range(17)) for _ in range(2))
    return SimpleNamespace(
        flow_lock=threading.RLock(),
        gameplay_quick_slots=QuickSlotsInfo(bars),
        gameplay_stickerbook_entries={key: object() for key in album},
        gameplay_inventory=SimpleNamespace(
            items=tuple(SimpleNamespace(template_id=t) for t in bag)
        ),
    )


def _select(monkeypatch, s, resource_id):
    sent = []
    monkeypatch.setattr(
        CharacterService, "_send_stickerbook_commands",
        staticmethod(lambda _s, commands, label: sent.extend(commands)),
    )
    assert CharacterService._select_mount_quickbar_shortcut(s, resource_id)
    return sent


def test_an_album_mount_goes_on_o_as_a_sticker(monkeypatch):
    s = _session(album=("mythical_mount_manticore_01",))
    sent = _select(monkeypatch, s, "mythical_mount_manticore_01")
    assert all(bar[8] == QuickSlotInfo(2, "mythical_mount_manticore_01")
               for bar in s.gameplay_quick_slots.quickbars)
    assert len(sent) == 1


def test_a_bag_mount_not_in_the_album_goes_on_o_as_an_item(monkeypatch):
    s = _session(album=("mythical_mount_manticore_01",), bag=("epicmount_steambike_01",))
    _select(monkeypatch, s, "epicmount_steambike_01")
    assert all(bar[8] == QuickSlotInfo(1, "epicmount_steambike_01")
               for bar in s.gameplay_quick_slots.quickbars)
