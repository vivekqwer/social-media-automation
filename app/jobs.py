from concurrent.futures import ThreadPoolExecutor

# One worker: downloads/renders are CPU + bandwidth heavy, so jobs run one at a time.
executor = ThreadPoolExecutor(max_workers=1)
