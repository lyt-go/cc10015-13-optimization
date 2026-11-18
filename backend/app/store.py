"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 内部分析表（如分群对比结果表/回执表/批次表）不作为业务模块出现在概览里。
INTERNAL_TABLES = {"segment_compare_result", "segment_compare_receipt", "segment_compare_batch"}


class TransactionResidueError(RuntimeError):
    """事务残留：提交前核对不一致或上一事务未清理，当前批次整体回滚。"""


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._versions: dict[str, int] = {name: 1 for name in self._tables}
        self._created_at: dict[str, float] = {name: time.time() for name in self._tables}
        self._sequences: dict[str, int] = {}
        self._tx_open = False

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    @contextmanager
    def transaction(self, *modules: str) -> Iterator["Store"]:
        """简易事务：进入时快照涉及的表，异常退出时整体回滚。

        事务进行中（含残留未清理）不允许再开新事务；提交前由调用方核对
        一致性，发现残留即抛错，本批次写入全部回滚。
        """
        if self._tx_open:
            raise TransactionResidueError("上一事务存在残留，本批次整体回滚")
        snapshot = {name: [dict(row) for row in self.rows(name)] for name in modules}
        versions = {name: self._versions.get(name, 1) for name in modules}
        self._tx_open = True
        try:
            yield self
        except Exception:
            for name, rows in snapshot.items():
                self._tables[name] = rows
            self._versions.update(versions)
            raise
        finally:
            self._tx_open = False

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def filter_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def next_id(self, module: str) -> int:
        """单调递增的 id 序列：与数据库序列一样，事务回滚也不回收已分配的号。"""
        seq = self._sequences.get(module)
        if seq is None:
            seq = max((int(r.get("id", 0)) for r in self.rows(module)), default=0)
        seq += 1
        self._sequences[module] = seq
        return seq

    def bump_version(self, module: str) -> int:
        self._versions[module] = self._versions.get(module, 0) + 1
        return self._versions[module]

    def version(self, module: str) -> int:
        return self._versions.get(module, 1)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
