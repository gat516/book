"""Pure safeguards for account-to-account copying; no live infrastructure."""
import hashlib
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from transfer_library import TABLES, prepare_rows, validate_bundle
from novel_llm.accounts import credential_aad

SOURCE = '00000000-0000-4000-8000-000000000001'
TARGET = '00000000-0000-4000-8000-000000000002'
NOVEL = '00000000-0000-4000-8000-000000000003'


@pytest.mark.parametrize('version', [0, 1])
def test_credentials_rebound_to_target_identity_and_key(version):
    old, new, nonce = b'a' * 32, b'b' * 32, b'c' * 12
    aad = credential_aad(SOURCE, 'custom', 'https://example.test/v1') if version else None
    row = dict(account_id=SOURCE, provider='custom', base_url='https://example.test/v1',
               api_key_nonce='\\x' + nonce.hex(), key_version=version,
               api_key_cipher='\\x' + AESGCM(old).encrypt(nonce, b'fake-key', aad).hex())
    result = prepare_rows('provider_credential', [row], TARGET, old, new)[0]
    encrypted, nonce = bytes.fromhex(result['api_key_cipher'][2:]), bytes.fromhex(result['api_key_nonce'][2:])
    assert AESGCM(new).decrypt(nonce, encrypted, credential_aad(TARGET, 'custom', row['base_url'])) == b'fake-key'
    with pytest.raises(InvalidTag):
        AESGCM(new).decrypt(nonce, encrypted, credential_aad(SOURCE, 'custom', row['base_url']))
    with pytest.raises(InvalidTag):
        AESGCM(old).decrypt(nonce, encrypted, credential_aad(TARGET, 'custom', row['base_url']))
    assert row['account_id'] == SOURCE
    assert result['key_version'] == 1


def test_progress_retains_highest_existing_clearance_without_advancing():
    rows = [dict(reader_id='browser-a', novel_id=NOVEL, current_chapter=8),
            dict(reader_id='browser-b', novel_id=NOVEL, current_chapter=28),
            dict(reader_id=SOURCE, novel_id=NOVEL, current_chapter=16)]
    assert prepare_rows('reader_progress', rows, TARGET) == [dict(reader_id=TARGET, novel_id=NOVEL, current_chapter=28)]


def test_content_and_knowledge_times_are_preserved():
    row = dict(novel_id=NOVEL, chapter_index=28, source_chapter=28, valid_from_chapter=3, text='private fact')
    assert prepare_rows('chapter_fact', [row], TARGET) == [row]


def test_copied_scrape_cannot_restart_automatically():
    assert prepare_rows('scrape_job', [dict(status='running')], TARGET)[0]['status'] == 'cancelled'
    assert prepare_rows('scrape_job', [dict(status='done')], TARGET) == [dict(status='done')]


def bundle(tmp_path):
    key = f'novels/{NOVEL}/raw.txt'
    filename = hashlib.sha256(key.encode()).hexdigest()
    (tmp_path / 'objects').mkdir()
    (tmp_path / 'objects' / filename).write_bytes(b'saved prose')
    data = dict(format=1, owner=SOURCE, novels=[NOVEL], tables={t: [] for t in TABLES},
                objects=[dict(key=key, file=filename, sha256=hashlib.sha256(b'saved prose').hexdigest())])
    data['tables']['novel'] = [dict(id=NOVEL, owner_id=SOURCE)]
    data['tables']['chapter'] = [dict(novel_id=NOVEL, raw_uri=key)]
    return data


def test_rejects_modified_prose(tmp_path):
    data = bundle(tmp_path)
    validate_bundle(tmp_path, data)
    (tmp_path / 'objects' / data['objects'][0]['file']).write_bytes(b'changed prose')
    with pytest.raises(ValueError, match='checksum'):
        validate_bundle(tmp_path, data)


def test_rejects_missing_referenced_prose(tmp_path):
    data = bundle(tmp_path)
    data['tables']['chapter'][0]['translated_uri'] = f'novels/{NOVEL}/missing.txt'
    with pytest.raises(ValueError, match='missing referenced prose'):
        validate_bundle(tmp_path, data)


@pytest.mark.parametrize('table,row', [
    ('novel', dict(id=NOVEL, owner_id=TARGET)),
    ('chapter_fact', dict(novel_id=TARGET)),
    ('provider_credential', dict(account_id=TARGET)),
])
def test_rejects_foreign_account_data(tmp_path, table, row):
    data = bundle(tmp_path)
    data['tables'][table] = [row]
    with pytest.raises(ValueError, match='foreign'):
        validate_bundle(tmp_path, data)


def test_rejects_unscoped_objects(tmp_path):
    data = bundle(tmp_path)
    data['objects'][0]['key'] = f'novels/{TARGET}/raw.txt'
    with pytest.raises(ValueError, match='outside'):
        validate_bundle(tmp_path, data)


def test_migration_versions_must_match_and_recorded_checksums_cannot_drift():
    from transfer_library import compatible_ledgers
    assert compatible_ledgers([['0125', None]], [('0125', 'hash')])
    assert compatible_ledgers([['0125', 'hash']], [('0125', 'hash')])
    assert not compatible_ledgers([['0124', None]], [('0125', 'hash')])
    assert not compatible_ledgers([['0125', 'changed']], [('0125', 'hash')])


def test_explicit_source_only_ledger_exception_cannot_hide_a_target_version():
    from transfer_library import compatible_ledgers
    source = [['0079_local', None], ['0125', 'hash']]
    assert not compatible_ledgers(source, [('0125', 'hash')])
    assert compatible_ledgers(source, [('0125', 'hash')], ['0079_local'])
    assert not compatible_ledgers(source, [('0125', 'different')], ['0079_local'])


def test_translation_objects_are_in_the_same_novel_boundary():
    from ops_common import novel_prefixes, object_novel
    assert novel_prefixes(NOVEL) == (f'novels/{NOVEL}/', f'translated/{NOVEL}/')
    assert object_novel(f'translated/{NOVEL}/1/version.txt') == NOVEL
    with pytest.raises(ValueError):
        object_novel(f'translated/{NOVEL}/../other.txt')
