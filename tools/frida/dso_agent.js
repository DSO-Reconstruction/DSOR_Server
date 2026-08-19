"use strict";
/*
 * Frida agent for a Drakensang Online client.
 *
 * Three layers, in increasing order of what they tell you and of how much setup
 * they need:
 *
 *   1. Socket capture (always works, no addresses needed).  Hooks ws2_32
 *      sendto/recvfrom and emits every UDP payload.  This is the same bytes a
 *      pcap gives, with two advantages: the real local endpoint instead of a
 *      NAT-translated one, and no capture permissions.
 *
 *   2. Backtrace on a byte pattern.  When an outgoing payload contains a chosen
 *      pattern, the call stack is emitted.  Useful only if the send is
 *      synchronous with the code that built the message -- RakNet often sends
 *      from its own update tick, in which case the game's serialiser is not on
 *      this stack at all.  The host tool reports how often the pattern was seen
 *      versus how often a plausible stack came with it, so this limitation shows
 *      up as data rather than as a wrong conclusion.
 *
 *   3. Arbitrary offset hooks.  Given `module+offset` (what Ghidra shows you),
 *      hooks the function, logs its integer arguments and hexdumps a pointer
 *      argument.  This is the layer that answers "what does the game write into
 *      this message", because it can sit on RakPeer::Send where the game hands
 *      its buffer in.  There is no way to find that address from here; find it
 *      statically, then instrument it.
 *
 * The agent never decides what is interesting: it forwards bytes and stacks, and
 * the host tool does the analysis.  That keeps the instrumentation dumb and the
 * judgement reviewable.
 */

const DEFAULTS = {
  // Hex string; when an outgoing payload contains it, emit a backtrace.
  tracePattern: null,
  // [{ module, offset, name, argCount, dumpArg, dumpLength }]
  hooks: [],
  // Cap on stack depth, and on how many stacks to emit per pattern.
  backtraceDepth: 24,
  maxBacktraces: 40,
  // Skip payloads larger than this, to avoid flooding the channel.
  maxPayload: 4096,
};

let config = Object.assign({}, DEFAULTS);
let backtracesSent = 0;
let patternSeen = 0;
let patternBytes = null;

function toHex(bytes) {
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += ("0" + bytes[i].toString(16)).slice(-2);
  }
  return out;
}

/* A Windows sockaddr_in: family (2, host order), port (2, network order),
 * address (4). Returned as "ip:port", or null when it is not IPv4. */
function readSockaddr(pointer) {
  if (pointer === null || pointer.isNull()) return null;
  try {
    const family = pointer.readU16();
    if (family !== 2) return null; // AF_INET
    const port = pointer.add(2).readU8() * 256 + pointer.add(3).readU8();
    const raw = new Uint8Array(pointer.add(4).readByteArray(4));
    return raw.join(".") + ":" + port;
  } catch (error) {
    return null;
  }
}

function containsPattern(bytes) {
  if (patternBytes === null) return false;
  const needle = patternBytes;
  outer: for (let i = 0; i + needle.length <= bytes.length; i++) {
    for (let j = 0; j < needle.length; j++) {
      if (bytes[i + j] !== needle[j]) continue outer;
    }
    return true;
  }
  return false;
}

/* Symbolise a stack as module-relative offsets, which is what can be looked up
 * in a disassembler. Absolute addresses are useless across runs because of ASLR. */
function describeStack(context) {
  let addresses;
  try {
    addresses = Thread.backtrace(context, Backtracer.ACCURATE);
  } catch (error) {
    addresses = [];
  }
  if (addresses.length === 0) {
    try {
      addresses = Thread.backtrace(context, Backtracer.FUZZY);
    } catch (error) {
      addresses = [];
    }
  }
  const frames = [];
  for (const address of addresses.slice(0, config.backtraceDepth)) {
    const module = Process.findModuleByAddress(address);
    if (module === null) {
      frames.push("?+" + address);
      continue;
    }
    const offset = address.sub(module.base);
    frames.push(module.name + "+0x" + offset.toString(16));
  }
  return frames;
}

/* The socket handle is carried through as a connection key. The peer address is
 * not enough: a client can hold several connections to the same server endpoint,
 * and the recorded session does exactly that three times over. */
function emitPayload(direction, bytes, peer, socket, context) {
  if (bytes.length === 0 || bytes.length > config.maxPayload) return;
  send({
    kind: "datagram",
    direction: direction,
    peer: peer,
    socket: socket,
    length: bytes.length,
    hex: toHex(bytes),
  });

  if (direction !== "tx" || !containsPattern(bytes)) return;
  patternSeen += 1;
  if (backtracesSent >= config.maxBacktraces) return;
  backtracesSent += 1;
  send({
    kind: "backtrace",
    reason: "pattern",
    frames: describeStack(context),
  });
}

/* Which DLL actually provides the socket calls is not a given.  The Drakensang
 * client imports sendto and recvfrom from **wsock32.dll**, not ws2_32.dll, so an
 * agent that only hooks ws2_32 may see nothing: wsock32 has its own thunks and
 * whether they route through ws2_32's exported symbol is a Windows
 * implementation detail, not a guarantee.  Both are hooked, wsock32 first. */
