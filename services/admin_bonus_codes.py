"""Bonus codes: rewards authored in the CMS, redeemed in the shop's voucher tab.

A code holds a fixed batch of grants. Redeeming one claims it and queues an
ordinary booking for the redeeming character in the same commit, so delivery,
retries, notifications and cancellation all behave like any other booking.
"""

import json
from queue import Full, Queue
import re
import threading
import time
import traceback

from dso_server.persistence.admin_registry import BonusCodeRejected
from dso_server.protocol.reward_protocol import (
    RedeemVoucherResponse, VOUCHER_ALREADY_USED, VOUCHER_FAILED, VOUCHER_INVALID,
    VOUCHER_NOT_ELIGIBLE, VOUCHER_REDEEMED, VOUCHER_TOO_MANY_ATTEMPTS,
)
from dso_server.services.accounts import FailureThrottle
from dso_server.services.admin_items import item_metadata


CODE_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9_-]{2,31}")
MAX_REDEMPTIONS = 1_000_000_000
# Year 3000: rejects infinity and millisecond timestamps.
MAX_EXPIRY = 32_503_680_000
NOTE_LENGTH = 200
# Only unknown codes count, so a guessing client stalls without touching
# players who retype a code they already used.
GUESS_LIMIT = 10
GUESS_WINDOW_SECONDS = 600

# Text from the client's gui.payment language file (eVoucherHintText*). The
# client never looks these keys up and shows the server's message instead.
REDEEMED = RedeemVoucherResponse("Code redeemed", VOUCHER_REDEEMED)
QUEUED = RedeemVoucherResponse("Code redeemed. Your reward will arrive the next time you enter a map.",
                               VOUCHER_REDEEMED)
FAILED = RedeemVoucherResponse("", VOUCHER_FAILED)
TOO_MANY_ATTEMPTS = RedeemVoucherResponse("Too many attempts. Please try again later.",
                                          VOUCHER_TOO_MANY_ATTEMPTS)
REJECTIONS = {
    "invalid": RedeemVoucherResponse("The code you entered is not valid. Please try again.", VOUCHER_INVALID),
    "used": RedeemVoucherResponse("The code you entered has already been redeemed.", VOUCHER_ALREADY_USED),
    "ineligible": RedeemVoucherResponse("You are not eligible to redeem this code.", VOUCHER_NOT_ELIGIBLE),
}


def code_state(record, now):
    if not record["enabled"]:
        return "disabled"
    if record["expires_at"] is not None and record["expires_at"] <= now:
        return "expired"
    if record["max_redemptions"] and record["redemptions"] >= record["max_redemptions"]:
        return "exhausted"
    return "active"


