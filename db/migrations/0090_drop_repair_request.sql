-- 0090_drop_repair_request.sql -- the last of the repair machinery.
-- 0089 retired the repair functions and the graph they operated on, but left this intent
-- ledger behind. Records maintenance replaced it: a failed chapter is retried in place and
-- a rebuild opens a new generation, so there is no queue of pending repair intents to keep.
-- Forward-only, as always: 0089 is already applied, so this is a new file rather than an
-- edit to it.
BEGIN;

DROP TABLE IF EXISTS repair_request;

COMMIT;
