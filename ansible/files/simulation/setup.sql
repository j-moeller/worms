-- Test fixtures for ansible/Simulate.yml (and tests/test_sandbox_api.py).
--
-- A competition, an inactive user, a team and an access token whose hash is
-- %(token_hash)s, all named "simulation-<run id>". For the scoring check it
-- also sets up one round, one finished RoundRun for the team, and one sandbox
-- VirtualMachine whose released secret hash is %(flag_hash)s -- so that when
-- the team submits that flag, the conductor (or manage.py process_captures)
-- turns the attempt into a real FlagCapture, exactly as in a live round.
--
-- The run is `finished`, so the conductor never tries to advance it (it would
-- drive power control), but its window (worm_start_at .. ends_at) still spans
-- now, so a freshly submitted attempt is "live" and is scored. Everything is
-- removed again by cleanup.sql. Run as postgres. Returns the team id.
WITH competition AS (
    INSERT INTO competitions (name, created_at, is_dev_competition)
    VALUES ('simulation-' || %(run_id)s, now(), true)
    RETURNING id
), account AS (
    INSERT INTO users (password, is_superuser, username, first_name, last_name, email,
                       is_staff, is_active, date_joined, uuid, created_at)
    VALUES ('!', false, 'simulation-' || %(run_id)s, '', '', '', false, false, now(),
            gen_random_uuid(), now())
    RETURNING id
), team AS (
    INSERT INTO teams (uuid, name, competition_id, max_members, created_at)
    SELECT %(team_uuid)s::uuid, 'simulation-' || %(run_id)s, competition.id, 1, now()
    FROM competition
    RETURNING id, competition_id
), token AS (
    INSERT INTO access_tokens (name, description, access_token_hash, team_id, created_by_id,
                               revoked, created_at)
    SELECT 'simulation-' || %(run_id)s, 'worms simulation, deleted afterwards',
           %(token_hash)s, team.id, account.id, false, now()
    FROM team, account
    RETURNING team_id
), round AS (
    INSERT INTO rounds (seed, name, competition_id, round_start, round_end, created_at)
    SELECT 0, 'simulation-' || %(run_id)s, team.competition_id,
           now() - interval '2 minutes', now() + interval '30 minutes', now()
    FROM team
    RETURNING id
), run AS (
    INSERT INTO round_runs (uuid, round_id, team_id, position, seed, state, error,
                            sandbox_params, topology, sandbox_definition, created_at,
                            state_changed_at, worm_start_at, ends_at, finished_at)
    SELECT gen_random_uuid(), round.id, team.id, 0, 0, 'finished', '',
           '{}'::jsonb, '{}'::jsonb, '', now(), now(),
           now() - interval '1 minute', now() + interval '30 minutes', now()
    FROM round, team
    RETURNING id, round_id
), vm AS (
    INSERT INTO virtual_machines (vm_identifier, vm_maintenance_ip, ctf_flag_hash,
                                  round_id, run_id, proxmox_vm_id, addresses, networks,
                                  secret_released_at, uuid, created_at)
    SELECT 'simulation-' || %(run_id)s || '-vm', '10.99.' || (abs(hashtext(%(run_id)s)) %% 250) || '.5',
           %(flag_hash)s, run.round_id, run.id, NULL, '[]'::jsonb, '[]'::jsonb,
           now(), gen_random_uuid(), now()
    FROM run
    RETURNING id
)
SELECT team_id FROM token;
