"""Errors for validated shopping conversations."""


class IntentError(ValueError):
    pass


class PreferenceConflict(ValueError):
    pass


class ScopeConflict(Exception):
    """A proposed task cannot be applied inside the stored storefront."""
