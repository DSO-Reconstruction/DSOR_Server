COPY THE CLIENT STATIC DATABASE HERE

Copy the compatible client's export_win32/db/static.db4 to:
  data/db/static.db4

Use the same static.db4 content revision on the client and server, together
with matching maps and navigation files. Protocol indexes depend on row order.
Do not copy .bak files, *backup* files or WAL/SHM sidecars.

The static database is not included in this release. Do not copy development
accounts or players: accounts.sqlite3 and players.sqlite3 are created by the
server on first use.
