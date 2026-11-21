"""Tab completion for `worms`, in bash, written by hand (no argcomplete: the host has no internet).

    worms completion bash                     the script: `eval "$(worms completion bash)"` in
                                              ~/.bashrc, or into /etc/bash_completion.d/worms
    worms __complete targets|teams            what a TAB offers that changes: one per line

The script holds what does not change with the platform, taken from the CLI's own tables when it
is generated, so it cannot drift from them: the commands and their subcommands and flags (the
argparse parsers), the topics of `show`, the components and groups, the steps, the services. Run it
again after an update.

What changes, the VMs and the teams, comes from `worms __complete`, which main() runs before its
own imports (a TAB has to answer in well under a second) and which reads local files only: no
Proxmox API, no SSH, no database. The VMs: the generated Ansible inventories the VM catalogue reads
(this checkout's; on the host also the round agent's workspace's). The teams: ~/.worms/team-map.json,
written whenever the team map was read from the database (worms_cli/teams.py, write_cache).
"""

from __future__ import annotations

import json
import os

# The fast path imports nothing else (no re, pathlib, dataclasses, worms_cli.teams or .config:
# together they cost a TAB more than the rest of it). These follow worms_cli/teams.py.
DOMAIN = ".worms.internal"
AGENT_ENV = "/etc/worms/round-agent.env"  # REPO_ROOT: the round agent's workspace
WORKSPACE_INVENTORY = "/opt/worms/ansible/inventory.generated.yml"
TEAM_CACHE = "~/.worms/team-map.json"  # teams.CACHE
ROLES = ("a", "b", "c", "gw")  # teams.ROLES
SHARED_GATEWAY = "devbox-ssh-gateway"  # teams.SHARED_GATEWAY


# -- `worms __complete <kind>`: the fast path ------------------------------------


def _read(path: str) -> str:
    try:
        with open(path, errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _setting(text: str, key: str) -> str:
    """`KEY=value` from a config or environment file's text ("" if it is not set)."""
    for line in text.splitlines():
        name, _, value = line.strip().partition("=")
        if name.strip() == key:
            return value.strip()
    return ""


def repo_root() -> str:
    """As config.Config has it: WORMS_REPO_ROOT (the environment, ~/.worms/config), else this checkout."""
    return (os.environ.get("WORMS_REPO_ROOT") or _setting(_read(os.path.expanduser("~/.worms/config")), "WORMS_REPO_ROOT")
            or os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))  # fmt: skip


def inventories(root: str) -> list[str]:
    """The generated inventories there are here: the checkout's, and the agent's workspace's."""
    workspace = _setting(_read(AGENT_ENV), "REPO_ROOT")
    paths = [os.path.join(root, "ansible", "inventory.generated.yml"), WORKSPACE_INVENTORY]
    if workspace:
        paths.insert(1, os.path.join(workspace, "ansible", "inventory.generated.yml"))
    out = []
    for path in paths:
        if os.path.isfile(path) and os.path.realpath(path) not in {os.path.realpath(p) for p in out}:
            out.append(path)
    return out


def inventory_hosts(text: str) -> dict[str, str]:
    """{host: its devbox_team or ""} of a generated inventory, without YAML (it costs a TAB a
    tenth of a second): a host is the key just above its `ansible_host`."""
    hosts: dict[str, str] = {}
    last = None
    for line in text.splitlines():
        stripped = line.strip()
        if line[:1] == " " and stripped.endswith(":") and " " not in stripped:
            last = stripped[:-1]
            continue
        key, _, value = stripped.partition(":")
        if last and key in ("ansible_host", "devbox_team"):
            name = last.removesuffix(DOMAIN)
            if key == "ansible_host":
                hosts.setdefault(name, "")
            elif name in hosts:
                hosts[name] = value.strip().strip("'\"")
    hosts.pop("proxmox.local", None)
    return hosts


def of_vm(name: str) -> tuple[str, str] | None:
    """teams.of_vm without re: (slug, role) of devbox-t<n>-<a|b|c|gw>."""
    parts = name.split("-")
    if len(parts) == 3 and parts[0] == "devbox" and parts[1][:1] == "t" and parts[1][1:].isdigit() and parts[2] in ROLES:
        return parts[1], parts[2]
    return None


SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+-")  # teams.SAFE_NAME


