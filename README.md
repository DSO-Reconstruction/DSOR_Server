# DSO-SERVER

This repository is a fork used to develop the Drakensang Online server software. The upstream project is [DSO-Reconstruction/DSOR_Server](https://github.com/DSO-Reconstruction/DSOR_Server). This fork is hosted at [xm1rth/DSO-SERVER](https://github.com/xm1rth/DSO-SERVER).

## Required client assets

Game client assets are not included in this repository. Before starting the server, copy the following files and folders from a compatible client version:

| Source | Destination in this repository |
| --- | --- |
| `export_win32/db/static.db4` | `data/db/static.db4` |
| Contents of `export_win32/maps/` | `data/maps/` |
| Contents of `export_win32/navigation/` | `data/navigation/` |
| `export_win32/tables/navshapes.bin` | `data/navigation/navshapes.bin` |
| The client's `language/` folder | `data/language/` |
| The client's `quests.xml` file | `data/quests.xml` |
| The complete client `DSOClient/` folder | `DSOClient/` |

Copy the contents of the `maps` and `navigation` folders directly into their destinations. Do not create an extra nested `maps/` or `navigation/` folder. Preserve the navigation subfolder structure. `navshapes.bin` comes from the client's `tables/` folder.

The server and client must use matching content revisions. In particular, `static.db4`, map files, and navigation files must come from compatible versions. Do not copy backups (`.bak`, `*backup*`) or SQLite runtime sidecars (`-wal`, `-shm`). These client assets are excluded from Git by `.gitignore` and must be supplied locally. The server will not start without the required assets.

## Installation and startup

Install 64-bit Python 3.12 or newer. After cloning the repository:

1. Copy the required client assets listed above.
2. Copy `.env.example` to `.env`.
3. For local use, keep `DSO_PUBLIC_HOST=127.0.0.1`. To accept connections from other computers, set `DSO_HOST=0.0.0.0` and set `DSO_PUBLIC_HOST` in `.env` to an address reachable by clients.
4. On Windows, run `start.bat`. On Linux, run `bash start.sh`.

The server provides the account registration/login API over HTTP on port `8080`. The admin panel uses HTTP port `8081` and listens on loopback by default. Game services use UDP ports `80`, `2190`, `2191`, and `2192`. `DSO_PUBLIC_HOST` must be reachable by game clients.

`data/events.json` starts with an empty event schedule. Events can be configured through the server's admin panel.

## Fork and attribution

This project is a fork of [DSO Reconstruction](https://github.com/DSO-Reconstruction/DSOR_Server), a project focused on reverse engineering components related to Drakensang Online. Changes and additions in this repository are developed as part of this fork.

## Copyright, trademarks, and third-party material

This is an independent, unofficial research project. It is not affiliated with, endorsed by, or sponsored by Bigpoint GmbH or the owners of Drakensang Online. “Drakensang Online,” related names, logos, characters, artwork, music, client software, and game content remain the property of their respective rights holders. This repository does not claim ownership of or grant rights to that material.

Client files listed above are not distributed here. Do not commit or redistribute game client files, database dumps, maps, language files, account data, captures, or other third-party material unless you have the necessary rights and permission. Keep local-only assets out of Git. Repository privacy does not grant rights to third-party material.

This repository is a fork, but attribution alone does not grant permission to copy, modify, or redistribute upstream code. At the time of writing, the upstream repository does not show a `LICENSE` file. No license for upstream contributions or third-party material is claimed or granted by this README. Confirm the applicable license or obtain permission from the relevant rights holders before redistributing this fork or granting others rights to its contents. No `LICENSE` file is provided for this repository.

Bigpoint's [published Terms and Conditions](https://legal.bigpoint.com/PH/terms-and-conditions/en-GB) include restrictions concerning reproduction, analysis, or modification of games and game content. Check the terms and game rules that apply to your region and use; this project does not assert that any such activity is authorized. The upstream project's [README](https://github.com/DSO-Reconstruction/DSOR_Server) describes its reverse-engineering purpose.

This notice is informational only. A disclaimer or attribution does not make otherwise unauthorized use lawful, establish a license, or remove existing copyright obligations. It is not legal advice; consult a qualified lawyer about rights and permissions for this repository.
