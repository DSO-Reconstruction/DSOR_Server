# Every command the client knows

365 command classes, recovered from the retail binary by name with
`tools/command_ids.py`: each class registers a Nebula3 `Rtti` naming it, and its
id sits at vtable slot 3 as `mov eax, imm32; ret`. The tool reproduces every id
this project had already established by other means, which is what makes the rest
believable.

Two things worth knowing before using this table.

**The ids are one flat space.** All 365 are distinct — there is no per-service or
per-direction numbering, so a command id identifies a command outright. A list
that shows the same id twice has a mistake in it.

**Several namespaces carry commands**, not one. `Commands` holds 282 of the 365;
`Achievements`, `Chat`, `PVP`, `Ranking`, `StatusEffects`, `Talents` hold their own; and eleven register
under their bare class name with no namespace at all — which is how the skill
commands are declared. Enumerating only `Commands::` misses 83 of them.

`•` marks the ones this server currently reads or writes.

Three that lost their mark are worth naming, because the knowledge outlived the code.
`0x003C`, `0x003D` and `0x003E` had a codec that round-tripped the recorded payloads to
the byte — header, then per entry a 16-bit index, a flag, a length-prefixed shape class,
345 bits of geometry, a length-prefixed effect name and 460 bits, with the split fixed by
the fact that only it leaves the last entry ending exactly where the actor begins. They
carry **no position**: neither the recording caster's wire coordinates nor their described
form appears anywhere in the payload, in any byte order or bit alignment, and the actor is
0 — the client places them from the skill use it sent itself. The module went out with the
rest of the effects code when it was rebuilt; the layout is in the git history under
"Read the client's own asset bundles".

`0x0080 CurrencyChangedCommand` appears in no capture, because a currency only changes
when something is spent or earned. The account's andermant travels in the roster instead,
at 160 bits past an entry's map string — see the README.

`0x0054 InventoryInfoCommand` is read field by field now rather than replayed, and the
grammar came out of the client's own decoder: `Commands::InventoryInfoCommand` registers
its `Rtti` with the fourcc `'IvIC'`, whose creator reaches a constructor that plants the
vtable at `0x14115fcd8`, and slot 5 of that vtable is a flat run of reader calls — twelve
count-prefixed collections then nine scalars. Two of the collections are named: the
dictionary at `+0x58` maps an item to its cell in the bag, and the one at `+0xa8` maps an
item to the slot it is *worn* in, which has exactly fourteen entries in the live capture
against a character's fourteen equipment slots. The last two scalars are the player's
current health and current resource — the same pair `0x007B` carries. See
`dsor/inventory.py`.


## Commands — 282

