-- 0064_repair_retry_action.sql -- admit 'retry' as a repair_request action.
--
-- pipeline.repair._run's retry branch clears a staging revision's blocked_category/
-- blocked_at unconditionally on request (resume()'s preamble re-checks reachability
-- before spending a call, so this costs nothing when the underlying cause has not
-- actually cleared -- it just re-blocks with a fresh timestamp). Until now the only way
-- to escape a non-transient block was starting a whole new rebuild; a transient one
-- (model_unreachable, timeout) only ever self-cleared after a five-minute cooldown, with
-- no way to nudge it sooner even after the operator had already fixed the cause.
BEGIN;

ALTER TABLE repair_request DROP CONSTRAINT repair_request_action_check;
ALTER TABLE repair_request ADD CONSTRAINT repair_request_action_check
  CHECK (action IN ('prepare','review','activate','rollback','reextract',
                    'reextract_apply','discard','extend','retry'));

COMMIT;
