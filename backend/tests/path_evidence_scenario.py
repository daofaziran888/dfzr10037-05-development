"""边证据门槛端到端场景核对（纯服务层，内存仓库，无外部依赖）。

场景：
  S→T 两条路径：两跳 S→A→T（一条边 0.7 低于门槛 0.8）；
  三跳绕行 S→B→C→T（各边 0.9 全部合格）。
  推荐人员先领取绕行路径核验；提交前短路径补证（0.7→0.85），
  再从另一处尝试领取并提交，核对领用冲突、唯一结论与 T 只算一个候选。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.path_reason import PathReasonService  # noqa: E402
from app.services.reco_engine import RecoEngineService  # noqa: E402
from app.store import store  # noqa: E402

THRESHOLD = 0.8
MAX_HOPS = 3

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" —— {detail}" if detail else ""))
    if not condition:
        failures.append(f"{label} {detail}")


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# 清掉同领域表，保证脚本可重复执行
for table in ("path_edge", "path_case", "path_score", "path_verification", "reco_candidate"):
    store._tables.pop(table, None)

path_svc = PathReasonService()
reco_svc = RecoEngineService()

section("步骤1 建立关系边：门槛 0.8；两跳路径含 0.7 弱边，三跳绕行各边 0.9")
edges = [
    ("E-SA", "S", "A", 0.90),  # 两跳路径边1
    ("E-AT", "A", "T", 0.70),  # 两跳路径边2：低于门槛
    ("E-SB", "S", "B", 0.90),  # 三跳绕行
    ("E-BC", "B", "C", 0.90),
    ("E-CT", "C", "T", 0.90),
]
for edge_id, src, dst, ev in edges:
    edge, msg = path_svc.upsert_edge(
        {"edge_id": edge_id, "source": src, "target": dst, "evidence": ev,
         "relation": f"{src}-{dst}"})
    print("  ", msg)
check("关系边全部登记成功", len(path_svc.list_edges()) == 5)

section("步骤2 建立 S→T 推理任务：置信阈值 0.8，跳数上限 3")
case, msg = path_svc.create_case(
    {"source": "S", "target": "T", "max_hops": MAX_HOPS, "threshold": THRESHOLD})
print("  ", msg)
case_id = case["id"]
check("任务门槛为 0.8", case["threshold"] == 0.8)
check("任务跳数上限为 3", case["max_hops"] == 3)

section("步骤3 逐边校验并计算路径评分")
scores, msg = path_svc.score_paths(case_id)
print("  ", msg)
for card in scores:
    print(f"   路径 {card['path_key']} | 跳数 {card['hops']} | 瓶颈分 {card['path_score']} "
          f"| 弱边 {card['weak_edges'] or '无'} | 可用 {card['usable']}")
short = next(c for c in scores if c["path_key"] == "S->A->T")
detour = next(c for c in scores if c["path_key"] == "S->B->C->T")
check("共枚举到两条路径", len(scores) == 2)
check("两跳路径在跳数上限内", short["within_hop_limit"] is True and short["hops"] == 2)
check("两跳路径因 0.7 弱边不可用", short["usable"] is False and short["weak_edges"] == ["E-AT"])
check("弱边逐边校验结果为不通过",
      next(chk for chk in short["edge_checks"] if chk["edge_id"] == "E-AT")["passed"] is False)
check("三跳绕行在跳数上限内且各边合格",
      detour["usable"] is True and detour["hops"] == 3 and detour["weak_edges"] == [])
check("三跳路径瓶颈分为 0.9", detour["path_score"] == 0.9)

section("步骤4 推荐引擎从可用路径评分生成候选并更新使用资格")
reco, msg = reco_svc.generate_candidates(case_id)
print("  ", msg)
print(f"   候选数量 {reco['candidate_count']}，拒绝路径 "
      f"{[p['path_key'] for p in reco['rejected_paths']]}")
check("弱边路径不具备使用资格",
      len(reco["rejected_paths"]) == 1 and reco["rejected_paths"][0]["path_key"] == "S->A->T")
check("只有三跳绕行支撑的 T 一个候选", reco["candidate_count"] == 1)
check("候选目标为 T 且标记合格",
      reco["candidates"][0]["target"] == "T" and reco["candidates"][0]["eligible"] is True)

section("步骤5 推荐人员领取三跳绕行路径核验，实际领用边与门槛写回待办")
todo, msg = path_svc.claim_path(case_id, "S->B->C->T", "推荐人员-甲")
print("  ", msg)
todo_id = todo["id"]
check("待办状态为核验中", todo["status"] == "核验中")
check("实际领用的三条边已写回", todo["used_edges"] == ["E-SB", "E-BC", "E-CT"])
check("核定门槛 0.8 已快照写回", todo["threshold_snapshot"] == 0.8)
check("领用时刻路径证据已快照",
      todo["path_snapshot"]["edge_evidences"] == [0.9, 0.9, 0.9])
check("新领取人可查到本次采用的路径",
      path_svc.get_verification(todo_id)["path_snapshot"]["path_key"] == "S->B->C->T")

section("步骤6 提交结果前短路径补证：E-AT 由 0.7 补齐到 0.85")
edge, msg = path_svc.supplement_edge("E-AT", 0.85, reason="短路径补齐证据")
print("  ", msg)
check("当前证据已更新为 0.85", path_svc.find_edge("E-AT")["evidence"] == 0.85)
check("证据历史保留 0.7 旧值，可回查",
      [h["value"] for h in path_svc.find_edge("E-AT")["evidence_history"]] == [0.7, 0.7, 0.85])
check("已领用待办的证据快照不被补证覆盖",
      todo["path_snapshot"]["edge_evidences"] == [0.9, 0.9, 0.9]
      and todo["threshold_snapshot"] == 0.8)

section("步骤7 补证后重新评分：两条路径均合格，重新生成候选仍按目标去重")
scores2, msg = path_svc.score_paths(case_id)
print("  ", msg)
for card in scores2:
    print(f"   路径 {card['path_key']} | 瓶颈分 {card['path_score']} | 可用 {card['usable']}")
short2 = next(c for c in scores2 if c["path_key"] == "S->A->T")
check("补证后两跳路径转为可用", short2["usable"] is True and short2["weak_edges"] == [])
check("旧评分卡片已失效但仍可回查",
      any(not c["active"] and c["path_key"] == "S->A->T"
          for c in path_svc.get_scores(case_id, active_only=False)))
reco2, msg = reco_svc.generate_candidates(case_id)
print("  ", msg)
print(f"   候选数量 {reco2['candidate_count']}；候选支撑路径 "
      f"{reco2['candidates'][0]['supporting_paths']}")
check("两条合格路径只算一次目标命中，T 仍只有一个候选", reco2["candidate_count"] == 1)
check("候选记录两条支撑路径但不增加候选数",
      reco2["candidates"][0]["supporting_path_count"] == 2)

section("步骤8 另一处尝试领取补证后的短路径：与在验绕行路径发生领用冲突")
conflict, conflict_msg = path_svc.claim_path(case_id, "S->A->T", "推荐人员-乙")
print("  返回：", conflict_msg)
check("二次领用被冲突驳回", conflict is None and "领用冲突" in conflict_msg)
check("原待办仍为核验中且领取人不变",
      path_svc.get_verification(todo_id)["status"] == "核验中"
      and path_svc.get_verification(todo_id)["reviewer"] == "推荐人员-甲")
check("冲突尝试已留痕可回查",
      path_svc.get_verification(todo_id)["attempts"][0]["result"] == "冲突驳回"
      and path_svc.get_verification(todo_id)["attempts"][0]["reviewer"] == "推荐人员-乙")

section("步骤9 非领取人尝试提交核验结果：被驳回，仍只有一份待办")
wrong, wrong_msg = path_svc.submit_conclusion(todo_id, "推荐人员-乙", True, "代提交")
print("  返回：", wrong_msg)
check("非领取人提交被驳回", wrong is None and "仅领取人" in wrong_msg)
check("待办未形成结论", path_svc.get_verification(todo_id)["status"] == "核验中")

section("步骤10 领取人甲提交唯一有效结论")
done, msg = path_svc.submit_conclusion(
    todo_id, "推荐人员-甲", True, "绕行路径边证据逐边达标，确认推荐")
print("  ", msg)
check("待办转为已确认", done["status"] == "已确认")
check("一份待办只有一份有效结论", done["conclusion"]["passed"] is True
      and done["conclusion"]["adopted_path"] == "S->B->C->T")
again, again_msg = path_svc.submit_conclusion(todo_id, "推荐人员-甲", False, "重复提交")
print("  重复提交返回：", again_msg)
check("同一待办不能重复提交结论", again is None and "已存在有效结论" in again_msg)

section("步骤11 结论落地后再领取任何路径均被拒绝（一次只能一份有效结论）")
third, third_msg = path_svc.claim_path(case_id, "S->A->T", "推荐人员-丙")
print("  返回：", third_msg)
check("已有有效结论时新领用被拒绝", third is None and "已有有效核验结论" in third_msg)

section("步骤12 回查：旧核验记录、采用路径与候选终态")
history = path_svc.list_verifications(case_id)
final_candidates = reco_svc.list_candidates(case_id)
print(f"   核验记录数 {len(history)}（含冲突留痕的同一份待办）")
print(f"   最终候选数 {len(final_candidates)}，目标 {[c['target'] for c in final_candidates]}")
print(f"   结论采用路径 {history[0]['conclusion']['adopted_path']}，"
      f"领用边 {history[0]['used_edges']}，门槛快照 {history[0]['threshold_snapshot']}")
check("全流程只有一份核验待办记录", len(history) == 1)
check("新领取人可查到本次采用路径",
      history[0]["path_snapshot"]["path_key"] == "S->B->C->T"
      and history[0]["conclusion"]["adopted_path"] == "S->B->C->T")
check("T 最终仍只算一个候选", len(final_candidates) == 1 and final_candidates[0]["target"] == "T")

print("\n" + "=" * 72)
if failures:
    print(f"场景核对未通过：{len(failures)} 项")
    for item in failures:
        print(" -", item)
    sys.exit(1)
print("全部场景核对通过：弱边不支撑推荐、补证留痕、领用冲突、唯一结论、目标去重均符合预期")
