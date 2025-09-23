import socket
import struct
import time
import threading

# Global connection state
connected_clients = set()
client_states = {}  # Track ping/pong state for each client
message_0x82_sent = set()  # Track which clients have received the 0x82 message
message_0x86_sent = set()  # Track which clients have received the 0x86/0x88 messages

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
    except Exception as e:
        pass

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
    except Exception as e:
        pass

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
        
        # Send 0x82 message after more pings to match official server timing
        if ping_count == 8 and addr not in message_0x82_sent:
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
        
        # Check if it's a Frame Set (0x84) containing a 0x8a message FIRST
        if msg[0] == 0x84 and bytes.fromhex('8a') in msg[4:]:
            # Only respond to first 0x8a message
            if addr in message_0x86_sent:
                return
            
            map = 'a0200_kingscity' # map to be loaded
            payl_len = 2+3+1+(2+len(map))*2
            payl_len = payl_len * 8 + 1     
            # Send 0x86 response (building from scratch with exact values from official server)
            response_0x86 = b''.join([
                bytes.fromhex('84'),      # Frame Set packet 
                bytes.fromhex('050000'),  # Frame Set number 5 
                bytes.fromhex('60'),      # Message flags (reliable ordered) 
                struct.pack(">H", payl_len),    # Payload length
                bytes.fromhex('020000'),        # Reliable message number 
                bytes.fromhex('020000'),        # ordering index 2 
                bytes.fromhex('00'),            # Ordering channel
                bytes.fromhex('86'),            # Message ID 0x86 
                struct.pack("<H", len(map)),    # Length  
                map.encode(),                   # map name
                struct.pack("<H", len(map)),    # Length  
                map.encode(),                   # map name
                bytes.fromhex('ffffffff0000')   # Terminator
            ])
            
            try:
                server.sendto(response_0x86, addr)
                
                # Small delay before 0x88 like official server
                time.sleep(0.001)  # 1ms delay
                
                # Send 0x88 message (exact 18-byte payload like official server)
                response_0x88 = b'\x84\x06\x00\x00\x60\x00\x08\x03\x00\x00\x03\x00\x00\x00\x88\x43\x00\x00'
                
                server.sendto(response_0x88, addr)
                
                # Mark this client as having received 0x86/0x88
                message_0x86_sent.add(addr)
                
            except Exception as e:
                pass
            return
        
        # Check if it's a Frame Set (0x84) containing a 0x8d message
        elif msg[0] == 0x84 and bytes.fromhex('8d') in msg[4:]:
            # Just acknowledge, no specific response needed based on logs
            return
            
        # Check if it's a Frame Set (0x84) containing a 0x1b message
        elif msg[0] == 0x84 and bytes.fromhex('1b') in msg[4:]:
            print(f"RakNet Unknown message ID 0x1b detected from client")
            print(f"Full message hex: {msg.hex()}")
            print(f"About to send 0x1b response...")
            
            # Extract frame number from client message (bytes 1-3)
            client_frame_num = msg[1:4]
            
            # Create response frame number (increment by 1)
            frame_num = int.from_bytes(client_frame_num, 'little') + 1
            response_frame_num = frame_num.to_bytes(3, 'little')
            
            # Send 0x1b response matching client structure more closely
            response_0x1b = b''.join([
                bytes.fromhex('84'),          # Frame Set packet
                response_frame_num,           # Frame Set number (client + 1)
                bytes.fromhex('2000'),        # Message flags (reliable)
                bytes.fromhex('d000'),        # Payload length 
                bytes.fromhex('0000'),        # Reliable frame index
                bytes.fromhex('0400'),        # Ordering index
                bytes.fromhex('0000'),        # Ordering channel
                bytes.fromhex('1b'),          # Message ID 0x1b
                bytes.fromhex('00000000'),    # Data
                bytes.fromhex('0d3122a9'),    # Timestamp part 1
                bytes.fromhex('83750531'),    # Timestamp part 2  
                bytes.fromhex('d000'),        # Additional data
                bytes.fromhex('0000000000000000000000000000')  # Padding
            ])
            
            try:
                server.sendto(response_0x1b, addr)
                print("0x1b response sent successfully!")
            except Exception as e:
                print(f"ERROR sending 0x1b response: {e}")
            return
            
        # Check if it's a Frame Set (0x84) containing a Connected Ping (0x00)
        elif msg[0] == 0x84 and bytes.fromhex('00') in msg[4:]:
            
            # Stop responding to pings after 0x82 message is sent
            if addr in message_0x82_sent:
                return
            
            # Initialize client state if not exists
            if addr not in client_states:
                client_states[addr] = {'ping_count': 0, 'waiting_for_pong': False, 'first_ping_received': False}

            client_time = struct.unpack('>Q', msg[-8:])[0]  # Get client timestamp
            
            # Handle the first ping differently (from New Incoming Connection)
            if not client_states[addr]['first_ping_received']:
                client_states[addr]['first_ping_received'] = True
                client_states[addr]['ping_count'] = 1
                
                # Send Connected Ping + Connected Pong (like official server)
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
                    
                except Exception as e:
                    pass
                    
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
                    # Increment ping count for each ping received from client
                    client_states[addr]['ping_count'] += 1
                    
                    # Send 0x82 after receiving several pings from client (like official server)
                    if client_states[addr]['ping_count'] >= 4 and addr not in message_0x82_sent:
                        send_0x82_message(server, addr)
                        
                except Exception as e:
                    pass
            
            return
        
        # Check if it's a Frame Set (0x84) containing a Connected Pong (0x03)
        elif msg[0] == 0x84 and bytes.fromhex('03') in msg[4:]:
            
            # Initialize client state if not exists
            if addr not in client_states:
                client_states[addr] = {'ping_count': 0, 'waiting_for_pong': False, 'first_ping_received': False}
            
            return

    # Search for New Incoming Connection in Frame Set content
    if bytes.fromhex('13') in msg[10:70]:
        connected_clients.add(addr)
        # Reset client state for new connection
        client_states[addr] = {'ping_count': 0, 'waiting_for_pong': False, 'first_ping_received': False}
        # Clear previous message tracking to allow new responses
        message_0x82_sent.discard(addr)
        message_0x86_sent.discard(addr)
        
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
