import multiprocessing
try:
    multiprocessing.set_start_method("fork", force=True)
except RuntimeError:
    pass
