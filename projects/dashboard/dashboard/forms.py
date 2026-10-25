"""Forms of the staff pages (dashboard/views/staff*.py).

Plain Django forms rendered by the shared field template
(templates/staff/_field.html); they work without JavaScript. Every value is
validated here; the round parameters again by the round agent (the same
bounds: core/schedule.py).
"""

from __future__ import annotations

import random

from core import clock, schedule
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
    email = forms.EmailField(label="Email (the username)", max_length=150)
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

LIVE, DEV = "live", "dev"


def _minutes(label, default, low, high, required=True, help_text=""):
    return forms.IntegerField(
        label=label, initial=default, min_value=low, max_value=high, required=required, help_text=help_text
    )


class RoundForm(StyledForm):
    """A round of the competition (Live: every team's worm runs, one team
    after another) or of its development competition (Dev: only a time
    window for the devbox progression; nothing is built)."""

    environment = forms.ChoiceField(
        choices=[(LIVE, "Live — every team's worm runs in its own sandbox"),
                 (DEV, "Dev — a window for the devbox progression only")],
        initial=LIVE,
    )  # fmt: skip
    name = forms.CharField(max_length=100)
    round_start = _datetime("Starts", help_text="UTC. The upload deadline: each team's latest upload before it runs.")
    round_end = _datetime("Ends", help_text="UTC. Runs that cannot finish before it are skipped.")
    seed = forms.IntegerField(
        required=False, min_value=0, max_value=2**62,
        help_text="Empty: random. Every team's sandbox and game plan derive from it.",
    )  # fmt: skip

    # The sandbox (scripts/generate_sandbox.py), per team.
    vms = forms.IntegerField(label="VMs", min_value=schedule.SANDBOX_BOUNDS["vms"][1],
                             max_value=schedule.SANDBOX_BOUNDS["vms"][2], initial=schedule.SANDBOX_BOUNDS["vms"][0])
    networks = forms.IntegerField(min_value=schedule.SANDBOX_BOUNDS["networks"][1],
                                  max_value=schedule.SANDBOX_BOUNDS["networks"][2],
                                  initial=schedule.SANDBOX_BOUNDS["networks"][0])
    sites = forms.IntegerField(min_value=schedule.SANDBOX_BOUNDS["sites"][1],
                               max_value=schedule.SANDBOX_BOUNDS["sites"][2], initial=schedule.SANDBOX_BOUNDS["sites"][0])
    max_interfaces = forms.IntegerField(label="Interfaces per VM at most",
                                        min_value=schedule.SANDBOX_BOUNDS["max_interfaces"][1],
                                        max_value=schedule.SANDBOX_BOUNDS["max_interfaces"][2],
                                        initial=schedule.SANDBOX_BOUNDS["max_interfaces"][0])  # fmt: skip

    # One run (minutes).
    worm_start_delay = _minutes("Fuse (min)", 5, 0, 1440, help_text="From deploy to the worm's start.")
    run_duration = _minutes("Run length (min)", 60, 1, 1440, help_text="From the worm's start to the end of its run.")

    # The game plan (orchestrator/rounds/gameplan.py), minutes after the worm's start.
    initial_fraction = forms.FloatField(
        label="Secrets up front", min_value=0.0, max_value=1.0, initial=0.5,
        help_text="Share of the other VMs whose flag exists before the worm starts.",
    )  # fmt: skip
    first_stop_after = _minutes("First VM stop (min)", 10, 0, 1440, help_text="The start VM is switched off first.")
    stop_interval = _minutes("Then a stop every (min)", 10, 1, 1440)
    min_alive = forms.IntegerField(label="VMs kept running", min_value=0, max_value=250, initial=2)
    restart_after = _minutes("Restart a stopped VM after (min)", None, 1, 1440, required=False,
                             help_text="Empty: stopped VMs stay off.")  # fmt: skip

    LIVE_ONLY = ("seed", "vms", "networks", "sites", "max_interfaces", "worm_start_delay", "run_duration",
                 "initial_fraction", "first_stop_after", "stop_interval", "min_alive", "restart_after")  # fmt: skip

    def __init__(self, *args, competition=None, instance=None, started=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.competition, self.instance, self.started = competition, instance, started
        if instance is not None:
            # The environment of an existing round is fixed.
            del self.fields["environment"]
        if started:
            # A round that has begun: only its end can still move.
            for name in list(self.fields):
                if name != "round_end":
                    del self.fields[name]

    @property
    def is_dev(self) -> bool:
        if self.instance is not None:
            return self.instance.competition.is_dev_competition
        return self.cleaned_data.get("environment") == DEV

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
        if self.started or self.is_dev:
            return data

        vms, networks = data["vms"], data["networks"]
        interfaces, alive = data["max_interfaces"], data["min_alive"]
        if networks > vms:
            self.add_error("networks", "At most one network per VM.")
        if interfaces > networks:
            self.add_error("max_interfaces", "A VM cannot have more interfaces than there are networks.")
        if alive >= vms:
            self.add_error("min_alive", "Keep fewer VMs running than the sandbox has.")
        return data

    # -- results ---------------------------------------------------------------

    def target_competition(self, dev_competition):
        if self.instance is not None:
            return self.instance.competition
        return dev_competition if self.is_dev else self.competition

    def sandbox_params(self) -> dict:
        """The round's sandbox_params (core/schedule.py) in the conductor's
        and the agent's units (seconds)."""
        data = self.cleaned_data
        if self.is_dev:
            return {}
        minute = 60
        game_plan = {
            "initial_fraction": float(data["initial_fraction"]),
            "first_stop_after": data["first_stop_after"] * minute,
            "stop_interval": data["stop_interval"] * minute,
            "min_alive": data["min_alive"],
            "restart_after": data["restart_after"] * minute if data.get("restart_after") else None,
        }
        return {
            "vms": data["vms"],
            "networks": data["networks"],
            "sites": data["sites"],
            "max_interfaces": data["max_interfaces"],
            "game_plan": game_plan,
            "timing": {
                "worm_start_delay": data["worm_start_delay"] * minute,
                "run_duration": data["run_duration"] * minute,
            },
        }

    def chosen_seed(self) -> int:
        value = self.cleaned_data.get("seed")
        return value if value is not None else random.SystemRandom().randrange(2**62)

    @staticmethod
    def initial_from(round_obj) -> dict:
        """Form values of an existing round (seconds back to minutes)."""
        params = round_obj.sandbox_params or {}
        game_plan = params.get("game_plan") or {}
        timing = schedule.run_timing(params)
        initial = {
            "name": round_obj.name,
            "round_start": round_obj.round_start,
            "round_end": round_obj.round_end,
            "seed": round_obj.seed,
        }
        for key, (default, _, _) in schedule.SANDBOX_BOUNDS.items():
            initial[key] = params.get(key, default)
        initial.update(
            worm_start_delay=timing["worm_start_delay"] // 60,
            run_duration=timing["run_duration"] // 60,
            initial_fraction=game_plan.get("initial_fraction", 0.5),
            first_stop_after=game_plan.get("first_stop_after", 600) // 60,
            stop_interval=game_plan.get("stop_interval", 600) // 60,
            min_alive=game_plan.get("min_alive", 2),
            restart_after=(game_plan["restart_after"] // 60) if game_plan.get("restart_after") else None,
        )
        return initial


def overlapping_round(competition, start, end, exclude=None):
    """Another round of the competition whose window overlaps [start, end)."""
    rounds = Round.objects.filter(competition=competition, round_start__lt=end, round_end__gt=start)
    if exclude is not None:
        rounds = rounds.exclude(pk=exclude.pk)
    return rounds.order_by("round_start").first()
