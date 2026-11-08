"""Long-running ORM commands must replace a dead PostgreSQL connection."""
import pytest
from conftest import as_role


@pytest.mark.parametrize("kind", ["processor", "conductor"])
def test_daemon_recovers_a_closed_database_connection(db_clean, app_roles, monkeypatch, kind):
    from core.models.user import User
    from django.db import OperationalError, connection
    from orchestrator.management.commands import conduct_rounds, process_attempts

    class Finished(BaseException):
        pass

    attempts, recovered, sleeps = [], [], []

    def pass_once(*args):
        attempts.append(True)
        User.objects.count()  # establishes/uses the real application-role connection
        if len(attempts) == 1:
            connection.connection.close()  # as after the server drops a session
            raise OperationalError("database session was interrupted")
        recovered.append(True)
        return False

    def sleep(interval):
        sleeps.append(True)
        if len(sleeps) == 2:
            raise Finished

    module = process_attempts if kind == "processor" else conduct_rounds
    monkeypatch.setattr(module.time, "sleep", sleep)
    if kind == "processor":
        command = module.Command()
        monkeypatch.setattr(command, "pass_once", pass_once)
        options = {"once": False, "interval": 0, "grace": 120}
    else:
        class Conductor:
            stopping = False  # set by SIGTERM (conduct_rounds.py)
            tick = staticmethod(pass_once)
        monkeypatch.setattr(module, "Conductor", Conductor)
        command = module.Command()
        options = {"once": False, "plan_only": False, "interval": 0}
    with as_role("django_orchestrator"), pytest.raises(Finished):
        command.handle(**options)
    assert recovered == [True]
