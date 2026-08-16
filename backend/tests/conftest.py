from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="personal-agent-tests-"))
os.environ["DATABASE_PATH"] = str(TEST_ROOT / "test.db")
os.environ["CHROMA_PATH"] = str(TEST_ROOT / "chroma")
os.environ["UPLOAD_PATH"] = str(TEST_ROOT / "uploads")
os.environ["DOWNLOAD_PATH"] = str(TEST_ROOT / "downloads")
os.environ["MODEL_PROFILES_JSON"] = json.dumps(
    [
        {
            "alias": "default",
            "model": "unconfigured",
            "base_url": "https://api.example.com/v1",
            "api_key_env": "PERSONAL_AGENT_TEST_KEY",
            "context_window": 32768,
            "timeout_seconds": 5,
        }
    ]
)
os.environ["ONEBOT_TOKEN"] = "test-token"
os.environ["OWNER_QQ_IDS"] = '["10001"]'
