import uuid

from core import clock
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at = models.DateTimeField(default=clock.now)

    def __str__(self):
        if self.is_superuser:
            return f"{self.username} 👑"
        if self.is_staff:
            return f"{self.username} 🛡️"
        return self.username

    class Meta:
        db_table = "users"
