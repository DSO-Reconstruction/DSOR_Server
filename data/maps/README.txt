COPY CLIENT MAP CONTENTS HERE

Copy the contents of the compatible client's export_win32/maps/ folder into
this folder (data/maps/). Do not create a second maps/ folder inside it.
The server needs the .db4, .map and .col files for each installed map, e.g.:
  data/maps/a0200_kingscity.db4
  data/maps/a0200_kingscity.map
  data/maps/a0200_kingscity.col

Use the same client content revision as data/db/static.db4.
Do not copy backup files (.bak, *backup*, WAL or SHM sidecars).
Map assets are not included in this release.
