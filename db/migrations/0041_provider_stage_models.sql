-- Per-book model routing. Provider credentials and endpoint remain shared; only the
-- model differs by stage. Existing `model` selections become both values so an upgrade
-- cannot silently change a book's behaviour.
BEGIN;

ALTER TABLE novel_provider_config
  ADD COLUMN translate_model TEXT,
  ADD COLUMN extract_model TEXT;

UPDATE novel_provider_config
SET translate_model = model,
    extract_model = model
WHERE model IS NOT NULL;

COMMIT;
