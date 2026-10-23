-- Remove everything setup.sql created plus whatever the run produced (the
-- attempts submitted with the token, and the captures/logs/events scored from
-- them). Scoped to the test team, which is found from the token hash. Run as
-- postgres. Returns the number of attempts and competitions removed.
--
-- All deletes are data-modifying CTEs in one statement: they see the same
-- snapshot (so the SELECT CTEs still find the rows) and foreign keys are
-- checked once at the end, by which point every referencing row is also gone.
WITH team AS (
    SELECT team_id AS id FROM access_tokens WHERE access_token_hash = %(token_hash)s
), comp AS (
    SELECT competition_id AS id FROM teams WHERE id IN (SELECT id FROM team)
), runs AS (
    SELECT id FROM round_runs WHERE team_id IN (SELECT id FROM team)
), vms AS (
    SELECT id FROM virtual_machines WHERE run_id IN (SELECT id FROM runs)
), del_run_events AS (
    DELETE FROM run_events WHERE run_id IN (SELECT id FROM runs)
), del_steps AS (
    DELETE FROM game_plan_steps WHERE run_id IN (SELECT id FROM runs)
), del_host_jobs AS (
    DELETE FROM host_jobs WHERE run_id IN (SELECT id FROM runs)
), del_captures AS (
    DELETE FROM flag_captures WHERE team_id IN (SELECT id FROM team)
), del_logging AS (
    DELETE FROM logging WHERE team_id IN (SELECT id FROM team)
), del_vms AS (
    DELETE FROM virtual_machines WHERE id IN (SELECT id FROM vms)
), del_runs AS (
    DELETE FROM round_runs WHERE id IN (SELECT id FROM runs)
), del_rounds AS (
    DELETE FROM rounds WHERE competition_id IN (SELECT id FROM comp)
), flags AS (
    DELETE FROM flag_capture_attempts WHERE team_token_hash = %(token_hash)s RETURNING 1
), logs AS (
    DELETE FROM logging_attempts WHERE team_token_hash = %(token_hash)s RETURNING 1
), tokens AS (
    DELETE FROM access_tokens WHERE access_token_hash = %(token_hash)s RETURNING team_id, created_by_id
), teams_deleted AS (
    DELETE FROM teams WHERE id IN (SELECT team_id FROM tokens) RETURNING competition_id
), users_deleted AS (
    DELETE FROM users WHERE id IN (SELECT created_by_id FROM tokens) RETURNING 1
), competitions_deleted AS (
    DELETE FROM competitions WHERE id IN (SELECT competition_id FROM teams_deleted) RETURNING 1
)
SELECT (SELECT count(*) FROM flags) + (SELECT count(*) FROM logs) AS attempts,
       (SELECT count(*) FROM competitions_deleted) AS competitions;
