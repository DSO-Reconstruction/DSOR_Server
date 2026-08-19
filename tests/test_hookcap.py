"""The host side of the Frida capture.

The agent itself cannot be tested here — it needs a running Windows client — so
what is tested is everything that decides what the agent's output *means*: the
record shape, the hook-spec parser, and the stack diffing that turns a pile of
backtraces into a shortlist of candidate functions.

The stack diffing is the part most worth pinning down, because its failure mode
is a confident wrong answer: a frame that looks specific to the traced message
but is really on every send.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "frida"))

import hookcap  # noqa: E402


# ── record shape ────────────────────────────────────────────────────────────


def test_direction_comes_from_the_hook_not_from_an_address():
    """A pcap has to guess direction; a hook knows it."""
    received = hookcap.build_record(1, {"direction": "rx", "peer": "1.2.3.4:2190",
                                        "socket": "9", "hex": "84"})
    sent = hookcap.build_record(2, {"direction": "tx", "peer": "1.2.3.4:2190",
                                    "socket": "9", "hex": "84"})
    assert received["from_server"] is True
    assert sent["from_server"] is False


def test_socket_handle_separates_connections_to_one_endpoint():
    """Three connections shared a server endpoint in the recorded session, so the
    peer address alone is not a connection key."""
    first = hookcap.build_record(1, {"direction": "tx", "peer": "1.2.3.4:31575",
                                     "socket": "100", "hex": "84"})
    second = hookcap.build_record(2, {"direction": "tx", "peer": "1.2.3.4:31575",
                                      "socket": "200", "hex": "84"})
    assert first["conn"] != second["conn"]
    assert first["server_port"] == second["server_port"] == 31575


def test_record_matches_the_fixture_shape():
    """A hook capture has to drop into the existing suite with no conversion."""
    record = hookcap.build_record(7, {"direction": "rx", "peer": "10.0.0.1:2192",
                                      "socket": "3", "hex": "c0000101000000"})
    assert set(record) == {"frame", "from_server", "conn", "server_port", "hex"}
    assert record["frame"] == 7


def test_unparseable_peer_does_not_lose_the_payload():
    """Bytes are the point; a missing address must not discard them."""
    record = hookcap.build_record(1, {"direction": "tx", "peer": None, "hex": "84"})
    assert record["hex"] == "84"
    assert record["server_port"] == 0


# ── hook specs ──────────────────────────────────────────────────────────────


def test_hook_spec_parses_every_option():
    spec = hookcap.parse_hook_spec("client.exe+0xc66dd0:Send:args=6:dump=1:dumplen=32:bt")
    assert spec["module"] == "client.exe"
    assert spec["offset"] == 0xC66DD0
    assert spec["name"] == "Send"
    assert spec["argCount"] == 6
    assert spec["dumpArg"] == 1
    assert spec["dumpLength"] == 32
    assert spec["backtrace"] is True


def test_hook_spec_defaults_are_conservative():
    spec = hookcap.parse_hook_spec("client.exe+0x1000")
    assert spec["dumpArg"] is None, "dumping memory must be opt-in"
    assert spec["backtrace"] is False
    assert spec["name"] == "client.exe+0x1000"


def test_absolute_address_is_refused():
    """ASLR makes an absolute address meaningless in the next run, and hooking
    the wrong place silently is worse than an error."""
    with pytest.raises(ValueError, match="absolute address"):
        hookcap.parse_hook_spec("client.exe+0x140c66dd0")


def test_missing_offset_is_refused():
    with pytest.raises(ValueError, match="module\\+offset"):
        hookcap.parse_hook_spec("client.exe")


def test_bad_offset_is_refused():
    with pytest.raises(ValueError, match="bad offset"):
        hookcap.parse_hook_spec("client.exe+notanumber")


# ── stack diffing ───────────────────────────────────────────────────────────


def test_frames_unique_to_the_target_are_ranked_first():
    target = [
        ["client.exe+0xaaa", "client.exe+0xbbb", "client.exe+0xsend"],
        ["client.exe+0xaaa", "client.exe+0xsend"],
    ]
    baseline = [["client.exe+0xsend", "client.exe+0xccc"]]
    diff = hookcap.diff_stacks(target, baseline)

    assert [frame for frame, _ in diff.exclusive] == [
        "client.exe+0xaaa",
        "client.exe+0xbbb",
    ]
    assert diff.exclusive[0][1] == 2, "seen in both target stacks"
    assert [frame for frame, _ in diff.shared] == ["client.exe+0xsend"]
    assert "1 frame" in diff.verdict or "2 frame" in diff.verdict


def test_a_recursive_frame_does_not_outrank_a_specific_one():
    """Frames are counted once per stack, not once per appearance."""
    target = [["a", "a", "a", "a"], ["b"]]
    diff = hookcap.diff_stacks(target, [["z"]])
    assert dict(diff.exclusive) == {"a": 1, "b": 1}


def test_verdict_reports_when_nothing_is_specific():
    """The honest outcome when RakNet flushes from its own tick: the serialiser
    is simply not on the sendto stack, and the tool must say so."""
    shared = [["raknet+0x1", "raknet+0x2"]]
    diff = hookcap.diff_stacks(shared, shared)
    assert not diff.exclusive
    assert "not on this stack" in diff.verdict
    assert "RakPeer::Send" in diff.verdict


def test_verdict_refuses_to_conclude_without_a_baseline():
    diff = hookcap.diff_stacks([["a", "b"]], [])
    assert "nothing can be ruled out" in diff.verdict


def test_verdict_reports_an_empty_capture():
    assert "no stack was captured" in hookcap.diff_stacks([], []).verdict


# ── end to end ──────────────────────────────────────────────────────────────


def test_verify_accepts_a_real_capture():
    """`verify` is what makes a hook capture trustworthy, so it is exercised
    against the recorded session — whose format a hook capture reproduces."""
    fixture = ROOT / "tests" / "fixtures" / "character_selection.jsonl"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "frida" / "hookcap.py"),
         "verify", str(fixture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "problems    : 0" in result.stdout
    # It must name messages, not just count bytes.
    assert "SERVER_HANDOFF" in result.stdout


def test_agent_is_syntactically_valid_javascript():
    """A syntax error in the agent only shows up when a game is attached, which
    is an expensive place to discover it."""
    node = subprocess.run(["node", "--version"], capture_output=True, check=False)
    if node.returncode != 0:  # pragma: no cover - node is optional
        pytest.skip("node not available")
    result = subprocess.run(
        ["node", "--check", str(ROOT / "tools" / "frida" / "dso_agent.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
