import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("clipstudio.jobs")


class _LoggingExecutor(ThreadPoolExecutor):
    """Background jobs must never fail silently: log every exception with its job name."""

    def submit(self, fn, /, *args, **kwargs):
        fut = super().submit(fn, *args, **kwargs)
        name = getattr(fn, "__name__", repr(fn))
        fut.add_done_callback(lambda f: not f.cancelled() and f.exception()
                              and (log.error("job %s failed", name, exc_info=f.exception()), _remember(name, f.exception())))
        return fut


def _remember(name: str, exc: BaseException):
    try:  # shown in the dashboard (Setup guide -> If something goes wrong), so nobody has to read server logs
        from .guide import record_error

        record_error("background job: " + name, exc)
    except Exception:  # noqa: BLE001
        pass


# One worker: downloads/renders are CPU + bandwidth heavy, so jobs run one at a time.
executor = _LoggingExecutor(max_workers=1)
