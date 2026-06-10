#!/usr/bin/env python3
"""Focused checks for security fixes in legacy tooling and static reports."""

from __future__ import annotations

import importlib.util
import io
import os
import pickle
import shutil
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLBOX_ROOT = PROJECT_ROOT / "tools" / "download-extract" / "BSRN_Toolbox_py"
TEST_TMP = PROJECT_ROOT / ".security_regression_tmp"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def clean_test_tmp() -> None:
    if TEST_TMP.exists():
        shutil.rmtree(TEST_TMP, ignore_errors=True)
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    mpl_config = TEST_TMP / "mpl"
    mpl_config.mkdir(exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(mpl_config)


def test_restricted_pickle_loader() -> None:
    import sys
    import types

    sys.path.insert(0, str(TOOLBOX_ROOT))
    parallel_stub = types.ModuleType("logic.parallel")

    class Stoppable:
        pass

    parallel_stub.Stoppable = Stoppable
    sys.modules.setdefault("logic.parallel", parallel_stub)

    from logic.local_working_database import restricted_pickle_load, safe_unpack_zip, validate_buffer_state

    good = pickle.dumps(({"cab0425"}, {"cab0425": (0, 0, 0, set(), "ok")}))
    notfound, import_meta = validate_buffer_state(restricted_pickle_load(io.BytesIO(good)))
    assert notfound == {"cab0425"}
    assert "cab0425" in import_meta

    class Evil:
        def __reduce__(self):
            return (os.system, ("echo unsafe",))

    bad = pickle.dumps(Evil())
    try:
        restricted_pickle_load(io.BytesIO(bad))
    except pickle.UnpicklingError:
        pass
    else:
        raise AssertionError("restricted_pickle_load accepted an executable pickle payload")

    tmp = TEST_TMP / "zip"
    tmp.mkdir(parents=True, exist_ok=True)
    zip_path = tmp / "bad.zip"
    out_dir = tmp / "out"
    out_dir.mkdir()
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("../escape.txt", "nope")
    try:
        safe_unpack_zip(zip_path, out_dir)
    except ValueError:
        pass
    else:
        raise AssertionError("safe_unpack_zip accepted a traversal member")


def test_static_report_escapes_metadata() -> None:
    import pandas as pd

    modules = [
        PROJECT_ROOT / "tools" / "qc-graphs" / "bsrn_qc.py",
        PROJECT_ROOT / "tools" / "create-importfiles" / "bsrn_qc.py",
    ]
    metadata = {
        "station_name": "Test",
        "station_code": "TST",
        "latitude": 1.0,
        "longitude": 2.0,
        "elevation": 3,
        "month": 4,
        "year": 2026,
        "n_records": 0,
        "has_upward": False,
        "pi_name": "<script>alert(1)</script>",
        "filename": "tst0426.dat",
    }
    qc_summary = pd.DataFrame()

    for index, module_path in enumerate(modules):
        module = load_module(f"bsrn_qc_security_{index}", module_path)
        tmp = TEST_TMP / f"report_{index}"
        tmp.mkdir(parents=True, exist_ok=True)
        output = tmp / "report.html"
        module.generate_report(pd.DataFrame(), metadata, qc_summary, {}, output)
        html = output.read_text(encoding="utf-8", errors="replace")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


if __name__ == "__main__":
    clean_test_tmp()
    test_restricted_pickle_loader()
    test_static_report_escapes_metadata()
    clean_test_tmp()
    print("security regression checks passed")
