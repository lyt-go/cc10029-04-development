"""契约清单 HTTP 接口全流程验证（urllib 标准库实现，无第三方依赖）。

前提：后端已在 127.0.0.1:8011 启动。
"""
from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:8011/api"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create(module: str, values: dict) -> dict:
    result = call("POST", f"/{module}", {"values": values})
    assert result["ok"], result
    return result["entry"]


def action(module: str, entry_id: int, name: str) -> None:
    result = call("POST", f"/{module}/{entry_id}/actions", {"action": name})
    assert result["ok"], result


def main() -> None:
    # 1. 登记 A/B 两个源端
    for code in ("HTTP-A", "HTTP-B"):
        entry = create("source_register", {
            "源端编号": code, "系统名称": code, "数据域": "订单域",
            "记录格式": "JSON", "编码方式": "UTF-8", "日增量级": "10万",
            "抽取方式": "增量抽取", "登记游标": "0",
        })
        action("source_register", entry["id"], "确认登记")

    # 2. 登记尚未开始的窗口（草稿态）
    win_a = create("extract_window", {
        "窗口编号": "EXTR-HTTP-A", "源端编号": "HTTP-A", "增量字段": "updated_at",
        "窗口起点": "2026-11-24T00:00:00", "窗口终点": "2026-11-24T23:59:59",
        "抽取条数": "4", "窗口类型": "增量抽取", "推进水位": "0",
    })
    win_b = create("extract_window", {
        "窗口编号": "EXTR-HTTP-B", "源端编号": "HTTP-B", "增量字段": "updated_at",
        "窗口起点": "2026-11-24T00:00:00", "窗口终点": "2026-11-24T23:59:59",
        "抽取条数": "4", "窗口类型": "增量抽取", "推进水位": "0",
    })

    # 3. 发布规则：v1/v2 并存、通用、A/B 专属
    rules = [
        ("ext_amount", "dst_amount", "string", "v1.0", "通用", 1_000_000, 2_000_000, "HTTP-V1"),
        ("ext_amount", "dst_amount", "int", "v2.0", "通用", 1_500_000, "", "HTTP-V2"),
        ("ext_a", "dst_a", "string", "v1.0", "通用", "", "", "HTTP-CMN"),
        ("ext_secret_a", "dst_secret_a", "string", "v1.0", "HTTP-A", "", "", "HTTP-ONLY-A"),
        ("ext_secret_b", "dst_secret_b", "string", "v1.0", "HTTP-B", "", "", "HTTP-ONLY-B"),
    ]
    for sf, tf, dt, ver, scope, st, en, code in rules:
        entry = create("field_contract", {
            "契约编号": code, "源字段": sf, "目标字段": tf, "数据类型": dt,
            "转换规则": "直接映射", "必填标记": "是", "默认值": "", "契约版本": ver,
            "适用源端": scope, "生效开始": st, "生效结束": en, "撤回标记": "否",
        })
        action("field_contract", entry["id"], "确认生效")

    # 4. 构建 A 的可用契约清单
    catalog = call("POST", "/contract-catalog/build", {"window_id": win_a["id"], "at_ts": 1_500_000})
    assert catalog["window_started"] is False
    assert catalog["target_versions"]["dst_amount"] == "v2.0"
    denied = {d["契约编号"] for d in catalog["denied"]}
    assert denied == {"HTTP-ONLY-B"}, denied
    print("[OK] 清单构建: 版本 v2.0, 拒绝 B 专属:", sorted(denied))

    # 重复构建必须复用冻结清单
    again = call("POST", "/contract-catalog/build", {"window_id": win_a["id"], "at_ts": 9_999_999})
    assert again["checksum"] == catalog["checksum"]
    fetched = call("GET", "/contract-catalog/by-window/EXTR-HTTP-A")
    assert fetched["checksum"] == catalog["checksum"]
    print("[OK] 同窗口复用冻结清单且可回查")

    # 5. 启动 A 轮次（首批 2 条）→ 暂停 → 恢复
    records = [
        {"ext_a": f"a{i}", "ext_amount": str(i * 3),
         "ext_secret_a": f"as{i}", "ext_secret_b": f"bs{i}"}
        for i in range(4)
    ]
    round_a = call("POST", "/contract-catalog/rounds/start", {
        "window_id": win_a["id"], "mapping_name": "A",
        "source_records": records, "at_ts": 1_500_000, "batch_size": 2,
    })
    assert round_a["processed"] == 2 and round_a["adopted_versions"]["dst_amount"] == "v2.0"
    paused = call("POST", f"/contract-catalog/rounds/{round_a['round_code']}/pause", {})
    assert paused["status"] == "已暂停"
    resumed = call("POST", f"/contract-catalog/rounds/{round_a['round_code']}/resume", {})
    assert resumed["status"] == "已完成" and resumed["processed"] == 4
    assert resumed["catalog_checksum"] == catalog["checksum"]
    print("[OK] A 轮次暂停-恢复使用同一冻结清单")

    # 6. B 轮次读不到 A 的专属列
    round_b = call("POST", "/contract-catalog/rounds/start", {
        "window_id": win_b["id"], "mapping_name": "B",
        "source_records": records, "at_ts": 1_500_000,
    })
    state_b = call("GET", f"/contract-catalog/rounds/{round_b['round_code']}")
    b_targets = {x["target_field"] for x in state_b["lineage"]}
    assert "dst_secret_b" in b_targets and "dst_secret_a" not in b_targets
    print("[OK] B 专属权限隔离: ", sorted(b_targets))

    # 7. 重算评估：默认 v2 无需重算
    ev = call("POST", "/contract-catalog/evaluate-recompute", {
        "window_id": win_a["id"],
        "previous_round_code": round_a["round_code"],
        "at_ts": 2_500_000,
    })
    assert ev["need_recompute"] is False
    print("[OK] v2 首轮交接后无需重算")

    # 8. 通用资源路由可列出清单
    listed = call("GET", "/contract_catalog?page=1&size=5")
    assert listed["total"] >= 1
    print(f"[OK] 通用资源路由列表 total={listed['total']}")


if __name__ == "__main__":
    main()
    print("HTTP 全流程验证通过")
