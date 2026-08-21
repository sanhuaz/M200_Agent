from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

TEST_ROOT = Path(tempfile.mkdtemp(prefix="personal-agent-tests-"))
os.environ["DATABASE_PATH"] = str(TEST_ROOT / "test.db")
os.environ["CHROMA_PATH"] = str(TEST_ROOT / "chroma")
os.environ["UPLOAD_PATH"] = str(TEST_ROOT / "uploads")
os.environ["DOWNLOAD_PATH"] = str(TEST_ROOT / "downloads")
os.environ["TOOLS_PATH"] = str(TEST_ROOT / "tools")
os.environ["SKILLS_PATH"] = str(TEST_ROOT / "skills")
os.environ["WORKSPACE_PATH"] = str(TEST_ROOT / "workspace")
os.environ["MODEL_PROFILES_JSON"] = json.dumps(
    [
        {
            "alias": "default",
            "model": "unconfigured",
            "base_url": "https://api.example.com/v1",
            "api_key_env": "PERSONAL_AGENT_TEST_KEY",
            "context_window": 1000000,
            "input_soft_limit": 131072,
            "max_output_tokens": 16384,
            "timeout_seconds": 5,
        }
    ]
)
os.environ["ONEBOT_TOKEN"] = "test-token"
os.environ["OWNER_QQ_IDS"] = '["10001"]'


@pytest.fixture(scope="session", autouse=True)
def initialize_test_database() -> None:
    from app.db.session import SessionLocal, initialize_database
    from app.services.runtime import bootstrap_runtime

    initialize_database()
    with SessionLocal() as session:
        bootstrap_runtime(session)
