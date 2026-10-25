"""Continuously process the sandbox APIs' attempts into results.

The scoring and logging servers only append raw attempts
(flag_capture_attempts, logging_attempts). This process is the single writer
that turns them into flag_captures, logging rows and `captured` timeline
events (orchestrator/rounds/captures.py), marking each attempt processed. It
runs as a daemon on the orchestrator (role django_app, django_daemons), so
captures appear within a second or two of a submission, independently of the
conductor.

    manage.py process_attempts              # loop forever
    manage.py process_attempts --once       # one pass, then exit (tests/cron)

Exactly one instance should run (the processing is idempotent -- a VM counts
once per team -- but a second writer could race on the same attempt).
"""

import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from orchestrator.rounds import captures

log = logging.getLogger("orchestrator")


class Command(BaseCommand):
    help = "Continuously process pending flag/log attempts into captures and logs"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="a single pass, then exit")
        parser.add_argument("--interval", type=float, default=2.0, help="seconds between idle passes")
        parser.add_argument("--grace", type=int, default=None, help="seconds after a run ends that attempts still count")

    def pass_once(self, grace):
        flags = captures.process_flags(grace_seconds=grace)
        # Submissions that arrived before their flag's hash was stored.
        flags += captures.process_late_releases(grace_seconds=grace)
        logs = captures.process_logs(grace_seconds=grace)
        # Devbox attempts (always-on, source-attributed; docs/devbox.md).
        dev_flags = captures.process_devbox_flags()
        dev_logs = captures.process_devbox_logs()
        # Reconcile devbox VM records from completed devbox host jobs, so the
        # dashboard shows each team its VMs/SSH command and the devbox tokens
        # are known. Idempotent; cheap.
        try:
            from orchestrator.rounds import devbox

            devbox.apply_devbox_jobs()
        except Exception:  # noqa: BLE001  (must not kill the loop)
            log.exception("devbox job reconciliation failed")
        if flags or logs or dev_flags or dev_logs:
            log.info(
                "processed %s capture(s), %s log(s); devbox %s capture(s), %s log(s)",
                flags, logs, dev_flags, dev_logs,
            )
        # Something was processed -> maybe more is waiting; loop without sleeping.
        return bool(flags or logs or dev_flags or dev_logs)

    def handle(self, once, interval, grace, **options):
        if grace is None:
            grace = settings.ROUND_CONDUCTOR.get("capture_grace", 120)
        if once:
            self.pass_once(grace)
            return
        self.stdout.write("attempt processor started")
        while True:
            try:
                busy = self.pass_once(grace)
            except Exception:  # a transient DB error must not kill the loop
                log.exception("attempt processing failed")
                busy = False
            if not busy:
                time.sleep(interval)
