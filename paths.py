"""Project-owned content and state paths, independent of the working directory."""

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
DATA_ROOT = PACKAGE_ROOT / "data"
DATABASE_ROOT = DATA_ROOT / "db"
DEFAULT_STATIC_DB_PATH = Path(os.getenv("DSO_STATIC_DB") or DATABASE_ROOT / "static.db4")
DEFAULT_LANGUAGE_ROOT = Path(os.getenv("DSO_LANGUAGE_DIR") or DATA_ROOT / "language")
DEFAULT_PLAYER_DATABASE_PATH = DATABASE_ROOT / "players.sqlite3"
DEFAULT_ACCOUNT_DATABASE_PATH = DATABASE_ROOT / "accounts.sqlite3"
DEFAULT_SOCIAL_STATE_PATH = DATA_ROOT / "state" / "social-states.json"
DEFAULT_CHAT_AUDIT_PATH = DATA_ROOT / "logs" / "chat-service.log"
DEFAULT_CHEST_CONTENTS_MAP = DATA_ROOT / "catalogs" / "chest_contents.json"
# Server-authored contents of the loot lists named by _Template_Events.Loot.
DEFAULT_EVENT_LOOT_PATH = Path(
    os.getenv("DSO_EVENT_LOOT") or DATA_ROOT / "catalogs" / "event_loot.json"
)
# Server-authored contents of map chest loot lists (_Template_StateObject.LootListId).
DEFAULT_PROP_LOOT_PATH = Path(
    os.getenv("DSO_PROP_LOOT") or DATA_ROOT / "catalogs" / "prop_loot.json"
)
DESCRIPTION_CHEST_CONTENTS_MAP = DATA_ROOT / "catalogs" / "chest_contents_descriptions.json"
# Quest definitions decoded from the client's quests.pbxml (see docs/QUESTS.md).
DEFAULT_QUESTS_PATH = Path(os.getenv("DSO_QUESTS_FILE") or DATA_ROOT / "quests.xml")
# CMS edits to the Season Pass reward table (see docs/SEASON_PASS.md).
DEFAULT_SEASON_PASS_CONFIG_PATH = Path(
    os.getenv("DSO_SEASON_PASS_CONFIG") or DATA_ROOT / "season_pass.json"
)
