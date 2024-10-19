const dgram = require('dgram');
const server = dgram.createSocket('udp4');

const PORT = 2190; 
const HOST = '0.0.0.0';
const SERVER_GUID = Buffer.from('1234567890abcdef', 'hex'); //GUID Example

// Listening UDP Messages
server.on('message', (msg, rinfo) => {
    console.log(`Message received ${rinfo.address}:${rinfo.port}`);
    const headerHex = msg.slice(0, 1).toString('hex');

    //Open Connection Request 1
    if (headerHex === '05') {  // Open Connection Request 1
        console.log('Open Connection Request 1 detected, sending response...');

        // build answer to Open Connection Reply 1
        const response = Buffer.concat([
            Buffer.from('06', 'hex'),   // Open Connection Reply 1 (0x06)
            Buffer.from('00ffff00fefefefefdfdfdfd12345678', 'hex'), // Magic bytes
            SERVER_GUID,  // GUID  serveur
            Buffer.from('00008000', 'hex') // Size of the MTU
        ]);

        server.send(response, rinfo.port, rinfo.address, (err) => {
            if (err) {
                console.error('Error while building the answer :', err);
            } else {
                console.log('Answer Open Connection Reply 1 sent.');
            }
        });
    }

    // Open Connection Request 2
    else if (headerHex === '07') {  // Open Connection Request 2
        console.log('Open Connection Request 2 détecté, envoi de la réponse...');

        const response = Buffer.concat([
            Buffer.from('08', 'hex'),   // Open Connection Reply 2 (0x08)
            Buffer.from('00ffff00fefefefefdfdfdfd12345678', 'hex'), // Magic bytes
            SERVER_GUID,  // GUID server
            Buffer.from(rinfo.address.split('.').map(num => parseInt(num)).reverse()), // Client address, inversed
            Buffer.from('8000', 'hex'),  // Size MTU (exemple)
            Buffer.from('00', 'hex')  // Security (false)
        ]);

        server.send(response, rinfo.port, rinfo.address, (err) => {
            if (err) {
                console.error('Error while sending the answer :', err);
            } else {
                console.log('Answer Open Connection Reply 2 sent.');
            }
        });
    } else {
        console.log('Message not recognized.');
    }
});

server.on('error', (err) => {
    console.error(`Erreur : ${err.stack}`);
    server.close();
});

server.on('listening', () => {
    const address = server.address();
    console.log(`Serveur UDP listening ${address.address}:${address.port}`);
});

// Lancement du serveur UDP
server.bind(PORT, HOST);
