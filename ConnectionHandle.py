import socket
import struct

def pack_address(addr, port: int = -1):
    if port < 0:
        ip_bytes = bytes(list(map(int, addr[0].split('.')))[::-1])
        port_buffer = struct.pack('>H', addr[1])
    else:
        ip_bytes = bytes(list(map(int, addr.split('.')))[::-1])
        port_buffer = struct.pack('>H', port)
    return b''.join([bytes.fromhex('04'), ip_bytes, port_buffer])

def handle_frame_set_packet(msg, addr, server):
    client_mtu = struct.unpack('>H', msg[-10:-8])[0]
    mtu_size = client_mtu if client_mtu > 0 else 1400
    port_buffer = struct.pack('>H', addr[1])
    ip_bytes = bytes(list(map(int, addr[0].split('.')))[::-1])

    response = b''.join([
        bytes.fromhex('c0'),
        bytes.fromhex('0001'),  
        bytes.fromhex('01'),  
        bytes(3),
        bytes(11),
    ])
    try:
        server.sendto(response, addr)
    except Exception as e:
        pass
    
    response = b''.join([
        bytes.fromhex('84'), #flags
        bytes.fromhex('000000'),  #length in bit
        bytes.fromhex('60'),  #reliable frame index
        bytes.fromhex('0300'),  #Sequenced frame
        bytes(3), # Order frame index
        bytes(3), # Order channel
        bytes(1), # Compound size
        bytes.fromhex('10'), # Compound ID
        # bytes.fromhex('04'), #Compound size
        pack_address(addr), #Address
        struct.pack('>H', 0),
        pack_address('10.208.115.147', 2190),  # 
        pack_address('0.0.0.0', 0),            # 
        pack_address('0.0.0.0', 0),            # 
        pack_address('0.0.0.0', 0),            # 
        pack_address('0.0.0.0', 0),            # 
        pack_address('0.0.0.0', 0),            # 
        pack_address('0.0.0.0', 0),            # 
        pack_address('0.0.0.0', 0),            # 
        pack_address('0.0.0.0', 0),            # 
        pack_address('0.0.0.0', 0),            # 
        struct.pack('>Q', 100),                # Time since start
        struct.pack('>Q', 300000000)           # Time since start
    ])
    try:
        print("Client Address :", len(pack_address(addr)))              # Doit être 7
        print("Internal Address :", len(pack_address('10.208.115.147', 2190)))  # Doit être 7
        print("System Index :", len(struct.pack('>H', 0)))                        # Doit être 2

        server.sendto(response, addr)
    except Exception as e:
        pass

    pass

PORT = 2190
HOST = '0.0.0.0'
SERVER_GUID = bytes.fromhex('1234567890abcdef')

server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server.bind((HOST, PORT))

while True:
    msg, addr = server.recvfrom(2048)
    packet_id = msg[0]

    if packet_id == 0x05: 
        response = b''.join([
            bytes.fromhex('06'),  # Open Connection Reply 1 (0x06)
            bytes.fromhex('00ffff00fefefefefdfdfdfd12345678'),  # Magic bytes
            SERVER_GUID,  # Server GUID
            bytes.fromhex('00008000')  # MTU size (exemple)
        ])
        server.sendto(response, addr)

    elif packet_id == 0x07:  # Open Connection Request 2
        client_mtu = struct.unpack('>H', msg[-10:-8])[0]
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

        try:
            server.sendto(response, addr)
        except Exception as e:
            pass

    elif 0x80 <= packet_id <= 0x8d:  # Frame Set Packet (0x80..0x8d)
        handle_frame_set_packet(msg, addr, server)

server.close()
