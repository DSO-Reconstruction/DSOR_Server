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
