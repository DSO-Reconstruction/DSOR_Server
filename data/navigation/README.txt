COPY CLIENT NAVIGATION CONTENTS HERE

Copy the contents of the compatible client's export_win32/navigation/ folder
into this folder (data/navigation/), preserving its map subdirectories.
Do not create a second navigation/ folder inside it. For example:
  data/navigation/a0200_kingscity/a0200_kingscity.tok

Also copy export_win32/tables/navshapes.bin from the client to:
  data/navigation/navshapes.bin
This file comes from tables/, not the client's navigation/ folder.

The server uses the .tok meshes and navshapes.bin. Client preprocess files
(.pcr, .ppp, .pus, .path) are not required by the server.
Use the same client content revision as the maps and static database.
Do not copy backup files. Navigation assets are not included in this release.
