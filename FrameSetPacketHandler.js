import dgram from 'dgram';

const server = dgram.createSocket('udp4');
const PORT = 2190;
const HOST = '0.0.0.0';
const SERVER_GUID = Buffer.from('1234567890abcdef', 'hex');

export default function handleFrameSetPacket(msg, rinfo) {
    const packetID = msg.readUInt8(0);  // Read Packet ID (0x80 to 0x8d)

    if (packetID >= 0x80 && packetID <= 0x8d) {
        console.log(`Frame Set Packet detected with Packet ID: 0x${packetID.toString(16)}`);

        // Read the sequence number (24 bits Little Endian)
        const sequenceNumber = msg.readUIntLE(1, 3);
        console.log(`Sequence Number: ${sequenceNumber}`);

        let offset = 4;  // Start reading after Packet ID and sequence number

        while (offset < msg.length) {
            const flags = msg.readUInt8(offset);
            const reliability = flags >> 5;  // The top 3 bits represent reliability
            const isFragmented = (flags & 0x10) !== 0;  // The fourth bit for fragmentation
            offset += 1;

            console.log(`Reliability: ${reliability}, Fragmented: ${isFragmented}`);

            // Read the body length in bits
            const bodyLengthBits = msg.readUInt16BE(offset);
            const bodyLengthBytes = Math.ceil(bodyLengthBits / 8);
            offset += 2;

            console.log(`Body length in bits: ${bodyLengthBits}, in bytes: ${bodyLengthBytes}`);

            // Check that the body has a valid length before reading
            if (offset + bodyLengthBytes > msg.length) {
                console.error('Error: invalid body length. Attempt to access outside buffer bounds.');
                return; // Exit the function to avoid the error
            }

            // Extract the body of the packet
            const body = msg.slice(offset, offset + bodyLengthBytes);
            console.log(`Packet body: ${body.toString('hex')}`);
            offset += bodyLengthBytes;

            // Check if it's a connection request (0x09)
            if (body.length > 0 && body.readUInt8(0) === 0x09) {
                // Extract connection request information
                const clientGUID = body.readBigUInt64LE(1); // Long (Client GUID)
                const requestTimestamp = body.readBigUInt64LE(9); // Long (timestamp)
                const secure = body.readUInt8(17); // Boolean (security)

                console.log(`Connection request received - Client GUID: ${clientGUID.toString(16)}, Request Timestamp: ${requestTimestamp}, Secure: ${secure}`);

                // Build the "Connection Request Accepted" response packet
                sendConnectionRequestAccepted(rinfo, clientGUID, requestTimestamp);
            }
        }
    } else {
        console.log('Packet not recognized as Frame Set.');
    }
}

// Function to send the "Connection Request Accepted" response
function sendConnectionRequestAccepted(rinfo, clientGUID, requestTimestamp) {
    const responsePacketID = 0x10;  // ID for "Connection Request Accepted"
    const systemIndex = 0x0000;  // Fixed value for system index (usually 0)

    // Construct the response packet
    const clientAddressBuffer = Buffer.from(rinfo.address.split('.').map(num => parseInt(num)));  // Client IP address
    const internalIDs = Buffer.alloc(10);  // 10x address, filled with 0 by default (or 255.255.255.255 for example)
    internalIDs.fill(0); // Fill with 0, adjust as needed

    // Timestamp and current time
    const requestTimeBuffer = Buffer.alloc(8);
    requestTimeBuffer.writeBigUInt64LE(BigInt(requestTimestamp));  // Request timestamp

    const currentTimeBuffer = Buffer.alloc(8);
    currentTimeBuffer.writeBigUInt64LE(BigInt(Date.now()));  // Current time

    // Build the response
    const response = Buffer.concat([
        Buffer.from([responsePacketID]),  // Packet ID
        clientAddressBuffer,               // Client IP address
        Buffer.alloc(2).writeUInt16BE(systemIndex, 0), // System index
        internalIDs,                       // Internal IDs
        requestTimeBuffer,                 // Request time
        currentTimeBuffer                  // Current time
    ]);

    // Wrap the response in a Frame Set Packet
    const frameSetPacket = createFrameSetPacket(response);

    // Send the response
    server.send(frameSetPacket, rinfo.port, rinfo.address, (err) => {
        if (err) {
            console.error('Error sending response:', err);
        } else {
            console.log('Connection Request Accepted response sent.');
        }
    });
}

// Function to create a Frame Set Packet
function createFrameSetPacket(body) {
    const sequenceNumber = 0; // For example, we set a fixed sequence number
    const flags = 0x00; // No fragmentation for now
    const bodyLengthBits = body.length * 8; // Body length in bits

    const frameSetPacket = Buffer.concat([
        Buffer.from([0x84]), // Frame Set Packet ID (0x80..0x8d)
        Buffer.alloc(3).writeUIntLE(sequenceNumber, 0, 3), // Sequence number
        Buffer.from([flags]), // Flags
        Buffer.alloc(2).writeUInt16BE(bodyLengthBits, 0), // Body length in bits
        body // Packet body (Connection Request Accepted)
    ]);

    return frameSetPacket;
}
