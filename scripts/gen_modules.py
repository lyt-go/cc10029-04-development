"""从后端领域服务定义生成前端模块元数据 src/data/modules.ts。

前端工作台是通用资源页面，模块差异（字段/状态/动作/指标）由本脚本同步，
避免手工维护两套清单。运行：python scripts/gen_modules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.services import SERVICES  # noqa: E402

NAMES: dict[str, str] = {
    "source_register": "源端登记",
    "target_register": "目标端登记",
    "field_contract": "字段契约",
    "contract_catalog": "可用字段契约清单",
    "extract_window": "增量窗口",
    "transfer_batch": "传输批次",
    "staging_area": "暂存落地",
    "checksum_verify": "校验核对",
    "arrival_reconcile": "到达对账",
    "conflict_resolve": "字段冲突",
    "resume_point": "断点续传",
    "speed_control": "速率整形",
    "reject_record": "拒收回执",
    "format_adapt": "格式适配",
    "transfer_lineage": "传输血缘",
    "history_archive": "批次归档",
    "exchange_replay": "历史重放",
    "sla_monitor": "时效监控",
    "channel_config": "通道配置",
}

DESCRIPTIONS: dict[str, str] = {
    "contract_catalog": "按抽取窗口校验规则归属与权限，冻结可回查的可用契约集合，支撑映射轮次与版本重算。",
}

DEFAULT_DESC = "跨系统数据交换平台业务模块，支持查询、登记、状态流转与清单导出。"


def ts_array(values: list[str]) -> str:
    return "[" + ", ".join(f"'{v}'" for v in values) + "]"


def main() -> None:
    lines = [
        "// 本文件由 scripts/gen_modules.py 依据后端服务定义生成，请勿手工编辑。",
        "",
        "export type ModuleMeta = {",
        "  key: string",
        "  name: string",
        "  entity: string",
        "  desc: string",
        "  fields: string[]",
        "  statuses: string[]",
        "  actions: string[]",
        "  metrics: [string, string, string]",
        "}",
        "",
        "const MODULES: Record<string, ModuleMeta> = {",
    ]
    for key, service in SERVICES.items():
        name = NAMES.get(key, key)
        statuses = list(service.STATUS_ORDER)
        metrics = [f"{name}总量", f"{statuses[1] if len(statuses) > 1 else '待处理'}量",
                   f"{statuses[-1]}量"]
        lines.append(f"  '{key}': {{")
        lines.append(f"    key: '{key}',")
        lines.append(f"    name: '{name}',")
        lines.append(f"    entity: '{service.ENTITY}',")
        lines.append(f"    desc: '{DESCRIPTIONS.get(key, DEFAULT_DESC)}',")
        lines.append(f"    fields: {ts_array(list(service.LIST_FIELDS))},")
        lines.append(f"    statuses: {ts_array(statuses)},")
        lines.append(f"    actions: {ts_array(list(service.ACTION_RULES.keys()))},")
        lines.append(f"    metrics: {ts_array(metrics)} as [string, string, string],")
        lines.append("  },")
    lines.append("}")
    lines.append("")
    lines.append("export const MODULE_KEYS = Object.keys(MODULES)")
    lines.append("")
    lines.append("export function moduleMeta(key: string): ModuleMeta {")
    lines.append("  const meta = MODULES[key]")
    lines.append("  if (!meta) {")
    lines.append("    throw new Error(`未知业务模块：${key}`)")
    lines.append("  }")
    lines.append("  return meta")
    lines.append("}")
    lines.append("")
    lines.append("export const NAV_ITEMS = MODULE_KEYS.map((key) => ({")
    lines.append("  label: MODULES[key].name,")
    lines.append("  path: `/${key}`,")
    lines.append("}))")
    lines.append("")

    target = Path(__file__).resolve().parent.parent / "frontend" / "src" / "data" / "modules.ts"
    target.write_text("\n".join(lines), encoding="utf-8")
    print(f"generated {target} with {len(SERVICES)} modules")


if __name__ == "__main__":
    main()
