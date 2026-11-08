# No gameplan yet. The round agent writes this file from a round's gameplan when it builds the
# round's sandbox (src/gameplan/build.py); the node's copy is runtime state.
locals {
  gameplan        = ""
  gameplan_sha256 = ""

  sandbox_bridges = {}
  sandbox_vms     = {}
}
