import json
from copy import deepcopy
from test_evidence_memory import case, policy_for, args, Fake
from evidence_format import parse_lines, merge_repairs
from evidence_jobs import EvidenceJobs, write_json
from evidence_trial import extract, load_artifact

GOOD = 'R | r1 | commitment | 2,3 | e1 | -\nE | e1 | person | 1 | new | 凌峰;龍飛'


def test_lines_salvage_independently_and_normalize_fences():
    good, bad = parse_lines('```text\n' + GOOD + '\nR | r2 | nope | 2 | e1 | -\n```', case(), policy_for(), [])
    assert len(good['records']) == len(good['entities']) == 1
    assert len(bad) == 1 and bad[0]['error'] == 'priority is not configured'


def test_repair_cannot_overwrite_or_rename_accepted_entries():
    good, bad = parse_lines(GOOD + '\nR | r2 | nope | 2 | e1 | -', case(), policy_for(), [])
    original = deepcopy(good)
    merged, remaining = merge_repairs('FIX | 3 | R | r1 | commitment | 2 | e1 | -', good, bad, case(), policy_for(), [])
    assert merged == original and remaining == bad
    merged, remaining = merge_repairs('FIX | 3 | R | r2 | commitment | 2 | e1 | -', good, bad, case(), policy_for(), [])
    assert len(merged['records']) == 2 and not remaining
    assert good == original


async def test_one_repair_preserves_first_response_and_replays_without_calls(tmp_path):
    write_json(tmp_path / 'case.json', {'novel_id': 'book1', 'case': case()})
    fake = Fake([GOOD + '\nR | r2 | nope | 2 | e1 | -', 'FIX | 3 | R | r2 | commitment | 2 | e1 | -'])
    with EvidenceJobs(tmp_path, {}, max_attempts=2, interval=0) as jobs:
        result = await extract(args(tmp_path), jobs, lambda: fake, {})
    assert result['records'] == 2
    assert len(fake.requests) == 2 and all(not r['json_mode'] for r in fake.requests)
    assert fake.requests[1]['max_output_tokens'] == 900
    assert len(json.loads(fake.requests[1]['prompt'])['broken']) == 1
    original = (tmp_path / 'evidence.json').read_bytes()
    with EvidenceJobs(tmp_path, {}, max_attempts=0, replay_only=True) as jobs:
        await extract(args(tmp_path), jobs, lambda: fake, {})
    assert len(fake.requests) == 2
    assert (tmp_path / 'evidence.json').read_bytes() == original


async def test_failed_repair_retains_usable_records_without_loop(tmp_path):
    write_json(tmp_path / 'case.json', {'novel_id': 'book1', 'case': case()})
    fake = Fake([GOOD + '\nmalformed line', RuntimeError('provider failed')])
    with EvidenceJobs(tmp_path, {}, max_attempts=2, interval=0) as jobs:
        await extract(args(tmp_path), jobs, lambda: fake, {})
    artifact = load_artifact(tmp_path / 'evidence.json')
    assert len(artifact['result']['records']) == 1
    assert artifact['repair']['status'] == 'unavailable'
    assert len(fake.requests) == 2
