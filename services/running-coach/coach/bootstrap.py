"""One-time bot device/room provisioning; no model is started."""

import asyncio
import json
import os
from pathlib import Path

from nio import (
    AsyncClient,
    AsyncClientConfig,
    LoginResponse,
    RoomCreateResponse,
    RoomPreset,
)


async def provision():
    config = json.loads(Path("/run/bootstrap.json").read_text())
    state = Path("/state")
    (state / "crypto").mkdir(mode=0o700, exist_ok=True)
    credentials_path = state / "credentials.json"
    saved = json.loads(credentials_path.read_text()) if credentials_path.exists() else {}
    client = AsyncClient(
        "http://127.0.0.1:8008",
        config["user_id"],
        device_id=saved.get("device_id", ""),
        store_path=str(state / "crypto"),
        config=AsyncClientConfig(encryption_enabled=True, pickle_key=config["pickle_key"]),
    )
    try:
        if saved:
            client.restore_login(config["user_id"], saved["device_id"], saved["access_token"])
        else:
            login = await client.login(config["password"], device_name=config["device_name"])
            if not isinstance(login, LoginResponse):
                raise RuntimeError("Bot login failed")
            saved = {
                "user_id": login.user_id,
                "device_id": login.device_id,
                "access_token": login.access_token,
                "pickle_key": config["pickle_key"],
            }
            descriptor = os.open(credentials_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(saved, output)
                output.flush()
                os.fsync(output.fileno())
        if client.should_upload_keys:
            await client.keys_upload()
        await client.sync(timeout=0, full_state=True)
        if not saved.get("room_id"):
            room = await client.room_create(
                name=config["room_name"],
                is_direct=True,
                federate=False,
                preset=RoomPreset.private_chat,
                invite=[config["athlete_id"]],
                initial_state=[
                    {
                        "type": "m.room.encryption",
                        "state_key": "",
                        "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                    }
                ],
            )
            if not isinstance(room, RoomCreateResponse):
                raise RuntimeError("Encrypted room creation failed")
            saved["room_id"] = room.room_id
        await client.sync(timeout=0, full_state=True)
        members = await client.joined_members(saved["room_id"])
        if not hasattr(members, "members"):
            raise RuntimeError("Room members could not be read")
        joined = config["athlete_id"] in {member.user_id for member in members.members}
        if joined:
            if client.should_query_keys:
                await client.keys_query()
            trusted = {}
            for device_id, device in client.device_store[config["athlete_id"]].items():
                if not device.deleted:
                    trusted[device_id] = {
                        "ed25519": device.ed25519,
                        "curve25519": device.curve25519,
                    }
                    client.verify_device(device)
            if not trusted:
                raise RuntimeError("Athlete device keys are not available yet")
            if saved.get("trusted_devices") and saved["trusted_devices"] != {
                config["athlete_id"]: trusted
            }:
                raise RuntimeError(
                    "Device enrollment already exists; new devices require explicit enrollment"
                )
            saved["trusted_devices"] = {config["athlete_id"]: trusted}
        saved["athlete_joined"] = joined
        temporary = credentials_path.with_suffix(".new")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(saved, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(credentials_path)
        print(
            json.dumps(
                {
                    "room_name": config["room_name"],
                    "room_id": saved["room_id"],
                    "athlete_joined": joined,
                    "enrolled_device_ids": list(
                        saved.get("trusted_devices", {}).get(config["athlete_id"], {})
                    ),
                }
            )
        )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(provision())
