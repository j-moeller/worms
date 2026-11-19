"""Round one: a 20-host network behind a default-deny firewall, with a credential chain the
worm follows outward from a single entry host.

    A  (entry; holds an unprotected SSH key; short grace before it is powered off)
    ├─ B1  (its own unprotected SSH key)
    │   ├─ C1.1 ── D1.1.1 (ssh pwd, creds in a txt) ── E1.1.1 (encrypted key; passphrase Caesar-ciphered in a note)
    │   ├─ C1.2  (home-directory notes; one has the telnet login for D1.2.1)
    │   │   └─ D1.2.1 (telnet) ── E1.2.1 (ssh pwd; login in a KeePass vault, master in a note)
    │   └─ C1.3  (sshd on port 2222) ── D1.3.1 (ssh key, key in a zip) ── E1.3.1 (telnet, pwd in a txt)
    └─ B2  (its own SSH key, passphrase-protected; the passphrase is in note.txt)
        ├─ C2.1 ── D2.1.1 (telnet, creds in a docx) ── E2.1.1 (ssh pwd, creds in a zip/txt)
        │       └─ D2.1.2 (ssh key, key in a zip)
        └─ C2.2 ── D2.2.1 (ssh pwd, creds in a docx) ── E2.2.1 (encrypted key; passphrase from easy hints in a note)
                                                     └─ E2.2.2 (telnet, pwd in a txt)

Layers (A=0, B=1, C=2, D=3, E=4). Layers 0-2 are the fixed opening the brief specifies: SSH
keys on A and B1, a passphrase key on B2, a telnet note on C1.2, sshd on 2222 on C1.3. From
layer 3 on the remaining eleven hosts are spread across both subtrees, reached by a mix of the
three methods -- an SSH key, an SSH password found on the connecting host, a telnet password
noted there -- with the credential carried in a plain .txt, a .docx or a .zip holding a .txt,
and a few decoy files scattered in each home directory.

Two of the deepest hosts hide a harder credential (not the same one for both, as the brief
asks). E1.1.1's key is passphrase-encrypted and the passphrase is written only in Caesar-
ciphered form, with a note giving the exact shift to reverse. E2.2.1's key is passphrase-
encrypted too, but its passphrase is never written down -- a note gives easy hints that lead
to it. (The framework generates every key fresh and injects it at build time and has no
transform step, so the cipher/hints act on the key's passphrase, which is what makes the
encrypted key usable; the key's own bytes cannot be scrambled from here.)

One more host needs a technique rather than a credential lying in a file: E1.2.1's login sits
inside a KeePass vault whose master password is on a sticky-note file beside it. (A weak
brute-forceable PIN, a TOTP second factor and an FTP upload-then-SSH foothold are held back for
round two, to keep this opening round approachable.)

The worm lands on A and has a short grace period before A is powered off, so every host past A
hands back its flag only once A is gone: only a worm that spread survives. After a second grace
period the network begins to churn -- a fixed, seeded selection of hosts is rebooted (shut down,
then started again after a delay), identically in every team's round. Every key, password and
flag is new for each run.

(Framework: docs/05-live-environment.md; a smaller worked example is gameplans/credential_chain.py.)
"""

import random

from gameplan import (Allow, BasicVM, Docx, Entry, FirewallRule, FreezeState, Gameplan, Incoming, KeePass, Outgoing,
                      Zip)  # fmt: skip


