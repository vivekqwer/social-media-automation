import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("clipstudio.jobs")


class _LoggingExecutor(ThreadPoolExecutor):
    """Background jobs must never fail silently: log every exception with its job name."""

    def submit(self, fn, /, *args, **kwargs):
        from . import tenant

        tid, name = tenant.current(), getattr(fn, "__name__", repr(fn))

        def as_tenant():  # the job sees the same customer's database and files as the code that queued it
            with tenant.use(tid):
                return fn(*args, **kwargs)

        fut = super().submit(as_tenant)
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
