"""路径推理领域服务。

在通用资源骨架之外承载边证据门槛相关的真实领域能力：

1. 关系边证据登记与补证（保留完整证据变更历史，旧核验记录可回查）；
2. 依据推理路径上的关系证据逐边计算路径评分，置信阈值按边校验，
   并在最大跳数内衡量路径强弱，未达阈值的边不能支撑已确认推荐；
3. 核验待办的领用与结论提交：同一推理任务同一时刻只允许一份有效领用，
   领用时把实际领用的边与核定门槛快照写回核验待办。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 边证据门槛领域使用的内存表
EDGE_TABLE = "path_edge"
CASE_TABLE = "path_case"
SCORE_TABLE = "path_score"
VERIFY_TABLE = "path_verification"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


class PathReasonService(BaseModuleService):
    MODULE = "path_reason"
    ENTITY = "推理任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "起始实体", "目标实体", "推理模式", "最大跳数", "路径数量", "置信阈值", "推理耗时"]
    REQUIRED_FIELDS = ["任务编号", "起始实体", "目标实体"]
    STATUS_ORDER = ["待推理", "推理中", "已完成", "推理超时"]
    ACTION_RULES = {"启动推理": "推理中", "取消推理": "已完成", "标记超时": "推理超时"}
    NEGATIVE_ACTIONS = []

    # ===== 关系边证据 =====
    def list_edges(self) -> list[dict[str, Any]]:
        return list(store.rows(EDGE_TABLE))

    def find_edge(self, edge_id: str) -> dict[str, Any] | None:
        for edge in store.rows(EDGE_TABLE):
            if edge.get("edge_id") == edge_id:
                return edge
        return None

    def upsert_edge(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记一条带关系证据的边；edge_id 已存在时按补证处理并保留历史。"""
        source = str(values.get("source") or "").strip()
        target = str(values.get("target") or "").strip()
        if not source or not target:
            return None, "关系边必须提供起点实体和终点实体"
        try:
            evidence = float(values.get("evidence"))
        except (TypeError, ValueError):
            return None, "关系证据必须是 0~1 之间的数值"
        if not 0.0 <= evidence <= 1.0:
            return None, "关系证据必须落在 0~1 区间"
        edge_id = str(values.get("edge_id") or "").strip() or f"EDGE-{_next_id(EDGE_TABLE):04d}"
        existing = self.find_edge(edge_id)
        if existing is not None:
            existing["evidence_history"].append(
                {"value": existing["evidence"], "at": _now(), "reason": "补证前快照"})
            existing["evidence"] = evidence
            existing["relation"] = str(values.get("relation") or existing.get("relation") or "")
            existing["evidence_history"].append(
                {"value": evidence, "at": _now(), "reason": str(values.get("reason") or "重新登记")})
            return existing, f"关系边 {edge_id} 证据已更新，历史快照已保留"
        edge = {
            "id": _next_id(EDGE_TABLE),
            "edge_id": edge_id,
            "source": source,
            "target": target,
            "relation": str(values.get("relation") or ""),
            "evidence": evidence,
            "evidence_history": [{"value": evidence, "at": _now(), "reason": "初始登记"}],
        }
        store.rows(EDGE_TABLE).append(edge)
        return edge, f"关系边 {edge_id} 已登记，关系证据 {evidence:g}"

    def supplement_edge(
        self, edge_id: str, evidence: float, reason: str = ""
    ) -> tuple[dict[str, Any] | None, str]:
        """补齐边证据：旧值进入证据历史，核验待办里的领用快照不受影响。"""
        edge = self.find_edge(edge_id)
        if edge is None:
            return None, f"关系边 {edge_id} 不存在"
        try:
            evidence = float(evidence)
        except (TypeError, ValueError):
            return None, "关系证据必须是 0~1 之间的数值"
        if not 0.0 <= evidence <= 1.0:
            return None, "关系证据必须落在 0~1 区间"
        edge["evidence_history"].append(
            {"value": edge["evidence"], "at": _now(), "reason": reason or "补证前快照"})
        edge["evidence"] = evidence
        edge["evidence_history"].append(
            {"value": evidence, "at": _now(), "reason": reason or "证据补齐"})
        return edge, f"关系边 {edge_id} 证据已由历史值补齐到 {evidence:g}"

    # ===== 推理任务（门槛场景） =====
    def list_cases(self) -> list[dict[str, Any]]:
        return list(store.rows(CASE_TABLE))

    def get_case(self, case_id: int) -> dict[str, Any] | None:
        return store.find(CASE_TABLE, case_id)

    def create_case(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        source = str(values.get("source") or "").strip()
        target = str(values.get("target") or "").strip()
        if not source or not target:
            return None, "推理任务必须提供起始实体和目标实体"
        try:
            max_hops = int(values.get("max_hops", 3))
            threshold = float(values.get("threshold", 0.8))
        except (TypeError, ValueError):
            return None, "最大跳数必须为整数，置信阈值必须为数值"
        if max_hops < 1:
            return None, "最大跳数必须大于等于 1"
        if not 0.0 <= threshold <= 1.0:
            return None, "置信阈值必须落在 0~1 区间"
        case = {
            "id": _next_id(CASE_TABLE),
            "case_no": f"PATH-CASE-{_next_id(CASE_TABLE):04d}",
            "source": source,
            "target": target,
            "max_hops": max_hops,
            "threshold": threshold,
            "status": "待推理",
            "scored_at": "",
        }
        store.rows(CASE_TABLE).append(case)
        return case, f"推理任务 {case['case_no']} 已建立，核定门槛 {threshold:g}，跳数上限 {max_hops}"

    def _enumerate_paths(self, case: dict[str, Any]) -> list[list[str]]:
        """在最大跳数内枚举起点到终点的全部简单路径（关系边按有向边处理）。"""
        adjacency: dict[str, list[dict[str, Any]]] = {}
        for edge in store.rows(EDGE_TABLE):
            adjacency.setdefault(edge["source"], []).append(edge)

        results: list[list[str]] = []
        stack: list[tuple[str, list[str], set[str]]] = [
            (case["source"], [case["source"]], {case["source"]})]
        while stack:
            node, path, visited = stack.pop()
            if len(path) - 1 >= case["max_hops"]:
                continue
            for edge in adjacency.get(node, []):
                nxt = edge["target"]
                if nxt in visited:
                    continue
                next_path = [*path, nxt]
                if nxt == case["target"]:
                    results.append(next_path)
                else:
                    stack.append((nxt, next_path, {*visited, nxt}))
        results.sort(key=lambda item: (len(item) - 1, "->".join(item)))
        return results

    def _edge_between(self, source: str, target: str) -> dict[str, Any] | None:
        for edge in store.rows(EDGE_TABLE):
            if edge["source"] == source and edge["target"] == target:
                return edge
        return None

    def score_paths(self, case_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        """逐边校验关系证据并计算可用路径评分数据。

        - 置信阈值按关系证据逐边校验，任一弱边即判定路径不可用；
        - 路径强弱由瓶颈分（最弱边证据）与平均证据共同衡量；
        - 超过跳数上限的路径不参与评分。
        """
        case = self.get_case(case_id)
        if case is None:
            return None, f"推理任务 {case_id} 不存在"
        case["status"] = "推理中"
        node_paths = self._enumerate_paths(case)

        previous = [row for row in store.rows(SCORE_TABLE)
                    if row.get("case_id") == case_id and row.get("active", True)]
        next_version = max((int(row.get("version", 1)) for row in
                            store.rows(SCORE_TABLE) if row.get("case_id") == case_id), default=0) + 1
        for row in previous:
            row["active"] = False

        cards: list[dict[str, Any]] = []
        for nodes in node_paths:
            edge_ids: list[str] = []
            evidences: list[float] = []
            checks: list[dict[str, Any]] = []
            missing = False
            for src, dst in zip(nodes, nodes[1:]):
                edge = self._edge_between(src, dst)
                if edge is None:
                    missing = True
                    break
                edge_ids.append(edge["edge_id"])
                evidences.append(float(edge["evidence"]))
                checks.append({
                    "edge_id": edge["edge_id"],
                    "source": src,
                    "target": dst,
                    "evidence": float(edge["evidence"]),
                    "passed": float(edge["evidence"]) >= float(case["threshold"]),
                })
            if missing:
                continue
            weak_edges = [item["edge_id"] for item in checks if not item["passed"]]
            min_evidence = min(evidences)
            avg_evidence = sum(evidences) / len(evidences)
            within_hop = (len(nodes) - 1) <= int(case["max_hops"])
            card = {
                "id": _next_id(SCORE_TABLE),
                "case_id": case_id,
                "path_key": "->".join(nodes),
                "nodes": nodes,
                "hops": len(nodes) - 1,
                "edge_ids": edge_ids,
                "edge_evidences": evidences,
                "edge_checks": checks,
                "min_evidence": round(min_evidence, 4),
                "avg_evidence": round(avg_evidence, 4),
                # 瓶颈分：路径上最弱的一条边决定整条路径能否支撑推荐
                "path_score": round(min_evidence, 4),
                "within_hop_limit": within_hop,
                "weak_edges": weak_edges,
                "usable": within_hop and not weak_edges,
                "threshold": float(case["threshold"]),
                "version": next_version,
                "active": True,
                "scored_at": _now(),
            }
            store.rows(SCORE_TABLE).append(card)
            cards.append(card)

        cards.sort(key=lambda item: (-int(item["usable"]), item["hops"], -item["path_score"]))
        case["status"] = "已完成"
        case["scored_at"] = _now()
        usable_count = sum(1 for card in cards if card["usable"])
        return cards, f"已计算 {len(cards)} 条路径评分，其中 {usable_count} 条达到证据门槛可用"

    def get_scores(self, case_id: int, *, active_only: bool = True) -> list[dict[str, Any]]:
        rows = store.rows(SCORE_TABLE)
        if active_only:
            rows = [row for row in rows if row.get("case_id") == case_id and row.get("active", True)]
        else:
            rows = [row for row in rows if row.get("case_id") == case_id]
        return sorted(rows, key=lambda item: (-int(item.get("active", True)),
                                              item.get("version", 1), item.get("id", 0)))

    def find_active_score(self, case_id: int, path_key: str) -> dict[str, Any] | None:
        for row in store.rows(SCORE_TABLE):
            if (row.get("case_id") == case_id and row.get("path_key") == path_key
                    and row.get("active", True)):
                return row
        return None

    # ===== 核验待办：领用 / 冲突 / 结论 =====
    def _open_todo(self, case_id: int) -> dict[str, Any] | None:
        for todo in store.rows(VERIFY_TABLE):
            if todo.get("case_id") == case_id and todo.get("status") in ("核验中", "已确认"):
                return todo
        return None

    def claim_path(
        self, case_id: int, path_key: str, reviewer: str
    ) -> tuple[dict[str, Any] | None, str]:
        """领用一条可用路径做核验。

        同一推理任务同时只允许一份有效领用；领用时把实际领用的边与核定门槛
        写回核验待办，后续补证不会覆盖领用快照。
        """
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            return None, "领用核验必须提供领取人"
        case = self.get_case(case_id)
        if case is None:
            return None, f"推理任务 {case_id} 不存在"
        card = self.find_active_score(case_id, path_key)
        if card is None:
            return None, f"路径 {path_key} 不在当前评分结果中，请先完成路径评分"
        if not card["usable"]:
            weak = "、".join(card["weak_edges"]) or "存在弱边"
            return None, f"路径 {path_key} 存在未达阈值的边（{weak}），不能支撑领用核验"

        open_todo = self._open_todo(case_id)
        if open_todo is not None:
            reason = (f"已有有效核验结论（{open_todo['reviewer']}）"
                      if open_todo["status"] == "已确认"
                      else f"路径 {open_todo['path_key']} 已由 {open_todo['reviewer']} 领用核验中")
            open_todo.setdefault("attempts", []).append({
                "reviewer": reviewer,
                "path_key": path_key,
                "at": _now(),
                "result": "冲突驳回",
                "reason": reason,
            })
            return None, f"领用冲突：{reason}，同一推理任务一次只能提交一份有效结论"

        todo = {
            "id": _next_id(VERIFY_TABLE),
            "todo_no": f"VERIF-{_next_id(VERIFY_TABLE):04d}",
            "case_id": case_id,
            "path_key": path_key,
            "reviewer": reviewer,
            "status": "核验中",
            "claimed_at": _now(),
            "concluded_at": "",
            "conclusion": None,
            # 写回核验待办：实际领用的边 + 核定门槛 + 领用时刻路径证据快照
            "used_edges": list(card["edge_ids"]),
            "threshold_snapshot": float(case["threshold"]),
            "path_snapshot": {
                "path_key": card["path_key"],
                "nodes": list(card["nodes"]),
                "edge_ids": list(card["edge_ids"]),
                "edge_evidences": list(card["edge_evidences"]),
                "path_score": card["path_score"],
            },
            "attempts": [],
        }
        store.rows(VERIFY_TABLE).append(todo)
        return todo, (f"领取人 {reviewer} 已领用路径 {path_key}，实际领用边 "
                      f"{'、'.join(todo['used_edges'])} 与核定门槛 "
                      f"{todo['threshold_snapshot']:g} 已写回核验待办 {todo['todo_no']}")

    def submit_conclusion(
        self, todo_id: int, reviewer: str, passed: bool, comment: str = ""
    ) -> tuple[dict[str, Any] | None, str]:
        """提交核验结论：只有待办领取人可提交，且一份待办只能形成一份有效结论。"""
        todo = store.find(VERIFY_TABLE, todo_id)
        if todo is None:
            return None, f"核验待办 {todo_id} 不存在"
        if todo["status"] == "已确认":
            return None, "该核验待办已存在有效结论，不能重复提交"
        if str(reviewer or "").strip() != todo["reviewer"]:
            todo.setdefault("attempts", []).append({
                "reviewer": str(reviewer or "").strip(),
                "path_key": todo["path_key"],
                "at": _now(),
                "result": "提交驳回",
                "reason": "非该待办领取人，无权提交结论",
            })
            return None, f"仅领取人 {todo['reviewer']} 可提交本待办结论，提交已驳回"
        todo["conclusion"] = {
            "passed": bool(passed),
            "comment": str(comment or ""),
            "submitted_at": _now(),
            "adopted_path": todo["path_key"],
            "used_edges": list(todo["used_edges"]),
            "threshold_snapshot": todo["threshold_snapshot"],
        }
        todo["status"] = "已确认"
        todo["concluded_at"] = _now()
        return todo, f"核验待办 {todo['todo_no']} 已提交唯一有效结论，采用路径 {todo['path_key']}"

    def list_verifications(self, case_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(VERIFY_TABLE) if row.get("case_id") == case_id]

    def get_verification(self, todo_id: int) -> dict[str, Any] | None:
        return store.find(VERIFY_TABLE, todo_id)

    # ===== 跨模块数据流实现（保留原链路 L10 / L11） =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        # 链路 L10: path_reason → query_cache（推理结果写入查询缓存）
        if entry.get("status") == "已完成":
            cache_msg = self._push_to_query_cache(entry)
            message += "；" + cache_msg
        # 链路 L11: path_reason → reco_engine（推理路径推送推荐引擎）
        if entry.get("status") == "已完成":
            reco_msg = self._push_to_reco_engine(entry)
            message += "；" + reco_msg
        return entry, message

    def _push_to_query_cache(self, entry: dict[str, Any]) -> str:
        rows = store.rows("query_cache")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        qc_entry: dict[str, Any] = {
            "id": next_id,
            "status": "有效",
            "pending": True,
            "abnormal": False,
            "条目编号": f"QUER-PATH-{next_id:04d}",
            "查询模板": f"path_{entry.get('起始实体','')}_to_{entry.get('目标实体','')}",
            "结果哈希": f"reasoned_{entry.get('路径数量','')}",
            "命中次数": "0",
            "TTL秒数": "7200",
            "存储大小": "2KB",
            "最后命中": "",
            "缓存状态": "fresh",
        }
        rows.append(qc_entry)
        return f"推理结果已缓存 {qc_entry['条目编号']}"

    def _push_to_reco_engine(self, entry: dict[str, Any]) -> str:
        rows = store.rows("reco_engine")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        reco_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "规则编号": f"RECO-PATH-{next_id:04d}",
            "推荐场景": f"路径推理关联推荐",
            "源实体": str(entry.get("起始实体", "")),
            "推荐算法": f"path-rank-{entry.get('推理模式','')}",
            "候选数量": int(entry.get("路径数量", 10)) + 5,
            "过滤条件": str(entry.get("置信阈值", "")),
            "命中次数": "0",
            "采纳率": "0%",
        }
        rows.append(reco_entry)
        return f"路径推理结果推送推荐引擎 {reco_entry['规则编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["query_cache", "reco_engine"]
        return base
