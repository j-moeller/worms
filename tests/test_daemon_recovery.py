"""Long-running ORM commands must replace a dead PostgreSQL connection."""
import pytest
from conftest import as_role


def test_daemon_recovers_a_closed_database_connection(db_clean, app_roles, monkeypatch):
    from core.models.rounds import HostJob
    from django.db import OperationalError, connection
    from orchestrator.management.commands import process_attempts

    class Finished(BaseException):
        pass

    attempts, recovered, sleeps = [], [], []

    def pass_once(*args):
        attempts.append(True)
        HostJob.objects.count()  # establishes/uses the real application-role connection (a table it reads)
        if len(attempts) == 1:
            connection.connection.close()  # as after the server drops a session
            raise OperationalError("database session was interrupted")
        recovered.append(True)
        return False

    def sleep(interval):
        sleeps.append(True)
        if len(sleeps) == 2:
            raise Finished

    monkeypatch.setattr(process_attempts.time, "sleep", sleep)
    command = process_attempts.Command()
    monkeypatch.setattr(command, "pass_once", pass_once)
    options = {"once": False, "interval": 0, "grace": 120}
    with as_role("django_orchestrator"), pytest.raises(Finished):
        command.handle(**options)
    assert recovered == [True]
