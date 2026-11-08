"""Forms of the staff pages (dashboard/views/staff*.py).

Plain Django forms rendered by the shared field template
(templates/staff/_field.html); they work without JavaScript. Every value is
validated here. A round's gameplan is picked by file name from the list the
orchestrator keeps (GameplanFile): the dashboard never reads a gameplan.
"""

from __future__ import annotations

import random

from core import accounts, clock, schedule
from core.models.competition import Round
from core.models.team import Team
from django import forms
from django.core.exceptions import ValidationError

DATETIME_FORMAT = "%Y-%m-%dT%H:%M"


class DateTimeLocal(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format=DATETIME_FORMAT, **kwargs)


def _datetime(label, required=True, help_text="UTC"):
    return forms.DateTimeField(label=label, required=required, widget=DateTimeLocal(), help_text=help_text)


class StyledForm(forms.Form):
    """Gives every widget the site's input class (base.css)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "input")


# -- competition ---------------------------------------------------------------


class CompetitionForm(StyledForm):
    name = forms.CharField(max_length=200)
    registration_starts_at = _datetime("Registration opens", required=False)
    registration_ends_at = _datetime("Registration closes", required=False)

    def clean(self):
        data = super().clean()
        start, end = data.get("registration_starts_at"), data.get("registration_ends_at")
        if (start is None) != (end is None):
            raise ValidationError("Set both registration dates, or neither (no registration window).")
        if start is not None and end <= start:
            raise ValidationError("Registration must close after it opens.")
        return data


# -- users -----------------------------------------------------------------------


class TeamChoice(forms.ModelChoiceField):
    def label_from_instance(self, team):
        return team.name


class UserCreateForm(StyledForm):
    email = forms.EmailField(label="Email (the username)", max_length=accounts.MAX_USERNAME_LENGTH)
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(label="Surname", max_length=150, required=False)
    team = TeamChoice(queryset=Team.objects.none(), required=False, empty_label="— no team —")

    def __init__(self, *args, competition=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["team"].queryset = Team.objects.filter(competition=competition).order_by("name")


class UserImportForm(StyledForm):
    csv_file = forms.FileField(label="CSV file", required=False)
    csv_text = forms.CharField(
        label="…or paste the CSV", required=False, widget=forms.Textarea(attrs={"rows": 6}),
        help_text="A header row, then one person per row: email, first name, surname and optionally team.",
    )  # fmt: skip

    MAX_BYTES = 512 * 1024

    def clean(self):
        data = super().clean()
        upload, text = data.get("csv_file"), (data.get("csv_text") or "").strip()
        if upload is None and not text:
            raise ValidationError("Choose a CSV file or paste the CSV.")
        if upload is not None:
            if upload.size > self.MAX_BYTES:
                raise ValidationError("The CSV is too large (at most 512 KiB).")
            try:
                text = upload.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValidationError("The CSV must be UTF-8 text.")
        if len(text.encode("utf-8")) > self.MAX_BYTES:
            raise ValidationError("The CSV is too large (at most 512 KiB).")
        data["text"] = text
        return data


class UserEditForm(StyledForm):
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(label="Surname", max_length=150, required=False)
    email = forms.EmailField(max_length=254, required=False)


class UserTeamForm(StyledForm):
    team = TeamChoice(queryset=Team.objects.none(), required=False, empty_label="— no team —")

    def __init__(self, *args, competition=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["team"].queryset = Team.objects.filter(competition=competition).order_by("name")


# -- teams -----------------------------------------------------------------------


class TeamForm(StyledForm):
    name = forms.CharField(max_length=100)
    max_members = forms.IntegerField(label="Members at most", min_value=1, max_value=20, initial=2)

    def __init__(self, *args, competition=None, team=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.competition, self.team = competition, team

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise ValidationError("A team needs a name.")
        clash = Team.objects.filter(competition=self.competition, name__iexact=name)
        if self.team is not None:
            clash = clash.exclude(pk=self.team.pk)
        if clash.exists():
            raise ValidationError(f"A team named {name!r} already exists.")
        return name

    def clean_max_members(self):
        value = self.cleaned_data["max_members"]
        if self.team is not None and value < self.team.users.count():
            raise ValidationError(f"{self.team.name} already has {self.team.users.count()} members.")
        return value


class AddMemberForm(StyledForm):
    user = forms.ModelChoiceField(queryset=None, label="Add a member", empty_label="— choose a user —")

    def __init__(self, *args, competition=None, **kwargs):
        from core.models.user import User

        super().__init__(*args, **kwargs)
        # Active accounts that are not in a team of this competition yet.
        self.fields["user"].queryset = (
            User.objects.filter(is_active=True, is_superuser=False)
            .exclude(team_memberships__team__competition=competition)
            .order_by("username")
        )
        self.fields["user"].label_from_instance = lambda user: user.username


# -- rounds ----------------------------------------------------------------------

def _minutes(label, default, low, high, required=True, help_text=""):
    return forms.IntegerField(
        label=label, initial=default, min_value=low, max_value=high, required=required, help_text=help_text
    )


class RoundForm(StyledForm):
    """A round of the competition: every team's worm runs by the round's gameplan, one team
    after another (docs/05-live-environment.md)."""

    name = forms.CharField(max_length=100)
    round_start = _datetime("Starts", help_text="UTC. The upload deadline: each team's latest upload before it runs.")
    round_end = _datetime("Ends", help_text="UTC. When the round should be over. Every team still runs, retries "
                                             "included; results are final when the last one has.")  # fmt: skip
    gameplan = forms.ChoiceField(
        required=False,
        help_text="A file in gameplans/ (docs/05-live-environment.md). Only gameplans the orchestrator and the node both "
                  "hold, and find valid, are listed. It can be chosen later, until the first team's attempt starts; "
                  "the round waits for it.",
    )  # fmt: skip
    team_duration = _minutes(
        "Duration per team (min)", schedule.TEAM_DURATION[0] // 60, schedule.TEAM_DURATION[1] // 60,
        schedule.TEAM_DURATION[2] // 60, help_text="From a team's worm starting to the end of its run: the "
        "gameplan's round_duration.",
    )  # fmt: skip
    seed = forms.IntegerField(
        required=False, min_value=0, max_value=2**62,
        help_text="Empty: random. The order the teams run in derives from it.",
    )  # fmt: skip

    # A round that has begun: its end, and its gameplan until the first attempt starts.
    STARTED_FIELDS = ("round_end", "gameplan", "team_duration")

    def __init__(self, *args, competition=None, instance=None, started=False, **kwargs):
        from core.models.rounds import GameplanFile

        super().__init__(*args, **kwargs)
        self.competition, self.instance, self.started = competition, instance, started
        if started:
            for name in list(self.fields):
                if name not in self.STARTED_FIELDS:
                    del self.fields[name]
        choices = [(g.filename, g.filename) for g in GameplanFile.objects.filter(valid=True).order_by("filename")]
        current = instance.gameplan if instance is not None else ""
        if current and current not in dict(choices):
            choices.append((current, f"{current} (not valid now)"))
        self.fields["gameplan"].choices = [("", "— none yet —"), *choices]

    def clean_gameplan(self):
        from core.models.rounds import GameplanFile

        value = self.cleaned_data["gameplan"]
        if value and not GameplanFile.objects.filter(filename=value, valid=True).exists():
            raise ValidationError("Choose a gameplan the orchestrator lists as valid.")
        return value

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        start = data.get("round_start", self.instance.round_start if self.instance else None)
        end = data.get("round_end")
        if start is not None and end is not None and end <= start:
            self.add_error("round_end", "The round must end after it starts.")
        if not self.started and start is not None and start <= clock.now():
            self.add_error("round_start", "Start the round in the future (the conductor plans its runs at the start).")
        return data

    # -- results ---------------------------------------------------------------

    def team_duration_seconds(self) -> int:
        return self.cleaned_data["team_duration"] * 60

    def chosen_seed(self) -> int:
        value = self.cleaned_data.get("seed")
        return value if value is not None else random.SystemRandom().randrange(2**62)

    @staticmethod
    def initial_from(round_obj) -> dict:
        """Form values of an existing round (seconds back to minutes)."""
        return {
            "name": round_obj.name,
            "round_start": round_obj.round_start,
            "round_end": round_obj.round_end,
            "seed": round_obj.seed,
            "gameplan": round_obj.gameplan,
            "team_duration": round_obj.team_duration // 60,
        }


def overlapping_round(competition, start, end, exclude=None):
    """Another round of the competition whose window overlaps [start, end)."""
    rounds = Round.objects.filter(competition=competition, round_start__lt=end, round_end__gt=start)
    if exclude is not None:
        rounds = rounds.exclude(pk=exclude.pk)
    return rounds.order_by("round_start").first()