class BonusCodeAdministration:
    def __init__(self, service):
        self.service = service
        self.throttle = FailureThrottle(limit=GUESS_LIMIT, window_seconds=GUESS_WINDOW_SECONDS)
        self._pending = Queue(maxsize=64)
        self._lock = threading.Lock()
        self._active = set()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="bonus-codes", daemon=True)
            self._thread.start()

    def stop(self):
        thread, self._thread = self._thread, None
        if thread is not None:
            try:
                self._pending.put(None, timeout=1)
            except Full:
                pass
            thread.join(timeout=2)

    def submit(self, character_id, code, reply):
        """Queue a redemption; False means the caller must answer the client."""
        with self._lock:
            if self._thread is None or character_id in self._active:
                return False
            try:
                self._pending.put_nowait((character_id, code, reply))
            except Full:
                return False
            self._active.add(character_id)
        return True

    def _run(self):
        while (job := self._pending.get()) is not None:
            character_id, code, reply = job
            try:
                response = self.redeem(character_id, code)
            except Exception as exc:
                print(f"[BonusCodes] redemption failed for {character_id}: {type(exc).__name__}: {exc}")
                traceback.print_exc()
                response = FAILED
            with self._lock:
                self._active.discard(character_id)
            try:
                reply(response)
            except Exception as exc:
                print(f"[BonusCodes] reply failed for {character_id}: {type(exc).__name__}: {exc}")

    def redeem(self, character_id, code):
        """Claim ``code`` for this character. Runs off the game threads."""
        service, repository = self.service, self.service.repository
        owner = service.registry.character_owner(character_id)
        profile = service.store.get_profile(character_id)
        if owner is None or profile is None:
            return REJECTIONS["ineligible"]
        guesser = str(owner.account_id)
        if self.throttle.blocked(guesser):
            return TOO_MANY_ATTEMPTS
        code = str(code).strip().upper()
        try:
            record = repository.open_bonus_code(code, owner.account_id)
            try:
                service.delivery.validate("batch", json.dumps(record["grants"]), 1,
                                          level=profile.level, class_id=profile.class_id)
            except ValueError as exc:
                # Refuse before the claim: delivery would apply this same
                # validation and strand the booking on a used-up code.
                print(f"[BonusCodes] {code} cannot be granted to {character_id}: {exc}")
                raise BonusCodeRejected("ineligible") from None
            booking = repository.redeem_bonus_code(code, owner.account_id, character_id)
        except BonusCodeRejected as rejected:
            if rejected.reason == "invalid":
                self.throttle.record_failure(guesser)
            return REJECTIONS[rejected.reason]
        try:
            booking = service.live.deliver(booking["id"])
        except Exception as exc:
            # The claim is committed, so the booking still arrives at map entry.
            print(f"[BonusCodes] live delivery of booking {booking['id']} failed: {type(exc).__name__}: {exc}")
        return REDEEMED if booking["status"] == "delivered" else QUEUED

    def _validated_grants(self, items):
        if not isinstance(items, list) or not items:
            raise ValueError("a bonus code must grant at least one item or currency")
        return self.service.delivery.validate("batch", json.dumps(items), 1)["items"]

    @staticmethod
    def _limits(changes):
        limits = {}
        if "max_redemptions" in changes:
            value = changes["max_redemptions"]
            if type(value) is not int or not 0 <= value <= MAX_REDEMPTIONS:
                raise ValueError("the redemption limit must be a whole number from 0 to 1,000,000,000")
            limits["max_redemptions"] = value
        if "expires_at" in changes:
            value = changes["expires_at"]
            if value is not None and (type(value) not in {int, float} or not 0 < value < MAX_EXPIRY):
                raise ValueError("the expiry must be a time, or empty for no expiry")
            limits["expires_at"] = value
        if "note" in changes:
            value = changes["note"]
            if not isinstance(value, str) or len(value) > NOTE_LENGTH:
                raise ValueError(f"the note must be at most {NOTE_LENGTH} characters")
            limits["note"] = value.strip()
        return limits

    def _grant_view(self, grant):
        name = grant["resource"]
        if grant["kind"] == "item":
            try:
                name = item_metadata(self.service.static_database_path, grant["resource"])["name"]
            except (ValueError, TypeError):
                pass  # A template dropped by a later static.db4 keeps its ID.
        return {**grant, "name": name}

    def _document(self, record, now):
        return {**record, "state": code_state(record, now),
                "grants": [self._grant_view(grant) for grant in record["grants"]]}

    def listing(self, *, offset, limit, query):
        data = self.service.repository.bonus_codes(offset=offset, limit=limit, query=query)
        now = time.time()
        return {**data, "rows": [self._document(row, now) for row in data["rows"]]}

    def detail(self, code_id, *, offset, limit):
        repository = self.service.repository
        history = repository.bonus_code_redemptions(code_id, offset=offset, limit=limit)
        for row in history["rows"]:
            profile = self.service.store.get_profile(row["character_id"])
            row["character_name"] = profile.name if profile else None
        return {**self._document(repository.bonus_code(code_id), time.time()), "history": history}

    def create(self, payload, account, reason):
        code = str(payload.get("code") or "").strip().upper()
        if not CODE_PATTERN.fullmatch(code):
            raise ValueError("a code is 3 to 32 letters, digits, hyphens or underscores, "
                             "starting with a letter or digit")
        limits = self._limits({"max_redemptions": 0, "expires_at": None, "note": "", **payload})
        record = self.service.repository.create_bonus_code(
            account.username, code, self._validated_grants(payload.get("items")),
            limits["max_redemptions"], limits["expires_at"], limits["note"], reason)
        return self._document(record, time.time())

    def update(self, code_id, changes, account, reason):
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        limits = self._limits(changes)
        if "enabled" in changes:
            if type(changes["enabled"]) is not bool:
                raise ValueError("enabled must be a boolean")
            limits["enabled"] = changes["enabled"]
        record = self.service.repository.update_bonus_code(account.username, code_id, limits, reason)
        return self._document(record, time.time())
