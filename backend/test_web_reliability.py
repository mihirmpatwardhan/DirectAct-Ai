"""Fast, offline regression tests for generic browser-automation reliability."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.services.llm_service import KeyRotationManager, LLMService, StubProvider
from app.services.web_engine import _completion_rejection_reason, _is_explicitly_authorized_form_value


class FakeProvider:
    def __init__(self, name: str, result=None, error: Exception | None = None):
        self._name = name
        self._result = result
        self._error = error
        self.calls = 0

    @property
    def provider_name(self):
        return self._name

    @property
    def is_available(self):
        return True

    async def complete_json(self, _prompt):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


async def test_key_failover() -> None:
    service = object.__new__(LLMService)
    service._rotation_manager = KeyRotationManager()
    service._stub = StubProvider()
    exhausted = FakeProvider("exhausted", error=RuntimeError("429 quota exceeded"))
    healthy = FakeProvider("healthy", result={"action": "read", "target": "body"})
    service._rotation_manager.register(exhausted, "first-test-key")
    service._rotation_manager.register(healthy, "second-test-key")

    result = await service.complete_json("Return JSON", timeout=2)
    assert result == {"action": "read", "target": "body"}
    assert exhausted.calls == 1 and healthy.calls == 1


def test_completion_proof() -> None:
    task = "Book the cheapest bus from Pune to Mumbai on 4 October for 2 people and stop at payment scanner"
    payment_page = "Pune to Mumbai 4 October 2 passengers Payment method Scan to pay"
    assert _completion_rejection_reason(
        task, "Selected the cheapest Pune to Mumbai ticket and reached payment", "Scan to pay", payment_page, 5
    ) is None

    assert _completion_rejection_reason(
        task, "done", "Scan to pay", payment_page, 5
    ) == "The completion summary is empty or generic"
    assert _completion_rejection_reason(
        task, "Reached checkout", "Pune to Mumbai", "Pune to Mumbai results", 5
    ) == "The requested payment/scan stopping point is not visible yet"
    assert _completion_rejection_reason(
        task, "Reached payment", "Scan to pay", "Delhi to Goa Payment Scan to pay", 5
    ) == "The visible page no longer contains a specific term from the requested task"
    assert not _is_explicitly_authorized_form_value(task, "passenger name", "Test Passenger")
    assert not _is_explicitly_authorized_form_value(task, "mobile", "9999999999")
    assert _is_explicitly_authorized_form_value(
        "Book for Ada Lovelace with mobile 9876543210", "passenger name", "Ada Lovelace"
    )


async def main() -> None:
    await test_key_failover()
    test_completion_proof()
    print("PASS: generic completion proof and multi-key failover")


if __name__ == "__main__":
    asyncio.run(main())
