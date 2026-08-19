#!/usr/bin/env python3
"""Host side of the Frida capture: run the agent, then make sense of what it says.

Three subcommands:

``capture``
    Attach to (or spawn) the client, stream every UDP payload, and write it as
    JSONL in the same shape as ``tests/fixtures/*.jsonl`` — so a hook capture
    drops straight into the existing test suite and codec with no conversion.

``verify``
    Run a captured file through ``raknet/`` and ``dsor/`` and report what parsed,
    what re-encoded identically, and which application messages were seen.  This
    is the loop that makes a capture trustworthy: bytes the codec cannot
    round-trip are either a codec gap or a bad capture, and either way you want
    to know before building on them.

``analyse``
    Diff call stacks.  Stacks recorded while a chosen message was being sent are
    compared with stacks recorded otherwise, and frames appearing only in the
    former are ranked.  Those are the candidates for the code that builds that
    message — which is the whole point of hooking rather than sniffing.

The frida module is imported only inside ``capture``, so verification and
analysis work on a machine with no Frida and no game.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

AGENT = Path(__file__).with_name("dso_agent.js")

#: Service ports that are fixed. Anything else is a map server, whose port the
#: client learns at runtime from a 0x84/SERVER_HANDOFF message.
SERVICE_PORTS = (2190, 2191, 2192)


# ── record building ─────────────────────────────────────────────────────────


def build_record(index: int, message: dict) -> dict:
    """Turn an agent datagram message into a fixture-shaped record.

    Direction comes from the hook rather than being inferred from an address,
    which removes the guesswork a pcap needs — and the peer address here is the
    real one, not a NAT translation.
    """
    peer = message.get("peer") or "unknown:0"
    host, _, port = peer.rpartition(":")
    try:
        server_port = int(port)
    except ValueError:
        server_port = 0
    socket_handle = message.get("socket") or "?"
    return {
        "frame": index,
        "from_server": message["direction"] == "rx",
        # The socket handle disambiguates several connections to one endpoint,
        # which the recorded session does three times over.
        "conn": f"{host}:{server_port}/sock{socket_handle}",
        "server_port": server_port,
        "hex": message["hex"],
    }


# ── stack analysis ──────────────────────────────────────────────────────────


@dataclass
class StackDiff:
    """Frames seen while sending the target message, ranked by exclusivity."""

    exclusive: list[tuple[str, int]] = field(default_factory=list)
    shared: list[tuple[str, int]] = field(default_factory=list)
    target_stacks: int = 0
    other_stacks: int = 0

    @property
    def verdict(self) -> str:
        """A plain reading of whether the diff is worth acting on."""
        if self.target_stacks == 0:
            return "no stack was captured for the target message"
        if self.other_stacks == 0:
            return (
                "only target stacks were captured, so nothing can be ruled out; "
                "record a baseline by capturing without --pattern"
            )
        if not self.exclusive:
            return (
                "every frame also appears in unrelated sends, which is what "
                "happens when RakNet flushes from its own tick: the serialiser is "
                "not on this stack. Hook RakPeer::Send by offset instead."
            )
        return f"{len(self.exclusive)} frame(s) appear only when sending the target"


def diff_stacks(target: list[list[str]], baseline: list[list[str]]) -> StackDiff:
    """Rank stack frames by how specific they are to *target*.

    A frame present in every send is RakNet's own plumbing.  A frame present only
    when the interesting message goes out is the code that produced it.
    """
    target_counts: collections.Counter[str] = collections.Counter()
    for stack in target:
        # Counted once per stack, not once per appearance: a recursive frame
        # would otherwise outrank a more specific one.
        target_counts.update(set(stack))
    baseline_frames = {frame for stack in baseline for frame in stack}

    exclusive = [
        (frame, count)
        for frame, count in target_counts.items()
        if frame not in baseline_frames
    ]
    shared = [
        (frame, count)
        for frame, count in target_counts.items()
        if frame in baseline_frames
    ]
    exclusive.sort(key=lambda item: (-item[1], item[0]))
    shared.sort(key=lambda item: (-item[1], item[0]))
    return StackDiff(exclusive, shared, len(target), len(baseline))


def parse_hook_spec(spec: str) -> dict:
    """Parse ``module+0xoffset[:name][:args=N][:dump=N][:dumplen=N][:bt]``.

    The offset is what a disassembler shows.  An absolute address is refused
    because ASLR makes it meaningless in the next run, and silently hooking the
    wrong place is worse than an error.
    """
    parts = spec.split(":")
    location = parts[0]
    if "+" not in location:
        raise ValueError(
            f"{spec!r}: expected module+offset, e.g. 'client.exe+0xc66dd0'"
        )
    module, _, offset_text = location.partition("+")
    try:
        offset = int(offset_text, 0)
    except ValueError as error:
        raise ValueError(f"{spec!r}: bad offset {offset_text!r}") from error
    # An offset inside a module is far below 4 GiB, while a Windows x64 image is
    # based at 0x140000000, so anything above 4 GiB is an absolute address that
    # someone pasted straight out of a disassembler.
    if offset >= 0x100000000:
        raise ValueError(
            f"{spec!r}: {hex(offset)} looks like an absolute address. Subtract the "
            "image base (0x140000000 for a Windows x64 PE) and pass a "
            "module-relative offset, because ASLR moves the base every run."
        )

    out = {
        "module": module,
        "offset": offset,
        "name": None,
        "argCount": 4,
        "dumpArg": None,
        "dumpLength": 64,
        "backtrace": False,
        "dumpOnReturn": False,
        "bitsArg": None,
        "minBits": 0,
    }
    for part in parts[1:]:
        if part == "bt":
            out["backtrace"] = True
        elif part == "ret":
            # Required for any function whose buffer is an output: it is only
            # filled by the time the call returns.
            out["dumpOnReturn"] = True
        elif part.startswith("args="):
            out["argCount"] = int(part[5:], 0)
        elif part.startswith("dumpbits="):
            # Size each dump from a bit-count argument, so it matches exactly what
            # the call read and can never overrun the destination.
            out["bitsArg"] = int(part[9:], 0)
        elif part.startswith("minbits="):
            # Skip small reads. Hooking a function called 100k times floods the
            # channel and slows the client enough to distort what it does.
            out["minBits"] = int(part[8:], 0)
        elif part.startswith("dumplen="):
            out["dumpLength"] = int(part[8:], 0)
        elif part.startswith("dump="):
            out["dumpArg"] = int(part[5:], 0)
        else:
            out["name"] = part
    if out["name"] is None:
        out["name"] = f"{module}+{hex(offset)}"
    return out


# ── subcommands ─────────────────────────────────────────────────────────────


def command_capture(args: argparse.Namespace) -> int:
    try:
        import frida
    except ImportError:
        print(
            "the frida module is required for capture: pip install frida-tools",
            file=sys.stderr,
        )
        return 1

    hooks = [parse_hook_spec(spec) for spec in args.hook or []]

    if args.host:
        device = frida.get_device_manager().add_remote_device(args.host)
    else:
        device = frida.get_local_device()

    datagrams = open(args.output, "w")
    stacks = open(args.stacks, "w") if args.stacks else None
    # Hooked calls are the most valuable output there is -- they are what a
    # packet capture cannot give -- so they get a file of their own. Printing
    # them was a mistake: Python's stdout is block-buffered when piped, so a run
    # ended with Ctrl-C loses whatever is still in the buffer, which was every
    # call it had recorded.
    calls = open(args.calls, "w") if args.calls else None
    counts: collections.Counter[str] = collections.Counter()
    written = [0]

    def on_message(message, data):
        if message["type"] != "send":
            print(f"[agent] {message}", file=sys.stderr)
            return
        payload = message["payload"]
        kind = payload.get("kind")
        counts[kind] += 1
        if kind == "datagram":
            written[0] += 1
            datagrams.write(json.dumps(build_record(written[0], payload)) + "\n")
        elif kind == "backtrace":
            if stacks is not None:
                stacks.write(json.dumps(payload) + "\n")
        elif kind == "call":
            if calls is not None:
                calls.write(json.dumps(payload) + "\n")
                calls.flush()
            else:
                print(
                    f"[call] {payload['name']} args={payload['args']} "
                    f"dump={payload.get('dump')}"
                )
            if stacks is not None and payload.get("frames"):
                stacks.write(json.dumps(payload) + "\n")
        elif kind == "ready":
            print(
                f"[ready] frida={payload.get('frida')} "
                f"sockets={payload['sockets']} hooks={payload['hooks']}"
            )
            if not payload["sockets"]:
                print(
                    "[warn] no socket hook installed yet; the deferred resolver "
                    "will report [late] lines if the modules appear",
                    file=sys.stderr,
                )
        elif kind == "hook-late":
            # Worth surfacing: it means the module was not there at startup and
            # the deferred resolver caught it, so anything sent in that window
            # was missed.
            print(
                f"[late] {payload['symbol']} hooked after {payload['afterMs']} ms "
                f"({payload.get('module')})"
            )
        elif kind == "warning":
            print(f"[warn] {payload['message']}", file=sys.stderr)

    agent_source = AGENT.read_text()
    options = {
        "tracePattern": args.pattern,
        "hooks": hooks,
        "maxBacktraces": args.max_stacks,
    }
    sessions = []

    def instrument(target, label, gate_children=False):
        """Attach, install the agent, start it, and return the live session.

        *gate_children* holds any process this one launches suspended until it has
        been instrumented too. It is enabled per session rather than per device:
        ``enable_child_gating`` is a Session method, while the ``child-added``
        signal it feeds is emitted by the Device.
        """
        session = device.attach(target)
        script = session.create_script(agent_source)
        script.on("message", on_message)
        script.load()
        script.exports_sync.start(options)
        sessions.append((session, script))
        if gate_children:
            # Applied to every session, not just the first, so a chain of
            # launcher -> updater -> client is followed all the way down.
            session.enable_child_gating()
        print(f"[instrumented] {label}")
        return session

    pid = None
    if args.spawn:
        # Spawning is what makes the early exchanges visible. Attaching to a
        # client that is already in-game misses the whole login sequence: the
        # handshake, the 0x8A credential, the 0x84 server handoff and the map
        # load all happen in the first seconds and never repeat.
        # argv[0] is the program path by convention, and frida treats a list as
        # the full argv. A game launched by a launcher usually needs both the
        # arguments the launcher passed and its working directory: without the
        # right cwd the client cannot resolve its data assigns and exits before
        # any network activity, which looks like the hook failing.
        argv = [args.spawn] + (args.spawn_arg or [])
        spawn_kwargs = {}
        if args.cwd:
            spawn_kwargs["cwd"] = args.cwd
        print(f"[spawn] {' '.join(argv)}")
        if args.cwd:
            print(f"[spawn] cwd={args.cwd}")
        pid = device.spawn(argv, **spawn_kwargs)

        if args.follow_children:
            # A launcher that spawns the real client would otherwise escape
            # instrumentation entirely: the interesting process is the child.
            def on_child(child):
                print(f"[child] pid {child.pid} {child.path or ''}")
                try:
                    instrument(child.pid, f"child pid {child.pid}", gate_children=True)
                finally:
                    # The child is held suspended by the gate until resumed, so
                    # this must run even if instrumenting it failed, or the game
                    # simply hangs with no explanation.
                    device.resume(child.pid)

            device.on("child-added", on_child)

        # Attach before resuming: gating has to be armed on the session while the
        # process is still suspended, otherwise a child launched immediately at
        # startup slips through.
        instrument(pid, f"spawned pid {pid}", gate_children=args.follow_children)
    else:
        target = int(args.target) if args.target.isdigit() else args.target
        instrument(target, f"attached to {target}")

    if pid is not None:
        device.resume(pid)

    print("capturing; press Ctrl-C to stop")
    try:
        sys.stdin.read()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            print(f"[stats] {script.exports_sync.stats()}")
        except Exception:  # pragma: no cover - the process may already be gone
            pass
        datagrams.close()
        if stacks is not None:
            stacks.close()
        if calls is not None:
            calls.close()
        for session, _ in sessions:
            try:
                session.detach()
            except Exception:  # pragma: no cover - process may be gone
                pass
    print(f"wrote {written[0]} datagrams to {args.output}: {dict(counts)}")
    if args.calls:
        print(f"wrote {counts['call']} hooked calls to {args.calls}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    """Push a capture through the codec and report what it understood."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from dsor.messages import parse_message
    from raknet.constants import DatagramFlag
    from raknet.datagram import (
        build_ack,
        build_datagram_header,
        parse_ack_payload,
        parse_datagram_header,
    )
    from raknet.frame import build_frame, parse_frames

    totals: collections.Counter[str] = collections.Counter()
    names: collections.Counter[str] = collections.Counter()
    problems: list[str] = []
    pending: dict[tuple, dict] = collections.defaultdict(dict)

    for line in Path(args.input).read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        raw = bytes.fromhex(record["hex"])
        totals["datagrams"] += 1
        if not raw[0] & DatagramFlag.IS_VALID:
            totals["offline"] += 1
            continue
        try:
            header, offset = parse_datagram_header(raw)
            if header.is_ack or header.is_nak:
                payload = parse_ack_payload(raw, 1, header.has_bandwidth_figure)
                totals["ack"] += 1
                if payload.arrival_rate is None:
                    if build_ack(payload.ranges, is_nak=header.is_nak) != raw:
                        problems.append(
                            f"frame {record['frame']}: ack re-encode differs"
                        )
                continue
            frames = parse_frames(raw, offset)
            totals["data"] += 1
            rebuilt = build_datagram_header(header.sequence, header.flags) + b"".join(
                build_frame(frame) for frame in frames
            )
            if rebuilt != raw:
                problems.append(f"frame {record['frame']}: re-encode differs")
            stream = (record.get("conn"), record["from_server"])
            for frame in frames:
                if frame.is_split:
                    buffer = pending[stream].setdefault(frame.split_id, {})
                    buffer[frame.split_index] = frame.payload
                    if len(buffer) != frame.split_count:
                        continue
                    body = b"".join(buffer[i] for i in range(frame.split_count))
                    del pending[stream][frame.split_id]
                else:
                    body = frame.payload
                if not body:
                    continue
                names[parse_message(body).name] += 1
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            problems.append(f"frame {record['frame']}: {type(error).__name__}: {error}")

    print(f"datagrams   : {totals['datagrams']}")
    print(f"  data      : {totals['data']}")
    print(f"  ack/nak   : {totals['ack']}")
    print(f"  offline   : {totals['offline']}")
    print(f"problems    : {len(problems)}")
    for problem in problems[:10]:
        print(f"  {problem}")
    incomplete = sum(len(v) for v in pending.values())
    print(f"split messages never completed: {incomplete}")
    print("application messages:")
    for name, count in names.most_common(args.top):
        print(f"  {count:7d}  {name}")
    return 1 if problems else 0


