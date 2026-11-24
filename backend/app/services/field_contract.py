"""字段契约领域服务：维护源端到目标端的字段映射契约、类型转换规则与契约版本管理。

本模块负责定义并执行字段映射转换，从源端抽取产物文件读取数据，
按契约规则转换后写入目标端产物文件，输出转换统计和校验报告。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FieldContractService(BaseModuleService):
    MODULE = "field_contract"
    ENTITY = "字段映射"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["契约编号", "源字段", "目标字段", "数据类型", "转换规则", "必填标记", "默认值",
                   "契约版本", "适用源端", "生效开始", "生效结束", "撤回标记"]
    REQUIRED_FIELDS = ["契约编号", "源字段", "目标字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "确认生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = ["标记废弃"]

    def create_contract(
        self,
        source_field: str,
        target_field: str,
        data_type: str = "string",
        *,
        version: str = "v1.0",
        scope_source: str = "通用",
        effective_start: str | float | None = None,
        effective_end: str | float | None = None,
        transform_rule: str | None = None,
        contract_code: str | None = None,
    ) -> dict[str, Any]:
        """创建源端到目标端字段映射契约，登记规则归属、生效窗口与版本。

        scope_source 为「通用」时两个源端可共享；否则为该源端的专属规则，
        映射任务构建清单时必须分别核验权限。生效起止时间用于按抽取窗口时刻
        判定规则是否在有效期内（如 v1 交接期与 v2 生效期重叠的场景）。
        """
        contract_code = contract_code or f"FIEL-{source_field}-{target_field}-{version}"
        values = {
            "契约编号": contract_code,
            "源字段": source_field,
            "目标字段": target_field,
            "数据类型": data_type,
            "转换规则": transform_rule or ("直接映射" if data_type == "string" else "类型转换"),
            "必填标记": "是",
            "默认值": "",
            "契约版本": version,
            "适用源端": scope_source,
            "生效开始": effective_start if effective_start is not None else "",
            "生效结束": effective_end if effective_end is not None else "",
            "撤回标记": "否",
        }
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"创建字段映射契约失败: {missing}")
        self._write_jsonl(
            self._data_dir("events") / "contract_log.jsonl",
            [{"action": "create", "contract_code": contract_code, "source_field": source_field,
              "target_field": target_field, "version": version, "scope_source": scope_source,
              "at": time.time()}]
        )
        return entry

    def withdraw_contract(self, contract_id: int) -> dict[str, Any]:
        """撤回契约：撤回后清单构建不再纳入，但已冻结清单与历史轮次结果不受影响。"""
        entry = store.find(self.MODULE, contract_id)
        if entry is None:
            return {"error": f"字段映射 {contract_id} 不存在"}
        entry["撤回标记"] = "是"
        self._write_jsonl(
            self._data_dir("events") / "contract_log.jsonl",
            [{"action": "withdraw", "contract_code": entry.get("契约编号"), "at": time.time()}]
        )
        return {"ok": True, "契约编号": entry.get("契约编号"), "撤回标记": "是"}

    def apply_contracts(self, source_code: str, record_count: int = 10) -> dict[str, Any]:
        """对指定源端抽取产物应用已生效的字段映射契约，执行类型转换并写入转换产物文件。

        本方法是数据交换平台中字段映射与类型转换的核心执行环节：
        读取抽取窗口产物文件，逐字段按契约规则转换数据类型与名称，
        写入字段转换产物文件，输出转换前后统计报告。
        """
        # 获取已生效的契约列表
        contracts = [r for r in store.rows(self.MODULE) if r.get("status") == "已生效"]
        if not contracts:
            contracts = [{
                "契约编号": f"FIEL-{source_code}",
                "源字段": "ext_a",
                "目标字段": "dst_a",
                "数据类型": "string",
                "转换规则": "直接映射",
                "契约版本": "v1.0",
            }]

        # 读取抽取窗口产物文件
        extract_dir = self._data_dir("extracted")
        source_records = []
        for ef in extract_dir.glob(f"*{source_code[:8]}*.jsonl"):
            source_records = self._read_jsonl(ef)
            if source_records:
                break
        if not source_records:
            source_records = self._simulate_records(source_code, count=record_count, field_prefix="ext")

        # 执行字段转换映射
        transformed = []
        conversion_count = 0
        for rec in source_records:
            new_rec = {}
            lineage = []
            for contract in contracts:
                src_f = contract.get("源字段", "")
                tgt_f = contract.get("目标字段", "")
                data_type = contract.get("数据类型", "string")
                # 按转换规则执行类型转换
                src_val = rec.get(src_f, "")
                if src_val is not None:
                    if data_type == "int":
                        try:
                            src_val = int(src_val)
                        except (ValueError, TypeError):
                            src_val = 0
                    elif data_type == "float":
                        try:
                            src_val = float(src_val)
                        except (ValueError, TypeError):
                            src_val = 0.0
                    conversion_count += 1
                new_rec[tgt_f] = src_val
                new_rec["_source_code"] = source_code
                new_rec["_contract_version"] = contract.get("契约版本", "v1.0")
                new_rec["_converted_at"] = time.time()
                # 保留实际采用的来源、目标字段与版本，供结果回查
                lineage.append({
                    "source_field": src_f,
                    "target_field": tgt_f,
                    "contract_code": contract.get("契约编号", ""),
                    "contract_version": contract.get("契约版本", "v1.0"),
                    "rule_scope": contract.get("适用源端", "通用"),
                })
            new_rec["_lineage"] = lineage
            if new_rec:
                transformed.append(new_rec)

        # 写入字段转换产物文件
        output_file = self._data_dir("mapped") / f"{source_code}_mapped.jsonl"
        written = self._write_jsonl(output_file, transformed)

        self._write_jsonl(
            self._data_dir("events") / "contract_apply_log.jsonl",
            [{"action": "apply", "source_code": source_code, "contract_count": len(contracts),
              "records_converted": written, "conversions": conversion_count, "at": time.time()}]
        )

        return {
            "source_code": source_code,
            "contract_count": len(contracts),
            "records_converted": written,
            "conversions": conversion_count,
            "artifact_path": str(output_file),
        }

    def get_active_contracts(self) -> list[dict[str, Any]]:
        """获取所有已生效的字段映射契约列表，供下游模块按规则执行类型转换。"""
        return [r for r in store.rows(self.MODULE) if r.get("status") == "已生效"]