| id | command | |
|---|---|---|
| `0x0001` | MasterServerInfoCommand | • |
| `0x0002` | MapServerInfoCommand | • |
| `0x0003` | ForkCommand | • |
| `0x0004` | ForkResultCommand |  |
| `0x0005` | AcpForceLogoutCommand |  |
| `0x0006` | AcpServerShutdownCommand |  |
| `0x0007` | AcpSendMessageToPlayerCommand |  |
| `0x0008` | AcpSendMessageToAllPlayersCommand | • |
| `0x0009` | SuspendMasterServerCommand |  |
| `0x000A` | ResumeMasterServerCommand | • |
| `0x000D` | KillMapServerCommand |  |
| `0x000E` | MapStatusCommand |  |
| `0x000F` | PlayerStatusCommand | • |
| `0x0010` | PlayerDeletedCommand | • |
| `0x0011` | ForceTransactionRequestCommand |  |
| `0x0012` | ForceLogoutCommand | • |
| `0x0013` | MembershipBookInfoCommand | • |
| `0x0014` | LoginQueueWaitCommand | • |
| `0x0015` | LoginQueueFullQueueCommand | • |
| `0x0016` | LoginQueueFullInstanceCommand |  |
| `0x0017` | LoginQueueInstanceNotReadyCommand |  |
| `0x0018` | ClientJoinedMapServerCommand |  |
| `0x0019` | ForcePlayerUpdateCommand |  |
| `0x001A` | InstanceConfigCommand |  |
| `0x001B` | InstanceConfigClientInfoCommand | • |
| `0x001C` | ActorRequestCommand | • |
| `0x001D` | NewPlayerCommand | • |
| `0x001E` | DiscardPlayerCommand |  |
| `0x001F` | PlayerReadyCommand |  |
| `0x0020` | PlayerUpdateCommand | • |
| `0x0021` | NewRemotePlayerCommand |  |
| `0x0022` | RemotePlayerInfoCommand |  |
| `0x0023` | UpdateShowPlayerOverheadIconsCommand |  |
| `0x0024` | MembershipUpdateCommand |  |
| `0x0025` | SettingsInfoCommand |  |
| `0x0026` | NewNPCCommand |  |
| `0x0027` | DiscardNPCCommand |  |
| `0x0028` | NPCInfoCommand |  |
| `0x0029` | NPCRequestCommand | • |
| `0x002A` | NewMonsterCommand | • |
| `0x002B` | DiscardMonsterCommand | • |
| `0x002C` | MonsterUpdateCommand |  |
| `0x002D` | NewItemCommand | • |
| `0x002E` | DiscardItemCommand | • |
| `0x002F` | ItemInfoCommand |  |
| `0x0030` | ItemUpdateCommand |  |
| `0x0031` | SalvageCommand | • |
| `0x0032` | SalvageCookItemCommand |  |
| `0x0033` | GlyphRemovalCommand | • |
| `0x0034` | NewPropCommand |  |
| `0x0035` | DiscardPropCommand |  |
| `0x0036` | PropInfoCommand | • |
| `0x0037` | PropUpdateCommand |  |
| `0x0038` | PropRequestCommand |  |
| `0x0039` | PropInteractionCommand | • |
| `0x003A` | NewDestroyableCommand |  |
| `0x003B` | DestroyablePropUpdateCommand |  |
| `0x0043` | VariationInfoCommand |  |
| `0x0044` | ExitInfoCommand |  |
| `0x0045` | PositionMarkerInfoCommand |  |
| `0x0050` | QuickSlotsInfoCommand |  |
| `0x0051` | QuickSlotsCommand |  |
| `0x0052` | QuickbarInfoCommand |  |
| `0x0053` | LockQuickbarCommand |  |
| `0x0054` | InventoryInfoCommand | • |
| `0x0055` | InventoryCommand |  |
| `0x0056` | GemRemovalResponseCommand |  |
| `0x0057` | CurrencyConversionCommand |  |
| `0x0059` | RequestOfferCommand |  |
| `0x005A` | OfferCommand |  |
| `0x005B` | TransactionCommand |  |
| `0x005C` | CraftingCommand |  |
| `0x005D` | CraftingResultCommand |  |
| `0x005E` | CraftingResultAcceptanceCommand |  |
| `0x005F` | MoveCommand | • |
| `0x0060` | UnlockMapCommand |  |
| `0x0061` | TravelCommand |  |
| `0x0062` | RespawnCommand |  |
| `0x0063` | LogoutCommand |  |
| `0x0064` | PickupItemCommand | • |
| `0x0065` | PickupInfoCommand |  |
| `0x0066` | EncounterCommand |  |
| `0x0067` | TalkCommand |  |
| `0x0068` | DeliverCommand |  |
| `0x0069` | PayCommand |  |
| `0x006A` | DefendCommand |  |
| `0x006B` | HitCommand | • |
| `0x006C` | KillCommand | • |
| `0x006D` | ReviveCommand |  |
| `0x006E` | ResurrectCommand |  |
| `0x006F` | StillAliveCommand |  |
| `0x0070` | SwitchMapCommand | • |
| `0x0071` | ForceTransformCommand |  |
| `0x0072` | ObstacleUpdateCommand |  |
| `0x0073` | ActorsLeftVicinityCommand | • |
| `0x0074` | ActorsEnterVicinityCommand | • |
| `0x0075` | SpectatorCameraCommand |  |
| `0x0076` | SpectateActorCommand |  |
| `0x0077` | UpdateSpectatorCameraCommand |  |
| `0x0078` | SetEventParticipationCommand |  |
| `0x0079` | UseTravelItemCommand |  |
| `0x007A` | FactionInfoCommand |  |
| `0x007B` | ActorStatsUpdateCommand | • |
| `0x007C` | PlayerLevelUpdateCommand | • |
| `0x007D` | XPChangedCommand | • |
| `0x007E` | GroupXPChangedCommand | • |
| `0x007F` | HonorPointsChangedCommand |  |
| `0x0080` | CurrencyChangedCommand |  |
| `0x0081` | ShowDeathDialogCommand |  |
| `0x0082` | TimedOffersInfoCommand |  |
| `0x0083` | TimedChallengesInfoCommand | • |
| `0x0084` | SpecialOfferCommand | • |
| `0x0085` | ConversionStoryInfoCommand | • |
| `0x0087` | CharacterSelectionCommand | • |
| `0x0088` | NotificationCommand | • |
| `0x0089` | MessageCommand |  |
| `0x008A` | QuestCommand | • |
| `0x008B` | QuestUpdateCommand | • |
| `0x008C` | QuestLogInfoCommand |  |
| `0x008D` | QuestMonsterIndicationRequestCommand | • |
| `0x008E` | QuestMonsterIndicationInfoCommand |  |
| `0x008F` | QuestTriggerIndicationRequestCommand |  |
| `0x0090` | QuestTriggerIndicationInfoCommand |  |
| `0x0091` | QuestDisableCommand |  |
| `0x0092` | ChapterUnlockedCommand |  |
| `0x0093` | ChapterProgressCommand |  |
| `0x0094` | DailyChallengeSelectionCommand |  |
| `0x0095` | DailyChallengeProgressCommand |  |
| `0x0096` | GroupListCommand |  |
| `0x0097` | GroupStatusCommand |  |
| `0x0098` | GroupFoundationCommand |  |
| `0x0099` | GroupFoundationResponseCommand |  |
| `0x009A` | GroupInviteCommand |  |
| `0x009B` | GroupMultipleInviteCommand |  |
| `0x009C` | GroupInvitationResponseCommand |  |
| `0x009D` | GroupJoinRequestCommand |  |
| `0x009E` | GroupJoinRequestResponseCommand |  |
| `0x009F` | GroupKickCommand |  |
| `0x00A0` | GroupLeaveCommand |  |
| `0x00A1` | GroupEditCommand |  |
| `0x00A2` | GroupEditResponseCommand |  |
| `0x00A3` | GroupingFailedCommand |  |
| `0x00A4` | GroupCandidatesRequestCommand |  |
| `0x00A5` | GroupCandidatesResponseCommand |  |
| `0x00A6` | GuildListCommand |  |
| `0x00A7` | GuildStatusCommand | • |
| `0x00A8` | GuildFoundationCommand |  |
| `0x00A9` | GuildFoundationResponseCommand |  |
| `0x00AA` | GuildInviteCommand |  |
| `0x00AB` | GuildInvitationResponseCommand |  |
| `0x00AC` | GuildKickCommand |  |
| `0x00AD` | GuildLeaveCommand |  |
| `0x00AE` | GuildNameEditCommand |  |
| `0x00AF` | GuildNameEditResponseCommand |  |
| `0x00B0` | BuddyListCommand |  |
| `0x00B1` | BuddyStatusCommand |  |
| `0x00B2` | IgnoredListCommand |  |
| `0x00B3` | IgnoredStatusCommand |  |
| `0x00B4` | PlayerQueryCommand |  |
| `0x00B5` | PlayerQueryResultCommand |  |
| `0x00B6` | GroupQueryCommand |  |
| `0x00B7` | GroupQueryResultCommand |  |
| `0x00B8` | GroupMemberQueryCommand |  |
| `0x00B9` | GroupMemberQueryResultCommand |  |
| `0x00BA` | GuildMessageOfTheDayCommand |  |
| `0x00BB` | GuildMessageOfTheDayRequestCommand |  |
| `0x00BC` | GuildMessageOfTheDayChangeCommand |  |
| `0x00BD` | SocialUpdateLevelCommand |  |
| `0x00BE` | SocialRenamePlayerCommand |  |
| `0x00BF` | TeamStatusCommand |  |
| `0x00DB` | NewEventCommand |  |
| `0x00DC` | DiscardEventCommand |  |
| `0x00DD` | EventUpdateCommand | • |
| `0x00DE` | ActiveEventsCommand |  |
| `0x00DF` | EventParticipationInfoCommand |  |
| `0x00E0` | EventProgressInfoCommand |  |
| `0x00E1` | EventProgressChangedCommand |  |
| `0x00E7` | PreloadRessourceCommand |  |
| `0x00E8` | ClientAverageFrameInfoTrackingCommand |  |
| `0x00E9` | ClientCurrentPositionFrameInfoTrackingCommand |  |
| `0x00EA` | ClientLogEventCommand | • |
| `0x0100` | ActivateGodModeCommand |  |
| `0x0101` | GodModeKillCommand |  |
| `0x0102` | GodModeTeleportCommand |  |
| `0x0103` | GodModeCreateItemCommand |  |
| `0x0104` | GodModeToggleLootDropCommand |  |
| `0x0105` | DebugMonsterInfoCommand |  |
| `0x0106` | DebugMonsterRequestCommand |  |
| `0x0107` | DebugSpecialOfferCommand |  |
| `0x0108` | DebugDamageComputationSkillCommand |  |
| `0x0109` | DebugDamageComputationEffectCommand |  |
| `0x010A` | PaymentForceLogoutCommand |  |
| `0x010B` | OCPOfferRequestCommand | • |
| `0x010C` | OCPMethodsCommand | • |
| `0x010D` | OCPOffersChangedCommand |  |
| `0x010E` | OCPOfferResponseCommand | • |
| `0x010F` | OCPBookingCommand |  |
| `0x0110` | RedeemVoucherCommand |  |
| `0x0111` | RedeemVoucherResponseCommand |  |
| `0x0112` | OpenPaymentDeeplinkCommand |  |
| `0x0113` | StagingSignalCommand |  |
| `0x0115` | SetTitleCommand |  |
| `0x0120` | RequestFindRandomMonsterCommand |  |
| `0x0121` | BeginAutoplayCommand |  |
| `0x0122` | EndAutoplayCommand |  |
| `0x0123` | QuickResurrectGroupMemberCommand |  |
| `0x0124` | CharacterServiceAccountLogoutCommand |  |
| `0x0125` | QuestTriggerSignalCommand |  |
| `0x0126` | GauntletInfoCommand |  |
| `0x0127` | GauntletResultCommand |  |
| `0x0128` | GauntletAbortionCommand |  |
| `0x0129` | GauntletConversionCommand |  |
| `0x012A` | GauntletStartCommand |  |
| `0x012B` | GauntletNotificationInfoCommand |  |
| `0x012C` | GauntletNotificationResponseCommand |  |
| `0x012D` | FastPayPurchaseSuccessfulCommand |  |
| `0x012E` | SendQuickbarsCommand |  |
| `0x012F` | StickerbookInfoCommand |  |
| `0x0130` | StickerbookItemInfoCommand |  |
| `0x0131` | UpdateStickerbookFavoriteCommand |  |
| `0x0132` | UpdateStickerbookFavoritePosCommand |  |
| `0x0133` | AddStickerbookFromInventoryCommand |  |
| `0x0134` | UpgradeStickerbookItemCommand |  |
| `0x0135` | UseStickerBookItemCommand | • |
| `0x0136` | GameCurrencyChangedCommand |  |
| `0x0137` | RecipeBookInfoCommand |  |
| `0x0138` | RecipeUpdateCommand |  |
| `0x0139` | AcceptedTierSpenderChallengesCommand |  |
| `0x013A` | UpdateProgressTierSpenderChallengesCommand |  |
| `0x013B` | MapAccessLockedCommand |  |
| `0x013D` | GroupXPFactorChangeCommand |  |
| `0x013F` | RevenueRecognitionCommand |  |
| `0x0140` | ItemCustomizationInfoCommand |  |
| `0x0141` | ItemCustomizationUpdateCommand |  |
| `0x0142` | ApplyItemCustomizationChangesCommand |  |
| `0x0143` | SkinPurchaseResponseCommand |  |
| `0x0144` | InformPlayerLoginStateCommand |  |
| `0x0145` | DeltaDNACommand |  |
| `0x0146` | PlayerLoginInfoCommand |  |
| `0x0147` | PlayerLogoutInfoCommand |  |
| `0x014D` | ActivityGetExtraBonusCommand |  |
| `0x014E` | UseSteamInventoryCommand |  |
| `0x014F` | UseSteamInventoryResultCommand |  |
| `0x0150` | GetCraftingCakesInfoCommand |  |
| `0x0151` | GetCraftingCakesInfoResultCommand |  |
| `0x0152` | GetCraftingCakesAwardCommand |  |
| `0x0153` | GetPaymentSigninInfoCommand |  |
| `0x0154` | GetPaymentSigninInfoResultCommand |  |
| `0x0155` | GetPaymentSigninAwardCommand |  |
| `0x0156` | GetPaymentLoginInfoCommand |  |
| `0x0157` | GetPaymentLoginInfoResultCommand |  |
| `0x0158` | GetCameBackSigninInfoCommand |  |
| `0x0159` | GetCameBackSigninInfoResultCommand |  |
| `0x015A` | GetCameBackSigninAwardCommand |  |
| `0x015B` | UserHatchEggsInfoCommand |  |
| `0x015C` | OperateHatchEggsCommand |  |
| `0x015D` | SelectWishingWellMsgCommand |  |
| `0x015E` | GetSeasonPassRewardsInfoCommand |  |
| `0x015F` | GetSeasonPassRewardsInfoResultCommand |  |
| `0x0160` | GetSeasonPassAwardCommand |  |
| `0x0161` | AddSeasonPassExpCommand |  |
| `0x0162` | GetSeasonPassMissionsInfoCommand |  |
| `0x0163` | GetSeasonPassMissionsInfoResultCommand |  |
| `0x0164` | GetWantedThiefInfoCommand |  |
| `0x0165` | GetWantedThiefInfoResultCommand |  |
| `0x0166` | AcceptWantedThiefMissionCommand |  |
| `0x0167` | RefreshWantedThiefMissionCommand |  |
| `0x0168` | KickOutToHubLimitTimeLeftCommand |  |
| `0x0169` | KillMonsterGetTotalScoresCommand |  |
| `0x016A` | GetHighScoreRankAllDataCommand |  |
| `0x016B` | GetHighScoreRankAllDataResultCommand |  |
| `0x016C` | GetSelfEndlessTowerDataCommand |  |
| `0x016D` | GetEndlessTowerAllBuffsCommand |  |
| `0x016E` | GetEndlessTowerAllBuffsResultCommand |  |
| `0x016F` | GetHighScoreRankGroupDataCommand |  |
| `0x0170` | GetHighScoreRankGroupDataResultCommand |  |
| `0x0171` | GetHighRankFirstPassAwardCommand |  |
| `0x0172` | GetSecondDaySignedAwardCommand |  |
| `0x0173` | GetSecondDaySignedAwardResultCommand |  |
| `0x0174` | GetBlackFridayTransactionsInfoCommand |  |
| `0x0175` | GetBlackFridayTransactionsInfoResultCommand |  |
| `0x0176` | ServerToClientMsgCommand |  |

