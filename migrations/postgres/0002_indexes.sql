-- Indexes for the hot query paths (Postgres twin of migrations/sqlite/0002_indexes.sql).
-- Query indexes for the hot paths: recent activity, mission lookup, recall, approval queue.
CREATE INDEX IF NOT EXISTS idx_events_kind_seq ON events(kind, seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_mission ON events(mission_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_risk ON events(risk, seq DESC);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_missions_state ON missions(state, priority, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approval_requests(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_state ON mcp_servers(state, enabled);

-- down
DROP INDEX IF EXISTS idx_events_kind_seq;
DROP INDEX IF EXISTS idx_events_mission;
DROP INDEX IF EXISTS idx_events_trace;
DROP INDEX IF EXISTS idx_events_risk;
DROP INDEX IF EXISTS idx_memories_kind;
DROP INDEX IF EXISTS idx_missions_state;
DROP INDEX IF EXISTS idx_approvals_status;
DROP INDEX IF EXISTS idx_mcp_servers_state;
