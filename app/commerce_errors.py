"""Errors for validated shopping conversations."""


class IntentError(ValueError):
    pass


class InvalidShoppingIntent(ValueError):
    """Malformed/incomplete model output, distinct from transport or service failures."""

    def __init__(self, result):
        super().__init__("Return complete valid IntentPlan JSON with all required fields")
        self.result = result


class PreferenceConflict(ValueError):
    pass


class ScopeConflict(Exception):
    """A proposed task cannot be applied inside the stored storefront."""
