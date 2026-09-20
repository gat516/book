import socket
import httpx
import pytest
from novel_llm.netguard import PublicTransport, approved_endpoint, public_address
from novel_llm.accounts import credential_aad
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

@pytest.mark.parametrize('ip',['127.0.0.1','10.1.1.1','169.254.169.254','::1','::ffff:127.0.0.1','fc00::1','100.64.0.1'])
def test_private(ip): assert not public_address(ip)

def test_bound_key():
    key=AESGCM(b'k'*32);nonce=b'n'*12
    cipher=key.encrypt(nonce,b'secret',credential_aad('a','custom','https://models.example'))
    for aad in [credential_aad('b','custom','https://models.example'),credential_aad('a','custom','https://evil.example')]:
        with pytest.raises(InvalidTag): key.decrypt(nonce,cipher,aad)

async def test_pins_public_resolution_and_preserves_tls(monkeypatch):
    import asyncio
    async def resolve(*args,**kwargs):return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',resolve)
    transport=PublicTransport()
    async def accept(request):
        assert request.url.host=='8.8.8.8'
        assert request.headers['host']=='models.example'
        assert request.extensions['sni_hostname']=='models.example'
        return httpx.Response(200,json={})
    await transport.inner.aclose();transport.inner=httpx.MockTransport(accept)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get('https://models.example/v1')

async def test_rebinding_blocked(monkeypatch):
    import asyncio
    async def resolve(*args,**kwargs):return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('169.254.169.254',443))]
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',resolve)
    async with httpx.AsyncClient(transport=PublicTransport()) as client:
        with pytest.raises(ValueError,match='non-public'):await client.get('https://models.example/v1')
