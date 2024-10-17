const dgram = require('dgram');
const server = dgram.createSocket('udp4');


const PORT = 2190;
const HOST = '0.0.0.0';

server.on('message', (msg, rinfo) => {
    console.log(`Message from ${rinfo.address}:${rinfo.port}`);

    console.log(`Binary content (hexdump) : ${msg.toString('hex')}`);

    let header = msg.slice(0, 4);
    let headerHex = header.toString('hex');
    console.log(`Hexa header : ${headerHex}`);

    let number = msg.readUInt32BE(12);
    console.log(`Number read at the offset 12 : ${number}`);
});

server.on('error', (err) => {
    console.error(`Error : ${err.stack}`);
    server.close();
});

server.on('listening', () => {
    const address = server.address();
    console.log(`Serveur UDP listening on : ${address.address}:${address.port}`);
});

server.bind(PORT, HOST);
