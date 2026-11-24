"""可用字段契约清单领域服务：按抽取窗口校验规则归属与权限，冻结一份可回查的契约集合。

业务背景
--------
同一目标字段可能同时存在两版合法规则（如 v1 仍在交接有效期、v2 已生效且未撤回 v1），
不同源端可以共享通用规则，但专属字段的权限必须分别核验。映射任务在窗口启动时
不能直接读取全量契约，而要先按「抽取窗口 + 生效时段 + 权限归属」筛出可用集合，
并冻结成一份带校验值的清单，同一轮处理始终复用同一份，保证结果可回查、可重算判定。

本服务提供：
1. 按窗口构建/复用可用字段契约清单（时间归属、权限归属、版本选择）；
2. 基于冻结清单执行 A/B 映射轮次，结果保留实际采用的来源、目标字段与版本；
3. 暂停/恢复：同一轮只能使用同一份清单，恢复时不得混入其他类型或读取他方专属列；
4. 版本重算评估：对比上一轮采用版本与当前清单选择，判定是否需要按新规则重算。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 通用规则归属标识：适用源端为该值的契约视为两个源端可共享的通用规则
SCOPE_COMMON = "通用"
# 清单冻结状态
CATALOG_FROZEN = "已冻结"
# 映射轮次状态
ROUND_RUNNING = "进行中"
ROUND_PAUSED = "已暂停"
ROUND_DONE = "已完成"


def _parse_ts(value: Any) -> float:
    """把字符串时间（ISO）或时间戳解析为 epoch 秒；无法解析时返回 0.0。"""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return time.mktime(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        try:
            return float(text)
        except ValueError:
            return 0.0


def _version_key(version: str) -> tuple[int, ...]:
    """把 v1.0 / v2 这类版本号解析为可比较的数字元组，缺省数字按 0 处理。"""
    digits = "".join(ch if ch.isdigit() or ch == "." else " " for ch in str(version or ""))
    parts = [p.strip() for p in digits.split(".") if p.strip()]
    if not parts:
        return (0,)
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (0,)


class ContractCatalogService(BaseModuleService):
    """可用字段契约清单：窗口级规则归属、权限核验与映射轮次管理。"""

    MODULE = "contract_catalog"
    ENTITY = "可用字段契约清单"
    OPERATION_TERM = "冻结"
    LIST_FIELDS = ["清单编号", "窗口编号", "源端编号", "生效时间", "清单版本", "契约条目数", "校验值"]
    REQUIRED_FIELDS = ["清单编号", "窗口编号", "源端编号"]
    STATUS_ORDER = ["草稿", "待校验", CATALOG_FROZEN, "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "冻结清单": CATALOG_FROZEN, "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = ["标记废弃"]

    # ------------------------------------------------------------------
    # 规则归属与时间/权限判定
    # ------------------------------------------------------------------

    def _contract_scope(self, contract: dict[str, Any]) -> str:
        """读取契约归属源端；缺省按通用规则处理，兼容历史种子数据。"""
        return str(contract.get("适用源端") or SCOPE_COMMON).strip() or SCOPE_COMMON

    def _is_effective_at(self, contract: dict[str, Any], at_ts: float) -> bool:
        """判断契约在指定时刻是否处于有效期内（含边界），未登记起止时间视为长期有效。"""
        start = contract.get("生效开始")
        end = contract.get("生效结束")
        if start not in (None, "") and at_ts < _parse_ts(start):
            return False
        if end not in (None, "") and at_ts > _parse_ts(end):
            return False
        return True

    def _can_access(self, contract: dict[str, Any], source_code: str) -> bool:
        """权限核验：通用规则可共享；专属规则只有归属源端本人可用。"""
        scope = self._contract_scope(contract)
        if scope == SCOPE_COMMON:
            return True
        return scope == source_code

    def _active_contracts(self) -> list[dict[str, Any]]:
        """取当前已生效、未撤回且字段登记完整的契约（排除种子占位样例）。"""
        active = []
        for row in store.rows("field_contract"):
            if row.get("status") != "已生效" or str(row.get("撤回标记", "否")) == "是":
                continue
            # 种子样例的源/目标字段是中文占位，不具备真实映射条件
            if "样例" in str(row.get("源字段", "")) or "样例" in str(row.get("目标字段", "")):
                continue
            active.append(row)
        return active

    # ------------------------------------------------------------------
    # 可用契约清单构建与冻结
    # ------------------------------------------------------------------

    def _assemble_catalog(
        self,
        window: dict[str, Any],
        check_ts: float,
        preferred_versions: dict[str, str],
    ) -> dict[str, Any]:
        """按窗口时刻组装可用契约集合（不持久化），供冻结与重算评估共用。"""
        source_code = str(window["源端编号"])
        # 逐条做时间归属与权限归属核验
        eligible: list[dict[str, Any]] = []
        denied: list[dict[str, str]] = []
        for contract in self._active_contracts():
            if not self._is_effective_at(contract, check_ts):
                continue
            if not self._can_access(contract, source_code):
                denied.append({
                    "契约编号": str(contract.get("契约编号", "")),
                    "目标字段": str(contract.get("目标字段", "")),
                    "归属源端": self._contract_scope(contract),
                    "拒绝原因": "专属字段权限不匹配",
                })
                continue
            eligible.append(contract)

        # 同一目标字段可能存在多版合法规则，按版本策略择一并保留候选版本
        candidates: dict[str, list[dict[str, Any]]] = {}
        for contract in eligible:
            target_field = str(contract["目标字段"])
            candidates.setdefault(target_field, []).append(contract)

        items: list[dict[str, Any]] = []
        ordered_candidates: dict[str, list[str]] = {}
        for target_field in sorted(candidates):
            group = sorted(
                candidates[target_field],
                key=lambda c: _version_key(str(c.get("契约版本", "v1.0"))),
                reverse=True,
            )
            preferred = preferred_versions.get(target_field)
            picked = group[0]
            if preferred:
                for c in group:
                    if str(c.get("契约版本")) == str(preferred):
                        picked = c
                        break
            items.append({
                "契约编号": str(picked.get("契约编号", "")),
                "源字段": str(picked.get("源字段", "")),
                "目标字段": target_field,
                "数据类型": str(picked.get("数据类型", "string")),
                "转换规则": str(picked.get("转换规则", "直接映射")),
                "契约版本": str(picked.get("契约版本", "v1.0")),
                "规则归属": self._contract_scope(picked),
                "权限范围": "通用共享" if self._contract_scope(picked) == SCOPE_COMMON else f"专属:{self._contract_scope(picked)}",
                "候选版本": [str(c.get("契约版本", "v1.0")) for c in group],
            })
            ordered_candidates[target_field] = [
                str(c.get("契约版本", "v1.0")) for c in group
            ]

        return {
            "window_code": str(window["窗口编号"]),
            "window_id": int(window["id"]),
            "source_code": source_code,
            "effective_at": check_ts,
            "items": items,
            "denied": denied,
            "target_versions": {item["目标字段"]: item["契约版本"] for item in items},
            "target_candidates": ordered_candidates,
        }

    def build_catalog(
        self,
        window_id: int,
        *,
        at_ts: float | None = None,
        preferred_versions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """按抽取窗口构建可用字段契约清单并冻结；同一窗口重复调用返回已冻结的同一份。

        筛选链路：窗口登记校验 → 生效时段过滤 → 权限归属核验 → 同目标字段版本选择。
        版本选择默认取有效期内最高版本（newest wins），``preferred_versions`` 可按
        目标字段显式指定（如交接期继续钉住 v1）。
        """
        window = store.find("extract_window", window_id)
        if window is None:
            raise ValueError(f"抽取窗口 {window_id} 不存在，无法核对源端窗口记录")
        window_code = str(window["窗口编号"])

        # 同一窗口/同一轮处理复用已冻结清单，保证可回查且不会中途换规则
        existing = self._find_frozen_catalog(window_code)
        if existing is not None:
            return existing

        check_ts = float(at_ts) if at_ts is not None else time.time()
        payload = self._assemble_catalog(window, check_ts, preferred_versions or {})
        catalog_code = f"CATL-{window_code}"
        payload["catalog_code"] = catalog_code
        payload["window_status"] = window.get("status", "草稿")
        payload["window_started"] = window.get("status") != "草稿"
        payload["frozen_at"] = time.time()
        payload["checksum"] = self._compute_checksum(
            json.dumps(
                {k: v for k, v in payload.items() if k != "checksum"},
                ensure_ascii=False, sort_keys=True,
            )
        )

        # 落盘冻结快照（可回查的契约集合）
        catalog_file = self._data_dir("catalogs") / f"{catalog_code}.json"
        catalog_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        # 同步登记到通用资源表，供列表/导出/动作接口消费
        entry, missing = self.create_entry({
            "清单编号": catalog_code,
            "窗口编号": window_code,
            "源端编号": str(window["源端编号"]),
            "生效时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(check_ts)),
            "清单版本": payload["checksum"][:8],
            "契约条目数": str(len(payload["items"])),
            "校验值": payload["checksum"],
        })
        if entry is not None:
            entry, _ = self.run_action(entry["id"], "冻结清单")
        payload["catalog_id"] = entry["id"] if entry else None
        payload["catalog_file"] = str(catalog_file)
        return payload

    def _find_frozen_catalog(self, window_code: str) -> dict[str, Any] | None:
        """按窗口编号查找已冻结清单快照，未找到返回 None。"""
        catalog_file = self._data_dir("catalogs") / f"CATL-{window_code}.json"
        if not catalog_file.exists():
            return None
        return json.loads(catalog_file.read_text(encoding="utf-8"))

    def get_catalog(self, window_code: str) -> dict[str, Any] | None:
        """对外暴露的清单回查入口。"""
        return self._find_frozen_catalog(window_code)

    def list_available_contracts(self, window_id: int, **kwargs: Any) -> dict[str, Any]:
        """供映射任务核对源端窗口记录与实际可用契约的查询接口。"""
        return self.build_catalog(window_id, **kwargs)

    # ------------------------------------------------------------------
    # 映射轮次：基于冻结清单转换，结果保留实际采用的来源/目标/版本
    # ------------------------------------------------------------------

    def _round_file(self, round_code: str) -> Path:
        return self._data_dir("mapping_rounds") / f"{round_code}.json"

    def _load_round(self, round_code: str) -> dict[str, Any] | None:
        path = self._round_file(round_code)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_round(self, state: dict[str, Any]) -> Path:
        path = self._round_file(state["round_code"])
        state["updated_at"] = time.time()
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path

    def start_mapping_round(
        self,
        window_id: int,
        mapping_name: str,
        *,
        record_count: int = 10,
        source_records: list[dict[str, Any]] | None = None,
        at_ts: float | None = None,
        preferred_versions: dict[str, str] | None = None,
        batch_size: int | None = None,
    ) -> dict[str, Any]:
        """在窗口生效时段内启动一个映射轮次（A/B），先冻结清单再转换首批记录。

        - 同一窗口的多个映射共用同一份冻结清单；
        - 每条结果保留实际采用的源字段、目标字段、契约编号与版本（lineage）；
        - ``batch_size`` 可限制首批处理条数，剩余部分经暂停/恢复继续；
        - 暂停后可凭 round_code 恢复，恢复绝不重新构建清单。
        """
        window = store.find("extract_window", window_id)
        if window is None:
            raise ValueError(f"抽取窗口 {window_id} 不存在")
        window_code = str(window["窗口编号"])
        source_code = str(window["源端编号"])

        catalog = self.build_catalog(window_id, at_ts=at_ts, preferred_versions=preferred_versions)

        round_code = f"MAP-{window_code}-{mapping_name}"
        existing = self._load_round(round_code)
        if existing is not None:
            # 同一轮次重复启动直接回查，不产生第二套规则
            return existing

        if source_records is None:
            source_records = self._simulate_records(source_code, count=record_count, field_prefix="ext")

        state = {
            "round_code": round_code,
            "mapping_name": mapping_name,
            "window_code": window_code,
            "source_code": source_code,
            "catalog_code": catalog["catalog_code"],
            "catalog_checksum": catalog["checksum"],
            "status": ROUND_RUNNING,
            "total_records": len(source_records),
            "processed": 0,
            "output_file": str(self._data_dir("mapped") / f"{round_code}.jsonl"),
            "lineage": [],
            "denied": catalog.get("denied", []),
            "started_at": time.time(),
        }
        self._save_round(state)
        first_batch = batch_size if batch_size is not None else len(source_records)
        return self._process_next(state, source_records, limit=first_batch)

    def _convert_value(self, value: Any, data_type: str, default: Any = "") -> Any:
        """按契约数据类型执行转换，转换失败回落到默认值，保证一轮内类型一致。"""
        if value is None:
            return default
        if data_type == "int":
            try:
                return int(value)
            except (ValueError, TypeError):
                return int(default) if str(default).lstrip("-").isdigit() else 0
        if data_type == "float":
            try:
                return float(value)
            except (ValueError, TypeError):
                try:
                    return float(default)
                except (ValueError, TypeError):
                    return 0.0
        if data_type == "bool":
            return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}
        return str(value) if not isinstance(value, (int, float, bool)) else value

    def _process_next(
        self,
        state: dict[str, Any],
        source_records: list[dict[str, Any]],
        *,
        limit: int,
    ) -> dict[str, Any]:
        """按冻结清单处理下一批记录；清单内不存在的契约一律不读不转。"""
        catalog = self._find_frozen_catalog(state["window_code"])
        if catalog is None or catalog["checksum"] != state["catalog_checksum"]:
            raise RuntimeError(f"轮次 {state['round_code']} 的冻结清单丢失或被替换，拒绝继续处理")
        items = catalog["items"]

        output_path = Path(state["output_file"])
        processed = state["processed"]
        upper = min(processed + limit, len(source_records))
        new_rows: list[dict[str, Any]] = []

        for idx in range(processed, upper):
            rec = source_records[idx]
            new_rec: dict[str, Any] = {}
            for item in items:
                # 专属权限在清单构建时已核验；这里只访问清单内的源字段，
                # 天然保证 A 轮次不会读取 B 的专属列
                value = self._convert_value(rec.get(item["源字段"]), item["数据类型"])
                new_rec[item["目标字段"]] = value
                state["lineage"].append({
                    "line_no": idx + 1,
                    "source_field": item["源字段"],
                    "target_field": item["目标字段"],
                    "contract_code": item["契约编号"],
                    "contract_version": item["契约版本"],
                    "rule_scope": item["规则归属"],
                })
            new_rec["_round"] = state["round_code"]
            new_rec["_catalog_checksum"] = state["catalog_checksum"]
            new_rows.append(new_rec)

        if new_rows:
            self._write_jsonl(output_path, new_rows)
        state["processed"] = upper
        if upper >= len(source_records):
            state["status"] = ROUND_DONE
        self._save_round(state)
        return {
            "round_code": state["round_code"],
            "mapping_name": state["mapping_name"],
            "window_code": state["window_code"],
            "source_code": state["source_code"],
            "catalog_code": state["catalog_code"],
            "catalog_checksum": state["catalog_checksum"],
            "status": state["status"],
            "processed": state["processed"],
            "total_records": state["total_records"],
            "output_file": state["output_file"],
            "adopted_versions": catalog["target_versions"],
            "denied": state["denied"],
        }

    def pause_mapping_round(self, round_code: str) -> dict[str, Any]:
        """暂停映射轮次：保留冻结清单与已处理水位，允许后续恢复。"""
        state = self._load_round(round_code)
        if state is None:
            return {"error": f"映射轮次 {round_code} 不存在"}
        if state["status"] == ROUND_DONE:
            return {"error": "轮次已完成，无需暂停", "round_code": round_code}
        state["status"] = ROUND_PAUSED
        self._save_round(state)
        return {"round_code": round_code, "status": ROUND_PAUSED, "processed": state["processed"]}

    def resume_mapping_round(
        self,
        round_code: str,
        *,
        source_records: list[dict[str, Any]] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """恢复暂停的轮次：复用同一份冻结清单与同一批源记录，禁止重建清单。"""
        state = self._load_round(round_code)
        if state is None:
            return {"error": f"映射轮次 {round_code} 不存在"}
        if state["status"] != ROUND_PAUSED:
            return {"error": f"轮次状态为 {state['status']}，仅暂停态可恢复", "round_code": round_code}

        window = next(
            (w for w in store.rows("extract_window") if str(w.get("窗口编号")) == state["window_code"]),
            None,
        )
        if window is None:
            return {"error": f"窗口 {state['window_code']} 不存在，无法恢复"}
        if source_records is None:
            source_records = self._simulate_records(
                state["source_code"], count=state["total_records"], field_prefix="ext"
            )
        state["status"] = ROUND_RUNNING
        self._save_round(state)
        remaining = state["total_records"] - state["processed"]
        return self._process_next(state, source_records, limit=limit or remaining)

    def get_round(self, round_code: str) -> dict[str, Any] | None:
        """回查映射轮次状态（含冻结清单校验值与逐字段血缘）。"""
        return self._load_round(round_code)

    # ------------------------------------------------------------------
    # 版本重算评估
    # ------------------------------------------------------------------

    def evaluate_recompute(
        self,
        window_id: int,
        previous_round_code: str,
        *,
        at_ts: float | None = None,
    ) -> dict[str, Any]:
        """对比上一轮实际采用版本与当前窗口清单选择，判定是否需要按新规则重算。

        - 上一轮采用版本在当前清单中仍是选中版本 → 无需重算；
        - 当前选中版本更新（如 v1 交接期结束后只剩/改选 v2）→ 需要重算并给出差异字段。
        """
        previous = self._load_round(previous_round_code)
        if previous is None:
            return {"error": f"历史轮次 {previous_round_code} 不存在，无法评估"}

        window = store.find("extract_window", window_id)
        if window is None:
            return {"error": f"抽取窗口 {window_id} 不存在"}

        # 重算评估必须按当前时刻重新组装临时清单，不能复用轮次冻结的旧清单
        check_ts = float(at_ts) if at_ts is not None else time.time()
        current_assembled = self._assemble_catalog(window, check_ts, {})
        current_checksum = self._compute_checksum(
            json.dumps(current_assembled, ensure_ascii=False, sort_keys=True, default=str)
        )
        previous_versions: dict[str, str] = {}
        for lineage in previous.get("lineage", []):
            previous_versions.setdefault(
                lineage["target_field"], lineage["contract_version"]
            )

        diffs: list[dict[str, str]] = []
        for target_field, current_version in current_assembled["target_versions"].items():
            old_version = previous_versions.get(target_field)
            if old_version is not None and old_version != current_version:
                diffs.append({
                    "目标字段": target_field,
                    "旧版本": old_version,
                    "当前版本": current_version,
                })

        return {
            "previous_round": previous_round_code,
            "previous_catalog_checksum": previous["catalog_checksum"],
            "current_catalog_code": f"CATL-{current_assembled['window_code']}",
            "current_catalog_checksum": current_checksum,
            "need_recompute": bool(diffs),
            "changed_fields": diffs,
            "current_versions": current_assembled["target_versions"],
            "previous_versions": previous_versions,
        }