def safe(word: str) -> bool:
    """Only words of these characters reach the shell: a team names itself on the dashboard, and
    a name like `$(...)` must never be offered to a completion, which bash may expand."""
    return bool(word) and all(c in SAFE for c in word)


def handle(slug: str, name: str) -> str:
    """teams.Team.handle: the name when it is a safe word, else the slug."""
    return name if safe(name) else slug


def cached_teams() -> dict[str, str]:
    """{slug: name} as the team map last read them from the database ({} if it never did)."""
    try:
        data = json.loads(_read(os.path.expanduser(TEAM_CACHE)) or "{}")
        return {str(t["slug"]): str(t.get("name") or "") for t in data.get("teams", []) if t.get("slug")}
    except (ValueError, AttributeError, TypeError, KeyError):
        return {}


def candidates(kind: str, root: str) -> list[str]:
    """Safe words only (safe; a target is two of them around a "/"): whatever a file says."""
    hosts: dict[str, str] = {}
    for path in inventories(root):
        hosts.update(inventory_hosts(_read(path)))
    hosts = {name: team for name, team in hosts.items() if safe(name)}
    known = {slug: name for slug, name in cached_teams().items() if safe(slug)}
    for slug in {*(t for t in hosts.values() if safe(t)), *(of_vm(n)[0] for n in hosts if of_vm(n))}:
        known.setdefault(slug, "")
    if kind == "teams":
        return sorted({*known, *(handle(slug, name) for slug, name in known.items())})
    if kind != "targets":
        return []
    out = {"host", *hosts}
    if SHARED_GATEWAY in hosts:
        out.add("ssh-gateway")
    for slug, name in known.items():
        for role in ROLES:
            out.update({f"devbox-{slug}-{role}", f"{slug}/{role}", f"{handle(slug, name)}/{role}"})
    return sorted(word for word in out if all(safe(part) for part in word.split("/", 1)))


def complete_main(argv) -> int:
    """`worms __complete targets|teams`: one candidate per line; nothing, and 0, when it cannot say."""
    if len(argv) != 1:
        return 0
    try:
        print("\n".join(candidates(argv[0], repo_root())))
    except Exception:  # noqa: BLE001  (a TAB never shows a traceback)
        return 0
    return 0


# -- `worms completion bash`: the script ----------------------------------------------


def _options(parser) -> list[str]:
    return sorted({o for a in parser._actions for o in a.option_strings})


def _subparsers(parser):
    import argparse

    found = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return found[0].choices if found else {}


def _positional_choices(parser) -> list[str]:
    import argparse

    for action in parser._actions:
        if not action.option_strings and action.choices and not isinstance(action, argparse._SubParsersAction):
            return list(action.choices)
    return []


def table(parser, slots: dict[str, list[str]]) -> dict:
    """What the script holds, from the parsers: the commands, each command's (and subcommand's)
    flags and subcommands, and its positional slots: `slots` (what the parsers cannot say: the
    components, the services, `@targets`, `@teams`), else a positional's choices."""
    commands = _subparsers(parser)
    flags, subs, positions = {}, {}, {}
    for name, command in commands.items():
        flags[name] = _options(command)
        inner = _subparsers(command)
        if inner:
            subs[name] = list(inner)
        for key, p in [(name, command), *((f"{name} {s}", sp) for s, sp in inner.items())]:
            flags[key] = _options(p)
            if key in slots:
                positions[key] = slots[key]
            elif _positional_choices(p):
                positions[key] = [" ".join(_positional_choices(p)), ""]
    return {"commands": list(commands), "global": _options(parser), "flags": flags, "subs": subs, "positions": positions}


