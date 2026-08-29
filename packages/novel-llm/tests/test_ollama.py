"""Exercise schema delivery through the real adapter and batch path, without a server."""

import json
import asyncio

import httpx
import pytest

from novel_llm.ollama import OllamaProvider
from novel_llm.admission import ollama_session
from novel_llm.provider import Class, AdmissionRejected

SCHEMA = {"type": "object", "properties": {"value": {"type": "string", "minLength": 1}},
          "required": ["value"]}


@pytest.fixture(autouse=True)
def isolated_admission(monkeypatch,tmp_path):
    monkeypatch.setenv('BOOK_OLLAMA_LOCK_DIR',str(tmp_path))


@pytest.mark.parametrize("streaming", [False, True])
async def test_batch_delivers_schema_in_buffered_and_streaming_modes(streaming):
    provider = OllamaProvider(host="http://test", model="local-model")
    await provider._client.aclose()
    previews = []

    async def sink(text):
        previews.append(text)

    if streaming:
        provider.stream_sink = sink

    def handle(request):
        payload = json.loads(request.content)
        assert payload["format"] == SCHEMA
        assert payload["options"] == {"temperature": 0}
        assert payload["stream"] is streaming
        assert payload["model"] == "requested-model"
        body = {"message": {"content": '{"value":"known"}'}, "done": True,
                "prompt_eval_count": 10, "eval_count": 5}
        return httpx.Response(200, content=json.dumps(body) + "\n")

    provider._client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle))
    try:
        batch = await provider.batch_submit([{
            "id": "extract", "prompt": "source", "system": "instructions",
            "model": "requested-model", "json_schema": SCHEMA,
        }])
        result = (await provider.batch_poll(batch))[0]
        assert result["error"] is None
        assert result["output"] == '{"value":"known"}'
        assert result["served_model"] == "requested-model"
        assert bool(previews) is streaming
    finally:
        await provider.aclose()


@pytest.mark.parametrize("json_mode", [False, True])
async def test_ordinary_calls_keep_their_existing_payload(json_mode):
    provider = OllamaProvider(host="http://test", model="model")
    await provider._client.aclose()

    def handle(request):
        payload = json.loads(request.content)
        assert payload.get("format") == ("json" if json_mode else None)
        assert "options" not in payload
        return httpx.Response(200, json={"message": {"content": "{}"}})

    provider._client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle))
    try:
        assert (await provider.complete("source", json_mode=json_mode)).text == "{}"
    finally:
        await provider.aclose()


async def test_reports_actual_model_and_explicit_context_budget():
    provider=OllamaProvider(host="http://test",model="requested",num_ctx=16384,num_predict=4096)
    await provider._client.aclose()
    def handle(request):
        payload=json.loads(request.content)
        assert payload["options"]["num_ctx"]==16384
        assert payload["options"]["num_predict"]==4096
        return httpx.Response(200,json={"model":"actually-served","message":{"content":"{}"}})
    provider._client=httpx.AsyncClient(base_url="http://test",transport=httpx.MockTransport(handle))
    try:
        result=await provider.complete("source",json_schema=SCHEMA)
        assert result.served_model=="actually-served"
    finally:
        await provider.aclose()


class SlowStream(httpx.AsyncByteStream):
    def __init__(self,records,delay=.005):
        self.records,self.delay=records,delay

    async def __aiter__(self):
        for record in self.records:
            await asyncio.sleep(self.delay)
            yield (json.dumps(record)+'\n').encode()


async def test_internal_streaming_without_preview_collects_metrics():
    provider=OllamaProvider(host='http://test',model='model',stream=True,timeout=900,total_timeout=1800)
    assert provider._client.timeout.read==900
    assert provider._client.timeout.connect==10
    await provider._client.aclose()
    def handle(request):
        assert json.loads(request.content)['stream'] is True
        return httpx.Response(200,stream=SlowStream([
            dict(message=dict(content='{"value":')),
            dict(message=dict(content='"known"}')),
            dict(done=True,done_reason='stop',model='actual',eval_count=5,prompt_eval_count=10,
                 total_duration=9_000_000_000,load_duration=1_000_000_000,
                 prompt_eval_duration=2_000_000_000,eval_duration=6_000_000_000)]))
    provider._client=httpx.AsyncClient(base_url='http://test',transport=httpx.MockTransport(handle))
    try:
        result=await provider.complete('source',json_schema=SCHEMA)
        assert result.text=='{"value":"known"}'
        assert result.served_model=='actual' and result.input_tokens==10 and result.output_tokens==5
        assert result.timings['load_seconds']==1 and result.timings['eval_seconds']==6
        assert result.timings['first_token_seconds']>0
    finally:
        await provider.aclose()


