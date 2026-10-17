from xhs_to_opencook.notifications import Outbox
from xhs_to_opencook.storage import write_json


def test_disabled_outbox_neither_queues_nor_sends_existing_messages(tmp_path):
    write_json(tmp_path / "outbox.json", {
        "enabled": False, "pending": [{"key": "progress"}], "events": [],
    })

    def unexpected_cli(*args, **kwargs):
        raise AssertionError("disabled notifications must not invoke Feishu")

    outbox = Outbox(tmp_path, run=unexpected_cli)
    outbox.enqueue("progress", "progress", progress=True)
    outbox.enqueue("complete", "complete")
    assert len(outbox.data["pending"]) == 1
    outbox.flush()
    assert outbox.data["pending"] == []
    assert Outbox(tmp_path).data["enabled"] is False
