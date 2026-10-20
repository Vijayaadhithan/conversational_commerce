"""Bounded availability checks; no inference, catalog writes, or credential output."""
import asyncio
from datetime import datetime, timezone
import time

import psycopg


async def probe_dependencies(state, model):
    stamp = datetime.now(timezone.utc).isoformat()
    async def check(function):
        started = time.monotonic()
        try:
            detail = await function()
            return {'status': 'ready', 'checked_at': stamp, 'seconds': time.monotonic() - started, **(detail or {})}
        except Exception as exc:
            # Exception messages may contain connection URLs and credentials.
            return {'status': 'unavailable', 'checked_at': stamp, 'error_type': type(exc).__name__, 'seconds': time.monotonic() - started}

    async def sqlite():
        with state.store.connect() as db: db.execute('SELECT 1').fetchone()

    async def models():
        response = await state.ollama.get('/api/tags', timeout=3)
        response.raise_for_status()
        installed = {item.get('name') for item in response.json().get('models', [])}
        required = {model, state.product_search.settings.model}
        if not required.issubset(installed): raise RuntimeError('Required models missing')

    def postgres_sync():
        from app.product_search import pin_search_build
        with psycopg.connect(state.product_search.database_url, connect_timeout=3,
                             options='-c statement_timeout=3000') as db:
            build = pin_search_build(db)
            if build['config']['model'] != state.product_search.settings.model: raise RuntimeError('Embedding model mismatch')
            return {'build_id': build['build_id']}

    async def postgres(): return await asyncio.to_thread(postgres_sync)

    async def redis():
        client = state.product_search.cache.client
        if client is None: return {'status': 'disabled'}
        await asyncio.to_thread(client.ping)

    async def reranker():
        import importlib.util
        import os
        from pathlib import Path
        from app.reranker import ROOT, DEFAULT_MODEL_PATH
        path = Path(os.getenv('RERANK_MODEL_PATH', str(DEFAULT_MODEL_PATH)))
        if not path.is_absolute(): path = ROOT / path
        required = ('model.safetensors', 'config.json', 'modules.json', '1_LogitScore/config.json')
        if not all((path / name).is_file() for name in required): raise FileNotFoundError('Reranker files missing')
        if any(importlib.util.find_spec(name) is None for name in ('torch', 'sentence_transformers')):
            raise ImportError('Reranker runtime missing')
        return {'loaded': state.reranker.model is not None, 'device': state.reranker.device,
                'check': 'files_and_runtime_presence'}

    names = ('sqlite', 'models', 'postgres', 'redis', 'reranker')
    results = await asyncio.gather(*(check(f) for f in (sqlite, models, postgres, redis, reranker)))
    state.metrics.dependencies = dict(zip(names, results))
    return state.metrics.dependencies
