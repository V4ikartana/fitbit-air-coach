import fcntl
import os

import pytest

from fitbit_air.lock import SyncLock


def test_second_acquire_fails_until_released(tmp_path):
    a, b = SyncLock(tmp_path / ".lock"), SyncLock(tmp_path / ".lock")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_leftover_garbage_lock_file_is_acquirable(tmp_path):
    p = tmp_path / ".lock"
    p.write_text("not-a-pid")  # a file with no holder is not a held lock
    lock = SyncLock(p)
    assert lock.acquire()
    assert p.read_text() == str(os.getpid())
    lock.release()


def test_context_manager_acquires_and_releases(tmp_path):
    p = tmp_path / "data" / ".lock"
    with SyncLock(p) as acquired:
        assert acquired and p.read_text() == str(os.getpid())
        assert not SyncLock(p).acquire()
    again = SyncLock(p)
    assert again.acquire()
    again.release()


def test_release_when_not_held_is_noop(tmp_path):
    lock = SyncLock(tmp_path / ".lock")
    lock.release()
    assert not lock.held
    other = SyncLock(tmp_path / ".lock")
    assert other.acquire()
    lock.release()  # must not drop someone else's lock
    assert not SyncLock(tmp_path / ".lock").acquire()
    other.release()


def test_failed_acquire_does_not_leak_or_release_holder(tmp_path):
    a, b = SyncLock(tmp_path / ".lock"), SyncLock(tmp_path / ".lock")
    assert a.acquire()
    assert not b.acquire()
    b.release()
    assert not SyncLock(tmp_path / ".lock").acquire()
    a.release()


def test_leftover_file_with_live_pid_but_no_holder_is_acquirable(tmp_path):
    p = tmp_path / ".lock"
    p.write_text(str(os.getpid()))  # PID is alive, but nothing holds the kernel lock
    lock = SyncLock(p)
    assert lock.acquire()
    lock.release()


def test_unexpected_flock_error_closes_descriptor_and_propagates(tmp_path, monkeypatch):
    closed = []
    real_close = os.close
    monkeypatch.setattr(fcntl, "flock", lambda fd, op: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd)))
    lock = SyncLock(tmp_path / ".lock")
    with pytest.raises(OSError, match="boom"):
        lock.acquire()
    assert len(closed) == 1 and not lock.held
