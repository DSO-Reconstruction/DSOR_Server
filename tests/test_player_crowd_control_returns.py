"""A player stunned by monsters gets the authored stun immunity when it ends."""

from types import SimpleNamespace

from dso_server.services.pvp_combat import PVPCombatService

STUN = SimpleNamespace(resource_id="debuff_cc_stun", effect_id=7, duration_ticks=125)


class Combat:
    # debuff_cc_stun: Stun groups; StopModifiers -> debuff_cc_stun_immunity
    # (G_debuff_stun, stun) for D:0.5 s in PvE.
    def _status_effect_content(self, resource_id):
        return frozenset({"stun", "g_debuff_stun"}), (frozenset({"g_debuff_stun", "stun"}), 0.5)


def test_a_stun_cannot_land_in_the_half_second_after_one_ends(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("time.perf_counter", lambda: clock[0])
    s = SimpleNamespace()
    returns = PVPCombatService._player_crowd_control_returns
    assert returns(s, Combat(), (STUN,)) == (STUN,)
    clock[0] = 102.0  # still stunned: a new stun replaces it
    assert returns(s, Combat(), (STUN,)) == (STUN,)
    clock[0] = 107.2  # ended at 107.0: immune until 107.5
    assert returns(s, Combat(), (STUN,)) == ()
    clock[0] = 107.6
    assert returns(s, Combat(), (STUN,)) == (STUN,)
    assert returns(SimpleNamespace(), Combat(), (STUN,), pvp=True) == (STUN,)