const SOCKET_MODULES = ["wsock32.dll", "ws2_32.dll"];

/* Resolve an exported symbol across Frida versions.
 *
 * Frida 17 removed the static helpers on Module (Module.findExportByName and
 * friends) in favour of an instance API, so calling the old one raises
 * "TypeError: not a function" rather than returning null -- which fails the
 * whole agent at load instead of degrading. Every candidate is therefore guarded
 * by a typeof check, and the tiers are tried oldest-compatible first:
 *
 *   1. Module.findExportByName(moduleName, symbol)   -- Frida <= 16
 *   2. Process.findModuleByName(name).findExportByName(symbol)  -- Frida >= 17
 *   3. Module.findGlobalExportByName(symbol)         -- Frida >= 17, any module
 *
 * Tier 3 cannot say which module provided the symbol, so it reports "global".
 */
function findExportIn(moduleName, symbol) {
  if (typeof Module.findExportByName === "function") {
    try {
      return Module.findExportByName(moduleName, symbol);
    } catch (error) {
      /* fall through to the instance API */
    }
  }
  if (typeof Process.findModuleByName === "function") {
    const module = Process.findModuleByName(moduleName);
    if (module !== null && typeof module.findExportByName === "function") {
      return module.findExportByName(symbol);
    }
  }
  return null;
}

function findExportAnywhere(symbol) {
  if (typeof Module.findGlobalExportByName === "function") {
    return Module.findGlobalExportByName(symbol);
  }
  return null;
}

function resolveExport(name) {
  for (const moduleName of SOCKET_MODULES) {
    const address = findExportIn(moduleName, name);
    if (address !== null && address !== undefined) {
      return { address: address, module: moduleName };
    }
  }
  const global = findExportAnywhere(name);
  if (global !== null && global !== undefined) {
    return { address: global, module: "global" };
  }
  return null;
}

/* Resolve an export now, or keep trying until it appears.
 *
 * This exists for spawn mode. When Frida launches the process itself the agent
 * runs before the loader has necessarily mapped wsock32.dll, so resolving once
 * at load time returns null and the hook is never installed -- silently, which
 * presents as "the game sent nothing" rather than as an error. Polling closes
 * that window: the socket DLLs are mapped during process init, long before any
 * network activity, so a short poll always wins the race. */
function whenResolved(name, install) {
  const immediate = resolveExport(name);
  if (immediate !== null) {
    install(immediate);
    return true;
  }
  let attempts = 0;
  const timer = setInterval(function () {
    attempts += 1;
    const found = resolveExport(name);
    if (found !== null) {
      clearInterval(timer);
      install(found);
      send({ kind: "hook-late", symbol: name, module: found.module, afterMs: attempts * 50 });
    } else if (attempts > 400) {
      // 20 s. Past this the module is genuinely absent, and saying so beats
      // polling forever while the user waits for datagrams that cannot come.
      clearInterval(timer);
      send({ kind: "warning", message: name + " never appeared in " + SOCKET_MODULES.join("/") });
    }
  }, 50);
  return false;
}

function hookSocketLayer() {
  const attached = [];

  const gotSendto = whenResolved("sendto", function (target) {
    Interceptor.attach(target.address, {
      onEnter(args) {
        // int sendto(SOCKET, const char *buf, int len, int flags,
        //            const sockaddr *to, int tolen)
        const length = args[2].toInt32();
        if (length <= 0) return;
        const bytes = new Uint8Array(args[1].readByteArray(length));
        emitPayload("tx", bytes, readSockaddr(args[4]), args[0].toString(), this.context);
      },
    });
  });
  if (gotSendto) attached.push("sendto");

  const gotRecvfrom = whenResolved("recvfrom", function (target) {
    Interceptor.attach(target.address, {
      onEnter(args) {
        // The buffer is only filled in by the time the call returns, so the
        // pointer is kept and read in onLeave with the real length.
        this.buffer = args[1];
        this.from = args[4];
        this.socket = args[0].toString();
      },
      onLeave(retval) {
        const length = retval.toInt32();
        if (length <= 0 || this.buffer === undefined) return;
        const bytes = new Uint8Array(this.buffer.readByteArray(length));
        emitPayload("rx", bytes, readSockaddr(this.from), this.socket, this.context);
      },
    });
  });
  if (gotRecvfrom) attached.push("recvfrom");

  // WSASendTo/WSARecvFrom scatter the payload across a WSABUF array. Only the
  // first buffer is read: RakNet uses a single buffer, and guessing at more
  // would produce plausible-looking nonsense if another library is in play.
  const gotWsa = whenResolved("WSASendTo", function (target) {
    Interceptor.attach(target.address, {
      onEnter(args) {
        try {
          const count = args[2].toInt32();
          if (count < 1) return;
          const length = args[1].readU32();
          const buffer = args[1].add(Process.pointerSize).readPointer();
          if (length <= 0) return;
          const bytes = new Uint8Array(buffer.readByteArray(length));
          emitPayload("tx", bytes, readSockaddr(args[6]), args[0].toString(), this.context);
        } catch (error) {
          send({ kind: "warning", message: "WSASendTo: " + error.message });
        }
      },
    });
  });
  if (gotWsa) attached.push("WSASendTo");

  return attached;
}

