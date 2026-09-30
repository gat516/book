import copy
import sys

import pytest

import gateway_smoke as smoke


def args(monkeypatch, requests=6):
    monkeypatch.setattr(sys, 'argv', ['smoke', '--account', '00000000-0000-4000-8000-000000000001',
        '--novel', '00000000-0000-4000-8000-000000000002', '--gate', '170', '--requests', str(requests)])
    monkeypatch.setattr(smoke.time, 'sleep', lambda _: None)


@pytest.mark.parametrize('requests', [0, 7, 24])
def test_paid_request_bound(monkeypatch, requests):
    args(monkeypatch, requests)
    with pytest.raises(SystemExit): smoke.main()


def test_six_request_success_without_queue_mutation(monkeypatch, capsys):
    args(monkeypatch)
    paid = []
    state = {'progress':170,'chapters':[[176,'done',True],[177,'ingested',False]],
             'pending':0,'processing':0,'usage':[],'failures':[]}
    def remote(_args, service, source, since):
        if source == smoke.SNAPSHOT: return copy.deepcopy(state)
        if source in (smoke.CONTRACT, smoke.PIPELINE): return {'ok':True}
        assert source == smoke.ASK
        paid.append(1)
        return {'status':200,'gate':170,'served_by':{'provider':'deepseek'},'seconds':1}
    monkeypatch.setattr(smoke,'remote',remote)
    smoke.main()
    assert len(paid)==6
    assert '"chapter_processing_started": false' in capsys.readouterr().out


@pytest.mark.parametrize('scenario', ['clearance','busy','failed_answer','chapter_changed'])
def test_guards_stop_further_paid_calls(monkeypatch, scenario):
    args(monkeypatch)
    paid=[]
    snapshots=[]
    def remote(_args, service, source, since):
        if source == smoke.SNAPSHOT:
            snapshots.append(1)
            return {'progress':169 if scenario=='clearance' else 170,
                    'pending':1 if scenario=='busy' else 0,'processing':0,
                    'chapters':[len(snapshots)] if scenario=='chapter_changed' else [],
                    'usage':[],'failures':[]}
        if source in (smoke.CONTRACT, smoke.PIPELINE): return {}
        paid.append(1)
        return {'status':503}
    monkeypatch.setattr(smoke,'remote',remote)
    with pytest.raises((AssertionError,RuntimeError)): smoke.main()
    assert len(paid)==(1 if scenario=='failed_answer' else 0)
