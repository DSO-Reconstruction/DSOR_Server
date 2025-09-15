import socket
import struct
import time
import threading

# Global connection state
connected_clients = set()
client_states = {}  # Track ping/pong state for each client
message_0x82_sent = set()  # Track which clients have received the 0x82 message

def server_time():
    global START_TIME
    return int(time.time()*1000) - START_TIME

def send_periodic_ping(server, addr):
    """Send periodic pings to client"""
    if addr not in client_states:
        return
    
    if addr not in connected_clients:
        return
        
    client_states[addr]['ping_count'] += 1
    ping_number = struct.pack('<I', client_states[addr]['ping_count'])[:3]
    
    ping_response = b''.join([
        bytes.fromhex('84'),  # Frame Set packet
        ping_number,  # Ping number
        bytes.fromhex('00'),  # Message flags (unreliable)
        bytes.fromhex('0048'),  # Payload length (72 bits = 9 bytes)
        bytes.fromhex('00'),  # Connected Ping ID
        struct.pack('>Q', server_time()),  # Our timestamp
    ])
    
    try:
        server.sendto(ping_response, addr)
        print(f"Sent periodic Connected Ping #{client_states[addr]['ping_count']} to client")
    except Exception as e:
        print(f"Error sending periodic ping: {e}")

def send_0x82_message(server, addr):
    """Send the 0x82 message with DrasaCharacterService"""
    if addr in message_0x82_sent:
        return
        
    message_0x82_sent.add(addr)
    
    service_name = b"DrasaCharacterService"
    
    # Frame Set packet with 0x82 message (copying exact structure from Wireshark)
    message_0x82 = b''.join([
        bytes.fromhex('84'),      # Frame Set packet
        bytes.fromhex('040000'),  # Frame Set number 4
        bytes.fromhex('60'),      # Message flags (reliable ordered) 
        bytes.fromhex('00c0'),    # Payload length (192 bits)
        bytes.fromhex('01'),      # Reliable message number
        bytes.fromhex('000001'),  # Padding + ordering index
        bytes.fromhex('000000'),  # Ordering channel + padding
        bytes.fromhex('82'),      # Message ID 0x82
        bytes.fromhex('1500'),    # Length (21 bytes in little endian)
        service_name              # Service name "DrasaCharacterService"
    ])
    
    try:
        server.sendto(message_0x82, addr)
        print(f"Sent 0x82 message (DrasaCharacterService) to client {addr}")
    except Exception as e:
        print(f"Error sending 0x82 message: {e}")

