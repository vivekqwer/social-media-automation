import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("clipstudio.jobs")


class _LoggingExecutor(ThreadPoolExecutor):
    """Background jobs must never fail silently: log every exception with its job name."""

    def submit(self, fn, /, *args, **kwargs):
        fut = super().submit(fn, *args, **kwargs)
        name = getattr(fn, "__name__", repr(fn))
        fut.add_done_callback(lambda f: not f.cancelled() and f.exception()
                              and log.error("job %s failed", name, exc_info=f.exception()))
        return fut


# One worker: downloads/renders are CPU + bandwidth heavy, so jobs run one at a time.
executor = _LoggingExecutor(max_workers=1)
