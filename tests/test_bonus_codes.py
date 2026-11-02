"""Bonus codes: CMS authoring, in-game redemption and the client wire format."""

from dataclasses import replace
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.protocol.reward_protocol import (
    REDEEM_VOUCHER_COMMAND, REDEEM_VOUCHER_RESPONSE_COMMAND, RedeemVoucherResponse, VOUCHER_FAILED,
    decode_redeem_voucher_request, decode_redeem_voucher_response, encode_redeem_voucher_response,
)
from dso_server.runtime.admin_commands import AdminCommandQueue
from dso_server.services.admin_bonus_codes import (
    FAILED, GUESS_LIMIT, QUEUED, REDEEMED, REJECTIONS, TOO_MANY_ATTEMPTS,
)
from dso_server.services.character_server import CharacterServer
from dso_server.tests.test_admin_cms import cms, login, profile, request, state
from dso_server.tests.test_admin_live import online


ANDERMANT = [{"kind": "currency", "resource": "andermant", "amount": 250}]


def create(cms, code="WELCOME", items=ANDERMANT, **limits):
    return cms.service.post("/api/bonus-codes", {"code": code, "items": items, "reason": "Launch event", **limits},
                            cms.admin, "")


def add_character(cms, character_id):
    cms.store.put_profile(replace(profile(character_id), name=f"Hero{character_id}"))
    cms.store.put(state(character_id), flush=True)


def second_account(cms, username="Second", character_id=44):
    account = cms.registry.register(username, "secret123", character_ids=[character_id])
    add_character(cms, character_id)
    return account


def redemptions(cms, record):
    return cms.service.bonus_codes.detail(record["id"], offset=0, limit=50)["history"]["rows"]


def client_command(code):
    encoded = code.encode()
    return b"\x8b" + REDEEM_VOUCHER_COMMAND.to_bytes(2, "little") + struct.pack("<H", len(encoded)) + encoded


def replies(s):
    return [decode_redeem_voucher_response(call.args[0], call.kwargs["total_bit_length"])
            for call in s.send_reliable_ordered_split.call_args_list
            if call.kwargs.get("label_prefix") == "redeem-voucher-response"]


