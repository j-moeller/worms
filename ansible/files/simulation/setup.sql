-- Test team for ansible/Simulate.yml (and tests/test_sandbox_api.py): a
-- competition, an inactive user, a team and an access token whose hash is
-- %(token_hash)s, all named "simulation-<run id>". Run as postgres; removed
-- again by cleanup.sql. Returns the team id.
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
    SELECT gen_random_uuid(), 'simulation-' || %(run_id)s, competition.id, 1, now()
    FROM competition
    RETURNING id
)
INSERT INTO access_tokens (name, description, access_token_hash, team_id, created_by_id,
                           revoked, created_at)
SELECT 'simulation-' || %(run_id)s, 'worms simulation, deleted afterwards',
       %(token_hash)s, team.id, account.id, false, now()
FROM team, account
RETURNING team_id
