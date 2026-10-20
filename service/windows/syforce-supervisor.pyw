"""syforce supervisor for the Windows Scheduled Task (run with pythonw from the install dir).

Task Scheduler only restarts tasks that fail to START, not processes that exit later, so this loop restarts
server.py with backoff. The child is put in a kill-on-close Job Object: stopping the task also stops the server.
Usage: pythonw syforce-supervisor.pyw [server.py args, default --bind 0.0.0.0 --port 9001]
"""
import ctypes, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app")
LOG = os.path.join(HERE, "syforce.log")
ARGS = sys.argv[1:] or ["--bind", "0.0.0.0", "--port", "9001"]


def note(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f: f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "supervisor: " + msg + "\n")
    except OSError: pass


def kill_on_close_job():
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = ctypes.c_void_p
        job = k.CreateJobObjectW(None, None)
        class BASIC(ctypes.Structure):
            _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", ctypes.c_uint32), ("c", ctypes.c_size_t),
                        ("d", ctypes.c_size_t), ("e", ctypes.c_uint32), ("f", ctypes.c_size_t), ("g", ctypes.c_uint32), ("h", ctypes.c_uint32)]
        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", ctypes.c_uint64 * 6), ("m", ctypes.c_size_t * 4)]
        info = EXT(); info.Basic.LimitFlags = 0x2000                      # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if job and k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)): return k, job
    except Exception as e:
        note("no job object (%r)" % e)
    return None, None


def single_instance():
    """named mutex: a 2nd supervisor (task firing late, manual start) exits instead of fighting for the port."""
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        h = k.CreateMutexW(None, False, "Global\\syforce-supervisor-" + ARGS[-1])
        return h if h and ctypes.get_last_error() != 183 else None          # 183 = ERROR_ALREADY_EXISTS
    except Exception:
        return True


def main():
    lock = single_instance()
    if not lock:
        note("another supervisor is running, exiting"); return
    k, job = kill_on_close_job()
    backoff = 2
    while True:
        t0 = time.time()
        try:
            p = subprocess.Popen([sys.executable, os.path.join(APP, "server.py"), "--log", LOG] + ARGS, cwd=APP,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=0x08000000)                 # CREATE_NO_WINDOW
            if job: k.AssignProcessToJobObject(job, int(p._handle))
            rc = p.wait()
        except Exception as e:
            rc = repr(e)
        backoff = 2 if time.time() - t0 > 300 else min(60, backoff * 2)
        note("server exited (%s), restart in %ds" % (rc, backoff))
        time.sleep(backoff)


if __name__ == "__main__":
    main()
