"""Socket procs reach the shared player status-effect publisher intact.

Regression: item-set program fields were added to
``VictimStatusEffectDefinition`` only, and ``_send_player_skill_status_effects``
read them unguarded. Every timed socket proc then raised AttributeError, which
also failed the map's monster tick whenever a summon hit fired a socket event.
"""

import ast
import inspect
import textwrap

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.socket_effect_protocol import (
    SocketEffectDatabase, SocketStatusDefinition,
)
from dso_server.services.base import CharacterService
from dso_server.services.combat_resolution import CombatResolutionService
from dso_server.tests.test_item_set_events import wearer  # noqa: F401 (fixture)


def _timed_user_socket_definitions():
    database = SocketEffectDatabase(DEFAULT_STATIC_DB_PATH)
    return [
        rule.definition
        for program in database.programs.values()
        for rule in program.rules
        if rule.definition is not None
        and rule.definition.duration_ticks > 0
        and rule.target == "user"
    ]


def test_socket_definition_has_every_field_the_publisher_reads():
    source = textwrap.dedent(inspect.getsource(
        CombatResolutionService._send_player_skill_status_effects
    ))
    read = {
        node.attr for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name) and node.value.id == "definition"
    }
    fields = set(SocketStatusDefinition.__dataclass_fields__)
    assert read, "publisher no longer reads definition fields"
    assert read <= fields, sorted(read - fields)


def test_every_authored_timed_socket_proc_is_published(wearer):
    definitions = _timed_user_socket_definitions()
    assert len(definitions) > 100
    for definition in definitions:
        wearer.send_reliable_ordered.reset_mock()
        CharacterService._send_player_skill_status_effects(
            wearer, (definition,), server_tick=200, label="socket-proc",
        )
        assert wearer.send_reliable_ordered.called, definition.resource_id


@pytest.mark.parametrize("duration_ticks", [0, 125])
def test_socket_proc_is_never_treated_as_an_item_set_program(wearer, duration_ticks):
    definition = _timed_user_socket_definitions()[0]
    definition = type(definition)(**{
        **{name: getattr(definition, name)
           for name in SocketStatusDefinition.__dataclass_fields__},
        "duration_ticks": duration_ticks,
    })
    assert definition.item_set_program is False
    CharacterService._send_player_skill_status_effects(
        wearer, (definition,), server_tick=200, label="socket-proc",
    )
    assert not getattr(wearer, "gameplay_item_set_programs", {})