## PVP — 28

| id | command | |
|---|---|---|
| `0x00C0` | PVPFlagCommand |  |
| `0x00C1` | PVPMatchOverCommand |  |
| `0x00C2` | PVPMatchStartCommand |  |
| `0x00C3` | PVPMatchCountdownCommand |  |
| `0x00C4` | PVPMatchInfoCommand |  |
| `0x00C5` | PVPEventCommand |  |
| `0x00C6` | PVPStateCommand |  |
| `0x00C7` | LeavePVPCommand |  |
| `0x00C8` | RefillPVPServerCommand |  |
| `0x00C9` | StageInfoCommand |  |
| `0x00CA` | PVPMatchDescriptionCommand |  |
| `0x00CB` | SetBattlegroundIdCommand |  |
| `0x00CC` | ShowPVPDeathDialogCommand |  |
| `0x00CD` | PVPEventProgressUpdateCommand |  |
| `0x00CE` | JoinMatchQueueCommand |  |
| `0x00CF` | JoinMatchQueueResponseCommand |  |
| `0x00D0` | JoinMatchQueueInfoCommand |  |
| `0x00D1` | AbortMatchQueueCommand |  |
| `0x00D2` | JoinBattleGroundCommand |  |
| `0x00D3` | JoinBattlegroundResponseCommand |  |
| `0x00D4` | BattlegroundInviteCommand |  |
| `0x00D5` | BattlegroundInviteResponseCommand |  |
| `0x00D6` | BattlegroundEventCommand |  |
| `0x00D7` | MatchMakingInfoCommand |  |
| `0x00D8` | AutoBalanceRequestCommand |  |
| `0x00D9` | EnableAutoBalanceCommand |  |
| `0x00DA` | BattlegroundRegistrationResultCommand |  |
| `0x013C` | ResetMatchQueuesCommand |  |

