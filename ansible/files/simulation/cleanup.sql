-- Removes what setup.sql created and the attempts submitted with its token
-- (the foreign keys cascade in Django only, so in order). Run as postgres.
-- Returns the number of attempts removed.
WITH flags AS (
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
       (SELECT count(*) FROM competitions_deleted) AS competitions
