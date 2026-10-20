"""Authenticated in-process clients for scripts using temporary conversation databases."""
from dataclasses import replace
from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import Store, create_app
from app.operations import OperationsSettings


class EvaluationClient(TestClient):
    def request(self, method, url, **kwargs):
        if method.upper() == 'POST':
            kwargs['headers'] = {'Idempotency-Key': str(uuid4()), **(kwargs.get('headers') or {})}
        return super().request(method, url, **kwargs)


def evaluation_client(settings):
    _, token = Store(settings.database).issue_token('isolated-evaluation')
    limits = replace(OperationsSettings(), monitor_interval=0, requests_per_minute=10000,
                     request_burst=10000, model_requests_per_minute=10000, model_burst=10000)
    return EvaluationClient(create_app(settings, operations=limits), headers={'Authorization': 'Bearer ' + token})
