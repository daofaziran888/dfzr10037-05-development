"""推荐引擎领域服务。

通用资源骨架之外的推荐资格逻辑：

1. 从路径推理产出的可用路径评分数据生成推荐候选；未达置信阈值的弱边路径
   不具备候选使用资格；
2. 同一源实体到同一目标实体无论存在多少条合格路径，只算一次目标命中，
   候选数量按去重后的目标计；
3. 发起核验领用时，把实际领用的边与核定门槛写回路径推理核验待办。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.path_reason import (
    CASE_TABLE,
    SCORE_TABLE,
    PathReasonService,
)
from app.store import store

RECO_CANDIDATE_TABLE = "reco_candidate"


class RecoEngineService(BaseModuleService):
    MODULE = "reco_engine"
    ENTITY = "推荐规则"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["规则编号", "推荐场景", "源实体", "推荐算法", "候选数量", "过滤条件", "命中次数", "采纳率"]
    REQUIRED_FIELDS = ["规则编号", "推荐场景", "源实体"]
    STATUS_ORDER = ["草稿", "已生效", "效果差", "已停用"]
    ACTION_RULES = {"启效规则": "已生效", "标记差效": "效果差", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def _active_scores(self, case_id: int) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(SCORE_TABLE)
            if row.get("case_id") == case_id and row.get("active", True)
        ]

    def generate_candidates(self, case_id: int) -> tuple[dict[str, Any] | None, str]:
        """从可用路径评分数据生成候选，并同步更新推荐候选的使用资格。"""
        case = store.find(CASE_TABLE, case_id)
        if case is None:
            return None, f"推理任务 {case_id} 不存在，请先建立推理任务"
        scores = self._active_scores(case_id)
        if not scores:
            return None, "推理任务尚未产出路径评分数据，无法生成推荐候选"

        # 按目标实体去重：重复提交同源同目标的推理只算一次目标命中，
        # 不能因为多一条路径而提升候选数量。
        qualified_by_target: dict[str, dict[str, Any]] = {}
        rejected_paths: list[dict[str, Any]] = []
        for card in scores:
            target = card["nodes"][-1]
            if card["usable"]:
                holder = qualified_by_target.setdefault(target, {
                    "target": target,
                    "path_keys": [],
                    "best_path": card["path_key"],
                    "best_score": float(card["path_score"]),
                })
                holder["path_keys"].append(card["path_key"])
                if float(card["path_score"]) > holder["best_score"]:
                    holder["best_score"] = float(card["path_score"])
                    holder["best_path"] = card["path_key"]
            else:
                rejected_paths.append({
                    "path_key": card["path_key"],
                    "target": target,
                    "weak_edges": list(card["weak_edges"]),
                    "min_evidence": card["min_evidence"],
                    "threshold": card["threshold"],
                    "eligible": False,
                })

        # 同一推理任务重复生成按幂等处理：先清掉旧候选，再按最新评分重建
        table = store.rows(RECO_CANDIDATE_TABLE)
        for row in [row for row in table if row.get("case_id") == case_id]:
            table.remove(row)

        candidates: list[dict[str, Any]] = []
        for target, holder in sorted(qualified_by_target.items()):
            candidate = {
                "id": len(table) + 1,
                "case_id": case_id,
                "source": case["source"],
                "target": target,
                "eligible": True,
                "supporting_path_count": len(holder["path_keys"]),
                "supporting_paths": list(holder["path_keys"]),
                "adopted_path": holder["best_path"],
                "path_score": round(holder["best_score"], 4),
                "threshold": float(case["threshold"]),
                "hit_once": True,
            }
            table.append(candidate)
            candidates.append(candidate)

        result = {
            "case_id": case_id,
            "source": case["source"],
            "threshold": float(case["threshold"]),
            "candidate_count": len(candidates),
            "candidates": candidates,
            "rejected_paths": rejected_paths,
            "dedupe_note": "同源同目标只计一次目标命中，多路径不增加候选数量",
        }
        return result, (f"已从可用路径评分生成 {len(candidates)} 个去重候选；"
                        f"{len(rejected_paths)} 条路径因弱边未获使用资格")

    def list_candidates(self, case_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(RECO_CANDIDATE_TABLE)
        if case_id is not None:
            rows = [row for row in rows if row.get("case_id") == case_id]
        return rows

    def claim_for_verification(
        self, case_id: int, path_key: str, reviewer: str
    ) -> tuple[dict[str, Any] | None, str]:
        """推荐侧发起核验领用，实际领用边与核定门槛由路径推理写回核验待办。"""
        return PathReasonService().claim_path(case_id, path_key, reviewer)
