"""Persistence barriers must not hold shared gameplay/account locks."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from dso_server.persistence import account_registry as registry_module
from dso_server.persistence.account_registry import (
    AccountDisabledError, AccountRegistry, InvalidCredentialsError,
)
from dso_server.persistence.account_state import (
    AccountState, AccountStateStore, CharacterProfile,
)
from dso_server.persistence.social_state import SocialStateStore, make_social_member
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import CurrencyChanged, QuickbarInfo


@pytest.mark.parametrize("known", [True, False])
def test_password_hashing_does_not_block_account_reads(monkeypatch, known):
    registry = AccountRegistry(":memory:", password_iterations=10000)
    account = registry.register("AuditUser", "password123", character_ids=(42,))
    entered, release = threading.Event(), threading.Event()
    original = registry_module.hash_password

    def blocked_hash(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(registry_module, "hash_password", blocked_hash)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            verification = pool.submit(
                registry.verify_password, "AuditUser" if known else "Unknown", "password123"
            )
            try:
                assert entered.wait(3)
                assert pool.submit(registry.character_owner, 42).result(1) == account
            finally:
                release.set()
            if known:
                assert verification.result(3) == account
            else:
                with pytest.raises(InvalidCredentialsError):
                    verification.result(3)
    finally:
        registry.close()


@pytest.mark.parametrize("change", ["disable", "delete", "password"])
def test_password_proof_is_revalidated_after_hashing(monkeypatch, change):
    registry = AccountRegistry(":memory:", password_iterations=10000)
    registry.register("AuditUser", "password123")
    entered, release = threading.Event(), threading.Event()
    original = registry_module.hash_password

    def blocked_hash(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(registry_module, "hash_password", blocked_hash)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            verification = pool.submit(registry.verify_password, "AuditUser", "password123")
            try:
                assert entered.wait(3)
                monkeypatch.setattr(registry_module, "hash_password", original)
                if change == "disable":
                    registry.set_disabled("AuditUser", True)
                elif change == "delete":
                    registry.delete_account("AuditUser")
                else:
                    registry.set_password("AuditUser", "password123", "newpassword123")
            finally:
                release.set()
            expected = AccountDisabledError if change == "disable" else InvalidCredentialsError
            with pytest.raises(expected):
                verification.result(3)
    finally:
        registry.close()


def test_wallet_ownership_stays_in_ram_and_tracks_mutations(tmp_path):
    registry = AccountRegistry(tmp_path / "accounts.sqlite3", password_iterations=10000)
    try:
        account = registry.register("AuditUser", "password123", character_ids=(42, 43))
        queries = []
        registry._connection.set_trace_callback(queries.append)
        assert registry.wallet_owner(42) == (account.account_id, (42, 43))
        assert registry.wallet_owner(999) is None
        assert queries == []
        registry.release_character(42)
        assert registry.wallet_owner(42) is None
        assert registry.wallet_owner(43) == (account.account_id, (43,))
        registry.claim_character("AuditUser", 44)
        assert registry.wallet_owner(44) == (account.account_id, (43, 44))
        registry.close()
        registry = AccountRegistry(tmp_path / "accounts.sqlite3")
        assert registry.wallet_owner(44) == (account.account_id, (43, 44))
        registry.delete_account("AuditUser")
        assert registry.wallet_owner(43) is None
        assert registry.wallet_owner(44) is None
    finally:
        registry.close()


def test_matching_state_and_profile_commit_in_one_batch(tmp_path, monkeypatch):
    path = tmp_path / "players.sqlite3"
    store = AccountStateStore(path)
    batches = []
    original = store._database._write_rows

    def record_batch(connection, states, profiles, deletes):
        batches.append((len(states), len(profiles)))
        return original(connection, states, profiles, deletes)

    monkeypatch.setattr(store._database, "_write_rows", record_batch)
    state = AccountState(
        42, "a0200_kingscity", None, InventoryInfo(),
        CurrencyChanged(100, 200, (3, 0, 4), 500, False), 0, 10000,
        QuickbarInfo(2, 0), {}, (), None,
    )
    profile = CharacterProfile(42, "AuditHero", "a0200_kingscity", 1, 0, 0, 0, 0, 0)
    try:
        store.put(state, profile=profile, flush=True)
        assert batches == [(1, 1)]
        reopened = AccountStateStore(path)
        try:
            assert reopened.get(42).currency == state.currency
            assert reopened.get_profile(42).name == profile.name
            assert reopened.get_profile(42).premium_currency == state.currency.andermant
        finally:
            reopened.close()
    finally:
        store.close()


def test_social_io_does_not_hold_membership_lock_and_snapshots_stay_detached(tmp_path, monkeypatch):
    path = tmp_path / "social.json"
    store = SocialStateStore(path)
    entered, release, updated = threading.Event(), threading.Event(), threading.Event()
    snapshots = []
    original_write, original_queue = store._write_snapshot, store._queue_write_locked

    def blocked_write(snapshot):
        snapshots.append(snapshot)
        if len(snapshots) == 1:
            entered.set()
            assert release.wait(3)
        return original_write(snapshot)

    def queued():
        original_queue()
        if store._queued_revision >= 2:
            updated.set()

    monkeypatch.setattr(store, "_write_snapshot", blocked_write)
    monkeypatch.setattr(store, "_queue_write_locked", queued)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            creation = pool.submit(
                store.create_group, "Audit", "", "map", make_social_member(1, "One", 1, 1)
            )
            try:
                assert entered.wait(3)
                group = pool.submit(store.group_for, 1).result(1)
                addition = pool.submit(
                    store.add_group_member, group.group_id, 1,
                    make_social_member(2, "Two", 1, 1),
                )
                assert updated.wait(3)
                # The pending write retains the original immutable version.
                assert len(snapshots[0][3][0].members) == 1
                assert len(pool.submit(store.group_for, 2).result(1).members) == 2
                assert not creation.done()
            finally:
                release.set()
            creation.result(3)
            addition.result(3)
        assert len(json.loads(path.read_text())["groups"][0]["members"]) == 2
        store.leave_group(1)
        assert store.group_for(1) is None
        assert store.group_for(2).owner_character_id == 2
        store.leave_group(2)
        assert store.group_for(2) is None
    finally:
        store.close()


def test_social_failed_replace_preserves_file_and_reports_flush_failure(tmp_path, monkeypatch):
    from dso_server.persistence import social_state as module

    path = tmp_path / "social.json"
    store = SocialStateStore(path)
    store.create_group("Audit", "", "map", make_social_member(1, "One", 1, 1))
    before = path.read_bytes()
    original_replace, original_flush = module.os.replace, store.flush
    failed = threading.Event()

    def fail_replace(*args):
        failed.set()
        raise OSError("disk unavailable")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    monkeypatch.setattr(store, "flush", lambda: original_flush(timeout=0.15))
    try:
        with pytest.raises(TimeoutError, match="disk unavailable"):
            store.add_buddy(1, 2)
        assert failed.is_set()
        assert path.read_bytes() == before
        assert store.buddies_for(1) == (2,)
        assert list(tmp_path.glob("*.tmp")) == []
    finally:
        monkeypatch.setattr(module.os, "replace", original_replace)
        monkeypatch.setattr(store, "flush", original_flush)
        store.close()
    assert json.loads(path.read_text())["buddies"] == {"1": [2]}


def test_social_durable_operations_reject_external_lock_ownership(tmp_path):
    store = SocialStateStore(tmp_path / "social.json")
    try:
        with store.lock:
            with pytest.raises(RuntimeError, match="without.*holding"):
                store.add_buddy(1, 2)
            with pytest.raises(RuntimeError, match="without holding"):
                store.flush()
        assert store.buddies_for(1) == ()
    finally:
        store.close()
