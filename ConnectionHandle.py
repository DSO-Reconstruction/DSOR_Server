import socket
import struct
import time

# Global connection state
connected_clients = set()

def server_time():
    global START_TIME
    return int(time.time()*1000) - START_TIME

def pack_address(addr, port: int = -1):
    if port < 0:
        ip_bytes = bytes(list(map(int, addr[0].split('.')))[::-1])
        port_buffer = struct.pack('>H', addr[1])
    else:
        ip_bytes = bytes(list(map(int, addr.split('.')))[::-1])
        port_buffer = struct.pack('>H', port)
    return b''.join([bytes.fromhex('04'), ip_bytes, port_buffer])

def handle_frame_set_packet(msg, addr, server):
    # If client is already connected, ignore new packets except for ping
    if addr in connected_clients:
        # Check if it's a Frame Set (0x84) containing a Connected Ping (0x00)
        if msg[0] == 0x84 and bytes.fromhex('00') in msg[10:30] :

            client_time = struct.unpack('>Q', msg[-8:])[0]  # Get client timestamp
               # Our server timestamp
            # Response with Connected Pong in a Frame Set
            pong_response = b''.join([
                bytes.fromhex('84'),  # Frame Set packet
                bytes.fromhex('010000'),
                # Connected Ping
                bytes.fromhex('00'),  # Message flags (unreliable)
                bytes.fromhex('0048'),  # Payload length (72 bits = 9 bytes)
                bytes.fromhex('00'),  # Connected Ping ID
                struct.pack('>Q', client_time),  # Our timestamp
                # First Connected Pong
                bytes.fromhex('00'),  # Message flags (unreliable)
                bytes.fromhex('0088'),  # Payload length (136 bits = 17 bytes)
                bytes.fromhex('03'),  # Connected Pong ID
                struct.pack('>Q', server_time()),  # Our timestamp
                struct.pack('>Q', client_time),  # Received client timestamp
                # Second Connected Pong
                bytes.fromhex('00'),  # Message flags (unreliable)
                bytes.fromhex('0088'),  # Payload length (136 bits = 17 bytes)
                bytes.fromhex('03'),  # Connected Pong ID
                struct.pack('>Q', server_time()),  # Our timestamp + 16ms
                struct.pack('>Q', client_time),  # Received client timestamp
            ])

            try:
                server.sendto(pong_response, addr)
            except Exception as e:
                pass

            # Second Ping
            ping_response = b''.join([
                bytes.fromhex('84'),  # Frame Set packet
                bytes.fromhex('020000'), # Ping number 2
                bytes.fromhex('00'),  # Message flags (unreliable)
                bytes.fromhex('0048'),  # Payload length (72 bits = 9 bytes)
                bytes.fromhex('00'),  # Connected Ping ID
                struct.pack('>Q', client_time),  # Our timestamp
            ])
            try:
                server.sendto(ping_response, addr)
            except Exception as e:
                pass
            return
        
        # Check if it's a Frame Set (0x84) containing a Connected Pong (0x03)
        elif msg[0] == 0x84 and bytes.fromhex('03') in msg[10:70]:

            print("Connected Pong received - Sending Connected Ping")
            # Response with a single Connected Ping in a Frame Set
            ping_response = b''.join([
                bytes.fromhex('84'),  # Frame Set packet
                cp,
                bytes.fromhex('00'),  # Message flags (unreliable)
                bytes.fromhex('0048'),  # Payload length (72 bits = 9 bytes)
                bytes.fromhex('00'),  # Connected Ping ID
                struct.pack('>Q', server_time()),  # Our timestamp
            ])
            try:
                server.sendto(ping_response, addr)
            except Exception as e:
                pass
            return

    # Search for New Incoming Connection in Frame Set content
    if bytes.fromhex('13') in msg[10:70]:
        print("New Incoming Connection detected - Client registered")
        connected_clients.add(addr)
        
        # Send ACK for New Incoming Connection
        ack_response = b''.join([
            bytes.fromhex('c0'),  # ACK packet
            bytes.fromhex('0001'),  # Sequence number
            bytes.fromhex('01'),  # Version
            bytes(3),  # Padding
            bytes(11)  # Padding
        ])
        try:
            server.sendto(ack_response, addr)
        except Exception as e:
            pass
        return

    # If not a New Incoming Connection, continue with Connection Request Accepted
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
        print("Client Address:", len(pack_address(addr)))              # Should be 7
        print("Internal Address:", len(pack_address('10.208.115.147', 2190)))  # Should be 7
        print("System Index:", len(struct.pack('>H', 0)))                        # Should be 2

        server.sendto(response, addr)
    except Exception as e:
        pass

    pass

PORT = 2190
HOST = '0.0.0.0'
SERVER_GUID = bytes.fromhex('1234567890abcdef')

server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server.bind((HOST, PORT))

START_TIME = int(time.time()*1000)

while True:
    msg, addr = server.recvfrom(2048)
    packet_id = msg[0]
    print(msg)
    if packet_id == 0x05: 
        response = b''.join([
            bytes.fromhex('06'),  # Open Connection Reply 1 (0x06)
            bytes.fromhex('00ffff00fefefefefdfdfdfd12345678'),  # Magic bytes
            SERVER_GUID,  # Server GUID
            bytes.fromhex('00008000')  # MTU size (example)
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
            bytes.fromhex('04'),  # IP version (4 for IPv4)
            ip_bytes,  # Client IP address reversed
            port_buffer,  # Port 
            struct.pack('>H', mtu_size),  # MTU Size
            bytes.fromhex('00')  # Security (false)
        ])

        try:
            server.sendto(response, addr)
        except Exception as e:
            pass

    elif 0x80 <= packet_id <= 0x8d:  # Frame Set Packet (0x80..0x8d)
        handle_frame_set_packet(msg, addr, server)

server.close()
