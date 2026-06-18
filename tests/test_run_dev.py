from tools.run_dev import SupervisorLock


def test_supervisor_lock_rejects_live_owner(tmp_path, monkeypatch):
    lock_path = tmp_path / "dev_supervisor.lock"
    lock_path.write_text("1234", encoding="ascii")
    monkeypatch.setattr(SupervisorLock, "_pid_alive", staticmethod(lambda pid: pid == 1234))

    acquired, existing_pid = SupervisorLock(lock_path).acquire()

    assert acquired is False
    assert existing_pid == 1234
    assert lock_path.read_text(encoding="ascii") == "1234"


def test_supervisor_lock_replaces_stale_owner_and_releases(tmp_path, monkeypatch):
    lock_path = tmp_path / "dev_supervisor.lock"
    lock_path.write_text("1234", encoding="ascii")
    monkeypatch.setattr(SupervisorLock, "_pid_alive", staticmethod(lambda _pid: False))
    lock = SupervisorLock(lock_path)

    acquired, owner_pid = lock.acquire()

    assert acquired is True
    assert owner_pid == lock.pid
    assert lock_path.read_text(encoding="ascii") == str(lock.pid)

    lock.release()

    assert not lock_path.exists()
