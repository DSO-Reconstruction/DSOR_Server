"""Authentication gateway which redirects clients to login/gameplay."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import LoginServer, Session


class GatewayLoginServer(LoginServer):
    """UDP/80 auth entry point which redirects into the captured service chain."""

    def __init__(self, host: str, login_server_addr: str):
        super().__init__(host, login_server_addr, 80)
        self.service_name = 'DrasaAuthGateway'

    def _send_conn_accepted(self, s: Session, request_payload: bytes):
        # The initial gateway sends accepted + a 16-byte opaque challenge
        # immediately on 0x09. Login/character state remains on later services.
        super()._send_conn_accepted(s, request_payload)
        s.send_reliable_ordered(b'\x82' + os.urandom(16))

    def _on_connected(self, s: Session):
        # The challenge was already sent after 0x09; 0x13 only needs its ACK.
        pass
