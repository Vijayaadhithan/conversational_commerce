"""Shared ownership, idempotency, and model rate-limit checks."""

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import State

from app.operations_config import OperationsSettings


class RequestAccess:
    def __init__(self, state: State, operations: OperationsSettings) -> None:
        self.state = state
        self.operations = operations

    def owner(self, request: Request) -> str:
        return request.state.principal["id"]

    def claim(self, request, operation, body):
        identity, replay = self.state.store.claim_request(
            self.owner(request),
            operation,
            request.headers.get("Idempotency-Key"),
            body.model_dump(mode="json"),
            self.operations.idempotency_required,
        )
        if replay:
            self.state.metrics.increment("idempotency_replays_total")
            return identity, JSONResponse(
                replay[0], status_code=replay[1], headers={"Idempotency-Replayed": "true"}
            )
        return identity, None

    def model_rate(self, request: Request) -> None:
        retry = self.state.model_buckets.consume(
            self.owner(request),
            self.operations.model_requests_per_minute,
            self.operations.model_burst,
        )
        if retry:
            self.state.metrics.increment("model_rate_limited_total")
            raise HTTPException(
                429, "Model request rate limit exceeded", headers={"Retry-After": str(retry)}
            )
