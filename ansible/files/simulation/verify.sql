-- What the simulation expects in the database (ansible/Simulate.yml). Run
-- as postgres on the database VM; polled until complete or timed out.
-- Ports are random per run, so nothing from an earlier run matches.
SELECT
  (SELECT count(*) FROM flag_capture_attempts
    WHERE team_token_hash = %(token_hash)s AND vm_flag_hash = %(flag_hash)s) AS flags,
  (SELECT count(*) FROM logging_attempts
    WHERE team_token_hash = %(token_hash)s AND log_data->>'simulation' = %(run_id)s) AS logs,
  (SELECT count(*) FROM network_flows
    WHERE src_ip = %(client)s::inet AND dst_ip = %(peer)s::inet AND dst_port = %(http_port)s
      AND ts >= %(since)s::timestamptz) AS http_flows,
  (SELECT row_to_json(f) FROM (
     SELECT transport, service, conn_state, src_pkts, dst_pkts, src_bytes, dst_bytes,
            src_ip_bytes, dst_ip_bytes, uid
     FROM network_flows
     WHERE src_ip = %(client)s::inet AND dst_ip = %(peer)s::inet AND dst_port = %(http_port)s
       AND ts >= %(since)s::timestamptz
     ORDER BY ts DESC LIMIT 1) f) AS http_flow,
  (SELECT count(*) FROM network_flows
    WHERE src_ip = %(client)s::inet AND dst_ip = %(peer)s::inet AND src_port = %(udp_sport)s
      AND dst_port = %(udp_port)s AND ts >= %(since)s::timestamptz) AS udp_flows,
  (SELECT row_to_json(f) FROM (
     SELECT transport, src_pkts, src_bytes, src_ip_bytes
     FROM network_flows
     WHERE src_ip = %(client)s::inet AND dst_ip = %(peer)s::inet AND src_port = %(udp_sport)s
       AND dst_port = %(udp_port)s AND ts >= %(since)s::timestamptz
     ORDER BY ts DESC LIMIT 1) f) AS udp_flow,
  (SELECT conn_state FROM network_flows
    WHERE src_ip = %(client)s::inet AND dst_ip = %(peer)s::inet AND dst_port = %(closed_port)s
      AND ts >= %(since)s::timestamptz ORDER BY ts DESC LIMIT 1) AS closed_state,
  (SELECT service FROM network_flows
    WHERE src_ip = %(api_client)s::inet AND dst_ip = %(scoring)s::inet AND dst_port = 443
      AND ts >= %(since)s::timestamptz AND service IS NOT NULL ORDER BY ts DESC LIMIT 1) AS api_service,
  (SELECT count(*) FROM network_events
    WHERE log = 'dns' AND data->>'query' = 'scoring.worms.internal'
      AND data->>'id.orig_h' = %(api_client)s AND ts >= %(since)s::timestamptz) AS dns_events,
  (SELECT count(*) FROM network_events
    WHERE log = 'http' AND data->>'id.resp_h' = %(peer)s
      AND (data->>'id.resp_p')::int = %(http_port)s AND ts >= %(since)s::timestamptz) AS http_events,
  (SELECT extract(epoch FROM now() - max(ts))::int FROM network_events WHERE log = 'shipper') AS shipper_age,
  (SELECT extract(epoch FROM now() - max(ts))::int FROM network_events WHERE log = 'stats') AS stats_age