## Ranking — 21

| id | command | |
|---|---|---|
| `0x00EB` | HonorRankingInfoCommand | • |
| `0x00EC` | HonorRankingResponseCommand | • |
| `0x00ED` | HonorRankingMatchUpdateCommand |  |
| `0x00EE` | HonorRankingMatchUpdateResponseCommand |  |
| `0x00EF` | HonorRankingUpdateCommand |  |
| `0x00F0` | HonorOverallUserRankingInfoCommand |  |
| `0x00F1` | HonorOverallUserRankingResponseCommand |  |
| `0x00F2` | HonorLocalUserRankingInfoCommand |  |
| `0x00F3` | HonorLocalUserRankingInfoResponseCommand |  |
| `0x00F4` | HonorOverallLocalUserRankingInfoCommand |  |
| `0x00F5` | HonorOverallLocalUserRankingInfoResponseCommand |  |
| `0x00F6` | ParallelWorldUserRankingInfoCommand |  |
| `0x00F7` | ParallelWorldUserRankingResponseCommand |  |
| `0x00F8` | ParallelWorldLocalUserRankingInfoCommand |  |
| `0x00F9` | ParallelWorldLocalUserRankingResponseCommand |  |
| `0x00FA` | ParallelWorldUserRankingUpdateCommand |  |
| `0x00FB` | ParallelWorldUserRankingUpdateResponseCommand |  |
| `0x00FC` | OverallUserRankingInfoCommand |  |
| `0x00FD` | OverallUserRankingResponseCommand |  |
| `0x00FE` | OverallLocalUserRankingInfoCommand |  |
| `0x00FF` | OverallLocalUserRankingInfoResponseCommand | • |

