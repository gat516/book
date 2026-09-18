"""Resuming rendering reuses only the exact, revalidated who's-who response."""
from types import SimpleNamespace
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from fixtures import make_config
from pipeline.fact_first import _source_passages
from pipeline.stages import records
from pipeline.llm.provider import AdmissionRejected


@pytest.mark.asyncio
async def test_identity_checkpoint_revalidates_response_and_preserves_served_model(monkeypatch):
    calls = []

    async def complete(*args, **kwargs):
        calls.append(kwargs)
        return ('<resolution><entity id="e1" kind="character" canonical="林" '
                'names="n1"/></resolution>', 'groq', 'actual-model')

    monkeypatch.setattr(records, '_complete', complete)
    ctx = SimpleNamespace(cfg=make_config(), provider_id='groq', model_override=None,
                          novel=SimpleNamespace(ontology={'kinds': ['character']}))
    source = '林来到这里。'
    state = SimpleNamespace(envelope=SimpleNamespace(
        chapter_index=1, raw_text=source, source_meta=SimpleNamespace(raw_hash='hash')))
    names = [{'id': 'n1', 'name': '林', 'mentions': ['林'],
              'passages': [_source_passages(source)[0]['id']]}]
    stage = records.RecordsStage()
    checkpoint = await stage._resolve(ctx, state, names, [])
    checkpoint['name_map'] = {'林': 'fabricated-identity'}
    resumed = await stage._resolve(ctx, state, names, [], checkpoint=checkpoint)
    assert len(calls) == 1
    assert resumed['name_map'] == {'林': 'e1'}
    assert resumed['served_model'] == 'actual-model'

    # Candidate changes must produce another authoritative decision, even if the
    # new candidate has exactly the same spelling (§0, entity drift).
    await stage._resolve(ctx, state, names,
                         [{'id': 'earlier-id', 'canonical': '林', 'kind': 'character'}],
                         checkpoint=checkpoint)
    assert len(calls) == 2

    # Historical checkpoints without served provenance must not invent it.
    checkpoint.pop('served_model')
    await stage._resolve(ctx, state, names, [], checkpoint=checkpoint)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_rendering_deferral_checkpoints_identity_before_retry(monkeypatch):
    source = '林来到这里。'
    pid = _source_passages(source)[0]['id']
    saved = {'discovery': {'response': '<claims/>'}}

    async def save_normalization(ctx, run, normalized):
        saved['normalization'] = deepcopy(normalized)

    monkeypatch.setattr(records, 'prepare_generation', AsyncMock())
    monkeypatch.setattr(records, 'mark_record_processing', AsyncMock(return_value=True))
    monkeypatch.setattr(records, 'ensure_run', AsyncMock(return_value='run'))
    monkeypatch.setattr(records, 'load_checkpoints', AsyncMock(side_effect=lambda *a: deepcopy(saved)))
    monkeypatch.setattr(records, 'save_selection', AsyncMock())
    monkeypatch.setattr(records, 'save_normalization', save_normalization)
    monkeypatch.setattr(records, 'validate_extraction', lambda *a, **kw: {
        'accepted': {'entities': [{'local_id': 'n1', 'canonical_source': '林',
                                 'source_aliases': ['林'], 'evidence_ids': [pid]}]},
        'rejected': [],
    })
    complete = AsyncMock(return_value=(
        '<resolution><entity id="e1" kind="character" canonical="林" names="n1"/></resolution>',
        'groq', 'actual-model'))
    monkeypatch.setattr(records, '_complete', complete)
    ctx = SimpleNamespace(cfg=make_config(), provider_id='groq', model_override=None,
                          db=SimpleNamespace(execute=AsyncMock()),
                          novel=SimpleNamespace(ontology={'kinds': ['character']}))
    state = SimpleNamespace(record_generation_id='generation', envelope=SimpleNamespace(
        chapter_index=1, raw_text=source, source_meta=SimpleNamespace(raw_hash='hash')))
    stage = records.RecordsStage()
    monkeypatch.setattr(stage, '_candidates', AsyncMock(return_value=[]))
    monkeypatch.setattr(stage, '_render_fact_first', AsyncMock(side_effect=[
        AdmissionRejected('capacity', retry_after_s=60), {}]))

    with pytest.raises(AdmissionRejected):
        await stage.run(ctx, state)
    assert saved['normalization']['resolution']['served_model'] == 'actual-model'
    await stage.run(ctx, state)
    assert complete.await_count == 1
    assert state.resolutions == {'林': 'e1'}
