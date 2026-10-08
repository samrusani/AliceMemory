"""The canonical reader must find no refused id in a loop response."""
import pytest

from alicebot_api.vnext_source_fence import cited_source_ids
from tests.unit.test_open_loop_references_read_fence import world, vault  # noqa: F401


@pytest.mark.parametrize("position", ["source_ids", "id", "ref", "mapping_key", "nested_key"])
@pytest.mark.parametrize("shape", ["arabic_list", "fullwidth_sentence", "hyphens_list", "space_zero", "tab_zero", "newline_zero"])
def test_real_key_withholds_every_named_spelling(world, shape, position):
    hidden, admitted = world.sources["beta"], world.sources["own"]
    if shape.endswith("zero"):
        # UUID accepts leading whitespace in place of a zero through int(hex, 16).
        hidden = "0447f7bb-8123-4567-8912-abcdefabcdef"
        original = world.sources["beta"]
        world.vault.sql("UPDATE sources SET id = ? WHERE id = ?", (hidden, original))
        whitespace = {"space_zero": " ", "tab_zero": "\t", "newline_zero": "\n"}[shape]
        value = whitespace + hidden.replace("-", "")[1:]
    else:
        digits = "٠١٢٣٤٥٦٧٨٩" if shape == "arabic_list" else "０１２３４５６７８９"
        encoded = hidden.translate(str.maketrans("0123456789", digits))
        if shape == "hyphens_list":
            encoded = hidden.replace("-", "")
            encoded = encoded[:3] + "-" + encoded[3:19] + "-" + encoded[19:]
        value = f"source:{encoded}, source:{admitted}" if shape != "fullwidth_sentence" else f"See source:{encoded} for details"
    reference = ({value: "control"} if position == "mapping_key" else
                 {"source_ids": {value: "control"}} if position == "nested_key" else {position: value})
    metadata = {"project_scope": ["alpha"], **reference, "kept": "control"}
    assert hidden in cited_source_ids(metadata).named
    world._plant("handoff", metadata=metadata)
    loop_id = world.loops["handoff"]
    world.vault.sql("DELETE FROM open_loops WHERE id != ?", (loop_id,))
    owner = world.vault.wire("alice_open_loops", {"status": "all", "limit": 100}, key=None)
    assert owner["payload"]["items"][0]["metadata_json"] == metadata
    item = world.list_items("alpha_project")[0]
    assert hidden not in cited_source_ids(item["metadata_json"]).named
    assert item["metadata_json"]["kept"] == "control"
