# DSO-SERVER

This repository is a fork used to develop the Drakensang Online server software. The upstream project is [DSO-Reconstruction/DSOR_Server](https://github.com/DSO-Reconstruction/DSOR_Server). This fork is hosted at [xm1rth/DSO-SERVER](https://github.com/xm1rth/DSO-SERVER).

## Required client assets

Game client assets are not included in this repository. On Windows, run `py -3 setup_client_assets.py` after cloning. The script reads the official client's local cache at `%LOCALAPPDATA%\Temp\DSOClient`, extracts the compatible server data files into `data/`, and prepares the local `DSOClient/` CDN cache. It shows the source, destinations, and copy size before continuing. It does not download the game or copy the client executable, DLLs, logs, or local session files.

The client asset extraction and startup setup have been tested with client revision `rel261_192`. Use matching client and server content revisions.

If `%LOCALAPPDATA%\Temp\DSOClient` is missing or incomplete, install the official Drakensang Online client and let it finish downloading/updating. Launch it once, wait until it finishes loading, close it, then run the script again. The script never downloads client files.

On other platforms, copy the following files and folders from a compatible client cache manually:

| Source | Destination in this repository |
| --- | --- |
| `export_win32/db/static.db4` | `data/db/static.db4` |
| Contents of `export_win32/maps/` | `data/maps/` |
| Contents of `export_win32/navigation/` | `data/navigation/` |
| `export_win32/tables/navshapes.bin` | `data/navigation/navshapes.bin` |
| The client's `language/` folder | `data/language/` |
| The client's binary `quests.pbxml` | `data/quests.pbxml` and decoded `data/quests.xml` |
| CDN cache files (`__root.txt`, TOC, hashed resources, and bundles) | `DSOClient/` |

Copy the contents of the `maps` and `navigation` folders directly into their destinations. Do not create an extra nested `maps/` or `navigation/` folder. Preserve the navigation subfolder structure. `navshapes.bin` comes from the client's `tables/` folder.

The server and client must use matching content revisions. In particular, `static.db4`, map files, and navigation files must come from compatible versions. Do not copy backups (`.bak`, `*backup*`) or SQLite runtime sidecars (`-wal`, `-shm`). These client assets are excluded from Git by `.gitignore` and must be supplied locally. The setup script saves the client's binary `quests.pbxml` as `data/quests.pbxml` and decodes its KCAP/BXML quest entries into `data/quests.xml`. An existing `data/quests.xml` is preserved by default; pass `--overwrite-quests-xml` to replace it with the newly decoded client data. If neither a quest XML nor a quest pack is available, the server starts with quest definitions disabled and prints a warning.

## Installation and startup

Install the server from a clean clone in this order:

1. Install 64-bit Python 3.12 or newer.
2. Install the official Drakensang Online client and let it finish downloading and updating. Launch it once, wait until it finishes loading, then close it. Skip this step if `%LOCALAPPDATA%\Temp\DSOClient` already contains a complete client cache.
3. Clone this repository. On Windows, open the repository folder and run `py -3 setup_client_assets.py`. If the client cache is missing or incomplete, the script stops and tells you to finish the official client installation first.
4. Review the source folder, destination paths, and CDN copy size printed by the setup script, then confirm. It extracts the server data, prepares `DSOClient/`, saves `data/quests.pbxml`, and converts its quests to `data/quests.xml`. If an XML file already exists, it is kept; pass `--overwrite-quests-xml` to replace it. On other platforms, copy the assets listed above manually, or run `python3 setup_client_assets.py --source <client-cache-folder>` when the cache is available.
5. Start the server with `start.bat` on Windows or `bash start.sh` on Linux. On first start, the launcher creates `.env` from `.env.example` with `DSO_HOST=127.0.0.1` and `DSO_PUBLIC_HOST=127.0.0.1` if `.env` is absent. Local use needs no additional configuration. To accept remote players, edit `.env`, set `DSO_HOST=0.0.0.0`, and set `DSO_PUBLIC_HOST` to an address reachable by clients.

The server provides the account registration/login API over HTTP on port `8080`. The admin panel uses HTTP port `8081` and listens on loopback by default. Game services use UDP ports `80`, `2190`, `2191`, and `2192`. `DSO_PUBLIC_HOST` must be reachable by game clients.

## Join the game with Launcher GUI

The Windows launcher provides account registration, sign-in, and game startup:

1. Start the server with `start.bat` and keep its window open.
2. Open another terminal in the repository folder and run `py -3 launcher_gui.py`.
3. In **Server IP Address**, enter `127.0.0.1` when playing on the same computer as the server. For a remote server, enter its reachable IP address. The launcher uses API port `8080` by default; include a custom port as `IP:PORT` if the server uses another API port.
4. To create an account, choose **No account yet? Create one**, enter a username and password twice, then click **Create Account**. The launcher signs in and starts the game after registration succeeds. For an existing account, enter the username and password and click **Sign In**. Check **Remember account** to save the account for later launches on this Windows user account.
5. The launcher looks for `dro_client64.exe` in the repository's `DSOClient/dlcache` or `Client/dlcache` folders and the official client's `%LOCALAPPDATA%\Temp\DSOClient\dlcache` folder. If it cannot find the executable, select the client folder when prompted. The setup script does not copy the game executable.

Keep the server running while playing. If sign-in cannot connect, check that the server is running and that the launcher has the correct server IP and API port.

`data/events.json` starts with an empty event schedule. Events can be configured through the server's admin panel.

## Fork and attribution

This project is a fork of [DSO Reconstruction](https://github.com/DSO-Reconstruction/DSOR_Server), a project focused on reverse engineering components related to Drakensang Online. Changes and additions in this repository are developed as part of this fork.

## Copyright, trademarks, and third-party material

This is an independent, unofficial research project. It is not affiliated with, endorsed by, or sponsored by Bigpoint GmbH or the owners of Drakensang Online. “Drakensang Online,” related names, logos, characters, artwork, music, client software, and game content remain the property of their respective rights holders. This repository does not claim ownership of or grant rights to that material.

Client files listed above are not distributed here. Do not commit or redistribute game client files, database dumps, maps, language files, account data, captures, or other third-party material unless you have the necessary rights and permission. Keep local-only assets out of Git. Repository privacy does not grant rights to third-party material.
