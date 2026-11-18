"""Cross-process admission lock shared by backup, migration and restore."""
import os
import stat
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def maintenance_lock(path=None, blocking=False):
    target = Path(path or os.environ.get("VAULT_MAINTENANCE_LOCK", "/backups/.backup.lock"))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise ValueError("维护锁路径不能是链接")
    fd = os.open(target, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("维护锁不是普通文件")
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("另一个备份、恢复或迁移正在进行，请稍后重试") from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise ValueError("另一个备份、恢复或迁移正在进行，请稍后重试") from None
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
