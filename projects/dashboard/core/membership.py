"""Serialize membership changes with the same user-before-team lock order."""

from contextlib import contextmanager

from django.db import transaction

from core.models.team import Team
from core.models.user import User


@contextmanager
def membership_change(user, team=None, *, using="default"):
    with transaction.atomic(using=using):
        User.objects.using(using).select_for_update().get(pk=user.pk)
        locked = None
        if team is not None:
            locked = Team.objects.using(using).select_for_update().get(pk=team.pk)
        yield locked
