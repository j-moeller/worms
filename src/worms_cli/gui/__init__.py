"""worms-gui: an htop-like, interactive terminal dashboard for the WORMS platform, beside the `worms` CLI.

    state.py     the data model and its thread-safe store (what the UI reads: Store.snapshot)
    collect.py   the collectors: threads that fill the store (Proxmox, the host, the database, SSH watchers)
    actions.py   the commands the UI offers (each a `worms` command) and the runner that runs them
    checks.py    the devbox checks of the Teams tab: `worms check <team> --json`, one at a time, kept per team
    ui.py        curses
    app.py       options, wiring, start and stop

It shows what `worms status` shows, live, and runs the CLI's own commands: every change goes through
src/manage.py, with its checks, its lock and its run log. Standard library only, plus what the CLI
already uses (proxmoxer, Django through worms_cli.django_setup).
"""
