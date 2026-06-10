-- Retire the commitment mechanic.  The remind_user at-job tool covers the
-- real use case; commitment tracking is removed entirely.

DROP TABLE IF EXISTS knowledge.commitments;
ALTER TABLE knowledge.episodes DROP COLUMN IF EXISTS commitments;
