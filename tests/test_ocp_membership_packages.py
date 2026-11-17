"""Premium and Deluxe membership packages in the OCP cash shop.

Each offer reuses the client's own 1/3/6/12-month payment package row, priced
at 9.90 EUR. Settlement grants the membership time directly instead of placing
a teaser consumable in the inventory.
"""

from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from dso_server.gameplay.ocp_store import (
    MEMBERSHIP_PACKAGE_REWARDS,
    membership_package_days,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.membership_protocol import (
    DELUXE_MEMBERSHIP,
    PREMIUM_MEMBERSHIP,
    parse_membership_expiry,
)
from dso_server.protocol.reward_protocol import (
    LOCAL_OCP_OFFERS,
    ocp_booking_completed_key,
    ocp_booking_notification_payload,
    ocp_booking_pending_key,
    resolve_ocp_booking_reward,
)
from dso_server.services.base import CharacterService
from dso_server.services.commerce import CommerceService

EXPECTED_PACKAGES = {
    "6_premiumgold_1.0000_1_MONTHS": (PREMIUM_MEMBERSHIP, 30),
    "6_premiumgold_1.0000_3_MONTHS": (PREMIUM_MEMBERSHIP, 90),
    "6_premiumgold_1.0000_6_MONTHS": (PREMIUM_MEMBERSHIP, 180),
    "6_premiumgold_1.0000_12_MONTHS": (PREMIUM_MEMBERSHIP, 365),
    "6_deluxe_1.0000_1_MONTHS": (DELUXE_MEMBERSHIP, 30),
    "6_deluxe_1.0000_3_MONTHS": (DELUXE_MEMBERSHIP, 90),
    "6_deluxe_1.0000_6_MONTHS": (DELUXE_MEMBERSHIP, 180),
    "6_deluxe_1.0000_12_MONTHS": (DELUXE_MEMBERSHIP, 365),
}


class MembershipOfferTests(unittest.TestCase):
    def test_all_packages_are_listed_at_9_90_euro(self):
        offers = {offer.product_key: offer for offer in LOCAL_OCP_OFFERS}
        self.assertEqual(set(MEMBERSHIP_PACKAGE_REWARDS), set(EXPECTED_PACKAGES))
        for key in EXPECTED_PACKAGES:
            self.assertIn(key, offers)
            self.assertEqual(offers[key].price, 9.90)
            self.assertEqual(offers[key].currency_symbol, "€")

    def test_offer_ids_stay_unique(self):
        ids = [offer.offer_id for offer in LOCAL_OCP_OFFERS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_client_payment_packages_exist_for_every_offer(self):
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
            rows = {
                row[0]
                for row in database.execute(
                    'SELECT "Id" FROM "_Template_PaymentPackages"'
                )
            }
        self.assertLessEqual(set(EXPECTED_PACKAGES), rows)

    def test_rewards_resolve_to_teaser_templates(self):
        reward = resolve_ocp_booking_reward(
            "6_premiumgold_1.0000_3_MONTHS", "external"
        )
        self.assertEqual(
            (reward.item_id, reward.amount),
            ("premium_teaser_one_month", 3),
        )
        reward = resolve_ocp_booking_reward(
            "6_deluxe_1.0000_12_MONTHS", "external"
        )
        self.assertEqual(
            (reward.item_id, reward.amount), ("deluxe_teaser_one_year", 1)
        )
        payload = ocp_booking_notification_payload(reward, b"\x03" * 16)
        self.assertEqual(payload["templates"], "deluxe_teaser_one_year")
        self.assertEqual(payload["amounts"], "1")

    def test_package_days(self):
        for key, expected in EXPECTED_PACKAGES.items():
            self.assertEqual(membership_package_days(key), expected)
        self.assertIsNone(membership_package_days("not-a-package"))


class MembershipSettlementTests(unittest.TestCase):
    def setUp(self):
        self.service = CommerceService()
        self.service.static_database_path = DEFAULT_STATIC_DB_PATH
        self._previous_implementation = CharacterService._implementation
        CharacterService._implementation = MagicMock()

    def tearDown(self):
        CharacterService._implementation = self._previous_implementation

    def _session(self, offer_key):
        session = MagicMock()
        session.flow_lock = threading.RLock()
        session.account_id = 1
        session.gameplay_membership_type = 0
        session.gameplay_membership_expires_at = ""
        session.gameplay_membership_active = False
        session.gameplay_membership_premium_expires_at = ""
        session.gameplay_next_status_effect_instance_id = 0
        session.gameplay_persistent_status_effects = {}
        session.gameplay_timed_status_effect_expiries = {}
        session.gameplay_last_move = None
        session.gameplay_currency_state = None
        session.gameplay_inventory = None
        session.gameplay_next_item_id = 1
        session.gameplay_character = SimpleNamespace(level=50, character_id=7)
        session.gameplay_event_progress_callback = None
        session.gameplay_presence_callback = None
        pending = ocp_booking_pending_key(b"\x04" * 16, offer_key)
        session.gameplay_timed_offer_purchases = {pending: 1}
        return session, pending

    def _settle(self, offer_key):
        reward = resolve_ocp_booking_reward(offer_key, "external")
        session, pending = self._session(offer_key)
        self.service._settle_ocp_booking(session, pending, reward)
        return session

    def test_premium_package_grants_its_full_duration(self):
        session = self._settle("6_premiumgold_1.0000_3_MONTHS")
        self.assertEqual(
            session.gameplay_membership_type, PREMIUM_MEMBERSHIP
        )
        self.assertTrue(session.gameplay_membership_active)
        remaining = (
            parse_membership_expiry(session.gameplay_membership_expires_at)
            - datetime.now(timezone.utc)
        )
        self.assertGreater(remaining, timedelta(days=89))
        self.assertLess(remaining, timedelta(days=91))
        self.assertIn(
            ocp_booking_completed_key(b"\x04" * 16),
            session.gameplay_timed_offer_purchases,
        )

    def test_deluxe_package_grants_its_full_duration(self):
        session = self._settle("6_deluxe_1.0000_6_MONTHS")
        self.assertEqual(
            session.gameplay_membership_type, DELUXE_MEMBERSHIP
        )
        self.assertTrue(session.gameplay_membership_active)
        remaining = (
            parse_membership_expiry(session.gameplay_membership_expires_at)
            - datetime.now(timezone.utc)
        )
        self.assertGreater(remaining, timedelta(days=179))
        self.assertLess(remaining, timedelta(days=181))

    def test_package_extends_an_existing_plan_instead_of_replacing_it(self):
        session, pending = self._session("6_deluxe_1.0000_1_MONTHS")
        first = resolve_ocp_booking_reward(
            "6_deluxe_1.0000_1_MONTHS", "external"
        )
        self.service._settle_ocp_booking(session, pending, first)
        first_expiry = parse_membership_expiry(
            session.gameplay_membership_expires_at
        )

        second_key = "6_deluxe_1.0000_1_MONTHS"
        second_pending = ocp_booking_pending_key(b"\x05" * 16, second_key)
        session.gameplay_timed_offer_purchases[second_pending] = 1
        self.service._settle_ocp_booking(
            session,
            second_pending,
            resolve_ocp_booking_reward(second_key, "external"),
        )
        second_expiry = parse_membership_expiry(
            session.gameplay_membership_expires_at
        )
        self.assertGreater(
            second_expiry - first_expiry, timedelta(days=29)
        )


if __name__ == "__main__":
    unittest.main()
