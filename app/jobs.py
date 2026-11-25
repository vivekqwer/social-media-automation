import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("clipstudio.jobs")

# A copy being restored from a backup (app/backup.py): its background work waits until the restore is over, and the
# restore waits until the work already running for that copy has finished. Keyed by customer, never mixed.
RESTORING: set[int] = set()
_running: dict[int, int] = {}
_rlock = threading.Lock()
# jobs handed to the executor and not finished yet, as (customer, job name, first argument) -> how many: the watchdog
# (app/watchdog.py) uses it to tell a clip that waits in line for its render from one whose render was lost
_queued: dict[tuple, int] = {}
_live: dict[int, int] = {}  # customer -> executor jobs running (not only waiting) right now


def queued(name: str, tid: int) -> set:
    """First arguments of the jobs called `name` for this customer that are waiting or running right now."""
    with _rlock:
        return {k[2] for k, n in _queued.items() if n > 0 and k[0] == tid and k[1] == name}


def busy(tid: int) -> bool:
    """An executor job of this customer is running right now (e.g. a Quick upload that renders its clips itself)."""
    with _rlock:
        return _live.get(tid, 0) > 0


def enter(tid: int) -> bool:
    """Background work for a copy starts (False = that copy is being restored right now: skip or wait)."""
    with _rlock:
        if tid in RESTORING:
            return False
        _running[tid] = _running.get(tid, 0) + 1
        return True


def leave(tid: int):
    with _rlock:
        _running[tid] = max(0, _running.get(tid, 0) - 1)


def start_restore(tid: int) -> bool:
    with _rlock:
        if tid in RESTORING:
            return False
        RESTORING.add(tid)
        return True


def end_restore(tid: int):
    with _rlock:
        RESTORING.discard(tid)


def wait_idle(tid: int, seconds: float) -> bool:
    """After start_restore: wait until nothing runs for this copy any more."""
    until = time.time() + seconds
    while True:
        with _rlock:
            if not _running.get(tid):
                return True
        if time.time() > until:
            return False
        time.sleep(0.5)


class _LoggingExecutor(ThreadPoolExecutor):
    """Background jobs must never fail silently: log every exception with its job name."""

    def submit(self, fn, /, *args, **kwargs):
        from . import tenant

        tid, name = tenant.current(), getattr(fn, "__name__", repr(fn))
        mark = (tid, name, args[0] if args and isinstance(args[0], (int, str)) else None)
        with _rlock:
            _queued[mark] = _queued.get(mark, 0) + 1

        def as_tenant():  # the job sees the same customer's database and files as the code that queued it
            waited = 0
            while not enter(tid):  # that copy is being restored from a backup: run on the restored data afterwards
                time.sleep(1)
                waited += 1
                if waited > 1800:
                    raise RuntimeError("waited too long for a restore to finish")
            with _rlock:
                _live[tid] = _live.get(tid, 0) + 1
            try:
                with tenant.use(tid):
                    return fn(*args, **kwargs)
            finally:
                with _rlock:
                    _live[tid] = max(0, _live.get(tid, 0) - 1)
                leave(tid)

        def done(_f):
            with _rlock:
                n = _queued.get(mark, 0) - 1
                if n > 0:
                    _queued[mark] = n
                else:
                    _queued.pop(mark, None)

        try:
            fut = super().submit(as_tenant)
        except BaseException:
            done(None)
            raise
        fut.add_done_callback(done)
        fut.add_done_callback(lambda f: not f.cancelled() and f.exception()
                              and (log.error("job %s failed (customer %s)", name, tid, exc_info=f.exception()),
                                   _remember(name, f.exception(), tid)))
        return fut


def _remember(name: str, exc: BaseException, tid: int = 1):
    try:  # shown in the dashboard (Setup guide -> If something goes wrong), so nobody has to read server logs
        from . import tenant
        from .guide import record_error

        with tenant.use(tid):
            record_error("background job: " + name, exc)
    except Exception:  # noqa: BLE001
        pass


# One worker: downloads/renders are CPU + bandwidth heavy, so jobs run one at a time.
executor = _LoggingExecutor(max_workers=1)
