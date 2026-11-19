"""Round one: three VMs in a chain, vm0 - vm1 - vm2. The worm starts on vm0, which holds the
key to vm1; vm1 holds a note with a password for vm2. The flags of vm1 and vm2 appear only
after vm0 is gone, so only a worm that spread can take them (docs/05-live-environment.md)."""

from gameplan import Allow, BasicVM, DjangoVM, FirewallRule, FreezeState, Gameplan, Incoming, Outgoing, PostgreVM


class RoundOne(Gameplan):
    def __init__(self):
        # self.round_duration = 60 * 60  # the default: one hour from the worm's start

        self.vm0 = BasicVM()
        self.vm1 = DjangoVM()
        self.vm2 = PostgreVM()

        self.vm0.install("python3-venv")
        self.vm1.uninstall("gcc")

        self.ssh_key0 = self.generate_ssh_key("id_rsa")
        self.ssh_pwd_login = self.generate_ssh_pwd_login()

        self.register_fw_rule([self.vm0, self.vm1, self.vm2], FirewallRule(Allow, 22, Incoming))
        self.register_fw_rule([self.vm0, self.vm1, self.vm2], FirewallRule(Allow, 22, Outgoing))

        self.add_private_ssh_key([self.vm0], self.ssh_key0)
        self.add_public_ssh_key([self.vm0, self.vm1], self.ssh_key0)

        self.add_ssh_pwd_login(self.vm2)

        self.write_file(self.vm1, "~/note", f"user: {self.ssh_pwd_login.user}, pwd: {self.ssh_pwd_login.pwd}\n")

        self.register_network([self.vm0, self.vm1])
        self.register_network([self.vm1, self.vm2])

        self.set_worm_entrypoint(self.vm0)

    def execute(self):
        # The flags of vm1 and vm2 go before the worm arrives (checked: an error ends the attempt).
        flag_vm1 = self.remove_flag(self.vm1)
        flag_vm2 = self.remove_flag(self.vm2)

        # The worm starts on vm0; the run's clock starts now.
        self.deploy_worm()

        # Ten minutes to spread.
        self.wait(10 * 60)

        # Freeze everything, let vm0 run again only to shut it down, then resume the rest.
        with FreezeState(self.vms) as freeze_state:
            freeze_state.unfreeze(self.vm0)
            self.vm0.shutdown()

        # Now the flags come back: only a worm that left vm0 can take them. If the worm broke a VM
        # so that the flag cannot be written, that VM's flag is lost to the team.
        self.add_flag(flag_vm1)
        self.add_flag(flag_vm2)

        # The rest of the hour passes; then every VM is stopped and the run is scored.