def command_analyse(args: argparse.Namespace) -> int:
    """Rank stack frames that are specific to the traced message."""
    target: list[list[str]] = []
    baseline: list[list[str]] = []
    for path, bucket in ((args.stacks, target), (args.baseline, baseline)):
        if path is None:
            continue
        for line in Path(path).read_text().splitlines():
            if not line.strip():
                continue
            frames = json.loads(line).get("frames") or []
            if frames:
                bucket.append(frames)

    diff = diff_stacks(target, baseline)
    print(f"target stacks   : {diff.target_stacks}")
    print(f"baseline stacks : {diff.other_stacks}")
    print(f"verdict         : {diff.verdict}")
    if diff.exclusive:
        print("\nframes seen only while sending the target message:")
        for frame, count in diff.exclusive[: args.top]:
            print(f"  {count:5d}  {frame}")
        print(
            "\nLook these up in the disassembler. The deepest frame that is not "
            "RakNet is the likely serialiser; hook it with "
            "--hook 'module+0xoffset:name:args=6:dump=1'."
        )
    if args.show_shared and diff.shared:
        print("\nframes common to other sends (RakNet plumbing, most likely):")
        for frame, count in diff.shared[: args.top]:
            print(f"  {count:5d}  {frame}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="run the agent against the client")
    group = capture.add_mutually_exclusive_group(required=True)
    group.add_argument("--target", help="process name or pid to attach to")
    group.add_argument("--spawn", help="executable to launch and attach to")
    capture.add_argument("--host", help="frida-server host, for a remote Windows box")
    capture.add_argument("-o", "--output", default="hookcap.jsonl")
    capture.add_argument("--stacks", help="file to write call stacks to")
    capture.add_argument(
        "--calls",
        help="file to write hooked function calls to. Strongly recommended with "
        "--hook: without it the calls are only printed, and printed output is "
        "lost if the run ends with Ctrl-C while stdout is piped",
    )
    capture.add_argument(
        "--pattern",
        help="hex pattern; sending a payload containing it records a stack "
        "(e.g. 8b5f00 for the client bulk message)",
    )
    capture.add_argument("--max-stacks", type=int, default=40)
    capture.add_argument(
        "--spawn-arg",
        action="append",
        metavar="ARG",
        help="argument to pass to the spawned executable; repeat for each one. "
        "Use the =VALUE form: --spawn-arg=-standalone. The game's arguments all "
        "start with a dash, and argparse would read a dashed value as another "
        "option. Or put everything after a bare -- instead",
    )
    capture.add_argument(
        "--cwd",
        help="working directory for the spawned executable. Often required: a "
        "client started from the wrong directory cannot find its data and exits "
        "before it ever opens a socket",
    )
    capture.add_argument(
        "--follow-children",
        action="store_true",
        help="instrument processes the spawned one launches, for a launcher that "
        "starts the real client",
    )
    capture.add_argument(
        "--hook",
        action="append",
        help="module+0xoffset[:name][:args=N][:dump=N][:dumplen=N][:dumpbits=N]"
        "[:minbits=N][:bt][:ret]. dumpbits=N sizes the dump from argument N read "
        "as a bit count; minbits=N skips reads smaller than that. "
        "Use :ret for a function that fills a buffer, so it is read after the "
        "call rather than before",
    )
    capture.set_defaults(func=command_capture)

    verify = sub.add_parser("verify", help="run a capture through the codec")
    verify.add_argument("input")
    verify.add_argument("--top", type=int, default=25)
    verify.set_defaults(func=command_verify)

    analyse = sub.add_parser("analyse", help="diff call stacks")
    analyse.add_argument("stacks", help="stacks recorded with --pattern")
    analyse.add_argument("--baseline", help="stacks recorded without --pattern")
    analyse.add_argument("--top", type=int, default=15)
    analyse.add_argument("--show-shared", action="store_true")
    analyse.set_defaults(func=command_analyse)

    # Everything after a bare "--" is passed through as spawn arguments. This is
    # the idiom that sidesteps the dashed-value problem entirely, and it is what
    # a person typing by hand will reach for.
    argv = sys.argv[1:]
    passthrough: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, passthrough = argv[:split], argv[split + 1 :]

    args = parser.parse_args(argv)
    if passthrough:
        if getattr(args, "spawn_arg", None) is None:
            args.spawn_arg = []
        args.spawn_arg.extend(passthrough)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
