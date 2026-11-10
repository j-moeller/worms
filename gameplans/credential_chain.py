"""A credential chain through documents, a vault, an encrypted zip, telnet and SSH with a
one-time code (docs/05-live-environment.md). The worm starts on sandbox-entry:

  sandbox-entry   handover.doc names the master password of passwords.kdbx, which holds
                  archivist's telnet login to sandbox-files
  sandbox-files   archivist's accounts.ods holds the password of backup.zip, which holds ops's
                  SSH key and an authenticator export with ops's TOTP secret
  sandbox-vault   ops logs in over SSH with that key and the current code

Every password, key and TOTP secret is new for every run. Only a worm that reads each kind of
file and crosses each kind of login reaches sandbox-vault."""

from gameplan import (Allow, BasicVM, Doc, Entry, FirewallRule, Gameplan, Incoming, KeePass, Ods, Outgoing,
                      TotpExport, Zip)


class CredentialChain(Gameplan):
    def __init__(self):
        self.entry = BasicVM()
        self.files = BasicVM()
        self.vault = BasicVM()

        # sandbox-files: telnet, with a password from the KeePass database on sandbox-entry.
        archivist = self.generate_ssh_pwd_login("archivist")
        self.add_telnet_login(self.files, archivist)

        # sandbox-vault: SSH with a key and a one-time code, both in an encrypted zip on sandbox-files.
        ops_key = self.generate_ssh_key("id_ops")
        ops_totp = self.generate_totp("ops@sandbox-vault", issuer="sandbox-vault")
        self.add_ssh_totp_login(self.vault, ops_key, ops_totp, user="ops")

        vault_password = self.generate_password()
        zip_password = self.generate_password(length=12)

        self.entry.write_file("~/handover.doc", Doc([
            "Handover",
            "Our logins are in passwords.kdbx (KeePass).",
            f"Master password: {vault_password}",
        ], title="Handover"))
        self.entry.write_file("~/passwords.kdbx", KeePass(vault_password, [
            Entry("file server", archivist.user, archivist.pwd, url="telnet://sandbox-files"),
        ]))
        self.files.write_file("~/accounts.ods", Ods({"Backups": [
            ["archive", "password"],
            ["backup.zip", zip_password],
        ]}), owner=archivist.user)
        self.files.write_file("~/backup.zip", Zip({
            "ops/id_ops": ops_key.private,
            "ops/aegis-export-plain.json": TotpExport(ops_totp),
        }, password=zip_password), owner=archivist.user)

        self.register_network([self.entry, self.files])
        self.register_network([self.files, self.vault])
        self.register_fw_rule(self.entry, FirewallRule(Allow, 23, Outgoing, peers=[self.files]))
        self.register_fw_rule(self.files, FirewallRule(Allow, 23, Incoming, peers=[self.entry]))
        self.register_fw_rule(self.files, FirewallRule(Allow, 22, Outgoing, peers=[self.vault]))
        self.register_fw_rule(self.vault, FirewallRule(Allow, 22, Incoming, peers=[self.files]))

        self.set_worm_entrypoint(self.entry)

    def execute(self):
        self.deploy_worm()
        # What the worm does to the vault is its own business; the journal notes whether it did.
        before = self.entry.read_file("~/passwords.kdbx")
        self.wait(30 * 60)
        if self.entry.read_file("~/passwords.kdbx") != before:
            self.log("passwords.kdbx on sandbox-entry changed")
        # The rest of the hour passes; then every VM is stopped and the run is scored.
