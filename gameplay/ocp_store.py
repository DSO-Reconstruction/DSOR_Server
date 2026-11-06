"""Server-side routing for event attire Euro offers."""

# The native payment packages already exist in static.db4 with their event
# attire names and icons. Reuse each as one offer and choose the
# character-specific item on the server; do not synthesize static database
# rows or expose the other classes' attire choices.
FULL_MOON_ATTIRE_PRODUCT_KEY = "8_event_pass_fullmoon_1.0000_0_NONE"
FULL_MOON_ATTIRE_ITEMS_BY_CLASS = {
    "warrior": "costume_event_pass_fullmoon_warrior",
    "mage": "costume_event_pass_fullmoon_mage",
    "ranger": "costume_event_pass_fullmoon_ranger",
    "dwarf": "costume_event_pass_fullmoon_dwarf",
}
EVENT_ATTIRE_EVENT_IDS_BY_PRODUCT = {
    FULL_MOON_ATTIRE_PRODUCT_KEY: "fullmoon_event_02",
}

# Reuse the native New Moon payment package and resolve it to the active
# character's class-specific attire on the server, as for Full Moon.
NEW_MOON_ATTIRE_PRODUCT_KEY = "8_event_pass_newmoon_1.0000_0_NONE"
NEW_MOON_ATTIRE_ITEMS_BY_CLASS = {
    "warrior": "costume_event_pass_newmoon_warrior",
    "mage": "costume_event_pass_newmoon_mage",
    "ranger": "costume_event_pass_newmoon_ranger",
    "dwarf": "costume_event_pass_newmoon_dwarf",
}
EVENT_ATTIRE_EVENT_IDS_BY_PRODUCT[NEW_MOON_ATTIRE_PRODUCT_KEY] = (
    "newmoon_event_02"
)
EVENT_ATTIRE_ITEMS_BY_PRODUCT_CLASS = {
    FULL_MOON_ATTIRE_PRODUCT_KEY: FULL_MOON_ATTIRE_ITEMS_BY_CLASS,
    NEW_MOON_ATTIRE_PRODUCT_KEY: NEW_MOON_ATTIRE_ITEMS_BY_CLASS,
}
EVENT_ATTIRE_PRODUCT_KEYS = frozenset(EVENT_ATTIRE_EVENT_IDS_BY_PRODUCT)

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
