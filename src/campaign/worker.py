"""Root worker: accepts runs over the local control socket and executes them.

systemd starts it on the first connection to $SNAP_COMMON/control.sock. It runs
one campaign, keeps answering stop requests meanwhile, and exits when idle. On
start, and from the boot-time `recover` service, it finalizes an interrupted run
without replaying load.

The socket is open to local users: each one submits runs with their own
OpenStack and Juju access, which the worker verifies before acceptance, and only
a run's owner or root can stop it.
"""

import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

import jubilant

from campaign import access, profiles
from campaign import state as run_state
from campaign.finalize import finalize
from campaign.run import Run
from probe.models import RunPlan

IDLE_EXIT_S = 60
MAX_REQUEST = 4 * 1024 * 1024
MAX_CONNECTIONS = 8
READ_TIMEOUT_S = 30


class Worker:
    """Socket server with at most one run or cleanup in flight."""

    def __init__(self) -> None:
        """Start without a run."""
        self.run: Run | None = None
        self.thread: threading.Thread | None = None
        self.mutex = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def busy(self) -> bool:
        """Whether a run or cleanup is executing in this process."""
        return self.thread is not None and self.thread.is_alive()

    def handle(self, conn: socket.socket) -> None:
        """Answer one request: a JSON line in, a JSON line out."""
        try:
            with conn:
                conn.settimeout(READ_TIMEOUT_S)
                conn.sendall(json.dumps(self.answer(conn)).encode() + b"\n")
        except OSError:
            pass
        finally:
            self.slots.release()

    def answer(self, conn: socket.socket) -> dict[str, object]:
        """Dispatch one request."""
        try:
            request = json.loads(read_line(conn))
            with self.mutex:
                if request.get("op") == "start":
                    return self.start(request, peer_uid(conn))
                if request.get("op") == "stop":
                    return self.stop(request, peer_uid(conn))
                return {"ok": False, "error": "unknown request"}
        except run_state.RunActiveError as error:
            return {"ok": False, "error": str(error)}
        except (ValueError, KeyError, TypeError, OSError) as error:
            return {"ok": False, "error": f"invalid request: {first_line(error)}"}

    def start(self, request: dict[str, object], uid: int) -> dict[str, object]:
        """Verify access with the run's own copy, then accept the run."""
        if self.busy():
            raise run_state.RunActiveError("A run is active.")
        schedule = profiles.resolve(
            str(request["profile"]),  # type: ignore[arg-type]
            int(str(request["duration_s"])),
        )
        plan = RunPlan.model_validate(request["plan"])
        model, cloud = str(request["model"]), str(request["cloud"])
        given = access.Access.model_validate(request["access"])
        until = run_state.now() + timedelta(seconds=schedule.max_elapsed_s)
        problem = access.login_problem(given, model, until)
        if problem:
            return {"ok": False, "error": problem}
        run_state.PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=run_state.PRIVATE))
        stack = ExitStack()
        try:
            access.install(given, staging)
            problems = access.verify(staging, model)
            if problems:
                return {"ok": False, "error": "; ".join(problems)}
            stack.enter_context(run_state.lock())
            state = run_state.create(
                profile=schedule.profile,
                duration_s=schedule.duration_s,
                model=model,
                cloud=cloud,
                plan=plan,
                schedule=schedule,
                owner_uid=uid,
            )
            private = run_state.private_dir(state.id)
            private.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            staging.rename(private)
            access.use_juju(private)
            self.run = Run(state, access.connect(private), jubilant.Juju(model=model))
        except BaseException:
            stack.close()
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self.thread = threading.Thread(target=self.execute, args=(stack,))
        self.thread.start()
        return {"ok": True, "run_id": state.id}

    def execute(self, stack: ExitStack) -> None:
        """Run the campaign while holding the run lock."""
        with stack:
            assert self.run
            try:
                self.run.execute()
            except Exception:  # noqa: BLE001 - never leave a run without cleanup
                recover(run_state.load(self.run.state.id))

    def stop(self, request: dict[str, object], uid: int) -> dict[str, object]:
        """Cancel the active run, or retry cleanup of an interrupted one."""
        run_id = request.get("run_id")
        if self.busy() and self.run and self.run.state.phase != "finished":
            if run_id not in (None, self.run.state.id):
                return {"ok": False, "error": f"run {run_id} is not the active run"}
            if uid not in (0, self.run.state.owner_uid):
                return {
                    "ok": False,
                    "error": "only the run's owner or root can stop it",
                }
            self.run.cancel.set()
            self.run.state.cancel_requested = True
            run_state.event(self.run.state.id, "stop-requested", uid=uid)
            return {"ok": True, "run_id": self.run.state.id}
        if self.busy():
            return {"ok": False, "error": "a cleanup is already in progress"}
        pending = run_state.unfinished()
        if pending is None or run_id not in (None, pending.id):
            return {"ok": False, "error": "no unfinished run to stop"}
        if uid not in (0, pending.owner_uid):
            return {"ok": False, "error": "only the run's owner or root can stop it"}
        if not request.get("access"):
            return {"ok": False, "error": "access", "needs_access": True}
        given = access.Access.model_validate(request["access"])
        stack = ExitStack()
        stack.enter_context(run_state.lock())
        try:
            private = run_state.private_dir(pending.id)
            private.mkdir(mode=0o700, parents=True, exist_ok=True)
            access.install(given, private)
        except BaseException:
            stack.close()
            raise
        abandon = bool(request.get("abandon_evidence"))
        self.thread = threading.Thread(
            target=self.cleanup, args=(stack, pending, abandon)
        )
        self.thread.start()
        return {"ok": True, "run_id": pending.id}

    def cleanup(
        self, stack: ExitStack, pending: run_state.RunState, abandon: bool
    ) -> None:
        """Finalize an interrupted run with the newly supplied access."""
        with stack:
            recover(pending, abandon)


