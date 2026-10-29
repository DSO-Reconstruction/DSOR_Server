"""Composed character and gameplay-map service."""

from __future__ import annotations

from dso_server.runtime.transport import RakNetServer
from dso_server.services.base import CharacterService
from dso_server.services.events import EventService
from dso_server.services.map_events import MapEventService
from dso_server.services.map_companions import MapCompanionService
from dso_server.services.quests import QuestService
from dso_server.services.achievements import AchievementService
from dso_server.services.lifecycle import LifecycleService
from dso_server.services.player_initialization import PlayerInitializationService
from dso_server.services.character_selection import CharacterSelectionService
from dso_server.services.customization import CustomizationService
from dso_server.services.world import WorldService
from dso_server.services.monster_activity import MonsterActivityService
from dso_server.services.socket_effects import SocketEffectService
from dso_server.services.player_resources import PlayerResourceService
from dso_server.services.player_progression import PlayerProgressionService
from dso_server.services.combat_resolution import CombatResolutionService
from dso_server.services.monster_combat import MonsterCombatService
from dso_server.services.monster_summons import MonsterSummonService
from dso_server.services.pvp_combat import PVPCombatService
from dso_server.services.matchmaking import MatchmakingService
from dso_server.services.battleground import BattlegroundService
from dso_server.services.ranking import RankingService
from dso_server.services.wanted_thief import WantedThiefService
from dso_server.services.season_pass import SeasonPassService
from dso_server.services.daily_challenges import DailyChallengeService
from dso_server.services.summon_pvp import SummonPVPService
from dso_server.services.player_combat import PlayerCombatService
from dso_server.services.player_projectiles import PlayerProjectileService
from dso_server.services.player_trails import PlayerTrailService
from dso_server.services.player_summons import PlayerSummonService
from dso_server.services.player_traps import PlayerTrapService
from dso_server.services.consumables import ConsumableService
from dso_server.services.premium_bags import PremiumBagService
from dso_server.services.inventory_map import InventoryMapService
from dso_server.services.inventory_interactions import InventoryInteractionService
from dso_server.services.food_recycling import FoodRecyclingService
from dso_server.services.seasonal_activities import SeasonalActivityService
from dso_server.services.map_transition import MapTransitionService
from dso_server.services.map_unlocks import MapUnlockService
from dso_server.services.player_respawn import PlayerRespawnService
from dso_server.services.loot_presence import LootPresenceService
from dso_server.services.presence import PresenceService
from dso_server.services.presence_movement import PresenceMovementService
from dso_server.services.presence_socket_effects import PresenceSocketEffectService
from dso_server.services.presence_identity import PresenceIdentityService
from dso_server.services.presence_visuals import PresenceVisualService
from dso_server.services.social import SocialService
from dso_server.services.commerce import CommerceService
from dso_server.services.trade import TradeService
from dso_server.services.talents import TalentService
from dso_server.services.dispatch import DispatchService
class CharacterServer(
        EventService,
        MapEventService,
        MapCompanionService,
        QuestService,
        AchievementService,
        LifecycleService,
        PlayerInitializationService,
        CharacterSelectionService,
        CustomizationService,
        WorldService,
        MonsterActivityService,
        SocketEffectService,
        PlayerResourceService,
        PlayerProgressionService,
        CombatResolutionService,
        MonsterCombatService,
        MonsterSummonService,
        PVPCombatService,
        MatchmakingService,
        BattlegroundService,
        RankingService,
        WantedThiefService,
        SeasonPassService,
        DailyChallengeService,
        SummonPVPService,
        PlayerSummonService,
        PlayerTrapService,
        PlayerTrailService,
        PlayerProjectileService,
        PlayerCombatService,
        ConsumableService,
        InventoryInteractionService,
        FoodRecyclingService,
        SeasonalActivityService,
        InventoryMapService,
        PremiumBagService,
        MapTransitionService,
        MapUnlockService,
        PlayerRespawnService,
        LootPresenceService,
        PresenceVisualService,
        PresenceIdentityService,
        PresenceSocketEffectService,
        PresenceService,
        PresenceMovementService,
        SocialService,
        CommerceService,
        TradeService,
        TalentService,
        DispatchService,
        RakNetServer):
    """Character selection and gameplay service assembled by domain."""


# Domain mixins use one small composition facade for cross-service calls. This
# avoids circular imports and keeps each service independently importable.
CharacterService.bind(CharacterServer)


__all__ = ["CharacterServer"]
