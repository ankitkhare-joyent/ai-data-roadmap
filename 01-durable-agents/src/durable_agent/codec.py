"""Encrypt every payload before it reaches the Temporal server.

Temporal stores workflow inputs, activity inputs and results in its database. For an agent that means
prompts, tool outputs and model answers, which can carry customer data. A PayloadCodec runs inside the
client and the worker, so the server (and anyone with database access) only ever sees ciphertext.

Keys live in OpenBao at secret/durable-agent/codec. The KV v2 version number is the key id: new payloads
use the latest version, old payloads name the version they were written with, so rotating the key is
"write a new version" and nothing needs re-encrypting.
"""
from __future__ import annotations

import base64
import dataclasses
import os
from collections.abc import Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec

from durable_agent.bao import BaoClient

ENCODING = b"binary/encrypted"
CODEC_PATH = "durable-agent/codec"


class EncryptionCodec(PayloadCodec):
    def __init__(self, keys: dict[str, bytes], current: str, loader=None):
        self._keys, self._current, self._loader = keys, current, loader

    @classmethod
    async def from_openbao(cls, bao: BaoClient) -> EncryptionCodec:
        data, version = await bao.kv_get(CODEC_PATH)

        async def loader(key_id: str) -> bytes:
            old, _ = await bao.kv_get(CODEC_PATH, version=int(key_id.removeprefix("v")))
            return base64.b64decode(old["key"])

        return cls({f"v{version}": base64.b64decode(data["key"])}, f"v{version}", loader)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        key = self._keys[self._current]
        out = []
        for p in payloads:
            nonce = os.urandom(12)
            # The key id is bound as associated data: swapping the metadata makes decryption fail.
            ct = AESGCM(key).encrypt(nonce, p.SerializeToString(), self._current.encode())
            out.append(Payload(metadata={"encoding": ENCODING, "encryption-key-id": self._current.encode()},
                               data=nonce + ct))
        return out

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out = []
        for p in payloads:
            if p.metadata.get("encoding") != ENCODING:
                out.append(p)          # written before encryption was turned on
                continue
            key_id = p.metadata["encryption-key-id"].decode()
            if key_id not in self._keys:
                if self._loader is None:
                    raise ValueError(f"unknown encryption key {key_id}")
                self._keys[key_id] = await self._loader(key_id)
            plain = AESGCM(self._keys[key_id]).decrypt(p.data[:12], p.data[12:], key_id.encode())
            out.append(Payload.FromString(plain))
        return out


def data_converter(codec: PayloadCodec) -> DataConverter:
    return dataclasses.replace(DataConverter.default, payload_codec=codec)
