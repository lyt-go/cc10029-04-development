"""可用字段契约清单与映射轮次测试。

覆盖业务叙事：
1. 按抽取窗口校验规则归属（生效时段）与权限（通用共享 / 源端专属）；
2. 同一目标字段两版合法规则（v1 交接有效期内、v2 已生效未撤回）的版本选择；
3. A 的窗口先登记未开始，B 同时提交专属映射，专属列权限分别核验；
4. 窗口开始后同一轮处理复用一份冻结、可回查的契约集合；
5. 结果保留实际采用的来源、目标字段与版本；
6. 两版都许可时段启动 A/B 映射，检查 A 首次结果版本及重算评估；
7. 暂停 A 后恢复：不混两套类型、不读取 B 的专属列。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.services.base import DATA_ROOT
from app.services.contract_catalog import (
    ContractCatalogService,
    ROUND_DONE,
    ROUND_PAUSED,
    SCOPE_COMMON,
)
from app.services.extract_window import ExtractWindowService
from app.services.field_contract import FieldContractService
from app.services.source_register import SourceRegisterService
from app.store import store


# 场景时钟：v1 交接期与 v2 生效期在 OVERLAP_TS 重叠
HANDOVER_START = 1_000_000
OVERLAP_TS = 1_500_000
HANDOVER_END = 2_000_000
AFTER_HANDOVER = 2_500_000


@pytest.fixture(autouse=True)
def reset_store_and_artifacts():
    """每个用例重置内存表，并清掉契约清单/映射轮次产物，保证隔离。"""
    from app.seed import SEED_ROWS
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    for sub in ("catalogs", "mapping_rounds", "mapped"):
        shutil.rmtree(DATA_ROOT / sub, ignore_errors=True)
    yield


def _register_source(code: str) -> None:
    svc = SourceRegisterService()
    entry = svc.register_source({
        "源端编号": code, "系统名称": f"系统-{code}", "数据域": "订单域",
        "记录格式": "JSON", "编码方式": "UTF-8", "日增量级": "10万",
        "抽取方式": "增量抽取", "登记游标": "0",
    })
    svc.confirm_register(entry["id"])


def _registered_window(source_code: str) -> dict:
    """登记一个尚未开始的抽取窗口（对应叙事：A 的窗口已登记尚未开始）。"""
    return ExtractWindowService().create_window(source_code, count=6)


def _issue_rules() -> None:
    """构造规则集：A 目标字段两版并存 + 通用规则 + A/B 各自专属规则。"""
    fc = FieldContractService()
    # 同一目标字段 dst_amount：v1 仍在交接有效期、v2 已生效且未撤回 v1
    v1 = fc.create_contract(
        "ext_amount", "dst_amount", "string", version="v1.0",
        scope_source=SCOPE_COMMON, effective_start=HANDOVER_START, effective_end=HANDOVER_END,
        contract_code="FIEL-AMOUNT-V1",
    )
    v2 = fc.create_contract(
        "ext_amount", "dst_amount", "int", version="v2.0",
        scope_source=SCOPE_COMMON, effective_start=OVERLAP_TS, effective_end="",
        contract_code="FIEL-AMOUNT-V2",
    )
    # 两个源端共享的通用规则
    common = fc.create_contract(
        "ext_a", "dst_a", "string", version="v1.0", scope_source=SCOPE_COMMON,
        contract_code="FIEL-COMMON-A",
    )
    # A 源端专属字段
    only_a = fc.create_contract(
        "ext_secret_a", "dst_secret_a", "string", version="v1.0",
        scope_source="SOUR-A", contract_code="FIEL-ONLY-A",
    )
    # B 源端专属字段（B 同时提交的专属映射）
    only_b = fc.create_contract(
        "ext_secret_b", "dst_secret_b", "string", version="v1.0",
        scope_source="SOUR-B", contract_code="FIEL-ONLY-B",
    )
    for entry in (v1, v2, common, only_a, only_b):
        fc.run_action(entry["id"], "确认生效")


def _source_records(source_code: str) -> list[dict]:
    """构造同时含通用列与 A/B 专属列的源记录，用于验证越权读取防护。"""
    return [
        {
            "batch_key": source_code,
            "line_no": i + 1,
            "ext_a": f"val_{i}_a",
            "ext_amount": str(i * 10),
            "ext_secret_a": f"A-secret-{i}",
            "ext_secret_b": f"B-secret-{i}",
        }
        for i in range(6)
    ]


class TestContractCatalogBuild:
    """清单构建：时间归属、权限归属与版本选择。"""

    def test_catalog_filters_by_window_and_permission(self):
        _register_source("SOUR-A")
        _register_source("SOUR-B")
        win_a = _registered_window("SOUR-A")
        _issue_rules()

        catalog = ContractCatalogService().build_catalog(win_a["id"], at_ts=OVERLAP_TS)

        # A 的清单包含通用规则与 A 专属规则，但 B 的专属规则被权限核验拒绝
        target_fields = {item["目标字段"] for item in catalog["items"]}
        assert {"dst_a", "dst_amount", "dst_secret_a"} <= target_fields
        assert "dst_secret_b" not in target_fields
        denied_codes = {d["契约编号"] for d in catalog["denied"]}
        assert "FIEL-ONLY-B" in denied_codes
        assert all(d["拒绝原因"] == "专属字段权限不匹配" for d in catalog["denied"])

        # B 源端视角：A 的专属规则同样被拒绝，专属权限分别核验
        win_b = _registered_window("SOUR-B")
        catalog_b = ContractCatalogService().build_catalog(win_b["id"], at_ts=OVERLAP_TS)
        assert "dst_secret_a" not in {i["目标字段"] for i in catalog_b["items"]}
        assert "dst_secret_b" in {i["目标字段"] for i in catalog_b["items"]}
        assert {d["契约编号"] for d in catalog_b["denied"]} == {"FIEL-ONLY-A"}

    def test_two_legal_versions_newest_wins_during_overlap(self):
        _register_source("SOUR-A")
        win_a = _registered_window("SOUR-A")
        _issue_rules()

        catalog = ContractCatalogService().build_catalog(win_a["id"], at_ts=OVERLAP_TS)
        amount = next(i for i in catalog["items"] if i["目标字段"] == "dst_amount")
        # 两版都许可，默认选择最高版本 v2.0，同时保留候选版本可回查
        assert amount["契约版本"] == "v2.0"
        assert set(amount["候选版本"]) == {"v1.0", "v2.0"}
        assert catalog["target_candidates"]["dst_amount"] == ["v2.0", "v1.0"]

    def test_preferred_version_can_pin_v1_during_handover(self):
        _register_source("SOUR-A")
        win_a = _registered_window("SOUR-A")
        _issue_rules()

        catalog = ContractCatalogService().build_catalog(
            win_a["id"], at_ts=OVERLAP_TS,
            preferred_versions={"dst_amount": "v1.0"},
        )
        amount = next(i for i in catalog["items"] if i["目标字段"] == "dst_amount")
        assert amount["契约版本"] == "v1.0"

    def test_effective_window_excludes_expired_rules(self):
        _register_source("SOUR-A")
        win_a = _registered_window("SOUR-A")
        _issue_rules()

        # 交接期结束后：v1 已超出有效期，清单中只剩 v2
        catalog = ContractCatalogService().build_catalog(win_a["id"], at_ts=AFTER_HANDOVER)
        amount = next(i for i in catalog["items"] if i["目标字段"] == "dst_amount")
        assert amount["契约版本"] == "v2.0"
        assert amount["候选版本"] == ["v2.0"]

    def test_catalog_is_frozen_and_reused_for_same_window(self):
        _register_source("SOUR-A")
        win_a = _registered_window("SOUR-A")
        _issue_rules()

        svc = ContractCatalogService()
        first = svc.build_catalog(win_a["id"], at_ts=OVERLAP_TS)
        second = svc.build_catalog(win_a["id"], at_ts=AFTER_HANDOVER)
        # 同一窗口重复构建必须返回同一份冻结快照（校验值不变，时刻参数被忽略）
        assert first["checksum"] == second["checksum"]
        assert second["frozen_at"] == first["frozen_at"]
        assert Path(first["catalog_file"]).exists()

        # 撤回 v2 不影响已冻结清单，历史集合仍可回查
        v2_row = next(r for r in store.rows("field_contract") if r["契约编号"] == "FIEL-AMOUNT-V2")
        FieldContractService().withdraw_contract(v2_row["id"])
        frozen = svc.get_catalog(first["window_code"])
        assert frozen["checksum"] == first["checksum"]

    def test_build_catalog_requires_registered_window(self):
        svc = ContractCatalogService()
        with pytest.raises(ValueError):
            svc.build_catalog(99_999, at_ts=OVERLAP_TS)


class TestMappingRounds:
    """映射轮次：首次版本、血缘保留、A/B 隔离、暂停恢复。"""

    def test_first_a_round_picks_v2_and_keeps_lineage(self):
        _register_source("SOUR-A")
        win_a = _registered_window("SOUR-A")
        _issue_rules()

        svc = ContractCatalogService()
        result = svc.start_mapping_round(
            win_a["id"], "A", record_count=6,
            source_records=_source_records("SOUR-A"), at_ts=OVERLAP_TS,
        )
        # A 的首次结果在两版都许可时段选了 v2.0
        assert result["adopted_versions"]["dst_amount"] == "v2.0"
        assert result["status"] == ROUND_DONE

        output = Path(result["output_file"])
        rows = svc._read_jsonl(output)
        assert len(rows) == 6
        first = rows[0]
        # 结果保留实际采用的目标字段，且不混入 B 的专属列
        assert set(first) >= {"dst_a", "dst_amount", "dst_secret_a", "_round", "_catalog_checksum"}
        assert "dst_secret_b" not in first
        # v2 是 int 规则：数值被转换为整型，而不是沿用 v1 的字符串类型
        assert all(isinstance(row["dst_amount"], int) for row in rows)

        state = svc.get_round(result["round_code"])
        amount_lineage = [x for x in state["lineage"] if x["target_field"] == "dst_amount"]
        assert len(amount_lineage) == 6
        assert {x["contract_version"] for x in amount_lineage} == {"v2.0"}
        assert {x["source_field"] for x in amount_lineage} == {"ext_amount"}
        assert {x["contract_code"] for x in amount_lineage} == {"FIEL-AMOUNT-V2"}

    def test_a_and_b_rounds_share_catalog_but_keep_exclusive_columns_apart(self):
        _register_source("SOUR-A")
        _register_source("SOUR-B")
        win_a = _registered_window("SOUR-A")
        win_b = _registered_window("SOUR-B")
        _issue_rules()

        svc = ContractCatalogService()
        round_a = svc.start_mapping_round(
            win_a["id"], "A", source_records=_source_records("SOUR-A"), at_ts=OVERLAP_TS)
        round_b = svc.start_mapping_round(
            win_b["id"], "B", source_records=_source_records("SOUR-B"), at_ts=OVERLAP_TS)

        rows_a = svc._read_jsonl(Path(round_a["output_file"]))
        rows_b = svc._read_jsonl(Path(round_b["output_file"]))
        # 通用规则两端共享：版本与目标字段一致
        assert round_a["adopted_versions"]["dst_a"] == round_b["adopted_versions"]["dst_a"]
        # 专属字段各读各的，互相读不到对方的专属列
        assert "dst_secret_a" in rows_a[0] and "dst_secret_b" not in rows_a[0]
        assert "dst_secret_b" in rows_b[0] and "dst_secret_a" not in rows_b[0]
        # 每个轮次都持有可回查的冻结清单校验值
        assert round_a["catalog_checksum"] and round_b["catalog_checksum"]

    def test_pause_and_resume_uses_one_catalog_and_consistent_types(self):
        _register_source("SOUR-A")
        win_a = _registered_window("SOUR-A")
        _issue_rules()

        svc = ContractCatalogService()
        records = _source_records("SOUR-A")
        # 首批只处理 2 条，模拟窗口开始后处理中途暂停
        partial = svc.start_mapping_round(
            win_a["id"], "A", source_records=records,
            at_ts=OVERLAP_TS, batch_size=2,
        )
        round_code = partial["round_code"]
        checksum = partial["catalog_checksum"]
        assert partial["processed"] == 2
        assert partial["status"] != ROUND_DONE

        pause_result = svc.pause_mapping_round(round_code)
        assert pause_result["status"] == ROUND_PAUSED

        # 即使交接期已结束再恢复，轮次仍钉住原冻结清单，不会切换到新规则
        resumed = svc.resume_mapping_round(round_code, source_records=records)
        assert resumed["status"] == ROUND_DONE
        assert resumed["catalog_checksum"] == checksum

        final_rows = svc._read_jsonl(Path(partial["output_file"]))
        assert len(final_rows) == 6
        # 同一轮结果里 dst_amount 只能有一套类型（v2 的 int），不能 v1/v2 混用
        assert all(isinstance(row["dst_amount"], int) for row in final_rows)
        # 恢复后也不能读取 B 的专属列
        assert all("dst_secret_b" not in row for row in final_rows)
        state = svc.get_round(round_code)
        assert {x["contract_version"] for x in state["lineage"]
                if x["target_field"] == "dst_amount"} == {"v2.0"}

    def test_resume_requires_paused_state(self):
        _register_source("SOUR-A")
        win_a = _registered_window("SOUR-A")
        _issue_rules()
        svc = ContractCatalogService()
        done = svc.start_mapping_round(
            win_a["id"], "A", source_records=_source_records("SOUR-A"), at_ts=OVERLAP_TS)
        result = svc.resume_mapping_round(done["round_code"])
        assert "error" in result


class TestRecomputeEvaluation:
    """版本重算评估：交接期结束后是否需要按新规则重算。"""

    def test_pinned_v1_round_needs_recompute_after_handover(self):
        _register_source("SOUR-A")
        win_a = _registered_window("SOUR-A")
        _issue_rules()
        svc = ContractCatalogService()

        # 交接期内钉住 v1 启动 A 的首次映射
        first = svc.start_mapping_round(
            win_a["id"], "A",
            source_records=_source_records("SOUR-A"), at_ts=OVERLAP_TS,
            preferred_versions={"dst_amount": "v1.0"},
        )
        assert first["adopted_versions"]["dst_amount"] == "v1.0"

        # 交接期结束后评估：当前清单只剩 v2，A 的历史结果需要重算
        evaluation = svc.evaluate_recompute(
            win_a["id"], first["round_code"], at_ts=AFTER_HANDOVER,
        )
        assert evaluation["need_recompute"] is True
        changed = evaluation["changed_fields"]
        assert len(changed) == 1
        assert changed[0]["目标字段"] == "dst_amount"
        assert changed[0]["旧版本"] == "v1.0"
        assert changed[0]["当前版本"] == "v2.0"

    def test_newest_v2_round_needs_no_recompute(self):
        _register_source("SOUR-A")
        win_a = _registered_window("SOUR-A")
        _issue_rules()
        svc = ContractCatalogService()
        first = svc.start_mapping_round(
            win_a["id"], "A",
            source_records=_source_records("SOUR-A"), at_ts=OVERLAP_TS,
        )
        evaluation = svc.evaluate_recompute(
            win_a["id"], first["round_code"], at_ts=AFTER_HANDOVER,
        )
        assert evaluation["need_recompute"] is False
        assert evaluation["changed_fields"] == []

    def test_evaluate_unknown_round_returns_error(self):
        _register_source("SOUR-A")
        win_a = _registered_window("SOUR-A")
        result = ContractCatalogService().evaluate_recompute(win_a["id"], "MAP-NOPE-A")
        assert "error" in result


class TestResourceRouterRegistration:
    """契约清单纳入统一资源路由，可被通用列表接口管理。"""

    def test_catalog_service_registered_and_listable(self):
        from app.services import service_for
        svc = service_for("contract_catalog")
        assert isinstance(svc, ContractCatalogService)
        items, total = svc.list_entries()
        assert total >= 1
        assert "清单编号" in svc.LIST_FIELDS

    def test_api_routes_exposed(self):
        from app.main import app
        # 该 FastAPI 版本以 _IncludedRouter 包装子路由，用 url_path_for 验证最可靠
        assert app.url_path_for("build_catalog") == "/api/contract-catalog/build"
        assert app.url_path_for("start_round") == "/api/contract-catalog/rounds/start"
        assert app.url_path_for("pause_round", round_code="X") == \
            "/api/contract-catalog/rounds/X/pause"
        assert app.url_path_for("resume_round", round_code="X") == \
            "/api/contract-catalog/rounds/X/resume"
        assert app.url_path_for("evaluate_recompute") == \
            "/api/contract-catalog/evaluate-recompute"
        assert app.url_path_for("list_entries", module="contract_catalog") == \
            "/api/contract_catalog"
