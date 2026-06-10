-- Lucyd schema v002 — drop the dead multi-tenant client_id/agent_id columns.
--
-- Deployment is one database per agent (lucyd_{client}_{agent}), so client_id
-- and agent_id are constant on every row (Law #1, single-tenant) — dead weight.
-- They were dropped in 2026-04 (schema_version=2). A later audit folded the
-- drop into 001 (the columns are no longer created) and removed this file, but
-- databases provisioned before that still carry a schema_version=2 row.
--
-- The file is restored in idempotent form so the migration history stays
-- contiguous and that version row has a matching file (and no future migration
-- silently reuses version 2, which older databases would skip). On a fresh
-- database 001 never creates the columns, so every statement is a no-op. The
-- original also rebuilt the PKs/indexes onto the narrow keys; 001 now creates
-- them narrow already, so only the column drops remain here.

ALTER TABLE sessions.sessions                    DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE sessions.messages                    DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE sessions.events                      DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE knowledge.facts                      DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE knowledge.episodes                   DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE knowledge.commitments                DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE knowledge.entity_aliases             DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE knowledge.consolidation_state        DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE knowledge.consolidation_file_hashes  DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE knowledge.evolution_state            DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE metering.costs                       DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE search.files                         DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE search.chunks                        DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
ALTER TABLE search.embedding_cache               DROP COLUMN IF EXISTS client_id, DROP COLUMN IF EXISTS agent_id;
