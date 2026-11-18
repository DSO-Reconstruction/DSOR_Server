from dso_server.runtime.udp_dispatcher import UdpDispatcher
from dso_server.runtime.instance_manager import InstanceManager


def test_instance_manager_allocation():
    dispatcher = UdpDispatcher(
        public_host="127.0.0.1",
        public_port=26200,
        default_worker_port=26201,
    )
    manager = InstanceManager(
        dispatcher,
        base_worker_port=26202,
        max_workers=5,
    )

    client_addr = ("127.0.0.1", 55555)

    # In test mode without launching python subprocess:
    # Check that route_player_to_map allocates an internal port and sets route
    port = manager._allocate_port_locked()
    assert port == 26202

    # Verify set_client_route works with dispatcher
    dispatcher.set_client_route(client_addr, port)
    assert dispatcher._route_overrides[client_addr] == 26202

    # Remove client route
    dispatcher.remove_client_route(client_addr)
    assert client_addr not in dispatcher._route_overrides