class RoundOne(Gameplan):
    description = (
        "Round one. The worm lands on a single entry host and has a short grace period before "
        "that host is powered off. Everything of value is deeper in the network: twenty hosts "
        "behind a default-deny firewall, each reachable only from the one before it. Follow the "
        "credentials outward -- SSH keys, passwords noted in files, a telnet login or two -- and "
        "spread before the entry host goes dark. Only a worm that moved keeps scoring."
    )
    # round_duration = 60 * 60    # the default: one hour per team from the worm's start
    grace = 10 * 60               # how long A stays up after the worm lands, before it is shut down
    churn_grace = 10 * 60         # the calm after A is down, before the network starts churning

    # Decoy files dropped into the home directories of the layer-3+ hosts (plausible noise the
    # worm has to sort the real credential out from).
    _NOISE = [
        ("readme.txt", "ops notes\n- .1 is the gateway on every subnet\n- logs rotate weekly\n"),
        ("todo.md", "- [ ] rotate the service passwords\n- [ ] move the last box off telnet\n"),
        (".gitconfig", "[user]\n\tname = ops\n[alias]\n\tst = status\n\tlg = log --oneline\n"),
        ("python-notes.txt", "reminders\n- dict.get(k, default)\n- pathlib over os.path\n- f-strings\n"),
        ("inventory.txt", "stale inventory\n  edge  relay  store  build  kiosk\n"),
        ("vpn.conf.bak", "remote vpn.example 1194\ncipher AES-256-GCM\nauth SHA256\n"),
        ("backup.log", "rsync 2026-11-02 03:00  done, 3 files changed\n"),
        ("scan.txt", "last scan\n  22/tcp open\n  23/tcp filtered\n  80/tcp closed\n"),
    ]

    def __init__(self):
        # -- the hosts (20) --------------------------------------------------------------------
        # Layers 0-2: the fixed opening.
        self.a = BasicVM()                               # entry
        self.b1 = BasicVM()
        self.b2 = BasicVM()
        self.c1_1 = BasicVM()
        self.c1_2 = BasicVM()
        self.c1_3 = BasicVM()
        self.c2_1 = BasicVM()
        self.c2_2 = BasicVM()
        self.d1_2_1 = BasicVM()                          # telnet box off C1.2 (layer 3)
        # Layers 3-4: the eleven remaining hosts, spread across both subtrees.
        self.d1_1_1 = BasicVM()
        self.d1_3_1 = BasicVM()
        self.d2_1_1 = BasicVM()
        self.d2_1_2 = BasicVM()
        self.d2_2_1 = BasicVM()
        self.e1_1_1 = BasicVM()
        self.e1_2_1 = BasicVM()
        self.e1_3_1 = BasicVM()
        self.e2_1_1 = BasicVM()
        self.e2_2_1 = BasicVM()
        self.e2_2_2 = BasicVM()

        # C1.3 answers SSH on 2222, not 22 (22 stays closed, for a worm to find the open port).
        self.c1_3.set_ssh_port(2222)

        self._noise_i = 0

        # == layers 0-2: the fixed opening (the brief) =========================================

        # A -> B1, B2 : one key, no passphrase, on A; B1 and B2 accept it.
        key_a = self.generate_ssh_key("id_a")
        self.add_private_ssh_key([self.a], key_a)
        self.add_public_ssh_key([self.b1, self.b2], key_a)

        # B1 -> C1.1, C1.2, C1.3 : B1's own key, no passphrase; the three C1 hosts accept it.
        key_b1 = self.generate_ssh_key("id_b1")
        self.add_private_ssh_key([self.b1], key_b1)
        self.add_public_ssh_key([self.c1_1, self.c1_2, self.c1_3], key_b1)

        # B2 -> C2.1, C2.2 : B2's own key, WITH a passphrase; the passphrase is in note.txt on B2.
        b2_passphrase = self.generate_password(length=12)
        key_b2 = self.generate_ssh_key("id_b2", passphrase=b2_passphrase)
        self.add_private_ssh_key([self.b2], key_b2)
        self.add_public_ssh_key([self.c2_1, self.c2_2], key_b2)
        self.b2.write_file("~/note.txt", f"ssh passphrase: {b2_passphrase}\n")

        # C1.2 -> D1.2.1 : telnet, with a username and password D1.2.1 accepts. The creds sit in
        # a note on C1.2, among decoy notes.
        d1_login = self.generate_login("operator")
        self.add_telnet_login(self.d1_2_1, d1_login)
        self.c1_2.write_file("~/notes-git.txt",
                             "git notes\n- git reset --soft HEAD~1 keeps the changes staged.\n"
                             "- git reflog finds commits you thought you lost.\n")
        self.c1_2.write_file("~/notes-python.txt",
                             "python idioms\n- dict.get(k, default) over try/except KeyError.\n"
                             "- enumerate(xs, start=1) instead of a manual counter.\n")
        self.c1_2.write_file("~/notes-networking.txt",
                             "networking basics\n- /24 is 256 addresses, .1 is usually the gateway.\n"
                             "- telnet is cleartext; prefer SSH where you can.\n")
        self.c1_2.write_file("~/notes-d1.txt",
                             f"reminder: telnet box D1.2.1 on the far subnet\n"
                             f"  username: {d1_login.user}\n  password: {d1_login.pwd}\n")
        self._noise(self.d1_2_1)   # D1.2.1 is layer 3, so it gets decoy files like the others

        # == layers 3-4: the remaining eleven, by method and carrier ===========================

        # C1.1 -> D1.1.1 : SSH password, the login noted in a plain txt on C1.1.
        self._pwd_hop(self.c1_1, self.d1_1_1, "webops", carrier="txt", host="the C1.1 back-office box")
        # D1.1.1 -> E1.1.1 : encrypted SSH key. The key (in a zip) needs a passphrase, and the
        # passphrase is written only Caesar-shifted -- the note gives the exact shift to undo.
        shift = 5
        e111_passphrase = "autumnforge"
        self._encrypted_key_hop(self.d1_1_1, self.e1_1_1, "id_e111", e111_passphrase, "passphrase-recovery.txt",
            "KEY RECOVERY NOTE\n"
            "The passphrase on the archive key was obscured before we wrote it down.\n"
            f"Each letter was shifted FORWARD {shift} places in the alphabet (a Caesar shift).\n"
            f"Shift each letter BACK {shift} places to read the real passphrase.\n"
            f"obscured passphrase: {self._caesar(e111_passphrase, shift)}\n")  # fmt: skip

        # D1.2.1 -> E1.2.1 : SSH password, but the login lives in a KeePass vault on D1.2.1; the
        # vault's master password is on a sticky-note file beside it.
        courier = self.generate_login("courier")
        self.add_ssh_pwd_login([self.e1_2_1], courier)
        vault_master = self.generate_password(length=10)
        self.d1_2_1.write_file("~/secrets.kdbx", KeePass(vault_master, [
            Entry("archive node (ssh)", courier.user, courier.pwd, url="ssh://archive-node",
                  notes="ssh login for the next box over"),
        ]))
        self.d1_2_1.write_file("~/vault-master.txt",
                               "KeePass master password for secrets.kdbx (yes, we know we should not keep it here)\n"
                               f"master: {vault_master}\n")
        self._hop(self.d1_2_1, self.e1_2_1, 22)
        self._noise(self.e1_2_1)

        # C1.3 -> D1.3.1 : SSH key, the private key inside a zip on C1.3.
        self._key_hop(self.c1_3, self.d1_3_1, "id_d131", carrier="zip")
        # D1.3.1 -> E1.3.1 : telnet, the password in a plain txt on D1.3.1.
        self._pwd_hop(self.d1_3_1, self.e1_3_1, "relay", carrier="txt", telnet=True, host="the relay on the far leg")

        # C2.1 -> D2.1.1 : telnet, the login carried in a .docx handover on C2.1.
        self._pwd_hop(self.c2_1, self.d2_1_1, "kiosk", carrier="docx", telnet=True, host="the kiosk terminal")
        # C2.1 -> D2.1.2 : SSH key, the private key inside a zip on C2.1 (C2.1's second neighbour).
        self._key_hop(self.c2_1, self.d2_1_2, "id_d212", carrier="zip")
        # D2.1.1 -> E2.1.1 : SSH password, the login in a txt inside a zip on D2.1.1.
        self._pwd_hop(self.d2_1_1, self.e2_1_1, "telemetry", carrier="zip", host="the telemetry collector")

        # C2.2 -> D2.2.1 : SSH password, the login carried in a .docx on C2.2.
        self._pwd_hop(self.c2_2, self.d2_2_1, "scanner", carrier="docx", host="the scanner host")
        # D2.2.1 -> E2.2.1 : encrypted SSH key. The passphrase is never written down; a note
        # gives easy hints that lead to it ("sunflower7"). (A different credential from E1.1.1.)
        self._encrypted_key_hop(self.d2_2_1, self.e2_2_1, "id_e221", "sunflower7", "passphrase-hints.txt",
            "passphrase hints for the vault key (we kept forgetting it)\n"
            "- it is a bright flower that turns to follow the sun, all one word, lowercase\n"
            "- then the only prime number between 6 and 8\n"
            "- no spaces, no capitals\n")  # fmt: skip
        # D2.2.1 -> E2.2.2 : telnet, the password in a plain txt on D2.2.1 (D2.2.1's second neighbour).
        self._pwd_hop(self.d2_2_1, self.e2_2_2, "printer", carrier="txt", telnet=True, host="the print spooler")

        # == networks and firewall for the fixed opening (one bridge per hop) ==================
        self.register_network([self.a, self.b1, self.b2])
        self.register_network([self.b1, self.c1_1])
        self.register_network([self.b1, self.c1_2])
        self.register_network([self.b1, self.c1_3])
        self.register_network([self.c1_2, self.d1_2_1])
        self.register_network([self.b2, self.c2_1])
        self.register_network([self.b2, self.c2_2])

        self.register_fw_rule(self.a, FirewallRule(Allow, 22, Outgoing, peers=[self.b1, self.b2]))

        self.register_fw_rule(self.b1, FirewallRule(Allow, 22, Incoming, peers=[self.a]))
        self.register_fw_rule(self.b1, FirewallRule(Allow, 22, Outgoing, peers=[self.c1_1, self.c1_2]))
        self.register_fw_rule(self.b1, FirewallRule(Allow, 2222, Outgoing, peers=[self.c1_3]))

        self.register_fw_rule(self.b2, FirewallRule(Allow, 22, Incoming, peers=[self.a]))
        self.register_fw_rule(self.b2, FirewallRule(Allow, 22, Outgoing, peers=[self.c2_1, self.c2_2]))

        self.register_fw_rule(self.c1_1, FirewallRule(Allow, 22, Incoming, peers=[self.b1]))
        self.register_fw_rule(self.c1_2, FirewallRule(Allow, 22, Incoming, peers=[self.b1]))
        self.register_fw_rule(self.c1_2, FirewallRule(Allow, 23, Outgoing, peers=[self.d1_2_1]))
        self.register_fw_rule(self.c1_3, FirewallRule(Allow, 2222, Incoming, peers=[self.b1]))
        self.register_fw_rule(self.d1_2_1, FirewallRule(Allow, 23, Incoming, peers=[self.c1_2]))

        self.register_fw_rule(self.c2_1, FirewallRule(Allow, 22, Incoming, peers=[self.b2]))
        self.register_fw_rule(self.c2_2, FirewallRule(Allow, 22, Incoming, peers=[self.b2]))

        self.set_worm_entrypoint(self.a)

    # -- helpers for the deeper hops -----------------------------------------------------------

    def _hop(self, parent, child, port):
        """One bridge joining parent and child, with parent allowed out and child allowed in on
        `port` and nothing else (the default-deny firewall drops the rest, both ways)."""
        self.register_network([parent, child])
        self.register_fw_rule(parent, FirewallRule(Allow, port, Outgoing, peers=[child]))
        self.register_fw_rule(child, FirewallRule(Allow, port, Incoming, peers=[parent]))

    def _key_hop(self, parent, child, name, carrier="zip"):
        """parent reaches child over SSH as the default user with a key child authorises; the
        private key is delivered on parent inside `carrier` (a zip holding the key file)."""
        key = self.generate_ssh_key(name)
        self.add_public_ssh_key([child], key)
        if carrier == "zip":
            parent.write_file(f"~/{name}.zip", Zip({
                name: key.private,
                "README.txt": f"private key for the next box; `unzip`, chmod 600 {name}, then ssh -i {name}\n",
            }))
        else:
            parent.write_file(f"~/{name}", key.private, mode=0o600)
        self._hop(parent, child, 22)
        self._noise(child)

    def _encrypted_key_hop(self, parent, child, name, passphrase, note_name, note_body):
        """As a key hop, but the key is passphrase-encrypted and the passphrase is NOT in the
        zip with it: it has to be recovered from a separate note (a cipher to reverse, or hints
        to follow). parent carries the encrypted key and the note; the decoy files alongside
        them make the note one more thing to find."""
        key = self.generate_ssh_key(name, passphrase=passphrase)
        self.add_public_ssh_key([child], key)
        parent.write_file(f"~/{name}.zip", Zip({
            name: key.private,
            "README.txt": f"encrypted private key for the next box; it needs a passphrase (see {note_name})\n",
        }))
        parent.write_file(f"~/{note_name}", note_body)
        self._hop(parent, child, 22)
        self._noise(child)

    @staticmethod
    def _caesar(text, shift):
        """Shift letters by `shift` places (wrapping a-z and A-Z); leave everything else alone."""
        out = []
        for ch in text:
            for base in ("a", "A"):
                if 0 <= ord(ch) - ord(base) < 26:
                    out.append(chr(ord(base) + (ord(ch) - ord(base) + shift) % 26))
                    break
            else:
                out.append(ch)
        return "".join(out)

    def _pwd_hop(self, parent, child, user, carrier="txt", telnet=False, host="the next box"):
        """parent reaches child by password -- SSH, or telnet (port 23) if `telnet` -- as `user`;
        the login is carried on parent in a .txt, a .docx, or a .txt inside a .zip."""
        login = self.generate_login(user)
        (self.add_telnet_login if telnet else self.add_ssh_pwd_login)([child], login)
        how = "telnet" if telnet else "ssh"
        if carrier == "docx":
            parent.write_file("~/handover.docx", Docx([
                "Operations handover",
                f"{how} access to {host}:",
                f"username: {login.user}",
                f"password: {login.pwd}",
            ], title="Handover"))
        elif carrier == "zip":
            parent.write_file(f"~/creds-{user}.zip", Zip({
                "creds.txt": f"{host}\n{how}\nuser: {login.user}\npassword: {login.pwd}\n",
            }))
        else:
            parent.write_file(f"~/creds-{user}.txt", f"{host} ({how})\nuser: {login.user}\npassword: {login.pwd}\n")
        self._hop(parent, child, 23 if telnet else 22)
        self._noise(child)

    def _noise(self, vm, k=2):
        """A few decoy files in `vm`'s home directory (layer 3 and deeper)."""
        for j in range(k):
            name, body = self._NOISE[(self._noise_i + j) % len(self._NOISE)]
            vm.write_file(f"~/{name}", body)
        self._noise_i += k

    # -- the run -------------------------------------------------------------------------------

    def execute(self):
        # Every host past A hands its flag back only after A is gone, so a worm has to spread off
        # A within the grace period to score anything beyond the entry host.
        downstream = [vm for vm in self.vms if vm is not self.a]
        held = [self.remove_flag(vm) for vm in downstream]

        self.deploy_worm()            # the worm lands on A; the run's clock starts
        self.wait(self.grace)         # A's grace period

        # Freeze everything, wake A only to power it off, then let the rest run on.
        with FreezeState(self.vms) as frozen:
            frozen.unfreeze(self.a)
            self.a.shutdown()

        # The downstream flags appear now: only a worm that left A can reach them.
        for flag in held:
            self.add_flag(flag)

        # After a second grace period the network starts to churn: a fixed, seeded selection of
        # hosts is rebooted (shut down, then started again after a delay). The seed is the class
        # name, so the selection and the delays are the same in every team's round -- deterministic
        # instability, not a per-run surprise. A worm has to survive its host going away and
        # coming back. We keep a margin before the round ends so no reboot is left hanging.
        self.wait(self.churn_grace)
        rng = random.Random(f"{type(self).__name__}/churn")
        churny = [vm for vm in self.vms if vm is not self.a]   # A is already powered off
        rng.shuffle(churny)
        for vm in churny[: len(churny) // 2]:
            if self.remaining < 5 * 60:        # leave the last few minutes quiet before scoring
                break
            down = rng.randint(30, 150)        # how long it stays off
            self.log(f"churn: rebooting {vm.name} (down for {down}s)")
            vm.shutdown()
            self.wait(down)
            vm.start()
            self.wait(rng.randint(30, 120))    # a gap before the next reboot

        # The remainder of the hour passes; then every host is stopped and the run is scored.
