import handleFrameSetPacket from './FrameSetPacketHandler.js';
import dgram from 'dgram';

const server = dgram.createSocket('udp4');
const PORT = 2190;
const HOST = '0.0.0.0';
const SERVER_GUID = Buffer.from('1234567890abcdef', 'hex');

server.on('message', (msg, rinfo) => {
    console.log(`Message received from ${rinfo.address}:${rinfo.port}`);

    const packetID = msg.readUInt8(0);  // Read packet ID

    if (packetID === 0x05) {  // Open Connection Request 1
        console.log('Open Connection Request 1 detected, sending response...');
        const response = Buffer.concat([
            Buffer.from('06', 'hex'),   // Open Connection Reply 1 (0x06)
            Buffer.from('00ffff00fefefefefdfdfdfd12345678', 'hex'), // Magic bytes
            SERVER_GUID,  // Server GUID
            Buffer.from('00008000', 'hex') // MTU size (example)
        ]);
        server.send(response, rinfo.port, rinfo.address);
    } else if (packetID === 0x07) {  // Open Connection Request 2
        console.log('Open Connection Request 2 detected, sending response...');

        // Extract MTU size from the client message
        const clientMtu = msg.readUInt16BE(msg.length - 10);
        console.log(`Client MTU size: ${clientMtu}`);

        // Ensure MTU size is not zero
        const mtuSize = clientMtu > 0 ? clientMtu : 1400; // Use 1400 as default if incorrect

        // Allocate buffer for the port
        const portBuffer = Buffer.alloc(2);
        portBuffer.writeUInt16BE(rinfo.port, 0); // Write client port in big-endian format

        // Build the response
        const response = Buffer.concat([
            Buffer.from('08', 'hex'),   // Open Connection Reply 2 (0x08)
            Buffer.from('00ffff00fefefefefdfdfdfd12345678', 'hex'), // Magic bytes
            SERVER_GUID,  // Server GUID
            Buffer.from('04', 'hex'),   // IP version (4 for IPv4)
            Buffer.from(rinfo.address.split('.').map(num => parseInt(num)).reverse()), // Reversed client IP address
            portBuffer,  // Client port
            Buffer.from(mtuSize.toString(16).padStart(4, '0'), 'hex'),  // MTU size
            Buffer.from('00', 'hex')  // Security (false)
        ]);

        console.log('Response buffer:', response.toString('hex'));
        console.log('Response length:', response.length);

        server.send(response, rinfo.port, rinfo.address, (err) => {
            if (err) {
                console.error('Error sending Open Connection Reply 2 response:', err);
            } else {
                console.log('Open Connection Reply 2 response sent.');
            }
        });

    } else if (packetID >= 0x80 && packetID <= 0x8d) {  // Frame Set Packet (0x80..0x8d)
        handleFrameSetPacket(msg, rinfo);

    } else {
        console.log('Unrecognized message.');
    }
});

server.on('error', (err) => {
    console.error(`Server error: ${err.stack}`);
    server.close();
});

server.on('listening', () => {
    const address = server.address();
    console.log(`Server listening on ${address.address}:${address.port}`);
});

server.bind(PORT, HOST);
