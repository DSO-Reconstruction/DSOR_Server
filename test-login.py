import socket
import struct
import threading
import time
import os

# ─── Config ────────────────────────────────────────────────────────────────────
HOST        = '127.0.0.2'
SERVER_GUID = bytes.fromhex('1234567890abcdef')
FRAG_SIZE   = 1208   # bytes per split fragment (MTU 1232 - 24B RakNet overhead)
DATA_DIR    = os.path.dirname(os.path.abspath(__file__))

START_MS = int(time.time() * 1000)

def server_time():
    return int(time.time() * 1000) - START_MS

def pack_addr(ip: str, port: int) -> bytes:
    return b'\x04' + bytes(map(int, ip.split('.'))) + struct.pack('>H', port)

# ─── RakNet frame builder ───────────────────────────────────────────────────────

class FrameBuilder:
    def __init__(self):
        self.frame_seq    = 0
        self.reliable_idx = 0
        self.order_idx    = 0
        self.lock = threading.Lock()

    def _seq(self):
        s = self.frame_seq; self.frame_seq += 1; return s

    def _rel(self):
        r = self.reliable_idx; self.reliable_idx += 1; return r

    def _ord(self):
        o = self.order_idx; self.order_idx += 1; return o

    def _frame_unreliable(self, payload: bytes) -> bytes:
        return b'\x00' + struct.pack('>H', len(payload) * 8) + payload

    def _frame_reliable_ordered(self, payload: bytes) -> bytes:
        return (b'\x60'
                + struct.pack('>H', len(payload) * 8)
                + struct.pack('<I', self._rel())[:3]
                + struct.pack('<I', self._ord())[:3]
                + b'\x00'
                + payload)

    def _frameset(self, *frames: bytes) -> bytes:
        with self.lock:
            seq = self._seq()
        return b'\x84' + struct.pack('<I', seq)[:3] + b''.join(frames)

    def unreliable(self, payload: bytes) -> bytes:
        return self._frameset(self._frame_unreliable(payload))

    def reliable_ordered(self, payload: bytes) -> bytes:
        return self._frameset(self._frame_reliable_ordered(payload))

    def multi_reliable_ordered(self, *payloads: bytes) -> bytes:
        return self._frameset(*[self._frame_reliable_ordered(p) for p in payloads])

    def send_split(self, big_payload: bytes, sock, addr):
        chunks = [big_payload[i:i+FRAG_SIZE] for i in range(0, len(big_payload), FRAG_SIZE)]
        total  = len(chunks)
        with self.lock:
            cid     = 0
            ord_idx = struct.pack('<I', self._ord())[:3]

        for idx, chunk in enumerate(chunks):
            with self.lock:
                seq     = self._seq()
                rel_idx = struct.pack('<I', self._rel())[:3]

            frame = (b'\x70'                           # reliable ordered | split
                     + struct.pack('>H', len(chunk) * 8)
                     + rel_idx
                     + ord_idx
                     + b'\x00'
                     + struct.pack('>I', total)         # compound_size
                     + struct.pack('>H', cid)           # compound_id
                     + struct.pack('>I', idx)           # fragment index
                     + chunk)
            pkt = b'\x84' + struct.pack('<I', seq)[:3] + frame
            try:
                sock.sendto(pkt, addr)
            except OSError:
                return


# ─── Per-client session ─────────────────────────────────────────────────────────

class Session:
    def __init__(self, addr, sock):
        self.addr = addr
        self.sock = sock
        self.fb   = FrameBuilder()
        self.connected = False

    def send(self, data: bytes):
        try:
            self.sock.sendto(data, self.addr)
        except OSError:
            pass

    def ack(self, seq: int):
        self.send(b'\xc0' + b'\x00\x01' + b'\x01' + struct.pack('<I', seq)[:3])


# ─── RakNet server base ─────────────────────────────────────────────────────────

