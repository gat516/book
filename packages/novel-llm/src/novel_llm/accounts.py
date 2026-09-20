"""Shared account credential format and scope (§15). No implicit hosted credentials."""
import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

LEGACY_ACCOUNT = '00000000-0000-4000-8000-000000000001'
def hosted(): return os.environ.get('BOOK_MODE') == 'hosted'
def credential_aad(account, provider, endpoint):
    return f'book-key-v1\n{account}\n{provider}\n{(endpoint or "").rstrip("/")}'.encode()

def encryption_key(version=1):
    raw=os.environ.get(f'PROVIDER_CONFIG_ENCRYPTION_KEY_V{version}') or os.environ.get('PROVIDER_CONFIG_ENCRYPTION_KEY','')
    key=base64.b64decode(raw)
    if len(key)!=32: raise RuntimeError('provider credential encryption key is not configured')
    return key

async def load_credential(conn,provider):
    row=await (await conn.execute('SELECT base_url,api_key_cipher,api_key_nonce,account_id::text,key_version FROM provider_credential WHERE provider=%s AND account_id=(SELECT current_account())',(provider,))).fetchone()
    if row is None:return None,None
    endpoint,cipher,nonce,account,version=row
    key=None
    if cipher is not None:
        key=AESGCM(encryption_key(version or 1)).decrypt(bytes(nonce),bytes(cipher),credential_aad(account,provider,endpoint) if version else None).decode()
    return endpoint,key

async def worker_scope(conn,novel_id):
    """Dispatcher projection returns only the owner of an active book; never its content."""
    row=await (await conn.execute('SELECT worker_novel_owner(%s)',(novel_id,))).fetchone()
    if not row or not row[0]: return None
    account=str(row[0])
    await conn.execute("SELECT set_config('app.account_id',%s,false)",(account,))
    return account
