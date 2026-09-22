"""DirectAct-AI services package.

Services are exposed lazily so importing one small service does not eagerly
construct the complete automation stack (and all of its Windows/Playwright
dependencies) during backend startup.
"""
from importlib import import_module

_EXPORTS = {
    "llm_service": ("app.services.llm_service", "llm_service"),
    "task_router": ("app.services.task_router", "task_router"),
    "security_guard": ("app.services.security_guard", "security_guard"),
    "orchestrator": ("app.services.orchestrator", "orchestrator"),
    "action_cache": ("app.services.action_cache", "action_cache"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    """Preserve ``from app.services import ...`` compatibility without eager imports."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
