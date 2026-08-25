"""A live control surface, so testing does not mean restarting.

Every change made during a session of this project cost a server restart and a
reconnect: the client had to log in again, walk back to the creatures, and lose
whatever state it had. That loop is the single biggest tax on finding anything out,
and it is entirely self-inflicted — the values being changed are plain attributes on
two objects.

This is a line-oriented TCP console, registered in the same selector as the game
sockets so it neither blocks the tick nor needs a thread. One request, one reply,
connection closed — usable with ``nc``, ``socat`` or a one-line shell function.

It is bound to the loopback interface by default, because it can hand out items and
levels and has no authentication whatsoever.
"""

from __future__ import annotations

import socket
from typing import Callable

HOST = "127.0.0.1"
PORT = 2199

#: Rules that may be set, with the type to read them as. Anything not listed here
#: is refused rather than guessed at, so a typo cannot silently do nothing.
SETTABLE: dict[str, Callable[[str], object]] = {
    "mob_damage": float,
    "mob_max_health": float,
    "creature_damage": float,
    "creature_skill": int,
    "strike_interval": float,
    "effect_stack": int,
    "first_slot": int,
    "slot_capacity": int,
    "animated_effects": bool,
    "reach": float,
    "reach_slack": float,
    "mob_speed": int,
    "mob_stop": float,
    "mob_aggro": float,
    "mob_chase": lambda text: text.lower() in ("1", "true", "yes", "on"),
    "kill_experience": int,
    "level_every": int,
    "skill_lead": int,
    "creature_hit_frame": int,
    "creature_unblock_frame": int,
    "creature_hit_range": float,
    "drop_items": lambda text: text.lower() in ("1", "true", "yes", "on"),
    "allow_pickup": lambda text: text.lower() in ("1", "true", "yes", "on"),
    "player_max": int,
    "mobs": int,
}

#: World state that may be set the same way.
#: Settings that live on the world rather than the rules. Two used to be here --
#: first_slot and slot_capacity -- and they are policy, not state, so they moved to the
#: rules where a config file can reach them.
WORLD_SETTABLE: dict[str, Callable[[str], object]] = {
    "drop_spacing": float,
}


