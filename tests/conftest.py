import os
import sys
import tempfile
from pathlib import Path

# Isolated data dir per test session; must be set before databridge modules import settings.
_TMP = tempfile.mkdtemp(prefix="databridge-test-")
os.environ["DATABRIDGE_DATA_DIR"] = _TMP
os.environ["DATABRIDGE_AI_HEALTH_MINUTES"] = "0"  # no background checks during tests
os.environ["DATABRIDGE_SOURCE_SCHEDULER"] = "false"  # tests call scheduler.run_due() themselves
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@pytest.fixture(scope="session", autouse=True)
def _db():
    if not (SAMPLES / "vendor_sales.xlsx").exists():
        import runpy
        runpy.run_path(str(SAMPLES / "make_samples.py"), run_name="__main__")
    from databridge.core.db import init_db
    init_db()
    yield


@pytest.fixture
def sales_bytes() -> bytes:
    return (SAMPLES / "vendor_sales.xlsx").read_bytes()


@pytest.fixture
def template_bytes() -> bytes:
    return (SAMPLES / "target_customer_orders.xlsx").read_bytes()
