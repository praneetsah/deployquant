"""PascalCase <-> snake_case interop.

LEAN's Python API historically used PascalCase (self.SetStartDate); modern LEAN
is snake_case. Both spellings appear in pasted code, so snake_case is the
canonical implementation and Pascal resolves onto it.
"""
import re

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def camel_to_snake(name: str) -> str:
    return _CAMEL.sub("_", name).lower()


class PascalMixin:
    def __getattr__(self, name):
        if name[:1].isupper():
            snake = camel_to_snake(name)
            if snake != name:
                try:
                    return getattr(self, snake)
                except AttributeError:
                    pass
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}")


def alias_methods(cls):
    for name, val in list(vars(cls).items()):
        if name.startswith("_") or not callable(val):
            continue
        pascal = "".join(p.capitalize() for p in name.split("_"))
        if not hasattr(cls, pascal):
            setattr(cls, pascal, val)
    return cls
