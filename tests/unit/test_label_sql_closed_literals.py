"""Caller strings cannot enter the conservative native SQL predicate."""
import pytest

from alicebot_api.vnext_label_sql import hidden_memory_input_sql, hidden_memory_event_sql


@pytest.mark.parametrize("sqlite", (False, True))
@pytest.mark.parametrize("alias", ("m", "memories"))
def test_prefilter_sensitivity_inputs_are_closed_kernel_literals(sqlite, alias):
    hostile = "public'); DROP TABLE memories; --"
    sql = hidden_memory_input_sql(["public", hostile], sqlite=sqlite, alias=alias)
    assert hostile not in sql
    assert "'confidential'" in sql
    assert f"{alias}.sensitivity" in sql


@pytest.mark.parametrize("sqlite", (False, True))
@pytest.mark.parametrize("values", ([], ["public"]))
def test_prefilter_refuses_a_caller_supplied_sql_alias(sqlite, values):
    with pytest.raises(ValueError, match="unsupported memory alias"):
        hidden_memory_input_sql(values, sqlite=sqlite, alias="m); DROP TABLE memories; --")


@pytest.mark.parametrize("sqlite", (False, True))
def test_event_prefilter_sensitivity_inputs_are_closed_kernel_literals(sqlite):
    hostile = "public'); DROP TABLE event_log; --"
    sql = hidden_memory_event_sql(["public", hostile], sqlite=sqlite)
    assert hostile not in sql
    assert "'confidential'" in sql
    assert "m.user_id" in sql
