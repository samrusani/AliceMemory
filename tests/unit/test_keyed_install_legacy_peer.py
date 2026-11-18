"""Read the mounted legacy operations and exercise the admission middleware."""

import asyncio
import json
import os
import re
import subprocess
import sys
from uuid import uuid4

from fastapi import Request, Response
import pytest

from alicebot_api import main
from alicebot_api.config import Settings
from tests.unit.test_main import _build_request


def _probe(app_env, enabled_outside_dev):
    main.get_settings = lambda: Settings(
        app_env=app_env, auth_user_id=str(uuid4()), legacy_v0_enabled_outside_dev=enabled_outside_dev,
    )

    async def probe(method, path, peer):
        request = _build_request(method=method, path=path, client_host=peer, headers={"host": "localhost:8000"})

        async def dispatch(_: Request) -> Response:
            return Response(status_code=204)

        response = await main.enforce_authenticated_user_identity(request, dispatch)
        return [response.status_code, json.loads(response.body) if response.body else None]

    async def inventory():
        outcomes = {}
        for path, item in main.app.openapi()["paths"].items():
            if not path.startswith("/v0/") or path == "/v0/vnext" or path.startswith("/v0/vnext/"):
                continue
            for method in ("get", "post", "put", "patch", "delete", "options", "head", "trace"):
                if method in item:
                    concrete_path = re.sub(r"\{[^}]+\}", str(uuid4()), path)
                    outcomes[f"{method.upper()} {path}"] = [await probe(method.upper(), concrete_path, peer)
                                                             for peer in ("127.0.0.1", "203.0.113.10")]
        return outcomes

    return asyncio.run(inventory())


@pytest.mark.parametrize("flag", ("0", "1"))
@pytest.mark.parametrize("app_env", ("development", "test", "production"))
def test_mounted_legacy_v0_operations_stay_on_loopback(flag, app_env):
    env = {**os.environ, "ALICE_LEGACY_SURFACES": flag}
    process = subprocess.run([sys.executable, "-m", __name__, app_env], env=env, check=True, capture_output=True, text=True)
    outcomes = json.loads(process.stdout)
    assert outcomes
    for operation, (local, remote) in outcomes.items():
        assert local == [204, None], (operation, local)
        assert remote == [403, {"detail": "legacy v0 API is restricted to loopback clients"}], (operation, remote)


if __name__ == "__main__":
    print(json.dumps(_probe(sys.argv[1], enabled_outside_dev=True), sort_keys=True))