def ping_timer(server, addr):
    """Timer function to send pings every few milliseconds"""
    ping_count = 0
    while addr in connected_clients:
        time.sleep(0.01)  # 10ms interval like in the original server
        
        # Stop spamming pings after sending 0x82 message
        if addr in message_0x82_sent:
            break
            
        send_periodic_ping(server, addr)
        ping_count += 1
        
        # Send 0x82 message after some pings (like in the original trace)
        if ping_count == 3 and addr not in message_0x82_sent:
            send_0x82_message(server, addr)
            # Stop the ping timer after sending 0x82
            break

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
        # Debug: log all Frame Set packets received
        print(f"Frame Set packet received: {msg.hex()}")
        # Check if it's a Frame Set (0x84) containing a 0x8a message FIRST
        if msg[0] == 0x84 and bytes.fromhex('8a') in msg[4:]:
            print("0x8a message received from client - sending 0x86 response")
            print(f"Message hex: {msg.hex()}")
            
            # Send 0x86 response with character data (copying exact structure from Wireshark)
            response_0x86 = b''.join([
                bytes.fromhex('84'),      # Frame Set packet
                bytes.fromhex('050000'),  # Frame Set number 5
                bytes.fromhex('60'),      # Message flags (reliable ordered)
                bytes.fromhex('00f1'),    # Payload length (241 bits)
                bytes.fromhex('02'),      # Reliable message number
                bytes.fromhex('000002'),  # Padding + ordering index 2
                bytes.fromhex('000000'),  # Ordering channel + padding
                bytes.fromhex('86'),      # Message ID 0x86
                bytes.fromhex('0a00'),    # Length (10 bytes)
                b'a0000_char',            # Character data
                bytes.fromhex('0a00'),    # Length (10 bytes) 
                b'a0000_char',            # Character data again
                bytes.fromhex('ffffffff0000')  # Terminator
            ])
            
            try:
                server.sendto(response_0x86, addr)
                print(f"Sent 0x86 message (character data) to client {addr}")
                
                # Also send 0x88 message (copying exact structure from Wireshark)
                response_0x88 = b''.join([
                    bytes.fromhex('84'),      # Frame Set packet
                    bytes.fromhex('060000'),  # Frame Set number 6
                    bytes.fromhex('60'),      # Message flags (reliable ordered)
                    bytes.fromhex('0008'),    # Payload length (8 bits)
                    bytes.fromhex('03'),      # Reliable message number
                    bytes.fromhex('000003'),  # Padding + ordering index 3
                    bytes.fromhex('000000'),  # Ordering channel + padding
                    bytes.fromhex('88'),      # Message ID 0x88
                    bytes.fromhex('4300'),    # Data (67 in little endian)
                    bytes.fromhex('00')       # Padding
                ])
                
                server.sendto(response_0x88, addr)
                print(f"Sent 0x88 message to client {addr}")
                
            except Exception as e:
                print(f"Error sending 0x86/0x88 messages: {e}")
            return
            
        # Check if it's a Frame Set (0x84) containing a Connected Ping (0x00)
        elif msg[0] == 0x84 and bytes.fromhex('00') in msg[4:]:
            print(f"Connected Ping received from client (msg length: {len(msg)})")
            
            # Stop responding to pings after 0x82 message is sent
            if addr in message_0x82_sent:
                print("Ignoring ping - 0x82 message already sent")
                return
            
            # Initialize client state if not exists
            if addr not in client_states:
                client_states[addr] = {'ping_count': 0, 'waiting_for_pong': False, 'first_ping_received': False}

            client_time = struct.unpack('>Q', msg[-8:])[0]  # Get client timestamp
            
            # Handle the first ping differently (from New Incoming Connection)
            if not client_states[addr]['first_ping_received']:
                print("First ping from client - starting ping/pong cycle")
                client_states[addr]['first_ping_received'] = True
                client_states[addr]['ping_count'] = 1
                
                # Combine Connected Ping and Connected Pong in one Frame Set packet
                ping_number = struct.pack('<I', client_states[addr]['ping_count'])[:3]  # Little endian, first 3 bytes
                
                combined_response = b''.join([
                    bytes.fromhex('84'),  # Frame Set packet
                    ping_number,  # Frame Set number
                    # Connected Ping (our ping to client)
                    bytes.fromhex('00'),  # Message flags (unreliable)
                    bytes.fromhex('0048'),  # Payload length (72 bits = 9 bytes)
                    bytes.fromhex('00'),  # Connected Ping ID
                    struct.pack('>Q', server_time()),  # Our timestamp
                    # Connected Pong (response to client's ping)
                    bytes.fromhex('00'),  # Message flags (unreliable)
                    bytes.fromhex('0088'),  # Payload length (136 bits = 17 bytes)
                    bytes.fromhex('03'),  # Connected Pong ID
                    struct.pack('>Q', server_time()),  # Our timestamp
                    struct.pack('>Q', client_time),  # Received client timestamp
                ])
                
                try:
                    server.sendto(combined_response, addr)
                    client_states[addr]['waiting_for_pong'] = True
                    print(f"Sent Combined Connected Ping + Pong #{client_states[addr]['ping_count']} to client")
                    
                    # Start periodic ping timer for this client
                    ping_thread = threading.Thread(target=ping_timer, args=(server, addr))
                    ping_thread.daemon = True
                    ping_thread.start()
                    print(f"Started ping timer for client {addr}")
                except Exception as e:
                    print(f"Error sending combined ping/pong: {e}")
                    
            else:
                # Just respond with pong for subsequent pings
                pong_response = b''.join([
                    bytes.fromhex('84'),  # Frame Set packet
                    bytes.fromhex('010000'),
                    # Connected Pong
                    bytes.fromhex('00'),  # Message flags (unreliable)
                    bytes.fromhex('0088'),  # Payload length (136 bits = 17 bytes)
                    bytes.fromhex('03'),  # Connected Pong ID
                    struct.pack('>Q', server_time()),  # Our timestamp
                    struct.pack('>Q', client_time),  # Received client timestamp
                ])

                try:
                    server.sendto(pong_response, addr)
                    print("Sent Connected Pong response")
                except Exception as e:
                    print(f"Error sending pong: {e}")
            
            return
        
        # Check if it's a Frame Set (0x84) containing a Connected Pong (0x03)
        elif msg[0] == 0x84 and bytes.fromhex('03') in msg[4:]:
            print("Connected Pong received from client")
            
            # Initialize client state if not exists
            if addr not in client_states:
                client_states[addr] = {'ping_count': 0, 'waiting_for_pong': False, 'first_ping_received': False}
            
            print(f"Client state: waiting_for_pong={client_states[addr]['waiting_for_pong']}, ping_count={client_states[addr]['ping_count']}")
            
            # Just log that we received a pong, timer handles ping sending
            print("Pong received from client - acknowledged")
            
            return
        
        # Debug: log unknown Frame Set packets
        else:
            print(f"Unknown Frame Set packet received (length: {len(msg)}, hex: {msg[:20].hex()})")

    # Search for New Incoming Connection in Frame Set content
    if bytes.fromhex('13') in msg[10:70]:
        print("New Incoming Connection detected - Client registered")
        connected_clients.add(addr)
        # Initialize client state
        client_states[addr] = {'ping_count': 0, 'waiting_for_pong': False, 'first_ping_received': False}
        
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
        #print("Client Address:", len(pack_address(addr)))              # Should be 7
        #print("Internal Address:", len(pack_address('10.208.115.147', 2190)))  # Should be 7
        #print("System Index:", len(struct.pack('>H', 0)))                        # Should be 2

        server.sendto(response, addr)
    except Exception as e:
        pass

    pass

PORT = 2190
HOST = '127.0.0.2'
SERVER_GUID = bytes.fromhex('1234567890abcdef')

server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server.bind((HOST, PORT))

START_TIME = int(time.time()*1000)

while True:
    msg, addr = server.recvfrom(2048)
    packet_id = msg[0]
    #print(msg)
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
