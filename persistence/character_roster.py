"""Build the character-selection roster from persisted player records."""

from dataclasses import asdict

from dso_server.persistence.account_state import AccountStateStore
from dso_server.protocol.character_selection import CharacterRecord, CharacterSelection


def load_character_roster(store: AccountStateStore | None) -> CharacterSelection:
    selection = CharacterSelection()
    if store is None:
        return selection
    for profile in store.profiles():
        character = CharacterRecord(**asdict(profile))
        state = store.get(profile.character_id)
        if state is not None:
            character.map_id = state.map_id
            character.premium_currency = state.currency.currency_type
        selection.characters.append(character)
    if selection.characters:
        selection.active_character_id = selection.characters[0].character_id
    return selection
