-- 0098_record_review_definer_owner.sql -- least-privilege owner for review projections.
--
-- 0097's narrowly gated SECURITY DEFINER functions were initially owned by the
-- repair_reporter role. That role is also used by unrelated operator functions and
-- does not have the SELECT privileges needed by record-review projection queries in
-- every deployment. Keep review bypass separate and explicit.
BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='records_review_reader') THEN
    CREATE ROLE records_review_reader NOLOGIN BYPASSRLS;
  END IF;
  ALTER ROLE records_review_reader NOLOGIN BYPASSRLS;
END
$$;

GRANT USAGE ON SCHEMA public TO records_review_reader;
GRANT SELECT ON novel, record_run, record_row, record_value, record_rendering,
  record_participant, record_evidence, record_passage, record_review_decision
  TO records_review_reader;
GRANT EXECUTE ON FUNCTION reader_novel(), reader_chapter() TO records_review_reader;
GRANT EXECUTE ON FUNCTION record_row_not_rejected(UUID, UUID) TO records_review_reader;

ALTER FUNCTION record_entity_not_rejected(UUID, UUID) OWNER TO records_review_reader;
ALTER FUNCTION reader_record_review_rows(UUID, UUID, INT) OWNER TO records_review_reader;
REVOKE EXECUTE ON FUNCTION record_entity_not_rejected(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION reader_record_review_rows(UUID, UUID, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_entity_not_rejected(UUID, UUID) TO rls_reader;
GRANT EXECUTE ON FUNCTION reader_record_review_rows(UUID, UUID, INT) TO rls_reader;

COMMIT;
