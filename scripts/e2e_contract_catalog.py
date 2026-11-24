"""端到端叙事验证脚本（手工执行，不属于 pytest 套件）。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/workspace/backend")

from app.seed import SEED_ROWS  # noqa: E402
from app.store import store  # noqa: E402
from app.services.base import DATA_ROOT  # noqa: E402

for sub in ("catalogs", "mapping_rounds", "mapped"):
    shutil.rmtree(DATA_ROOT / sub, ignore_errors=True)
store._tables = {n: [dict(r) for r in rows] for n, rows in SEED_ROWS.items()}

from app.services.contract_catalog import ContractCatalogService  # noqa: E402
from app.services.extract_window import ExtractWindowService  # noqa: E402
from app.services.field_contract import FieldContractService  # noqa: E402
from app.services.source_register import SourceRegisterService  # noqa: E402

OVERLAP, AFTER = 1_500_000, 2_500_000

ss = SourceRegisterService()
for code in ("SOUR-A", "SOUR-B"):
    entry = ss.register_source({
        "源端编号": code, "系统名称": code, "数据域": "订单域",
        "记录格式": "JSON", "编码方式": "UTF-8", "日增量级": "10万",
        "抽取方式": "增量抽取", "登记游标": "0",
    })
    ss.confirm_register(entry["id"])

ew = ExtractWindowService()
win_a = ew.create_window("SOUR-A", count=4)
win_b = ew.create_window("SOUR-B", count=4)
print("窗口已登记尚未开始 A:", ew.is_window_started(win_a["id"]) is False)

fc = FieldContractService()
rules = [
    ("ext_amount", "dst_amount", "string", "v1.0", "通用", 1_000_000, 2_000_000, "FIEL-AMT-V1"),
    ("ext_amount", "dst_amount", "int", "v2.0", "通用", 1_500_000, "", "FIEL-AMT-V2"),
    ("ext_a", "dst_a", "string", "v1.0", "通用", "", "", "FIEL-CMN"),
    ("ext_secret_a", "dst_secret_a", "string", "v1.0", "SOUR-A", "", "", "FIEL-A"),
    ("ext_secret_b", "dst_secret_b", "string", "v1.0", "SOUR-B", "", "", "FIEL-B"),
]
for sf, tf, dt, ver, scope, st, en, code in rules:
    entry = fc.create_contract(
        sf, tf, dt, version=ver, scope_source=scope,
        effective_start=st or None, effective_end=en or None, contract_code=code,
    )
    fc.run_action(entry["id"], "确认生效")

svc = ContractCatalogService()
cat_a = svc.build_catalog(win_a["id"], at_ts=OVERLAP)
print("A清单目标字段:", sorted(cat_a["target_versions"]))
print("A首次选中版本 dst_amount:", cat_a["target_versions"]["dst_amount"])
print("被拒(应含FIEL-B):", [d["契约编号"] for d in cat_a["denied"]])
print("窗口开始标记:", cat_a["window_started"], "状态:", cat_a["window_status"])

recs = [
    {"ext_a": f"a{i}", "ext_amount": str(i * 5),
     "ext_secret_a": f"as{i}", "ext_secret_b": f"bs{i}"}
    for i in range(4)
]
ra = svc.start_mapping_round(win_a["id"], "A", source_records=recs, at_ts=OVERLAP, batch_size=2)
rb = svc.start_mapping_round(win_b["id"], "B", source_records=recs, at_ts=OVERLAP)
print("A首批:", ra["processed"], ra["status"], "| B:", rb["processed"], rb["status"])
svc.pause_mapping_round(ra["round_code"])
ra2 = svc.resume_mapping_round(ra["round_code"], source_records=recs)
rows = svc._read_jsonl(Path(ra2["output_file"]))
print("A恢复后条数:", len(rows), "| 类型集合:", {type(r["dst_amount"]).__name__ for r in rows})
print("A结果含B专属列:", any("dst_secret_b" in r for r in rows))

ev = svc.evaluate_recompute(win_a["id"], ra["round_code"], at_ts=AFTER)
print("默认v2 -> 需重算:", ev["need_recompute"], ev["changed_fields"])

for sub in ("catalogs", "mapping_rounds", "mapped"):
    shutil.rmtree(DATA_ROOT / sub, ignore_errors=True)
rp = svc.start_mapping_round(
    win_a["id"], "A", source_records=recs, at_ts=OVERLAP,
    preferred_versions={"dst_amount": "v1.0"},
)
evp = svc.evaluate_recompute(win_a["id"], rp["round_code"], at_ts=AFTER)
print("钉住v1 -> 首次版本:", rp["adopted_versions"]["dst_amount"],
      "| 交接后需重算:", evp["need_recompute"])
print("重算差异:", evp["changed_fields"])
