"""The two ways a run goes wrong (gameplan/definition.py, gameplan/runtime.py)."""


class GameplanError(ValueError):
    """The gameplan is not valid, or used the API the wrong way. Before a round: the gameplan
    cannot be scheduled. During a run: the attempt fails like any other error."""


class RoundError(RuntimeError):
    """The infrastructure failed (a host job, the Proxmox API, a check). The attempt is thrown
    away and the team's worm runs again (docs/05-live-environment.md)."""
