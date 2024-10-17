const dgram = require('dgram');
const server = dgram.createSocket('udp4');


const PORT = 2190;
const HOST = '0.0.0.0';

server.on('message', (msg, rinfo) => {
    console.log(`Message reçu de ${rinfo.address}:${rinfo.port}`);

    console.log(`Contenu binaire (hexdump) : ${msg.toString('hex')}`);

    let header = msg.slice(0, 4);
    let headerHex = header.toString('hex');
    console.log(`En-tête hexadécimal : ${headerHex}`);

    let number = msg.readUInt32BE(12);
    console.log(`Nombre lu à partir de l'offset 12 : ${number}`);
});

server.on('error', (err) => {
    console.error(`Erreur : ${err.stack}`);
    server.close();
});

server.on('listening', () => {
    const address = server.address();
    console.log(`Serveur UDP en écoute sur ${address.address}:${address.port}`);
});

server.bind(PORT, HOST);
