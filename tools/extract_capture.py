#!/usr/bin/env python3
"""Turn a pcap of a Drakensang session into the JSONL fixture the tests use.

    python tools/extract_capture.py CharacterSelection1.pcap > tests/fixtures/out.jsonl

Wireshark's own RakNet dissector is used only to split UDP payloads out of the
capture, not to interpret them: the point of the fixture is to let this codebase
be checked against raw bytes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

#: Ports the real services listen on, used to label direction.
SERVER_PORTS = (2190, 2191, 2192)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", help="path to a .pcap or .pcapng file")
    parser.add_argument(
        "--ports",
        type=int,
        nargs="*",
        default=SERVER_PORTS,
        help="server-side UDP ports, to tell the two directions apart",
    )
    args = parser.parse_args()

    fields = ["frame.number", "ip.src", "udp.srcport", "udp.dstport", "udp.payload"]
    command = ["tshark", "-r", args.capture, "-Y", "udp", "-T", "fields"]
    for field in fields:
        command += ["-e", field]

    try:
        output = subprocess.run(
            command, capture_output=True, text=True, check=True
        ).stdout
    except FileNotFoundError:
        print("tshark not found; install wireshark-cli", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(error.stderr.strip(), file=sys.stderr)
        return 1

    written = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 5 or not parts[4]:
            continue
        src_port, dst_port = int(parts[2]), int(parts[3])
        print(
            json.dumps(
                {
                    "frame": int(parts[0]),
                    "src_port": src_port,
                    "dst_port": dst_port,
                    "from_server": src_port in args.ports,
                    "hex": parts[4].replace(":", ""),
                }
            )
        )
        written += 1
    print(f"{written} datagrams", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
