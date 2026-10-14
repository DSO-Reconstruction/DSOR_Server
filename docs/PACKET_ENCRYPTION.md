Encryption status
The data is not encrypted. It is:
Little-endian
Length-prefixed
Bit-packed
Frequently not byte-aligned
Wrapped and fragmented by RakNet
Therefore “decrypting” it means reassembling the RakNet fragments and using an accurate bit-level decoder.
Custom characters are feasible
We can now build a correct encoder that supports:
Adding or removing characters
Custom names
Custom destination maps
Cloning character class/configuration fields
Custom equipment asset identifiers
Custom equipment effects and colors
Correct character/equipment counts
Automatic RakNet fragmentation
Some numeric fields still lack proven semantic names, but they can be preserved from a known-good character while changing the understood fields. Adding a truly new selectable character will also require assigning a unique character ID and making the server recognize that ID when the client clicks “Play Now.”
lines 1-19
