# Honor leaderboards

The map service handles the leaderboard button's `0x00f4` and `0x00f0`
requests, returning `0x00f5` and `0x00f1`. It also handles `0x00eb` / `0x00ec`
for the detailed PvP table and `0x00f2` / `0x00f3` for local PvP statistics.
Command numbers resolve by class name through `DSO_COMMAND_IDS`.

The list contains existing player profiles, including offline characters, and
uses persisted career honor rather than spendable Badge of Honor currency.
Live sessions overlay pending saves. Equal scores are ordered by character ID.
The class selector uses 0..3 for individual classes and 4 for all classes.
Guild filtering uses actual membership. Friend relationships are not stored
by the current social service, so friend-only queries currently include self.

Pages contain at most ten rows, matching the client UI allocation. The player
page flag centers the page around the requesting character. Explicit offsets
support the client's first/previous/next/last buttons. Requests are read-only:
client ranking update commands are not accepted as score submissions.

Overall honor and the detailed all-time board (4) use current career results.
The detailed table includes honor, wins, losses, draws, kills, and deaths.
Kills and deaths are credited once at match settlement and persist alongside
honor; old saves default these newly tracked counters to zero. Draws remain
zero under the existing match rules. Solo/team/daily/weekly score history is
not available, so those boards return empty tables and an unranked local
response. PvE/parallel-world ranking requests are outside this implementation.

## Verified wire layout

Reconstructed against `dro_client64-patch5.exe` (release261), using its generated
serializers and matching UI functions in the existing patch2 Ghidra analysis:

- `0x140a136d8`: overall list request = start u32, count u32, context u32,
  filter u16, class u8. `ffffffff` with filter `0x10` asks for the player page.
- `0x140a1377c`: overall response = total u32, context u32, status u8,
  row count u32, rows (UTF-8 string, score u32, zero-based rank u32),
  page start u32, filter u16, class u8. Status 0 is used for successful reads.
- `0x140a636ec`: detailed rows = string, zero-based rank u32, six u32
  statistics (honor, wins, losses, draws, kills, deaths).
- `0x14031897c` and `0x140318ca8`: total and start feed the page controls;
  filters `0x20`, `0x40`, `0x80` carry explicit start offsets. `0x10` selects
  the local-player page. Low bits 1/2/4 select all/friends/guild.
- `0x140312d18`: list rank is displayed after adding one.
  `0x1403146dc` uses one-based local-player rank; `ffffffff` means unranked.
- `0x140a134e4`: detailed local response has eight u32 fields, a board u8,
  and one boolean bit. Its actor trailer is therefore not byte aligned.

Every server reply uses application packet `0x84` and appends actor ID 1 as
additional server data. Tests include the exact requests reported by the user,
independent response byte fixtures, pagination, filtering, offline/live state,
malformed packets, and database reload.

## Position/frame telemetry

The accompanying `0x00e9` is
`Commands::ClientCurrentPositionFrameInfoTrackingCommand`, not a ranking
request. The generated decoder at `0x140970a08` reads a character ID, one
double, five uint32 measurements, a counted array of six-uint32 samples, and
two trailing uint32 fields. The server validates and retains this diagnostic
record without applying it to authoritative movement or scores.

## English localization investigation (2026-09-09)

`C:/game-client/export_win32/language/en` contained only 75 XML files;
`C:/game-client/export_win32/language/en` contained 1,886.
The missing 1,811 files included 57 database localization files, 1,749 quest
files, and five other UI/input files. Other language folders still had their
travel-map XML. This explains the `LF:` fallback; the available evidence does
not identify the operation that removed the English files.

With user approval, the missing files were restored from that backup after
XML validation. Existing files were preserved and their SHA-256 hashes
verified unchanged; restored files were verified byte-for-byte against the
backup. The restoration manifest is
`data/diagnostics/english-localization-restore.json`.
`db.travelmap.item_misc_travelmapTitle` now resolves to `Universal Map`.
Restart the client to reload localization, and restart the server to load the
new ranking handlers. Live client UI rendering has not been tested here.
