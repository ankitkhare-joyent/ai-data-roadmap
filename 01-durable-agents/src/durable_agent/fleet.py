"""A stand-in for the EC2 API: a JSON fleet on a volume, with idempotent stops.

The interesting part is stop_instance. Temporal guarantees an activity runs *at least* once, so the
same stop can arrive twice (the worker died after stopping but before reporting success). The
idempotency key makes the second call a no-op that returns the first call's result. Real AWS APIs offer
the same thing as ClientToken; anything else needs a dedupe table like this one.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

SEED = Path(__file__).resolve().parents[2] / "data" / "fleet.json"


class FleetError(Exception):
    pass


class TransientFleetError(FleetError):
    """Throttling or a 5xx from the cloud API: safe to retry."""


class Fleet:
    def __init__(self, state_file: str | None = None):
        self.path = Path(state_file or os.environ.get("FLEET_STATE", "/data/fleet-state.json"))
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SEED, self.path)

    @contextmanager
    def _locked(self, write: bool):
        with open(self.path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            state = json.load(f)
            yield state
            if write:
                f.seek(0)
                json.dump(state, f, indent=2)
                f.truncate()

    def list_idle(self, max_cpu_p95: float, min_idle_days: int) -> list[dict]:
        with self._locked(write=False) as s:
            return [
                {k: i[k] for k in ("instance_id", "type", "cpu_p95", "idle_days", "env")}
                for i in s["instances"]
                if i["state"] == "running" and i["cpu_p95"] <= max_cpu_p95 and i["idle_days"] >= min_idle_days
            ]

    def describe(self, instance_id: str) -> dict:
        with self._locked(write=False) as s:
            for i in s["instances"]:
                if i["instance_id"] == instance_id:
                    return i
        raise FleetError(f"no such instance {instance_id}")

    def stop(self, instance_id: str, idempotency_key: str, fail_first_attempt: bool = False) -> dict:
        with self._locked(write=True) as s:
            done = s.setdefault("stops", {})
            if idempotency_key in done:
                return {**done[idempotency_key], "deduplicated": True}
            inst = next((i for i in s["instances"] if i["instance_id"] == instance_id), None)
            if inst is None:
                raise FleetError(f"no such instance {instance_id}")
            if inst["env"] == "prod":
                # Defence in depth: the prompt says never stop prod, the API enforces it anyway.
                raise FleetError(f"{instance_id} is env=prod; refusing to stop")
            result = {"instance_id": instance_id, "previous_state": inst["state"], "state": "stopped"}
            inst["state"] = "stopped"
            done[idempotency_key] = result
        if fail_first_attempt:
            # The stop happened, then the "network" dropped the response. The retry must not stop it again.
            raise TransientFleetError("connection reset after the stop was applied")
        return result
