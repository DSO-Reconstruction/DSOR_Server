import os
import socket
import pytest
from dso_server.app import ServerApplication


def test_app_multiprocess_initialization(tmp_path, monkeypatch):
    monkeypatch.setenv("DSO_MULTIPROCESS_INSTANCES", "1")
    monkeypatch.setenv("DSO_PLAYER_DATABASE", str(tmp_path / "player.db"))
    monkeypatch.setenv("DSO_SOCIAL_STATE_FILE", str(tmp_path / "social.json"))

    app = ServerApplication(
        host="127.0.0.1",
        account_service_enabled=False,
        admin_service_enabled=False,
    )

    assert app.multiprocess_instances is True
    assert app.dispatcher is not None
    assert app.instance_manager is not None
    assert app.characters.port == 21920
    assert app.dispatcher.public_port == 2192
    assert app.dispatcher.default_worker_port == 21920


def test_app_standard_mode_backward_compatible(tmp_path, monkeypatch):
    monkeypatch.delenv("DSO_MULTIPROCESS_INSTANCES", raising=False)
    monkeypatch.setenv("DSO_PLAYER_DATABASE", str(tmp_path / "player.db"))
    monkeypatch.setenv("DSO_SOCIAL_STATE_FILE", str(tmp_path / "social.json"))

    app = ServerApplication(
        host="127.0.0.1",
        account_service_enabled=False,
        admin_service_enabled=False,
    )

    assert app.multiprocess_instances is False
    assert app.dispatcher is None
    assert app.instance_manager is None
    assert app.characters.port == 2192
