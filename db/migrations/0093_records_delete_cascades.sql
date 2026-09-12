-- 0093_records_delete_cascades.sql -- make `DELETE FROM novel` work again.
--
-- Deletion is the one place this schema removes data, and 0030 settled how: every
-- novel-scoped FK cascades, so `DELETE FROM novel` is the whole operation and Go never
-- hand-maintains a copy of the FK graph. Four foreign keys added with the records tables
-- were declared with the default NO ACTION (and one RESTRICT), which meant the cascade
-- reached `entity` and then stopped: deleting a novel that had any extracted records
-- failed with a foreign key violation.
--
-- Every one of these children lives inside the same generation as the entity it names --
-- a participant, a candidate hint, a display binding -- so cascading is what they already
-- meant. Nothing here weakens the participant CHECK: a row still names exactly one of an
-- entity or an unresolved reference.
BEGIN;

ALTER TABLE record_participant DROP CONSTRAINT record_participant_entity_id_fkey;
ALTER TABLE record_participant ADD CONSTRAINT record_participant_entity_id_fkey
  FOREIGN KEY (entity_id) REFERENCES entity(id) ON DELETE CASCADE;

ALTER TABLE record_participant DROP CONSTRAINT record_participant_reference_id_fkey;
ALTER TABLE record_participant ADD CONSTRAINT record_participant_reference_id_fkey
  FOREIGN KEY (reference_id) REFERENCES record_reference(id) ON DELETE CASCADE;

ALTER TABLE record_reference
  DROP CONSTRAINT record_reference_candidate_entity_id_novel_id_generation_i_fkey;
ALTER TABLE record_reference
  ADD CONSTRAINT record_reference_candidate_entity_id_novel_id_generation_i_fkey
  FOREIGN KEY (candidate_entity_id, novel_id, generation_id)
  REFERENCES entity(id, novel_id, record_generation_id) ON DELETE CASCADE;

ALTER TABLE record_mention_binding
  DROP CONSTRAINT record_mention_binding_entity_id_novel_id_generation_id_fkey;
ALTER TABLE record_mention_binding
  ADD CONSTRAINT record_mention_binding_entity_id_novel_id_generation_id_fkey
  FOREIGN KEY (entity_id, novel_id, generation_id)
  REFERENCES entity(id, novel_id, record_generation_id) ON DELETE CASCADE;

COMMIT;
