"""One private copy of the whole system per customer ("tenant"), inside one server.

Every customer gets their own database part (a PostgreSQL schema, or a file with SQLite) and their own data folder.
Which copy the running code sees is decided by a context variable, set once per web request (from the login) and
once per background job. Code that says `SessionLocal()` or `DATA_DIR / "clips"` therefore never has to know about
customers at all, and cannot reach another customer's data by forgetting a filter.

Tenant 1 is the original installation (the Super Admin's own system): it keeps using the database and the data
folder exactly as before, so nothing existing moves.

This module imports nothing from the app, so everything else may import it.
"""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

OWNER = 1  # the original installation / the Super Admin's own copy
_current: ContextVar[int] = ContextVar("tenant", default=OWNER)
_base_dir: Path | None = None


def current() -> int:
    return _current.get()


@contextmanager
def use(tenant_id: int):
    """Run a block of code as one customer."""
    token = _current.set(int(tenant_id))
    try:
        yield
    finally:
        _current.reset(token)


def set_base_dir(path: Path):
    global _base_dir
    _base_dir = Path(path)


def root(tenant_id: int | None = None) -> Path:
    """The data folder of a customer (the original folder for tenant 1)."""
    tid = current() if tenant_id is None else int(tenant_id)
    if _base_dir is None:
        raise RuntimeError("data folder not configured yet")
    if tid == OWNER:
        return _base_dir
    p = _base_dir / "tenants" / str(tid)
    p.mkdir(parents=True, exist_ok=True)
    return p


def schema(tenant_id: int | None = None) -> str | None:
    """PostgreSQL schema of a customer (None = the default schema, for tenant 1)."""
    tid = current() if tenant_id is None else int(tenant_id)
    return None if tid == OWNER else f"t{tid}"


class TenantPath(os.PathLike):
    """A path inside the current customer's data folder. It is worked out when it is USED, not when it is written,
    so a constant like `DATA_DIR / "cookies.txt"` at the top of a module still points at the right customer."""
    __slots__ = ("_parts",)

    def __init__(self, *parts: str):
        self._parts = tuple(str(p) for p in parts)

    def _real(self) -> Path:
        return root().joinpath(*self._parts)

    def __truediv__(self, other):
        return TenantPath(*self._parts, os.fspath(other))

    def __fspath__(self) -> str:
        return str(self._real())

    def __str__(self) -> str:
        return str(self._real())

    def __repr__(self) -> str:
        return f"TenantPath({'/'.join(self._parts) or '.'} -> {self._real()})"

    def __format__(self, spec) -> str:
        return format(str(self._real()), spec)

    def __eq__(self, other) -> bool:
        try:
            return Path(os.fspath(other)) == self._real()
        except TypeError:
            return NotImplemented

    def __hash__(self) -> int:
        return hash(self._real())

    def __getattr__(self, name):  # exists(), mkdir(), glob(), parent, name, ... of the real path
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._real(), name)
