"""Values longer than their database columns: PostgreSQL rejects them where SQLite silently stores them.

Run the suite against PostgreSQL too:  DATABRIDGE_DATABASE_URL=postgresql+psycopg://... pytest
"""

import polars as pl
import pytest

from databridge.core.db import session_scope
from databridge.core.models import AuditLog, Snapshot
from databridge.services import runs, sources, targets, users


def test_snapshot_fingerprint_fits_its_column():
    digest = sources.frame_hash(pl.DataFrame({"a": [1, 2]}))
    assert len(digest) == Snapshot.__table__.c.file_hash.type.length == 64


def test_long_generated_values_are_trimmed_or_refused():
    users.audit("api-key:" + "k" * 200, "api.stream_connected", "t" * 500, "d", "i" * 100)
    with session_scope() as s:
        row = s.query(AuditLog).order_by(AuditLog.id.desc()).first()
        assert len(row.username) == 80 and len(row.target) == 200 and len(row.ip) == 64
    with runs.track("extract", "s" * 400) as run:
        pass
    assert len(run.subject) == 200
    with pytest.raises(ValueError, match="at most 160"):
        sources.check_new_name("n" * 161)
    with pytest.raises(ValueError, match="1 to 160"):
        targets.save_target("t" * 161, [{"name": "a", "type": "string"}])
