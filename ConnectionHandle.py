import socket
import struct

def handle_frame_set_packet(msg, addr, server):

    pass

PORT = 2190
HOST = '0.0.0.0'
SERVER_GUID = bytes.fromhex('1234567890abcdef')


server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server.bind((HOST, PORT))

print(f"Server listening on {HOST}:{PORT}")

while True:
    msg, addr = server.recvfrom(2048)
    print(f"Message received from {addr[0]}:{addr[1]}")

    packet_id = msg[0]

    if packet_id == 0x05: 
        print('Open Connection Request 1 detected, sending response...')
        response = b''.join([
            bytes.fromhex('06'),  # Open Connection Reply 1 (0x06)
            bytes.fromhex('00ffff00fefefefefdfdfdfd12345678'),  # Magic bytes
            SERVER_GUID,  # Server GUID
            bytes.fromhex('00008000')  # MTU size (exemple)
        ])
        server.sendto(response, addr)

    elif packet_id == 0x07:  # Open Connection Request 2
        print('Open Connection Request 2 detected, sending response...')
        
        client_mtu = struct.unpack('>H', msg[-10:-8])[0]
        print(f"Client MTU size: {client_mtu}")

        mtu_size = client_mtu if client_mtu > 0 else 1400

        port_buffer = struct.pack('>H', addr[1])

        ip_bytes = bytes(list(map(int, addr[0].split('.')))[::-1])

        response = b''.join([
            bytes.fromhex('08'),  # Open Connection Reply 2 (0x08)
            bytes.fromhex('00ffff00fefefefefdfdfdfd12345678'),  # Magic bytes
            SERVER_GUID,  # Server GUID
            bytes.fromhex('04'),  # IP version (4 pour IPv4)
            ip_bytes,  # Adress IP client inversed
            port_buffer,  # Port 
            struct.pack('>H', mtu_size),  # Size MTU
            bytes.fromhex('00')  # Security (false)
        ])

        print('Response buffer:', response.hex())
        print('Response length:', len(response))

        try:
            server.sendto(response, addr)
            print('Open Connection Reply 2 response sent.')
        except Exception as e:
            print(f"Error sending Open Connection Reply 2 response: {e}")

    elif 0x80 <= packet_id <= 0x8d:  # Frame Set Packet (0x80..0x8d)
        handle_frame_set_packet(msg, addr, server)

    else:
        print('Unrecognized message.')

server.close()
