"""Switch for generated end-game stock at the smith and jeweler vendors.

The generated Mythic/Dragan/Unique gear at smith_hub* shops and max-tier
jewels and runes at jewelry_hub_all are testing aids. They stay disabled
unless DSO_ENDGAME_VENDOR_STOCK is set to 1/true/yes/on. The setting is read
for each shop request.
"""

import os


def endgame_vendor_stock_enabled() -> bool:
    return os.getenv("DSO_ENDGAME_VENDOR_STOCK", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