def recover(pending: run_state.RunState, abandon: bool = False) -> None:
    """Finalize a run whose worker went away, without replaying load."""
    if pending.execution is None:
        pending.execution = "interrupted"
        pending.causes.append("interrupted: the worker stopped during the run")
    private = run_state.private_dir(pending.id)
    access.use_juju(private)
    try:
        conn = access.connect(private)
        conn.authorize()
    except Exception:  # noqa: BLE001 - expired access leaves requires-login items
        conn = None
    finalize(pending, conn, jubilant.Juju(model=pending.model), abandon)


def recover_unfinished() -> None:
    """Finalize an interrupted run unless a live worker holds the lock."""
    pending = run_state.unfinished()
    if pending is None or pending.code is not None:
        # A finalized run with cleanup left waits for `run stop` with new access.
        return
    try:
        with run_state.lock():
            recover(pending)
    except run_state.RunActiveError:
        return


def read_line(conn: socket.socket) -> bytes:
    """Read one bounded request line."""
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST:
            raise ValueError("request too large")
    return data


def first_line(error: BaseException) -> str:
    """First line of an error message, for a compact reply."""
    return str(error).splitlines()[0][:300] if str(error) else type(error).__name__


def peer_uid(conn: socket.socket) -> int:
    """User ID of the connecting process."""
    creds = conn.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), 12)
    return int(struct.unpack("3i", creds)[1])


def listener() -> socket.socket:
    """The socket systemd passed in, or a new one for development."""
    if os.environ.get("LISTEN_FDS") == "1":
        return socket.socket(fileno=3)
    run_state.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    run_state.SOCKET.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(run_state.SOCKET))
    os.chmod(run_state.SOCKET, 0o666)
    server.listen()
    return server


def main() -> None:
    """Serve control requests, or run boot-time recovery with `recover`."""
    os.umask(0o022)
    recover_unfinished()
    if sys.argv[1:] == ["recover"]:
        return
    worker = Worker()
    server = listener()
    server.settimeout(5)
    idle_since = time.monotonic()
    while True:
        try:
            conn, _ = server.accept()
        except TimeoutError:
            if worker.busy():
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since > IDLE_EXIT_S:
                return
            continue
        idle_since = time.monotonic()
        if not worker.slots.acquire(blocking=False):
            conn.close()
            continue
        threading.Thread(target=worker.handle, args=(conn,), daemon=True).start()
