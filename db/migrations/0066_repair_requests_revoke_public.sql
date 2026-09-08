-- 0066_repair_requests_revoke_public.sql -- re-close what 0065 reopened.
--
-- 0065 added retry_at to reader_repair_requests() by DROP FUNCTION + CREATE FUNCTION.
-- CREATE FUNCTION grants EXECUTE to PUBLIC by default, so the REVOKE that 0046 applied to
-- this exact function did not survive its recreation: the new function came back
-- PUBLIC-executable, and the explicit GRANT to rls_reader in 0065 decided nothing again --
-- the same trap 0046 was written to close.
--
-- It matters here for the reason 0046 gives: reader_repair_requests is SECURITY DEFINER,
-- owned by the BYPASSRLS repair_reporter role, and takes the novel as an explicit
-- argument rather than deriving scope from the app.novel_id GUC. So a PUBLIC grant lets
-- any role -- askai's included -- read repair state for any novel with RLS bypassed.
--
-- Verified before/after with:
--   SELECT proacl FROM pg_proc WHERE proname='reader_repair_requests';
-- A leading "=X/repair_reporter" entry (empty grantee) is the PUBLIC grant; its siblings
-- reader_repair_status and reader_repair_failures correctly have none.
--
-- Anything that recreates one of the repair functions must re-REVOKE it here as well.
BEGIN;

REVOKE EXECUTE ON FUNCTION reader_repair_requests(UUID) FROM PUBLIC;

COMMIT;
