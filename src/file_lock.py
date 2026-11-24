"""OS-owned locks: a crashed process cannot leave a permanent lock behind."""
import os
from pathlib import Path

def acquire(path, blocking=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open("a+b")
    try:
        if os.name=="nt":
            import msvcrt
            handle.seek(0,2)
            if handle.tell()==0:handle.write(b"0");handle.flush()
            handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|(0 if blocking else fcntl.LOCK_NB))
        return handle
    except BaseException:
        handle.close();raise