## (unqualified) — 11

| id | command | |
|---|---|---|
| `0x0046` | SkillCommand |  |
| `0x0047` | TargetSkillCommand | • |
| `0x0048` | BulletSkillCommand |  |
| `0x0049` | TargetPointBulletSkillCommand |  |
| `0x004A` | ShiftedSkillCommand |  |
| `0x004B` | TargetBulletSkillCommand |  |
| `0x004C` | SustainedSkillCommand |  |
| `0x004D` | SkillStopCommand |  |
| `0x0058` | SkillBookInfoCommand |  |
| `0x0086` | CharacterGenerationCommand | • |
| `0x011B` | TalentSelectionInfoCommand |  |

## StatusEffects — 9

| id | command | |
|---|---|---|
| `0x003C` | NewLocationEffectCommand |  |
| `0x003D` | DiscardLocationEffectCommand |  |
| `0x003E` | LocationEffectInfoCommand |  |
| `0x003F` | NewTrapCommand |  |
| `0x0040` | DiscardTrapCommand | • |
| `0x0041` | TrapInfoCommand | • |
| `0x0042` | TrapTrippedCommand |  |
| `0x004E` | StatusEffectInfoCommand |  |
| `0x004F` | StatusEffectCommand | • |

## Talents — 8

| id | command | |
|---|---|---|
| `0x0117` | SelectTalentCommand |  |
| `0x0118` | DeselectTalentCommand |  |
| `0x0119` | EnableTalentPreselectionCommand |  |
| `0x011A` | TalentCategoryPointsInfoCommand |  |
| `0x011C` | UnlockSkillTalentPreselectionCommand |  |
| `0x011D` | UnlockSkillTalentPreselectionInfoCommand |  |
| `0x011E` | ResetTalentsCommand |  |
| `0x011F` | ResetTalentCategoryCommand |  |

## Chat — 4

| id | command | |
|---|---|---|
| `0x00E2` | ChatClientLoggedInCommand |  |
| `0x00E3` | ChatErrorReportCommand |  |
| `0x00E6` | ChatServiceStartedCommand |  |
| `0x04D2` | BuiltinChatCommand |  |

## Achievements — 2

| id | command | |
|---|---|---|
| `0x0114` | AchievementInfoCommand | • |
| `0x0116` | ClearDungeonInfoCommand |  |
