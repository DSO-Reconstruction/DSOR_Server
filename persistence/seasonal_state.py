"""Server-wide state for seasonal activities.

Per-character activity progress (incubations, calendar claims, claimed
milestones) lives in the character save's claim map.  Two activities are
shared between characters and live here instead:

* anniversary cake totals -- gui.window.cakeinfo: "Every Anniversary Cake
  crafted by all Players adds to the server total";
* 2026 recall links -- one inactive player links to one returning player's
  code and a code links at most five players.  The Returned Coins a linked
  player claims stay in that player's own save; the code owner's 10% share
  is read from there, so no claim has to update two records.

Every mutation is written synchronously (temporary file, fsync, atomic
replace) before the in-memory state changes, so a caller can publish a result
only after it is durable and a failed write leaves the old state in place.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
import threading


STATE_VERSION = 1
STATE_FILE_NAME = "seasonal-states.json"
ENVIRONMENT_VARIABLE = "DSO_SEASONAL_STATE_FILE"


class SeasonalStateError(ValueError):
    """A shared seasonal mutation was refused."""


class RecallLinkConflict(SeasonalStateError):
    """The inactive player is already linked to a code."""


class RecallLinkLimit(SeasonalStateError):
    """The code owner already has the maximum number of links."""


@dataclass(frozen=True)
class RecallLink:
    returner_id: int
    summoner_id: int
    returner_name: str
    code: str
    linked_at: str


class SeasonalStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._cakes: dict[str, int] = {}
        self._links: dict[int, RecallLink] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self._cakes = {
            str(recipe): max(0, int(total))
            for recipe, total in document.get("crafting_cakes", {}).items()
        }
        for raw in document.get("recall_links", ()):
            link = RecallLink(
                int(raw["returner_id"]),
                int(raw["summoner_id"]),
                str(raw.get("returner_name", "")),
                str(raw.get("code", "")),
                str(raw.get("linked_at", "")),
            )
            self._links[link.returner_id] = link

    def _write(self, cakes: dict[str, int], links: dict[int, RecallLink]) -> None:
        document = {
            "version": STATE_VERSION,
            "crafting_cakes": dict(sorted(cakes.items())),
            "recall_links": [
                asdict(link) for _, link in sorted(links.items())
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as output:
                temporary = Path(output.name)
                json.dump(document, output, ensure_ascii=False, indent=1)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    # -- anniversary cakes ---------------------------------------------------

    def crafting_cakes_total(self, recipe_id: str) -> int:
        with self.lock:
            return int(self._cakes.get(str(recipe_id), 0))

    def add_crafting_cakes(self, recipe_id: str, amount: int) -> int:
        amount = int(amount)
        if amount <= 0:
            raise SeasonalStateError("crafted cake count must be positive")
        with self.lock:
            cakes = dict(self._cakes)
            cakes[str(recipe_id)] = int(cakes.get(str(recipe_id), 0)) + amount
            self._write(cakes, self._links)
            self._cakes = cakes
            return cakes[str(recipe_id)]

    # -- 2026 recall links ---------------------------------------------------

    def recall_link(self, returner_id: int) -> RecallLink | None:
        with self.lock:
            return self._links.get(int(returner_id))

    def recall_links_for(self, summoner_id: int) -> tuple[RecallLink, ...]:
        with self.lock:
            return tuple(sorted(
                (
                    link for link in self._links.values()
                    if link.summoner_id == int(summoner_id)
                ),
                key=lambda link: (link.linked_at, link.returner_id),
            ))

    def link_recall(
        self,
        returner_id: int,
        summoner_id: int,
        *,
        returner_name: str,
        code: str,
        linked_at: str,
        limit: int,
    ) -> RecallLink:
        returner_id, summoner_id = int(returner_id), int(summoner_id)
        if returner_id == summoner_id:
            raise SeasonalStateError("a player cannot link to their own code")
        with self.lock:
            if returner_id in self._links:
                raise RecallLinkConflict("player is already linked")
            if returner_id in {link.summoner_id for link in self._links.values()}:
                # A code owner whose code is in use cannot also be linked.
                raise RecallLinkConflict("code owners cannot link")
            if summoner_id in self._links:
                raise RecallLinkConflict("linked players cannot own a code")
            if sum(
                1 for link in self._links.values()
                if link.summoner_id == summoner_id
            ) >= int(limit):
                raise RecallLinkLimit("code reached its link limit")
            link = RecallLink(
                returner_id, summoner_id, str(returner_name), str(code),
                str(linked_at),
            )
            links = dict(self._links)
            links[returner_id] = link
            self._write(self._cakes, links)
            self._links = links
            return link

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "crafting_cakes": dict(self._cakes),
                "recall_links": copy.deepcopy(list(self._links.values())),
            }

    def close(self) -> None:
        """Writes are synchronous; nothing is pending."""


_STORES: dict[Path, SeasonalStateStore] = {}
_STORES_LOCK = threading.Lock()


def shared_seasonal_store(path: str | Path) -> SeasonalStateStore:
    """One store per file, shared by every service instance in the process.

    The character service and the legacy direct-map route are separate
    service objects over the same saves; two independent in-memory copies of
    one file would overwrite each other's links.
    """

    resolved = Path(path).resolve()
    with _STORES_LOCK:
        store = _STORES.get(resolved)
        if store is None:
            store = SeasonalStateStore(resolved)
            _STORES[resolved] = store
        return store


def default_seasonal_state_path(account_store) -> Path | None:
    """``DSO_SEASONAL_STATE_FILE`` or a file beside the character saves.

    Keeping the shared file next to the save database keeps test and
    alternate deployments (which point the save database elsewhere) from
    sharing one live file.
    """

    configured = (os.getenv(ENVIRONMENT_VARIABLE) or "").strip()
    if configured:
        return Path(configured)
    database_path = getattr(account_store, "path", None)
    if database_path is None:
        return None
    return Path(database_path).parent / STATE_FILE_NAME
