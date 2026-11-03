"""A jail for the python3 that Claude runs in a job.

tools/ is on PYTHONPATH for those runs, so Python imports this file at start-up. When the runner sets
AIWA_PYTHON_JAIL (the job folder), an audit hook (PEP 578) refuses:
- network: no sockets, no name lookups (so a prompt injection in a customer's file cannot send data out)
- other programs: no subprocess, os.system, exec or spawn (curl, node or a second, unguarded python)
- native code: no ctypes (the way around an audit hook)
- writing, renaming or deleting anything outside the job folder and the temp folder (the app's own code and data
  stay as they are), and the job's own records (job.json, events.jsonl, steps.jsonl) either way
- reading Claude's login, the app's state, other jobs' folders and any process environment (secrets)
- SQLite other than the knowledge index opened read-only (ATTACH DATABASE could create files anywhere otherwise)

Calculations, `python3 -m pec_calc`, `python3 -m kb_search`, reading the knowledge folder and writing output/ work as
before. Python's own documentation says audit hooks are not a sandbox: they raise the bar, the dashboard's other
limits (no web tools, files reviewed before use) still apply. Bash rules keep `python3 -I`/`-S` (which skip this
file) from running at all.
"""
import errno
import os
import sys


class JailError(OSError):
    """Raised for everything the jail refuses. Deliberately not a PermissionError: tempfile.TemporaryDirectory
    retries a PermissionError over and over (it assumes a mode problem) and ends in a RecursionError."""


def _install() -> None:
    jail = os.environ.get("AIWA_PYTHON_JAIL", "")
    if not jail:
        return
    real = os.path.realpath
    sep = os.sep
    import tempfile

    writable = [real(jail), real(tempfile.gettempdir())]
    secret = [real(p) for p in os.environ.get("AIWA_JAIL_NO_READ", "").split(os.pathsep) if p]
    # the app's own records of this job: Claude's tools cannot see them (app/runner.py DENY_RULES), so its Python
    # must not read or rewrite them either (a job's status, or the log of what was run)
    internal = {os.path.join(real(jail), name) for name in ("job.json", "events.jsonl", "steps.jsonl")}
    kb_index = os.environ.get("KB_INDEX", "")
    devices = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)  # Claude's login, should Claude Code pass it to its tools

    blocked = frozenset({
        "socket.__new__", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
        "socket.gethostbyaddr", "socket.getnameinfo", "socket.sethostname", "socket.connect", "socket.bind",
        "socket.sendto", "socket.sendmsg",
        "http.client.connect", "urllib.Request", "ftplib.connect", "smtplib.connect", "imaplib.open",
        "poplib.connect", "nntplib.connect", "telnetlib.Telnet.open", "webbrowser.open",
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.forkpty", "pty.spawn",
        "os.startfile", "_posixsubprocess.fork_exec",
        # the checks below resolve relative paths against the working directory, which therefore must not move;
        # a symbolic link made here could point anywhere, so none are made
        "os.chdir", "os.symlink",
    })
    paths_checked = {  # event -> positions of the path arguments that must stay inside the writable folders
        "os.remove": (0,), "os.rename": (0, 1), "os.rmdir": (0,), "os.mkdir": (0,), "os.symlink": (1,),
        "os.link": (1,), "os.chmod": (0,), "os.chown": (0,), "os.utime": (0,), "os.truncate": (0,),
        "os.chflags": (0,), "os.lchflags": (0,), "os.setxattr": (0,), "os.removexattr": (0,),
        "os.mkfifo": (0,), "os.mknod": (0,), "shutil.rmtree": (0,), "shutil.copyfile": (1,),
        "shutil.copymode": (1,), "shutil.copystat": (1,), "shutil.chown": (0,), "shutil.move": (1,),
        "shutil.unpack_archive": (1,), "shutil.make_archive": (0,),
    }
    # A path given with a directory file descriptor (dir_fd=) is relative to that directory, not to the working
    # directory this hook resolves against, so such calls are refused outright; the fd positions per event:
    dir_fds = {"os.remove": (1,), "os.rmdir": (1,), "os.mkdir": (2,), "os.symlink": (2,), "os.link": (2, 3),
               "os.rename": (2, 3), "os.chmod": (2,), "os.chown": (3,), "os.utime": (3,), "os.mkfifo": (2,),
               "os.mknod": (3,)}
    sources_read = {"os.link": 0}  # a hard link makes the source readable through the new name
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def refuse(what: str) -> None:
        raise JailError(errno.EPERM, f"ผู้ช่วยงานไม่อนุญาต: {what} (Python ในงานนี้ต่อเครือข่าย เรียกโปรแกรมอื่น "
                                     f"หรือเขียนนอกโฟลเดอร์งานไม่ได้)")

    def as_path(value):
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        return value if isinstance(value, str) else None

    def inside(path: str, roots) -> bool:
        target = real(path)
        return any(target == root or target.startswith(root + sep) for root in roots)

    def may_write(path) -> bool:
        path = as_path(path)
        if path is None:  # a file descriptor that is already open
            return True
        return path in devices or (inside(path, writable) and real(path) not in internal)

    def may_read(path) -> bool:
        path = as_path(path)
        if path is None:
            return True
        target = real(path)
        if target.startswith("/proc/") and target.rsplit("/", 1)[-1] in ("environ", "mem"):
            return False
        if target in internal:
            return False
        return inside(target, writable[:1]) or not inside(target, secret)  # its own job folder, not the others

    # SQLite opens and creates files on its own (ATTACH DATABASE 'x' on any read-write connection, even an
    # in-memory one) without passing through Python's open(), so the only database allowed is the knowledge
    # index, read-only, exactly as tools/kb_search.py opens it: a read-only connection cannot create files
    kb_readonly = f"file:{kb_index}?mode=ro" if kb_index else None

    def hook(event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & write_flags)
            if writing and not may_write(path):
                refuse(f"เขียนไฟล์ {path}")
            if not may_read(path):
                refuse(f"อ่านไฟล์ {path}")
            name = as_path(path)
            if name is not None and os.path.isdir(name):
                # os.open() on a directory gives a directory file descriptor, with which os.open(..., dir_fd=)
                # (whose audit event does not carry the fd) reaches anything from that directory. shutil.rmtree
                # and TemporaryDirectory need such descriptors, so they are not available in a job.
                refuse(f"เปิดโฟลเดอร์ {path} · ลบโฟลเดอร์ทีละไฟล์ด้วย os.remove และ os.rmdir แทน shutil.rmtree")
        elif event in blocked:
            refuse(event)
        elif event.startswith("ctypes."):
            refuse(event)
        elif event in paths_checked:
            for i in dir_fds.get(event, ()):
                if i < len(args) and args[i] is not None and args[i] != -1:
                    refuse(f"{event} dir_fd")
            if event in sources_read and len(args) > sources_read[event] and not may_read(args[sources_read[event]]):
                refuse(f"{event} {args[sources_read[event]]}")
            for i in paths_checked[event]:
                if i < len(args) and not may_write(args[i]):
                    refuse(f"{event} {args[i]}")
        elif event == "sqlite3.connect":
            database = as_path(args[0]) if args else None
            if database != kb_readonly:
                refuse(f"เปิดฐานข้อมูล {database} (ใช้ได้เฉพาะดัชนีคลังความรู้แบบอ่านอย่างเดียวผ่าน kb_search)")
        elif event in ("sqlite3.enable_load_extension", "sqlite3.load_extension"):
            refuse(event)  # a compiled extension is native code, like ctypes

    sys.addaudithook(hook)


_install()
del _install
