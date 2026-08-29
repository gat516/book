"""Endpoint reservations survive cancellation and exclude other tasks/processes."""
import asyncio
import sys

import pytest

from novel_llm.admission import lock_path, ollama_session
from novel_llm.provider import AdmissionRejected


@pytest.fixture(autouse=True)
def isolated_lock_dir(monkeypatch,tmp_path):
    monkeypatch.setenv('BOOK_OLLAMA_LOCK_DIR',str(tmp_path))


async def test_session_reentrance_does_not_leak_to_child_tasks():
    async def competing_request():
        with pytest.raises(AdmissionRejected):
            async with ollama_session('http://127.0.0.1:11434',timeout=0):
                pytest.fail('child inherited reservation')
    async with ollama_session('http://localhost:11434'):
        async with ollama_session('http://[::1]:11434',timeout=0):
            await asyncio.create_task(competing_request())
    async with ollama_session('http://127.0.0.1:11434',timeout=0):
        pass


async def test_cancelled_waiter_does_not_release_owner_and_owner_cancellation_releases():
    ready=asyncio.Event()
    async def owner():
        async with ollama_session('http://localhost:11434'):
            ready.set()
            await asyncio.Future()
    task=asyncio.create_task(owner())
    await ready.wait()
    async def waiter():
        async with ollama_session('http://localhost:11434'):
            pytest.fail('owner still holds lock')
    waiting=asyncio.create_task(waiter())
    await asyncio.sleep(.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    with pytest.raises(AdmissionRejected):
        async with ollama_session('http://localhost:11434',timeout=0):
            pass
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with ollama_session('http://localhost:11434',timeout=0):
        pass


async def test_process_exit_releases_reservation():
    # Real child process, no inference. Exit without a context-manager cleanup.
    code='''import asyncio, os
from novel_llm.admission import ollama_session
async def main():
    async with ollama_session("http://localhost:11434", timeout=0):
        print("locked", flush=True)
        input()
        os._exit(0)
asyncio.run(main())
'''
    child=await asyncio.create_subprocess_exec(sys.executable,'-c',code,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
    try:
        assert await asyncio.wait_for(child.stdout.readline(),5)==b'locked\n'
        with pytest.raises(AdmissionRejected):
            async with ollama_session('http://localhost:11434',timeout=0):
                pass
        child.stdin.write(b'\n');await child.stdin.drain()
        assert await asyncio.wait_for(child.wait(),5)==0
        async with ollama_session('http://localhost:11434',timeout=0):
            pass
    finally:
        if child.returncode is None:
            child.kill();await child.wait()
        child.stdin.close()


def test_ports_are_separate_endpoints():
    assert lock_path('http://localhost:11434') != lock_path('http://localhost:11435')
