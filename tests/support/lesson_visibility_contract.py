"""Common assertions for lesson visibility at the MCP doors."""

import json


def check_lesson_visibility(call, writer, reviewer, readers):
    block = {"applies_when": "rebasing", "what_happened": "A conflict hid a change.",
             "do_instead": "Inspect both sides.", "paths": ["src/*.py"]}
    ids = []
    for index in range(3):
        failed, result = call(writer, "alice_memory_commit", {
            "lesson": {**block, "applies_when": f"rebasing synthetic {index}"},
        })
        assert not failed and result["status"] == "review_required", result
        ids.append(str(result["memory"]["id"]))
    failed, result = call(reviewer, "alice_memory_correct", {
        "action": "reject", "review_item_id": ids[1], "reason": "Synthetic rejection reason",
    })
    assert not failed, result
    failed, result = call(reviewer, "alice_memory_correct", {"action": "approve", "review_item_id": ids[2]})
    assert not failed, result
    for reader in readers:
        failed, review = call(reader, "alice_memory_review", {"status": "all"})
        assert not failed and {str(item["id"]) for item in review["items"]} >= set(ids), review
        assert "Synthetic rejection reason" not in json.dumps(review)
        for memory_id in ids[:2]:
            failed, explanation = call(reader, "alice_explain", {"memory_id": memory_id})
            assert not failed and memory_id in json.dumps(explanation), explanation
            if memory_id == ids[1]:
                assert "Synthetic rejection reason" in json.dumps(explanation)
        failed, lessons = call(reader, "alice_lessons", {"task": "rebasing"})
        assert not failed and [str(item["id"]) for item in lessons["lessons"]] == ids[2:]
        item = lessons["lessons"][0]
        assert item["writer"]["id"] and item["published"]["by"]


def check_lesson_publisher_ids(call, writer, reviewer, reader):
    block = {"applies_when": "rebasing", "what_happened": "A conflict hid a change.",
             "do_instead": "Inspect both sides.", "paths": ["src/*.py"]}
    failed, result = call(writer, "alice_memory_commit", {"lesson": block})
    assert not failed and result["status"] == "review_required", result
    failed, result = call(reviewer, "alice_memory_correct", {
        "action": "approve", "review_item_id": result["memory"]["id"],
    })
    assert not failed, result
    failed, result = call(reader, "alice_lessons", {"task": "rebasing"})
    assert not failed and result["count"] == 1, result
    assert result["lessons"][0]["writer"]["id"] == "writer"
    assert result["lessons"][0]["published"]["by"] == "reviewer"
