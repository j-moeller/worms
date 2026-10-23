"""Turn the sandbox APIs' pending attempts into captures and logs, once.

This is the same pass the conductor runs on every tick
(orchestrator/rounds/captures.py); the command exists so it can be triggered
on demand -- by the simulation (ansible/Simulate.yml), or by an operator --
without waiting for the conductor's loop. Safe to run any time: it only
processes attempts not yet processed, and a VM counts once per team.

    manage.py process_captures
"""

from django.core.management.base import BaseCommand

from orchestrator.rounds import captures


class Command(BaseCommand):
    help = "Process pending flag/log attempts into captures and logs (one pass)"

    def add_arguments(self, parser):
        parser.add_argument("--grace", type=int, default=120, help="seconds after a run ends that attempts still count")

    def handle(self, grace, **options):
        flags = captures.process_flags(grace_seconds=grace)
        logs = captures.process_logs(grace_seconds=grace)
        self.stdout.write(f"captures: {flags}, logs: {logs}")
