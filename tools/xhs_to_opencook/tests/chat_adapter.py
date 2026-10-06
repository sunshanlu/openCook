"""Deterministic transport for existing workflow fixtures; matches durable adapter API."""

import json

from xhs_to_opencook.errors import TaskError


def install(driver):
    def begin(prompt, source, request, persist):
        request.update(status="sending", sent_at=1)
        persist(request)
        driver._request_source, driver._request_prompt = source, prompt
        request.update(
            status="submitted",
            conversation_id=driver.conversation_id,
            round_id=request["request_id"],
        )
        persist(request)

    def wait(request, timeout, persist):
        try:
            text = driver.ask(
                driver._request_prompt, driver._request_source, repair=request.get("repair", False)
            )
        except TaskError as exc:
            if exc.code == "json_invalid":
                request.update(status="answered", answer="{}")
                persist(request)
                return "{}"
            raise
        try:
            data = json.loads(text)
            if request["stage"] == "extract" and data.get("recipes"):
                data = {"extraction": data, "note_id": request["note_id"]}
            if "verdict" in data or "extraction" in data:
                data["request_id"] = request["request_id"]
                text = json.dumps(data)
        except (ValueError, TypeError):
            pass
        request.update(status="answered", answer=text)
        persist(request)
        return text

    def restore(cid):
        driver.conversation_id = cid

    driver.begin_request = begin
    driver.wait_request = wait
    driver.restore_chat = restore
    driver.request_snapshot = lambda request: {
        "matches": 1,
        "conversation_id": driver.conversation_id,
        "round_id": request["request_id"],
    }
    driver.confirm_idle = lambda: True
    driver.stop_generation = lambda: True
    return driver
