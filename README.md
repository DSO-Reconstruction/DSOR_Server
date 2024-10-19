
# Connection Handshake Protocol

## Open Connection Request 1
**Client → Server**

The client sends this when attempting to join the server.

- **0x05**
- **Magic**
- **Protocol version** (currently 11 or 0x0b)
- **RakNet Null Padding**

The null padding seems to be used to discover the maximum packet size the network can handle. The client will send this to the server with decreasing null padding until the server responds with an **Open Connection Reply 1**.

## Open Connection Reply 1
**Server → Client**

The server responds with this once the client attempts to join.

- **0x06**
- **Magic**
- **Server GUID**
- **ServerHasSecurity** (boolean)
- **Cookie** (uint32, if server has security)
- **MTU Size** (Unsigned short)

This is the first half of the handshake between the client and the server.

## Open Connection Request 2
**Client → Server**

The client responds with this after they receive the **Open Connection Reply 1** packet.

- **0x07**
- **Magic**
- **Cookie** (uint32, if server has security)
- **Client supports security** (Boolean(false), always false for the vanilla client, if server has security)
- **Server Address**
- **MTU Size** (Unsigned short)
- **Client GUID** (Long)

## Open Connection Reply 2
**Server → Client**

This is the last part of the handshake between the client and the server.

- **0x08**
- **Magic**
- **Server GUID** (Long)
- **Client Address**
- **MTU Size**
- **Security** (Boolean)

From here on, all RakNet messages are contained in a **Frame Set Packet**.
