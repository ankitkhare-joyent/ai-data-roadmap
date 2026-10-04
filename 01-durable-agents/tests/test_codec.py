import os

import pytest
from cryptography.exceptions import InvalidTag
from temporalio.api.common.v1 import Payload

from durable_agent.codec import EncryptionCodec


async def test_roundtrip_and_rotation():
    k1, k2 = os.urandom(32), os.urandom(32)
    old = EncryptionCodec({"v1": k1}, "v1")
    enc = await old.encode([Payload(metadata={"encoding": b"json/plain"}, data=b'{"prompt":"secret"}')])
    assert b"secret" not in enc[0].data and enc[0].metadata["encryption-key-id"] == b"v1"

    async def loader(key_id):
        return {"v1": k1}[key_id]

    new = EncryptionCodec({"v2": k2}, "v2", loader)       # after rotation: writes v2, still reads v1
    assert (await new.decode(enc))[0].data == b'{"prompt":"secret"}'


async def test_tampered_key_id_fails():
    codec = EncryptionCodec({"v1": os.urandom(32), "v2": os.urandom(32)}, "v1")
    enc = await codec.encode([Payload(data=b"x")])
    enc[0].metadata["encryption-key-id"] = b"v2"
    with pytest.raises(InvalidTag):
        await codec.decode(enc)
