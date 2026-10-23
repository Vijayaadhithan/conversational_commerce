"""OpenAPI authentication and idempotency declarations."""

from fastapi.openapi.utils import get_openapi


def configure_schema(app, operations):
    def schema():
        if app.openapi_schema is None:
            value = get_openapi(title=app.title, version=app.version, routes=app.routes)
            value.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
                "type": "http",
                "scheme": "bearer",
                "description": "Token issued by scripts/manage_api_users.py",
            }
            for path, methods in value["paths"].items():
                for method, spec in methods.items():
                    if operations.auth_required and path != "/livez":
                        spec["security"] = [{"BearerAuth": []}]
                    if method == "post" and (
                        path == "/conversations" or path.endswith("/messages")
                    ):
                        spec.setdefault("parameters", []).append(
                            {
                                "name": "Idempotency-Key",
                                "in": "header",
                                "required": operations.idempotency_required,
                                "schema": {"type": "string", "minLength": 8, "maxLength": 128},
                                "description": "Reuse for retries of the same logical request; change for a new request.",
                            }
                        )
            app.openapi_schema = value
        return app.openapi_schema

    app.openapi = schema