def wait_for_reply(s, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = replies(s)
        if found:
            return found
        time.sleep(0.01)
    raise AssertionError("the client never received an answer")


def test_request_matches_the_client_encoder():
    assert decode_redeem_voucher_request(client_command("ABCD-1234")) == "ABCD-1234"
    with pytest.raises(ValueError):
        decode_redeem_voucher_request(client_command("ABCD") + b"\x01")


def test_response_matches_the_client_decoder_field_order():
    packet, bits = encode_redeem_voucher_response(RedeemVoucherResponse("Nope", -1, "PAYMENT_URL", "u"), actor_id=7)
    assert bits == len(packet) * 8
    # 0x14097dd84: string message, int32 status, string action, string url.
    assert packet == (b"\x84" + REDEEM_VOUCHER_RESPONSE_COMMAND.to_bytes(2, "little") + b"\x04\x00Nope"
                      + b"\xff\xff\xff\xff" + b"\x0b\x00PAYMENT_URL" + b"\x01\x00u" + b"\x07\x00\x00\x00")
    assert decode_redeem_voucher_response(packet, bits) == RedeemVoucherResponse("Nope", -1, "PAYMENT_URL", "u")


def test_create_normalizes_and_lists_a_code(cms):
    record = create(cms, code=" welcome-2026 ", max_redemptions=5, note="  launch  ")
    assert (record["code"], record["max_redemptions"], record["note"]) == ("WELCOME-2026", 5, "launch")
    assert record["state"] == "active" and record["grants"][0]["resource"] == "andermant"
    listing = cms.service.get("/api/bonus-codes", {"q": "launch"}, cms.admin, {})
    assert [row["id"] for row in listing["rows"]] == [record["id"]]
    assert cms.service.get("/api/bonus-codes", {"q": "absent"}, cms.admin, {})["rows"] == []


@pytest.mark.parametrize("code", ["", "ab", "-LEADING", "HAS SPACE", "X" * 33, "<script>"])
def test_create_rejects_malformed_codes(cms, code):
    with pytest.raises(ValueError):
        create(cms, code=code)


@pytest.mark.parametrize("items", [None, [], [{"kind": "currency", "resource": "nope", "amount": 1}],
                                   [{"kind": "item", "resource": "no_such_item", "amount": 1}],
                                   [{"kind": "currency", "resource": "andermant", "amount": 0}]])
def test_create_rejects_rewards_a_booking_could_not_deliver(cms, items):
    with pytest.raises(Exception):
        create(cms, items=items)
    assert cms.service.get("/api/bonus-codes", {}, cms.admin, {})["total"] == 0


@pytest.mark.parametrize("limits", [{"max_redemptions": -1}, {"max_redemptions": True}, {"max_redemptions": 1.5},
                                    {"expires_at": "soon"}, {"expires_at": float("inf")}, {"note": "x" * 201}])
def test_create_rejects_invalid_limits(cms, limits):
    with pytest.raises(ValueError):
        create(cms, **limits)


def test_duplicate_code_is_refused_case_insensitively(cms):
    create(cms)
    with pytest.raises(ValueError, match="already exists"):
        create(cms, code="welcome")


def test_create_update_are_audited_with_their_reason(cms):
    record = create(cms)
    cms.service.post(f"/api/bonus-codes/{record['id']}", {"changes": {"enabled": False}, "reason": "Campaign ended"},
                     cms.admin, "")
    actions = {row["action"]: row["details"]["reason"] for row in cms.service.repository.history("admin_audit")["rows"]
               if row["action"].startswith("bonus_code")}
    assert actions == {"bonus_code.create": "Launch event", "bonus_code.update": "Campaign ended"}


def test_update_only_touches_allowed_fields(cms):
    record = create(cms)
    with pytest.raises(ValueError):
        cms.service.post(f"/api/bonus-codes/{record['id']}", {"changes": {}, "reason": "Nothing"}, cms.admin, "")
    with pytest.raises(ValueError):
        cms.service.post(f"/api/bonus-codes/{record['id']}", {"changes": {"enabled": 1}, "reason": "Not a bool"},
                         cms.admin, "")
    # Unknown keys never reach the UPDATE statement.
    with pytest.raises(ValueError):
        cms.service.post(f"/api/bonus-codes/{record['id']}", {"changes": {"code": "OTHER"}, "reason": "Rename"},
                         cms.admin, "")
    assert cms.service.repository.bonus_code(record["id"])["code"] == "WELCOME"


def test_cms_routes_require_session_csrf_and_reason(cms):
    assert request(cms, "GET", "/api/bonus-codes")[0] == 401
    cookie, csrf = login(cms)
    assert request(cms, "GET", "/admin_bonus_codes.js")[0] == 200
    payload = {"code": "WELCOME", "items": ANDERMANT, "reason": "Launch event"}
    assert request(cms, "POST", "/api/bonus-codes", payload, cookie=cookie)[0] == 403
    assert request(cms, "POST", "/api/bonus-codes", {**payload, "reason": ""}, cookie=cookie, csrf=csrf)[0] == 400
    status, created, _ = request(cms, "POST", "/api/bonus-codes", payload, cookie=cookie, csrf=csrf)
    assert status == 200
    assert request(cms, "GET", f"/api/bonus-codes/{created['id']}", cookie=cookie)[1]["code"] == "WELCOME"
    assert request(cms, "GET", "/api/bonus-codes/999999", cookie=cookie)[0] == 400


def test_online_redemption_is_a_delivered_booking(cms, online):
    _, s = online
    record = create(cms)
    assert cms.service.bonus_codes.redeem(42, "  welcome ") == REDEEMED
    assert s.gameplay_currency_state.andermant == 350
    [row] = redemptions(cms, record)
    booking = cms.service.repository.booking(row["booking_id"])
    assert (booking["status"], booking["actor"], booking["reason"]) == ("delivered", "bonus code", "Bonus code WELCOME")
    assert (row["account_id"], row["character_id"], row["username"], row["character_name"], row["booking_status"]) == (
        cms.player.account_id, 42, "Player", "PlayerHero", "delivered")
    assert cms.store.get(42).admin_booking_ids == (booking["id"],)


def test_mixed_item_and_currency_reward_is_delivered_live(cms, online):
    _, s = online
    create(cms, items=[*ANDERMANT, {"kind": "item", "resource": "potion_combined_health_rarity_05", "amount": 2}])
    assert cms.service.bonus_codes.redeem(42, "WELCOME") == REDEEMED
    assert s.gameplay_currency_state.andermant == 350
    assert sum(i.amount for i in s.gameplay_inventory.items if i.template_id == "potion_combined_health_rarity_05") == 2


def test_offline_redemption_is_delivered_at_next_entry_once(cms):
    create(cms)
    assert cms.service.bonus_codes.redeem(42, "WELCOME") == QUEUED
    assert cms.store.get(42).currency.andermant == 100
    cms.service.delivery.deliver(42)
    cms.service.delivery.deliver(42)
    assert cms.store.get(42).currency.andermant == 350


def test_each_account_redeems_once_on_any_character(cms):
    add_character(cms, 45)
    cms.registry.claim_character("Player", 45)
    record = create(cms)
    other = second_account(cms)
    assert cms.service.bonus_codes.redeem(42, "WELCOME") == QUEUED
    assert cms.service.bonus_codes.redeem(42, "WELCOME") == REJECTIONS["used"]
    assert cms.service.bonus_codes.redeem(45, "WELCOME") == REJECTIONS["used"]
    assert cms.service.bonus_codes.redeem(44, "WELCOME") == QUEUED
    assert {row["account_id"] for row in redemptions(cms, record)} == {cms.player.account_id, other.account_id}


def test_total_limit_is_shared_by_accounts(cms):
    record = create(cms, max_redemptions=1)
    second_account(cms)
    assert cms.service.bonus_codes.redeem(42, "WELCOME") == QUEUED
    assert cms.service.bonus_codes.redeem(44, "WELCOME") == REJECTIONS["used"]
    assert cms.service.bonus_codes.detail(record["id"], offset=0, limit=50)["state"] == "exhausted"


def test_concurrent_accounts_cannot_exceed_the_limit(cms):
    create(cms, max_redemptions=1)
    second_account(cms)
    barrier, results = threading.Barrier(2), []

    def attempt(character_id):
        barrier.wait()
        results.append(cms.service.bonus_codes.redeem(character_id, "WELCOME"))

    threads = [threading.Thread(target=attempt, args=(cid,)) for cid in (42, 44)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(result.status for result in results) == sorted((QUEUED.status, REJECTIONS["used"].status))


def test_disabled_expired_and_unknown_codes_are_invalid(cms):
    disabled, expired = create(cms, code="OFF"), create(cms, code="OLD", expires_at=time.time() - 60)
    cms.service.post(f"/api/bonus-codes/{disabled['id']}", {"changes": {"enabled": False}, "reason": "Stop it"},
                     cms.admin, "")
    assert expired["state"] == "expired"
    for code in ("OFF", "OLD", "UNKNOWN"):
        assert cms.service.bonus_codes.redeem(42, code) == REJECTIONS["invalid"]
    cms.service.post(f"/api/bonus-codes/{disabled['id']}", {"changes": {"enabled": True}, "reason": "Back on"},
                     cms.admin, "")
    assert cms.service.bonus_codes.redeem(42, "OFF") == QUEUED


def test_undeliverable_reward_never_uses_up_the_code(cms):
    record = create(cms)
    with patch.object(cms.service.delivery, "validate", side_effect=ValueError("template removed")):
        assert cms.service.bonus_codes.redeem(42, "WELCOME") == REJECTIONS["ineligible"]
    assert redemptions(cms, record) == []
    assert cms.service.repository.pending(42) == []
    assert cms.service.bonus_codes.redeem(42, "WELCOME") == QUEUED


def test_failed_live_delivery_keeps_the_claim_and_its_booking(cms):
    record = create(cms)
    with patch.object(cms.service.live, "deliver", side_effect=RuntimeError("boom")):
        assert cms.service.bonus_codes.redeem(42, "WELCOME") == QUEUED
    assert len(redemptions(cms, record)) == 1
    assert [row["status"] for row in cms.service.repository.pending(42)] == ["pending"]


def test_failed_claim_rolls_back_the_booking(cms):
    record = create(cms)
    repository = cms.service.repository
    with patch.object(repository, "_insert_booking", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            repository.redeem_bonus_code("WELCOME", cms.player.account_id, 42)
    assert redemptions(cms, record) == [] and repository.pending(42) == []
    assert repository.db.in_transaction is False


def test_guessing_is_throttled_per_account_but_valid_retries_are_not(cms):
    create(cms)
    second_account(cms)
    assert cms.service.bonus_codes.redeem(44, "WELCOME") == QUEUED
    for _ in range(GUESS_LIMIT + 2):
        assert cms.service.bonus_codes.redeem(44, "WELCOME") == REJECTIONS["used"]
    for _ in range(GUESS_LIMIT):
        assert cms.service.bonus_codes.redeem(42, "GUESS") == REJECTIONS["invalid"]
    assert cms.service.bonus_codes.redeem(42, "WELCOME") == TOO_MANY_ATTEMPTS
    assert cms.service.repository.pending(42) == []


def test_unowned_character_is_not_eligible(cms):
    create(cms)
    assert cms.service.bonus_codes.redeem(999, "WELCOME") == REJECTIONS["ineligible"]


def test_redemptions_survive_a_restart(cms):
    from dso_server.persistence.admin_registry import AdminRepository
    record = create(cms)
    cms.service.bonus_codes.redeem(42, "WELCOME")
    repository = AdminRepository(cms.registry)
    assert repository.bonus_code(record["id"])["redemptions"] == 1
    with pytest.raises(ValueError):
        repository.redeem_bonus_code("WELCOME", cms.player.account_id, 42)


def test_game_command_redeems_while_the_gameplay_gate_is_held(cms, online):
    """The real dispatch path holds this gate, and delivery needs it to commit."""
    server, s = online
    server.admin_commands = AdminCommandQueue(server)
    server.admin_bonus_codes = cms.service.bonus_codes
    server.start()
    try:
        create(cms)
        started = time.monotonic()
        with s.gameplay_mutation_lock:
            server._dispatch_char_op_now(s, client_command("welcome"))
            assert replies(s) == []  # Never blocks the handler.
        assert wait_for_reply(s) == [REDEEMED]
        assert time.monotonic() - started < 2.0
        assert s.gameplay_currency_state.andermant == 350
        assert s.gameplay_client_command_counts[REDEEM_VOUCHER_COMMAND] == 1
    finally:
        server.admin_commands.close()
        server.stop()


def test_every_failure_still_answers_so_the_redeem_button_returns(cms, online):
    _, s = online
    handler = CharacterServer._handle_redeem_voucher
    # No CMS on this service. The login service reaches this with its own self.
    handler(SimpleNamespace(), s, client_command("WELCOME"))
    # Malformed request.
    handler(SimpleNamespace(admin_bonus_codes=cms.service.bonus_codes), s, client_command("WELCOME") + b"\x01")
    # No character bound yet.
    character, s.gameplay_character = s.gameplay_character, None
    handler(SimpleNamespace(admin_bonus_codes=cms.service.bonus_codes), s, client_command("WELCOME"))
    s.gameplay_character = character
    assert replies(s) == [RedeemVoucherResponse("", VOUCHER_FAILED)] * 3


def test_worker_exception_answers_with_the_general_error(cms, online):
    _, s = online
    with patch.object(cms.service.bonus_codes, "redeem", side_effect=RuntimeError("boom")):
        CharacterServer._handle_redeem_voucher(SimpleNamespace(admin_bonus_codes=cms.service.bonus_codes), s,
                                               client_command("WELCOME"))
        assert wait_for_reply(s) == [FAILED]
    # The character is released for its next attempt.
    assert cms.service.bonus_codes.submit(42, "WELCOME", Mock())


def test_a_character_has_one_redemption_in_flight(cms):
    bonus_codes = cms.service.bonus_codes
    release, reply = threading.Event(), Mock()
    with patch.object(bonus_codes, "redeem", side_effect=lambda *_: release.wait(2) and FAILED):
        assert bonus_codes.submit(42, "WELCOME", reply)
        assert not bonus_codes.submit(42, "WELCOME", reply)
        release.set()
        deadline = time.monotonic() + 2
        while not reply.called and time.monotonic() < deadline:
            time.sleep(0.01)
    assert reply.call_count == 1


def test_stopped_service_refuses_new_redemptions(cms):
    bonus_codes = cms.service.bonus_codes
    bonus_codes.stop()
    assert not bonus_codes.submit(42, "WELCOME", Mock())
    bonus_codes.start()
