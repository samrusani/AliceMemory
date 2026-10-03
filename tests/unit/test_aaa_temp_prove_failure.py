"""TEMP: a deliberate failure in shard 1, only on a throwaway branch that proves the summary check fails."""


def test_deliberate_failure_in_shard_one() -> None:
    assert False, "TEMP: proving that a failed shard keeps the summary check from passing"
