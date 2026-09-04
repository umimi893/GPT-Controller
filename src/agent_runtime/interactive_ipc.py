from __future__ import annotations

import time
import uuid
from typing import Any

from .config import Config
from .util import atomic_write_json, load_json, utc_now


class InteractiveClient:
    def __init__(self, config: Config):
        self.config = config
        self.root = config.interactive_spool
        self.requests = self.root / "requests"
        self.processing = self.root / "processing"
        self.responses = self.root / "responses"

    def request(self, step: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        self.requests.mkdir(parents=True, exist_ok=True)
        self.processing.mkdir(parents=True, exist_ok=True)
        self.responses.mkdir(parents=True, exist_ok=True)
        request_path = self.requests / f"{request_id}.json"
        processing_path = self.processing / f"{request_id}.json"
        response_path = self.responses / f"{request_id}.json"
        atomic_write_json(request_path, {
            "protocol": "q-agent-v4-interactive-request",
            "id": request_id,
            "created_at": utc_now(),
            "step": step,
        })
        deadline = time.monotonic() + timeout_seconds
        started_wait = time.monotonic()
        while time.monotonic() < deadline:
            if response_path.exists():
                response = load_json(response_path)
                try:
                    response_path.unlink(missing_ok=True)
                    request_path.unlink(missing_ok=True)
                    processing_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if response.get("status") != "succeeded":
                    raise RuntimeError(response.get("error") or "interactive host failed")
                return response.get("result", {})
            time.sleep(0.2)
        # If the host never claimed the request, remove it so a later host restart cannot
        # unexpectedly execute an operation after the caller has already timed out.
        pending_cancelled = False
        if request_path.exists() and not processing_path.exists():
            try:
                request_path.unlink(missing_ok=True)
                pending_cancelled = True
            except OSError:
                pass
        elapsed = time.monotonic() - started_wait
        state = "pending request cancelled" if pending_cancelled else "request may already be processing"
        raise TimeoutError(f"interactive host did not respond within {elapsed:.1f}s ({state})")