class RakNetServer:
    def __init__(self, host: str, port: int, service_name: str):
        self.host         = host
        self.port         = port
        self.service_name = service_name
        self.sessions: dict[tuple, Session] = {}
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        threading.Thread(target=self._loop, daemon=True).start()
        print(f"[{self.service_name}] :{self.port}")

    def _loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
                threading.Thread(target=self._dispatch, args=(data, addr), daemon=True).start()
            except Exception:
                pass

    def _session(self, addr) -> Session:
        if addr not in self.sessions:
            self.sessions[addr] = Session(addr, self.sock)
        return self.sessions[addr]

    def _dispatch(self, data: bytes, addr):
        if not data: return
        pid = data[0]
        s   = self._session(addr)

        if pid == 0x05:
            self._reply_open1(s)
        elif pid == 0x07:
            self._reply_open2(s, data)
        elif 0x80 <= pid <= 0x8f:
            self._handle_frameset(s, data)

    def _reply_open1(self, s: Session):
        s.send(b'\x06'
               + bytes.fromhex('00ffff00fefefefefdfdfdfd12345678')
               + SERVER_GUID
               + b'\x00'        # no security
               + b'\x05\x98')   # MTU 1432

    def _reply_open2(self, s: Session, data: bytes):
        mtu = struct.unpack('>H', data[-10:-8])[0] if len(data) >= 10 else 1400
        s.send(b'\x08'
               + bytes.fromhex('00ffff00fefefefefdfdfdfd12345678')
               + SERVER_GUID
               + pack_addr(s.addr[0], s.addr[1])
               + struct.pack('>H', mtu)
               + b'\x00')

    def _handle_frameset(self, s: Session, data: bytes):
        seq = int.from_bytes(data[1:4], 'little')
        s.ack(seq)
        for frame in self._parse_frames(data):
            if frame['split']: continue
            self._on_message(s, frame['id'], frame['payload'])

    @staticmethod
    def _parse_frames(data: bytes) -> list:
        frames = []
        offset = 4
        while offset < len(data) - 2:
            if offset >= len(data): break
            flags   = data[offset]; offset += 1
            if offset + 1 >= len(data): break
            bit_len = int.from_bytes(data[offset:offset+2], 'big'); offset += 2
            byte_len = (bit_len + 7) // 8
            rel      = (flags >> 5) & 7
            has_split = bool(flags & 0x10)
            if rel in (2,3,4,6,7): offset += 3
            if rel in (1,3,4):     offset += 4
            split_info = None
            if has_split:
                if offset + 10 > len(data): break
                split_info = {
                    'total': int.from_bytes(data[offset:offset+4], 'big'),
                    'idx':   int.from_bytes(data[offset+6:offset+10], 'big'),
                }
                offset += 10
            if offset >= len(data): break
            payload = data[offset:offset+byte_len]
            frames.append({'id': data[offset], 'payload': payload, 'split': split_info})
            offset += byte_len
        return frames

    def _on_message(self, s: Session, msg_id: int, payload: bytes):
        if msg_id == 0x09:   # Connection Request
            self._send_conn_accepted(s)
        elif msg_id == 0x13: # New Incoming Connection
            s.connected = True
            self._on_connected(s)
        elif msg_id == 0x00: # Connected Ping
            self._send_pong(s, payload)
        elif msg_id == 0x8a: # Client Hello
            self._on_hello(s, payload)
        elif msg_id == 0x8d: # Data request
            self._on_data_request(s)
        elif msg_id == 0x8b: # Character operation
            self._on_char_op(s, payload)
        elif msg_id == 0x1b: # Keepalive
            pass
        elif msg_id == 0x15: # Disconnect
            self.sessions.pop(s.addr, None)

    def _send_conn_accepted(self, s: Session):
        payload = (b'\x10'
                   + pack_addr(s.addr[0], s.addr[1])
                   + b'\x00\x00'
                   + pack_addr('255.255.255.255', 0) * 10
                   + struct.pack('>Q', server_time())
                   + struct.pack('>Q', server_time()))
        s.send(s.fb.reliable_ordered(payload))

    def _send_pong(self, s: Session, payload: bytes):
        client_ts = struct.unpack('>Q', payload[1:9])[0] if len(payload) >= 9 else 0
        pong = b'\x03' + struct.pack('>Q', server_time()) + struct.pack('>Q', client_ts)
        s.send(s.fb.unreliable(pong))

    def _on_connected(self, s: Session):
        svc = self.service_name.encode()
        s.send(s.fb.reliable_ordered(b'\x82' + struct.pack('<H', len(svc)) + svc))

    # Hooks for subclasses
    def _on_hello(self, s: Session, payload: bytes): pass
    def _on_data_request(self, s: Session): pass
    def _on_char_op(self, s: Session, payload: bytes): pass


