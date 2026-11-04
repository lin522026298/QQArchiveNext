"""Lower our worker's CPU priority; keep bounded input cuts responsive."""
import os


def process_priority():
    import psutil
    process = psutil.Process()
    if os.name == 'nt':
        process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        # Low IO made a 3.75 GB source exceed the existing 30-second guard.
        # CPU stays BelowNormal; normal IO permits a short read-only input cut.
        process.ionice(psutil.IOPRIO_NORMAL)
    return {'cpu': process.nice(), 'io': process.ionice() if os.name == 'nt' else None}


def thread_priority():
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentThread.restype = ctypes.c_void_p
        if not kernel.SetThreadPriority(ctypes.c_void_p(kernel.GetCurrentThread()), -1):
            raise OSError(ctypes.get_last_error(), 'cannot lower background worker priority')
