import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os
import tempfile
from pathlib import Path
import pytest

_test_directory = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(_test_directory.name) / "tests.db").replace("\\", "/")
os.environ["AI_ENABLED"] = "false"
os.environ["AUTO_SYNC_WORKER_ENABLED"] = "false"
os.environ["APP_ACCESS_TOKEN"] = ""
os.environ["AUTH_REQUIRED"] = "false"
os.environ["ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1,testserver"
from app.db import Base, engine
from app.config import get_settings

@pytest.fixture(autouse=True)
def clean_database():
    get_settings.cache_clear()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    get_settings.cache_clear()

def pytest_sessionfinish(session, exitstatus):
    engine.dispose()
    _test_directory.cleanup()
