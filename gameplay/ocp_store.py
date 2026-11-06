"""Server-side routing for the native Full Moon Euro offer."""

# The native payment package already exists in static.db4 with the correct
# localized Full Moon Event Attire name and icon. Reuse it as one offer and
# choose the character-specific item on the server; do not synthesize rows in
# a static database or expose the other classes' attire choices.
FULL_MOON_ATTIRE_PRODUCT_KEY = "8_event_pass_fullmoon_1.0000_0_NONE"
FULL_MOON_ATTIRE_ITEMS_BY_CLASS = {
    "warrior": "costume_event_pass_fullmoon_warrior",
    "mage": "costume_event_pass_fullmoon_mage",
    "ranger": "costume_event_pass_fullmoon_ranger",
    "dwarf": "costume_event_pass_fullmoon_dwarf",
}

# Older server versions advertised one synthetic key per class. Keep their
# pending bookings settleable without advertising those keys to clients.
LEGACY_FULL_MOON_ATTIRE_PRODUCT_ITEMS = {
    "8_costume_event_pass_fullmoon_mage_1.0000_0_NONE":
        "costume_event_pass_fullmoon_mage",
    "8_costume_event_pass_fullmoon_warrior_1.0000_0_NONE":
        "costume_event_pass_fullmoon_warrior",
    "8_costume_event_pass_fullmoon_ranger_1.0000_0_NONE":
        "costume_event_pass_fullmoon_ranger",
    "8_costume_event_pass_fullmoon_dwarf_1.0000_0_NONE":
        "costume_event_pass_fullmoon_dwarf",
}
