-- §15: runtime writers use scoped policies rather than the database administrator.
BEGIN;
GRANT SELECT,INSERT,UPDATE,DELETE ON novel,chapter,chunk,job,glossary,glossary_candidate,
 glossary_candidate_chapter,mention_span,term_rendering_occurrence,character_name_review,
 character_name_occurrence,novel_provider_config,provider_credential,embedding_config,
 scrape_job TO ingest_writer;
GRANT SELECT,INSERT,DELETE ON glossary_changelog,chapter_failure,chapter_translation_version TO ingest_writer;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO ingest_writer;
COMMIT;
