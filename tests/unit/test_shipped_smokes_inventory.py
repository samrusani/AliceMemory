"""Every shipped smoke subcommand has an integration gate inventory."""

import argparse

from alicebot_api.cli.parser import build_parser
from tests.integration.test_shipped_smokes_postgres import GATES


def test_gate_inventory_names_every_registered_smoke_subcommand():
    """Mutation: rename the registered ``model-backed`` subcommand in the parser."""
    parser = build_parser()
    for command in ("vnext", "smoke"):
        actions = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        assert len(actions) == 1
        parser = actions[0].choices[command]
    actions = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    assert len(actions) == 1
    assert set(actions[0].choices) == set(GATES)