class Console:
    """The admin listener, and the commands it understands."""

    def __init__(self, service, host: str = HOST, port: int = PORT) -> None:
        self.service = service
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((host, port))
        self.socket.listen(4)
        self.socket.setblocking(False)

    def accept(self) -> None:
        """Take one connection, answer it, and close. Never blocks for long."""
        try:
            client, _ = self.socket.accept()
        except OSError:
            return
        try:
            client.settimeout(0.5)
            try:
                line = client.recv(4096).decode("utf-8", "replace").strip()
            except OSError:
                return
            reply = self.run(line)
            client.sendall((reply + "\n").encode())
        finally:
            client.close()

    # ── commands ─────────────────────────────────────────────────────────────

    def run(self, line: str) -> str:
        if not line:
            return self.help()
        parts = line.split()
        name, args = parts[0].lower(), parts[1:]
        handler = getattr(self, "_do_" + name, None)
        if handler is None:
            return f"unknown command {name!r}. try: help"
        try:
            return handler(*args)
        except TypeError:
            return f"wrong arguments for {name}. try: help"
        except Exception as error:  # a bad value must not take the server down
            return f"{name}: {error}"

    def help(self) -> str:
        return (
            "status                 what the world holds right now\n"
            "get [name]             read one rule, or all of them\n"
            "set <name> <value>     change a rule; see get for the names\n"
            "drops <a,b,c>          what a dying creature leaves\n"
            "xp <amount>            grant experience to everyone in the world\n"
            "level <n>              set everyone's level outright\n"
            "revive                 refill everyone's health\n"
            "tough <hp> [n]         give n creatures that much health, champions "
            "first\n"
            "kill                   kill every live creature, drops and all\n"
            "reset                  every creature alive again, ground cleared\n"
            "attrs [text]           attribute ids, filtered by name\n"
        )

    def _do_help(self) -> str:
        return self.help()

    def _do_status(self) -> str:
        world = self.service.world
        alive = sum(1 for c in world.creatures.values() if c.alive)
        lines = [
            f"creatures      {alive} alive of {len(world.creatures)}",
            f"players        {len(world.players)} known, "
            f"{len(world.inhabitants())} in the world",
            f"ground         {len(world.dropped)} item(s) lying",
            f"next actor     0x{world.next_item:02x}   next cell {world.next_slot}",
            f"drops          {', '.join(world.rules.drop_templates) or '(recorded)'}",
        ]
        for player in world.players.values():
            lines.append(
                f"player         {player.address} level {player.level} "
                f"xp {player.experience} health {player.health:.0f}"
                f"/{player.max_health:.0f}"
            )
        return "\n".join(lines)

    def _do_get(self, name: str | None = None) -> str:
        rules = self.service.rules
        world = self.service.world
        if name is None:
            rows = [f"{key:24} {getattr(rules, key)!r}" for key in sorted(SETTABLE)]
            rows += [
                f"{key:24} {getattr(world, key)!r}" for key in sorted(WORLD_SETTABLE)
            ]
            return "\n".join(rows)
        if name in SETTABLE:
            return f"{name} = {getattr(rules, name)!r}"
        if name in WORLD_SETTABLE:
            return f"{name} = {getattr(world, name)!r}"
        return f"{name!r} is not a setting. try: get"

    def _do_set(self, name: str, value: str) -> str:
        if name in SETTABLE:
            target, cast = self.service.rules, SETTABLE[name]
        elif name in WORLD_SETTABLE:
            target, cast = self.service.world, WORLD_SETTABLE[name]
        else:
            return f"{name!r} is not settable. try: get"
        was = getattr(target, name)
        setattr(target, name, cast(value))
        return f"{name}: {was!r} -> {getattr(target, name)!r}"

    def _do_drops(self, names: str) -> str:
        wanted = [part for part in names.split(",") if part]
        self.service.rules.drop_templates = wanted
        return f"drops: {len(wanted)} blueprint(s) per kill"

    def _do_xp(self, amount: str) -> str:
        """Grant experience and tell the client at once.

        An earlier version only moved the number and left the client to find out on
        the next kill, which made it useless for testing the thing it was for.
        """
        return self._grant(int(amount))

    def _do_level(self, level: str) -> str:
        """Put the player at the floor of *level*, experience and all.

        Setting the level alone leaves the bar describing a different character: the
        client is told a level and a pair of thresholds together, so both have to move.
        """
        from dsor.combat import level_bounds

        floor, _ceiling = level_bounds(int(level))
        lines = []
        for player in self.service.world.inhabitants():
            player.experience = floor
            lines.append(self._announce(player))
        return "\n".join(lines) or "nobody in the world"

    def _grant(self, amount: int) -> str:
        lines = []
        for player in self.service.world.inhabitants():
            player.experience += amount
            lines.append(self._announce(player))
        return "\n".join(lines) or "nobody in the world"

    def _announce(self, player) -> str:
        """Send the client this player's experience, level and damage."""
        from dsor.combat import (
            damage_at,
            encode_player_level,
            encode_xp_changed,
            hit_points_at,
            level_bounds,
            level_for,
        )

        world = self.service.world
        actor = int.from_bytes(world.PLAYER_ACTOR, "little") if hasattr(
            world, "PLAYER_ACTOR"
        ) else int.from_bytes(bytes([0x15, 0x00, 0x01, 0x00]), "little")
        level = level_for(player.experience)
        levelled = level != player.level
        player.level = level
        messages = [
            (
                player.address,
                encode_xp_changed(
                    player.experience, actor, level=level, levelled=levelled
                ),
            )
        ]
        if levelled:
            messages.append((player.address, encode_player_level(level, actor)))
        self.service._ship(messages)
        floor, ceiling = level_bounds(level)
        return (
            f"{player.address} level {level} xp {player.experience} "
            f"({floor}-{ceiling}) damage {damage_at(level)} "
            f"hp {hit_points_at(level)}"
        )

    def _do_revive(self) -> str:
        """Bring the dead back, and top up the living.

        Needed because death is now final: a player who dies stays at zero until
        something revives them. Refilling on the spot is what let a corpse be killed
        again — the client drew a body while the server thought the bar was full.
        """
        healed = []
        for player in self.service.world.inhabitants():
            was = player.health
            player.health = player.max_health or float(self.service.rules.player_max)
            player.max_health = player.health
            healed.append(f"{player.address} {was:.0f} -> {player.health:.0f}")
        return "\n".join(healed) or "nobody in the world"

    def _do_tough(self, health: str, count: str = "1") -> str:
        world = self.service.world
        chosen = world.toughen(float(health), int(count))
        if not chosen:
            return "no live creature to toughen"
        return "\n".join(
            f"{actor.hex(' ')}  {world.creatures[actor].blueprint or 'recorded'}"
            f"  {world.creatures[actor].max_health:.0f} hp"
            for actor in chosen
        )

    def _do_kill(self) -> str:
        world = self.service.world
        players = world.inhabitants()
        if not players:
            return "nobody is in the world to credit the kills to"
        alive = sum(1 for c in world.creatures.values() if c.alive)
        for player in players:
            self.service._ship(world.smite_all(player.address))
            break
        return f"killed {alive} creature(s)"

    def _do_reset(self) -> str:
        self.service.world.reset_creatures()
        return "creatures back, ground cleared"

    def _do_attrs(self, text: str = "") -> str:
        from dsor.attributes import ATTRIBUTES

        rows = [
            f"{value:4} {name}"
            for value, name in sorted(ATTRIBUTES.items())
            if text.lower() in name.lower()
        ]
        return "\n".join(rows) or f"no attribute matches {text!r}"