@pytest.mark.parametrize('records,error',[
    ([], 'without done=true'),
    ([dict(message=dict(content='partial'))], 'without done=true'),
    ([dict(error='runner failed')], 'runner failed'),
    ([dict(done=True,done_reason='length',message=dict(content='{}'))], 'num_predict'),
])
async def test_broken_or_truncated_streams_never_become_completions(records,error):
    provider=OllamaProvider(host='http://test',model='model',stream=True)
    await provider._client.aclose()
    provider._client=httpx.AsyncClient(base_url='http://test',transport=httpx.MockTransport(
        lambda request:httpx.Response(200,stream=SlowStream(records))))
    try:
        with pytest.raises(RuntimeError,match=error):
            await provider.complete('source',json_schema=SCHEMA)
    finally:
        await provider.aclose()


async def test_total_deadline_bounds_a_healthy_but_endless_stream():
    provider=OllamaProvider(host='http://test',model='model',stream=True,total_timeout=.03)
    await provider._client.aclose()
    provider._client=httpx.AsyncClient(base_url='http://test',transport=httpx.MockTransport(
        lambda request:httpx.Response(200,stream=SlowStream([dict(message=dict(content='token'))]*100))))
    try:
        with pytest.raises(TimeoutError,match='total inference deadline'):
            await provider.complete('source')
        # HTTP cancellation also releases admission for another task.
        async def available():
            async with ollama_session('http://test',timeout=0):
                pass
        await asyncio.create_task(available())
    finally:
        await provider.aclose()


async def test_benchmark_session_excludes_completion_and_embedding_http_calls():
    provider=OllamaProvider(host='http://test',model='model')
    await provider._client.aclose()
    def unexpected(request):
        pytest.fail('reserved endpoint must not receive HTTP calls')
    provider._client=httpx.AsyncClient(base_url='http://test',transport=httpx.MockTransport(unexpected))
    async def competing():
        with pytest.raises(AdmissionRejected):
            await provider.complete('source',cls=Class.INTERACTIVE)
        with pytest.raises(AdmissionRejected):
            await provider.embed(['source'],cls=Class.INTERACTIVE)
    try:
        async with ollama_session('http://test'):
            await asyncio.create_task(competing())
    finally:
        await provider.aclose()


@pytest.mark.parametrize('stall',[False,True])
async def test_real_http_idle_timeout_tracks_stream_activity(stall):
    """Local fake HTTP server (not Ollama): output may outlast the idle timeout."""
    handlers=[]
    async def serve(reader,writer):
        handlers.append(asyncio.current_task())
        try:
            headers=await reader.readuntil(b'\r\n\r\n')
            length=next(int(line.split(b':',1)[1]) for line in headers.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            await reader.readexactly(length)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\n\r\n')
            await writer.drain()
            if stall:
                await asyncio.sleep(.4)
            records=[dict(message=dict(content='x'))]*8+[dict(done=True)]
            for record in records:
                await asyncio.sleep(.05)
                chunk=(json.dumps(record)+'\n').encode()
                writer.write(f'{len(chunk):x}\r\n'.encode()+chunk+b'\r\n')
                await writer.drain()
            writer.write(b'0\r\n\r\n')
        except (ConnectionError,asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
    server=await asyncio.start_server(serve,'127.0.0.1',0)
    port=server.sockets[0].getsockname()[1]
    provider=OllamaProvider(host=f'http://127.0.0.1:{port}',model='fake',stream=True,timeout=.2,total_timeout=3)
    try:
        if stall:
            with pytest.raises(httpx.ReadTimeout):
                await provider.complete('test')
        else:
            result=await provider.complete('test')
            assert result.text=='xxxxxxxx'
            assert result.timings['request_seconds']>.2
    finally:
        await provider.aclose()
        server.close();await server.wait_closed()
        await asyncio.gather(*handlers)
