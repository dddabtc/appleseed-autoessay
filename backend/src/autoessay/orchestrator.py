from dataclasses import dataclass


@dataclass(frozen=True)
class OrchestratorRegistration:
    service_name: str
    status: str
    detail: str


def registration_stub() -> OrchestratorRegistration:
    return OrchestratorRegistration(
        service_name="appleseed-autoessay",
        status="deferred",
        detail="Live appleseed-orchestrator registration is deferred until v2.",
    )