TEMPLATE = r"""# bash completion for worms (written by `worms completion bash`: generate it again after an update)
# Install: eval "$(worms completion bash)" in ~/.bashrc, or: worms completion bash > /etc/bash_completion.d/worms
_WORMS_RUN=(@RUN@)
_WORMS_COMMANDS=@COMMANDS@
_WORMS_GLOBAL=@GLOBAL@
declare -gA _WORMS_FLAGS=(@FLAGS@)
declare -gA _WORMS_SUBS=(@SUBS@)
declare -gA _WORMS_SLOTS=(@SLOTS@)

# Candidates are matched here as plain strings, never handed to compgen as a word list, which it
# expands (a team names itself on the dashboard: a name like $(...) would run). One per line from
# `worms __complete`, which offers safe words only; and only safe words are taken here either,
# as one put on the command line would run when the line does.
_worms_offer() {  # $1: the word typed so far; stdin: candidates, one per line; $2: a suffix
    local word safe='^[A-Za-z0-9._+/-]+$'
    while IFS= read -r word; do
        [[ $word =~ $safe && $word == "$1"* ]] && COMPREPLY+=("$word$2")
    done
}

_worms() {
    local cur prev words cword
    if declare -F _get_comp_words_by_ref >/dev/null 2>&1; then
        _get_comp_words_by_ref -n : cur prev words cword
    else
        words=("${COMP_WORDS[@]}"); cword=$COMP_CWORD; cur=${COMP_WORDS[COMP_CWORD]}; prev=${COMP_WORDS[COMP_CWORD-1]}
    fi
    COMPREPLY=()
    local cmd="" sub="" word i
    local -a pos=()
    for ((i = 1; i < cword; i++)); do
        word=${words[i]}
        [[ $word == -- ]] && return 0  # the tools' own arguments: the shell's file completion
        if [[ $word == --config ]]; then ((i++)); continue; fi
        [[ $word == -* ]] && continue
        if [[ -z $cmd ]]; then cmd=$word
        elif [[ -z $sub && " ${_WORMS_SUBS[$cmd]} " == *" $word "* ]]; then sub=$word
        else pos+=("$word"); fi
    done
    case $prev in --config|-o|--output|--name|--kind|--hours|--limit|--since|-n|--lines|--timeout) return 0 ;; esac
    local key=$cmd${sub:+ $sub} list=""
    if [[ -z $cmd ]]; then
        [[ $cur == -* ]] && list=$_WORMS_GLOBAL || list=$_WORMS_COMMANDS
    elif [[ $cur == -* ]]; then
        list=${_WORMS_FLAGS[$key]}
    elif [[ -n ${_WORMS_SUBS[$cmd]} && -z $sub ]]; then
        list=${_WORMS_SUBS[$cmd]}
    else
        local -a slots
        IFS='|' read -ra slots <<< "${_WORMS_SLOTS[$key]}"
        ((${#slots[@]})) || return 0
        local at=${#pos[@]}
        ((at >= ${#slots[@]})) && at=$((${#slots[@]} - 1))
        local token
        local -a tokens
        read -ra tokens <<< "${slots[at]}"
        for token in "${tokens[@]}"; do
            case $token in
                @rsync)
                    [[ $cur == *:* ]] && return 0
                    compopt -o nospace 2>/dev/null
                    mapfile -t COMPREPLY < <(compgen -f -- "$cur")
                    _worms_offer "$cur" ":" < <("${_WORMS_RUN[@]}" __complete targets 2>/dev/null) ;;
                @*) _worms_offer "$cur" "" < <("${_WORMS_RUN[@]}" __complete "${token#@}" 2>/dev/null) ;;
                *) list+=" $token" ;;
            esac
        done
    fi
    local -a fixed
    read -ra fixed <<< "$list"  # this script's own words: no glob, no expansion
    _worms_offer "$cur" "" < <(printf '%s\n' "${fixed[@]}")
    declare -F __ltrim_colon_completions >/dev/null 2>&1 && __ltrim_colon_completions "$cur"
    return 0
}
complete -o default -F _worms worms
"""


def _bash_map(mapping: dict) -> str:
    import shlex

    return " ".join(f"[{shlex.quote(key)}]={shlex.quote(value)}" for key, value in mapping.items())


def script(parser, slots: dict[str, list[str]], run: list[str]) -> str:
    """The bash completion script; `run`: the argv that is `worms` (the CLI's python and
    manage.py, so it does not depend on the shell function)."""
    import shlex

    data = table(parser, slots)
    replacements = {
        "@RUN@": " ".join(shlex.quote(word) for word in run),
        "@COMMANDS@": shlex.quote(" ".join(data["commands"])),
        "@GLOBAL@": shlex.quote(" ".join(data["global"])),
        "@FLAGS@": _bash_map({key: " ".join(value) for key, value in data["flags"].items()}),
        "@SUBS@": _bash_map({key: " ".join(value) for key, value in data["subs"].items()}),
        "@SLOTS@": _bash_map({key: "|".join(value) for key, value in data["positions"].items()}),
    }
    text = TEMPLATE
    for marker, value in replacements.items():
        text = text.replace(marker, value)
    return text
