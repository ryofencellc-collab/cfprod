import os
import sys
import tempfile
from pathlib import Path

# Isolated data dir and fixed credentials, set before the app modules import config.
_tmp = tempfile.mkdtemp(prefix="clipforge-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["ADMIN_PASSWORD"] = "test-password"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["AUTOPILOT_DISABLED"] = "1"
for k in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PUBLIC_BASE_URL",
          "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"):
    os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from db.database import init_db  # noqa: E402

init_db()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def authed(client):
    r = client.post("/api/auth/login", json={"password": "test-password"})
    assert r.status_code == 200
    return client
