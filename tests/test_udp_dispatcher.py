import socket
import time
from dso_server.runtime.udp_dispatcher import UdpDispatcher


def test_udp_dispatcher_bidirectional_routing():
    public_port = 26100
    worker1_port = 26101
    worker2_port = 26102

    w1_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    w1_sock.bind(("127.0.0.1", worker1_port))
    w1_sock.settimeout(2.0)

    w2_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    w2_sock.bind(("127.0.0.1", worker2_port))
    w2_sock.settimeout(2.0)

    dispatcher = UdpDispatcher(
        public_host="127.0.0.1",
        public_port=public_port,
        default_worker_port=worker1_port,
    )
    dispatcher.start()

    try:
        # Client explicitly bound to loopback
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_sock.bind(("127.0.0.1", 0))
        client_sock.settimeout(2.0)
        client_addr = client_sock.getsockname()

        # 1. Client sends initial packet to public port
        client_sock.sendto(b"packet_to_city", ("127.0.0.1", public_port))

        # Worker 1 should receive it
        data, w1_sender = w1_sock.recvfrom(1024)
        assert data == b"packet_to_city"

        # Worker 1 can resolve the real client address from dispatcher!
        real_client = dispatcher.get_real_client_addr(w1_sender)
        assert real_client == client_addr

        # Worker 1 replies
        w1_sock.sendto(b"reply_from_city", w1_sender)

        # Client receives reply directly from public port
        reply, sender = client_sock.recvfrom(1024)
        assert reply == b"reply_from_city"
        assert sender == ("127.0.0.1", public_port)

        # 2. Now client enters dungeon -> route updated to Worker 2
        dispatcher.set_client_route(client_addr, worker2_port)

        # Client sends packet to the SAME public port
        client_sock.sendto(b"packet_to_dungeon", ("127.0.0.1", public_port))

        # Worker 2 should receive it now!
        data2, w2_sender = w2_sock.recvfrom(1024)
        assert data2 == b"packet_to_dungeon"

        # Worker 2 also resolves the real client address!
        real_client2 = dispatcher.get_real_client_addr(w2_sender)
        assert real_client2 == client_addr

        # Worker 2 replies
        w2_sock.sendto(b"reply_from_dungeon", w2_sender)

        # Client receives reply from public port
        reply2, sender2 = client_sock.recvfrom(1024)
        assert reply2 == b"reply_from_dungeon"
        assert sender2 == ("127.0.0.1", public_port)

    finally:
        dispatcher.stop()
        w1_sock.close()
        w2_sock.close()
        client_sock.close()
