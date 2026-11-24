"""One bounded retry owner for ordinary RPCs and UUID-based publications."""
from __future__ import annotations

import time


class InfrastructureError(RuntimeError):
    pass


RETRYABLE = {"CANCELLED", "DEADLINE_EXCEEDED", "UNKNOWN", "UNAVAILABLE", "INTERNAL"}
RETRY_INTERVAL_MS = 100


def status_name(error):
    code = getattr(error, "code", None)
    if not callable(code):
        return None
    value = code()
    return getattr(value, "name", str(value).removeprefix("StatusCode."))


class Rpc:
    def __init__(self, client, config):
        self.client, self.config = client, config

    def _pause(self):
        # The coordinator stops new operations. An operation already in flight
        # keeps its original bounded budget so shutdown can confirm its result.
        time.sleep(RETRY_INTERVAL_MS / 1000)

    def call(self, name, operation):
        for attempt in range(self.config.max_retries + 1):
            try:
                return operation()
            except Exception as error:
                status = status_name(error)
                if status not in RETRYABLE or attempt == self.config.max_retries:
                    raise InfrastructureError(f"{name} failed ({status or type(error).__name__})") from error
                self._pause()

    def publish_cmd(self, uuid, operation):
        """After an uncertain Cmd publication, only look up its UUID."""
        uncertain = False
        for attempt in range(self.config.max_retries + 1):
            try:
                if uncertain:
                    return self.client.get_seq_by_uuid(uuid)
                return operation()
            except Exception as error:
                status = status_name(error)
                if status not in RETRYABLE | {"ALREADY_EXISTS"} and not (uncertain and status == "NOT_FOUND"):
                    raise InfrastructureError(f"Publish failed ({status or type(error).__name__})") from error
                uncertain = True
            if attempt < self.config.max_retries:
                self._pause()
        raise InfrastructureError(f"Publish result remains unknown for UUID {uuid}")

    def publish_wal(self, uuid, operation):
        """Retry the same WAL record and look up its sequence once committed."""
        committed = False

        def publish_or_lookup():
            nonlocal committed
            if not committed:
                try:
                    return operation().seq
                except Exception as error:
                    if status_name(error) != "ALREADY_EXISTS":
                        raise
                    committed = True
            return self.client.get_seq_by_uuid(uuid)

        return self.call("Publish WAL", publish_or_lookup)

    @staticmethod
    def sdk(name, operation):
        """Chat and Model SDKs already own the retry budget."""
        try:
            return operation()
        except Exception as error:
            raise InfrastructureError(f"{name} failed ({type(error).__name__})") from error
