"""样本分群业务规则：提供分群人员列表供AB分析消费。"""
from __future__ import annotations

import random
import time
from typing import Any

from app.store import store

MODULE = "sample_group"
LIST_FIELDS = ["分群编号", "分群名称", "筛选条件", "样本基数", "分群日期", "更新方式", "有效天数", "负责人"]
REQUIRED_FIELDS = ["分群编号", "分群名称", "筛选条件"]
STATUS_ORDER = ["草稿", "计算中", "已就绪", "已过期"]
ACTION_RULES = {"开始计算": "计算中", "确认就绪": "已就绪", "标记过期": "已过期"}
NEGATIVE_ACTIONS = []


class SampleGroupService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("分群编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_group_members(self, group_id: int) -> list[dict[str, Any]]:
        """生成分群的模拟用户列表，供AB分析消费。

        随机数按分群播种：同一分群重复读取得到的成员明细完全一致，
        下游（如分群对比）重算时不会因为随机漂移而改变结果。
        """
        sg = self.get_entry(group_id)
        if sg is None:
            return []
        try:
            base = int(sg.get("样本基数", 1000))
        except Exception:
            base = 1000
        members: list[dict] = []
        group_type = sg.get("group_type", "control")
        rng = random.Random(f"sample_group:{group_id}:{base}:{group_type}")
        for i in range(min(base, 500)):  # 上限500避免内存爆炸
            user_id = f"U{group_id}{i:04d}"
            # 实验组用户指标值偏高，对照组偏低，形成可检测差异
            if group_type == "experiment":
                value = rng.gauss(mu=0.62, sigma=0.08)
            else:
                value = rng.gauss(mu=0.55, sigma=0.08)
            members.append({
                "user_id": user_id,
                "group_id": group_id,
                "group_type": group_type,
                "metric_value": round(max(0.0, min(1.0, value)), 4),
            })
        return members

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["created_at"] = time.time()
        store.rows(MODULE).append(entry)
        store.bump_version(MODULE)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"样本分群 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于样本分群可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.bump_version(MODULE)
        return entry, f"样本分群已{action}"
