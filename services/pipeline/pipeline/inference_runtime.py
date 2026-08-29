"""Read-only local inference preflight. Never loads a model or generates tokens."""
from pathlib import Path
import os

import httpx

from novel_llm.admission import lock_path
from pipeline.graph_rebuild import local_model


async def preflight(cfg, model):
    identity = await local_model(cfg, model)  # also enforces installed, loopback-only
    async with httpx.AsyncClient(base_url=cfg.ollama_host, timeout=10) as client:
        version = await client.get('/api/version')
        version.raise_for_status()
        loaded = await client.get('/api/ps')
        loaded.raise_for_status()
    models = [dict(name=m.get('name'),size=m.get('size'),size_vram=m.get('size_vram'),
                   context_length=m.get('context_length')) for m in loaded.json()['models']]
    memory = {}
    if Path('/proc/meminfo').exists():
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in {'MemTotal','MemAvailable','SwapTotal','SwapFree'}:
                memory[key + '_bytes'] = int(value.split()[0]) * 1024
    return dict(model=identity,ollama_version=version.json()['version'],loaded_models=models,
                logical_cpus=os.cpu_count(),memory=memory,admission_lock=str(lock_path(cfg.ollama_host)),
                inference_started=False,warnings=[
                    'Reservation covers updated Book processes sharing this lock directory, not external Ollama clients.',
                    'Restart existing worker and Ask AI processes after deploying admission changes.',
                    *(['Loaded models currently report CPU-only inference.'] if models and all(m['size_vram']==0 for m in models) else []),
                    *(['Multiple models are resident; an exclusive request does not evict idle models.'] if len(models)>1 else []),
                ])
