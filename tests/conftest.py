import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"
STATE_PDF = FIXTURES / "state_nsq_june_2025.pdf"
CENTRAL_PDF = FIXTURES / "cdsco_nsq_june_2025.pdf"
SPURIOUS_PDF = FIXTURES / "spurious_june_2025.pdf"
REVISED_PDF = FIXTURES / "revised_alert_may_2025.pdf"


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Fresh SQLite database, AI off, no access code."""
    import config
    monkeypatch.setattr(config, "_base_settings", config._base_settings.__class__(
        data_dir=str(tmp_path), database_url="", api_key="", access_code="", check_token=""))
    yield tmp_path