/* Hook a function by module-relative offset, as read off a disassembler.
 *
 * Deferred like the socket hooks, and for the same reason: in spawn mode the
 * client image itself is not mapped when the agent starts, so resolving once
 * would install nothing. The offset hooks are the ones that matter most, so
 * failing them silently would be the worst outcome of all. */
function hookOffset(spec) {
  const module = Process.findModuleByName(spec.module);
  if (module === null) {
    let attempts = 0;
    const timer = setInterval(function () {
      attempts += 1;
      const later = Process.findModuleByName(spec.module);
      if (later !== null) {
        clearInterval(timer);
        attachOffset(spec, later);
        send({ kind: "hook-late", symbol: spec.name, module: spec.module,
               afterMs: attempts * 50 });
      } else if (attempts > 400) {
        clearInterval(timer);
        send({ kind: "warning", message: "module never loaded: " + spec.module });
      }
    }, 50);
    return false;
  }
  attachOffset(spec, module);
  return true;
}

function attachOffset(spec, module) {
  const address = module.base.add(spec.offset);
  const name = spec.name || spec.module + "+0x" + spec.offset.toString(16);
  const argCount = spec.argCount === undefined ? 4 : spec.argCount;
  const dumpArg = spec.dumpArg === undefined ? null : spec.dumpArg;
  const dumpLength = spec.dumpLength === undefined ? 64 : spec.dumpLength;
  const dumpOnReturn = spec.dumpOnReturn === true;
  /* Index of an argument holding a bit count. Sizing the dump from it is what
   * keeps a read inside the destination buffer: a fixed length overruns a
   * one-byte target, readByteArray throws, and every dump comes back empty. */
  const bitsArg = spec.bitsArg === undefined ? null : spec.bitsArg;
  const minBits = spec.minBits === undefined ? 0 : spec.minBits;

  /* Everything needed later is read out of `args` during onEnter and kept as
   * plain values or NativePointers.
   *
   * The `args` array itself must never be stored: Frida invalidates it the
   * moment onEnter returns, and touching it from onLeave raises "invalid
   * operation" — which, thrown before the send, silently drops every single
   * call. That is exactly what happened to 10,045 ReadBits calls.
   */
  function readDump(pointer, bits) {
    if (pointer === null) return null;
    let size = dumpLength;
    if (bits !== null) {
      if (bits <= 0) return null;
      size = Math.min((bits + 7) >> 3, dumpLength);
    }
    if (size <= 0) return null;
    try {
      return toHex(new Uint8Array(pointer.readByteArray(size)));
    } catch (error) {
      return null;
    }
  }

  Interceptor.attach(address, {
    onEnter(args) {
      let bits = null;
      if (bitsArg !== null) {
        try {
          bits = args[bitsArg].toInt32();
        } catch (error) {
          bits = null;
        }
      }
      if (minBits > 0 && bits !== null && bits < minBits) {
        this.skip = true;
        return;
      }
      this.skip = false;

      const values = [];
      for (let i = 0; i < argCount; i++) {
        values.push(args[i].toString());
      }
      this.values = values;
      this.bits = bits;
      // A NativePointer is a value, so it stays valid past onEnter.
      this.buffer = dumpArg !== null ? args[dumpArg] : null;
      this.frames = spec.backtrace ? describeStack(this.context) : null;

      if (dumpOnReturn) return;
      send({
        kind: "call",
        name: name,
        args: values,
        bits: bits,
        dump: readDump(this.buffer, this.bits),
        frames: this.frames,
      });
    },
    onLeave(retval) {
      if (!dumpOnReturn || this.skip) return;
      send({
        kind: "call",
        name: name,
        args: this.values,
        bits: this.bits,
        retval: retval.toString(),
        dump: readDump(this.buffer, this.bits),
        frames: this.frames,
      });
    },
  });
}

rpc.exports = {
  start(options) {
    config = Object.assign({}, DEFAULTS, options || {});
    if (config.tracePattern) {
      const hex = config.tracePattern.replace(/[^0-9a-fA-F]/g, "");
      patternBytes = [];
      for (let i = 0; i + 1 < hex.length; i += 2) {
        patternBytes.push(parseInt(hex.substr(i, 2), 16));
      }
    }
    const sockets = hookSocketLayer();
    const hooked = [];
    for (const spec of config.hooks) {
      if (hookOffset(spec)) hooked.push(spec.name || spec.module);
    }
    send({
      kind: "ready",
      frida: (typeof Frida !== "undefined" && Frida.version) ? Frida.version : "?",
      sockets: sockets,
      hooks: hooked,
      modules: Process.enumerateModules().slice(0, 8).map((m) => m.name),
    });
    return { sockets: sockets, hooks: hooked };
  },
  stats() {
    return { patternSeen: patternSeen, backtracesSent: backtracesSent };
  },
};
