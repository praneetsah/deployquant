class UnsupportedApiError(Exception):
    pass


DOC = "supported: US equities/ETFs, minute/second/daily bars; see /docs/python-api"


def unsupported(name: str):
    raise UnsupportedApiError(f"{name} is not supported here — {DOC}")


def unsupported_arg(call: str, arg: str, value, why: str = ""):
    """An argument this runtime cannot honour.

    Silently swallowing one is the single worst failure mode this engine
    has: the code is valid LEAN, it is accepted without complaint, and it
    trades differently than it reads. That is how a strategy asking for 2x
    leverage ran at 1x for three days. Every argument we cannot honour must
    say so at the call, not diverge quietly.
    """
    tail = f" — {why}" if why else f" — {DOC}"
    raise UnsupportedApiError(
        f"{call}({arg}={value!r}) is not supported here{tail}")


def reject_extra(call: str, args: tuple, kwargs: dict, known: tuple = ()):
    """Anything positional or keyword we did not name explicitly.

    LEAN signatures grow, and an argument we have never heard of is far more
    likely to change behaviour than not. Refusing it is the honest answer;
    ignoring it is a guess made on the user's behalf.
    """
    if args:
        raise UnsupportedApiError(
            f"{call} got {len(args)} extra positional argument(s) this "
            f"runtime does not support — {DOC}")
    unknown = [k for k in kwargs if k not in known]
    if unknown:
        raise UnsupportedApiError(
            f"{call} got unsupported argument(s) "
            f"{', '.join(sorted(unknown))} — {DOC}")