# ─── Login server (port 2190) ───────────────────────────────────────────────────

class LoginServer(RakNetServer):
    def __init__(self, host: str, char_server_addr: str):
        super().__init__(host, 2190, 'DrasaOnlineLoginServer')
        self.char_server_addr = char_server_addr  # "ip:port"

    def _on_hello(self, s: Session, payload: bytes):
        addr_str = self.char_server_addr.encode()
        redirect = b'\x84\x70\x00' + struct.pack('<H', len(addr_str)) + addr_str + b'\x80'
        # 0x88 (1B) + 0x84(redirect) dans le même FrameSet
        s.send(s.fb.multi_reliable_ordered(b'\x88', redirect))


# ─── Character server (port 2192) ──────────────────────────────────────────────

class CharacterServer(RakNetServer):
    def __init__(self, host: str):
        super().__init__(host, 2192, 'DrasaCharacterService')
        self.char_list   = self._load('dso_char_list.bin')
        self.world_data  = self._load('dso_split_0.bin')
        self.secondary   = self._load('dso_split_1.bin')

    def _load(self, filename: str) -> bytes:
        path = os.path.join(DATA_DIR, filename)
        if os.path.exists(path):
            data = open(path, 'rb').read()
            print(f"  Loaded {filename}: {len(data)//1024}KB")
            return data
        print(f"  WARNING: {filename} not found")
        return b''

    def _on_connected(self, s: Session):
        # 1) Service announce
        super()._on_connected(s)
        # 2) Map load: 0x86("a0000_char") + 0x88 dans le même FrameSet
        map_name = b'a0000_char'
        map_load = (b'\x86'
                    + struct.pack('<H', len(map_name))
                    + map_name
                    + struct.pack('<H', len(map_name))
                    + map_name
                    + b'\xff\xff\xff\xff\x00\x00')
        s.send(s.fb.multi_reliable_ordered(map_load, b'\x88'))

    def _on_data_request(self, s: Session):
        if not self.char_list:
            return
        # Char list
        s.send(s.fb.reliable_ordered(self.char_list))
        # World data (split)
        if self.world_data:
            s.fb.send_split(self.world_data, self.sock, s.addr)
        # Secondary split
        if self.secondary:
            time.sleep(0.05)
            s.fb.send_split(self.secondary, self.sock, s.addr)

    def _on_char_op(self, s: Session, payload: bytes):
        # Subtype byte is payload[1] (after 0x8b ID)
        subtype = payload[1] if len(payload) > 1 else 0
        if subtype == 0x87 and len(payload) > 4:
            op = payload[3] if len(payload) > 3 else 0
            if op == 0x02:
                # Character selected — resend char list
                if self.char_list:
                    s.send(s.fb.reliable_ordered(self.char_list))
                if self.secondary:
                    s.fb.send_split(self.secondary, self.sock, s.addr)
                    s.send(s.fb.reliable_ordered(
                        bytes.fromhex('840c010000000000000000')))
            elif op == 0x03:
                # Play — send confirmation then expect disconnect
                confirm = bytes.fromhex('84870005') + payload[4:8] + b'\x06\x00\x00\x00'
                s.send(s.fb.multi_reliable_ordered(
                    confirm,
                    bytes.fromhex('847000000000')))


# ─── Chat server (port 2191) — stub ────────────────────────────────────────────

class ChatServer(RakNetServer):
    def __init__(self, host: str):
        super().__init__(host, 2191, 'ChatService')

    def _on_hello(self, s: Session, payload: bytes):
        s.send(s.fb.reliable_ordered(b'\x88'))


# ─── Main ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    char_addr = f'{HOST}:2192'

    login = LoginServer(HOST, char_addr)
    chars = CharacterServer(HOST)
    chat  = ChatServer(HOST)

    login.start()
    chars.start()
    chat.start()

    print(f"\nDSO local server running on {HOST}")
    print(f"  2190 → login  (redirect to {char_addr})")
    print(f"  2191 → chat   (stub)")
    print(f"  2192 → chars  (world: {len(chars.world_data)//1024}KB)")
    print("\nCtrl+C to stop\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped.")
