from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from .database import Session
from .plan_models import ComparisonJob, DensityPlan, DensityPlanReference
from .plan_schemas import PlanDecision, PlanIn
from .plan_service import (
    active_plan,
    comparison_view,
    compute_diff,
    create_plan,
    plan_view,
    publish_plan,
    retrain_plan,
    rollback_plan,
    setting_view,
)
from .security import allow, current

router = APIRouter()


@router.get("/density-plans")
def plans(u=Depends(current)):
    with Session() as s:
        active, setting = active_plan(s)
        data = [plan_view(s, x) for x in s.scalars(select(DensityPlan).order_by(DensityPlan.id.desc()))]
        for x in data:
            x["reference_samples"] = {"VLSFO": [], "MGO": []}
        by_id = {x["id"]: x for x in data}
        for r in s.scalars(select(DensityPlanReference).order_by(DensityPlanReference.sample_id)):
            if r.plan_id in by_id:
                by_id[r.plan_id]["reference_samples"][r.grade].append(
                    {"sample_id": r.sample_id, "code": r.code, "density_15": float(r.density_15)}
                )
        return {
            "active_plan_id": setting.active_plan_id if setting else None,
            "revision": setting.revision if setting else 0,
            "active_plan": plan_view(s, active) if active else None,
            "plans": data,
        }


@router.post("/density-plans")
def create(p: PlanIn, u=Depends(current)):
    allow(u, "admin")
    with Session.begin() as s:
        return create_plan(s, p, u)


@router.post("/density-plans/{id}/retrain")
def retrain(id: int, u=Depends(current)):
    allow(u, "admin")
    with Session.begin() as s:
        return retrain_plan(s, id, u)


@router.post("/density-plans/{id}/publish")
def publish(id: int, p: PlanDecision, u=Depends(current)):
    allow(u, "admin")
    with Session.begin() as s:
        return publish_plan(s, id, p, u)


@router.post("/density-plans/{id}/rollback")
def rollback(id: int, p: PlanDecision, u=Depends(current)):
    allow(u, "admin")
    with Session.begin() as s:
        return rollback_plan(s, id, p, u)


@router.get("/density-plans/{id}/diff")
def diff(id: int, u=Depends(current)):
    with Session() as s:
        new_plan = s.get(DensityPlan, id)
        if not new_plan:
            raise HTTPException(404, "方案不存在")
        active, setting = active_plan(s)
        if not new_plan.baseline_plan_id or new_plan.baseline_plan_id == new_plan.id:
            baseline = active
        else:
            baseline = s.get(DensityPlan, new_plan.baseline_plan_id)
        return compute_diff(s, new_plan, baseline if baseline and baseline.id != new_plan.id else None)


@router.get("/density-plan-setting")
def setting(u=Depends(current)):
    with Session() as s:
        return setting_view(s)


@router.get("/comparison-jobs")
def jobs(u=Depends(current)):
    with Session() as s:
        return [
            comparison_view(s, x)
            for x in s.scalars(select(ComparisonJob).order_by(ComparisonJob.id.desc()).limit(60))
        ]
