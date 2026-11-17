"""Versioned, reproducible density schemes.

A candidate scheme snapshots the selected reference samples, trains one KDE per
fuel grade, and only becomes usable when both grades are ready.  Publishing /
rolling back swaps the single in-use scheme under a revision check; comparison
jobs pin the scheme at acceptance time and keep their own reference snapshot.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastapi import HTTPException
from sqlalchemy import select, update
from sklearn.neighbors import KernelDensity
from sklearn.preprocessing import StandardScaler

from .constants import GRADES
from .density_model import within_spec
from .models import FuelSample, Tank
from .plan_models import (
    ComparisonJob,
    DensityPlan,
    DensityPlanEvent,
    DensityPlanModel,
    DensityPlanReference,
    DensityPlanSetting,
)
from .transfer_service import audit, get, obj

ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", "/artifacts"))
MIN_REFERENCE = 20


def now():
    return datetime.now(timezone.utc).isoformat()


def _write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def artifact_ok(model):
    if not model or not model.ready or not model.artifact_path or not model.artifact_sha256:
        return False
    path = Path(model.artifact_path)
    if not path.is_file():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == model.artifact_sha256


def load_artifact(model):
    if not artifact_ok(model):
        raise ValueError(f"{model.grade if model else '未知等级'} 方案训练产物缺失或校验失败")
    return json.loads(Path(model.artifact_path).read_text(encoding="utf-8"))


def _train_artifact(plan, grade, refs):
    values = np.array([[float(x.density_15)] for x in refs])
    if len(values) < MIN_REFERENCE:
        raise ValueError(f"{grade} 参考样不足{MIN_REFERENCE}条，拒绝训练")
    scaler = StandardScaler()
    z = scaler.fit_transform(values)
    if float(scaler.var_[0]) < 1e-8:
        raise ValueError(f"{grade} 参考密度没有足够变化，不能拟合核密度")
    kde = KernelDensity(kernel="gaussian", bandwidth=.3).fit(z)
    threshold = float(np.quantile(kde.score_samples(z), .05))
    return {
        "plan_id": plan.id,
        "plan_code": plan.code,
        "grade": grade,
        "reference_count": len(refs),
        "references": [
            {
                "code": x.code,
                "density_15": float(x.density_15),
                "viscosity": float(x.viscosity),
                "sulfur": float(x.sulfur),
            }
            for x in refs
        ],
        "reference_values": values.ravel().tolist(),
        "mean": float(scaler.mean_[0]),
        "scale": float(scaler.scale_[0]),
        "threshold_quantile": .05,
        "threshold": threshold,
        "bandwidth": .3,
        "kernel": "gaussian",
        "standardized": True,
        "seed": 42,
        "fit_scope": "方案快照参考样；拟合后待测样只调用打分，不进入参考库",
        "spec": GRADES[grade],
    }, threshold


def _store_artifact(plan, grade, refs):
    artifact, threshold = _train_artifact(plan, grade, refs)
    raw = json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path = ARTIFACTS_DIR / "plans" / str(plan.id) / f"{grade}.json"
    _write_atomic(path, raw)
    return artifact, threshold, str(path), hashlib.sha256(raw.encode("utf-8")).hexdigest()


def score_artifact(artifact, density):
    values = np.array(artifact["reference_values"], dtype=float).reshape(-1, 1)
    mean, scale = float(artifact["mean"]), float(artifact["scale"])
    z = (values - mean) / scale
    model = KernelDensity(kernel=artifact["kernel"], bandwidth=artifact["bandwidth"]).fit(z)
    score = float(model.score_samples(np.array([[float(density) - mean]]) / scale)[0])
    grid = np.linspace(float(values.min() - 8), float(values.max() + 8), 70)
    pdf = np.exp(model.score_samples(((grid - mean) / scale).reshape(-1, 1))) / scale
    return score, grid.tolist(), pdf.tolist()


def evidence_for(artifact, density, score, plan_revision, snapshot):
    _, grid, pdf = score_artifact(artifact, density)
    return {
        "grade": artifact["grade"],
        "reference_count": artifact["reference_count"],
        "reference_codes": [x["code"] for x in artifact["references"]],
        "reference_values": artifact["reference_values"],
        "threshold_quantile": artifact["threshold_quantile"],
        "threshold": artifact["threshold"],
        "score": score,
        "bandwidth": artifact["bandwidth"],
        "kernel": artifact["kernel"],
        "standardized": True,
        "seed": artifact["seed"],
        "grid": grid,
        "pdf": pdf,
        "spec": artifact["spec"],
        "fit_scope": artifact["fit_scope"],
        "plan": {
            "id": artifact["plan_id"],
            "code": artifact["plan_code"],
            "revision": plan_revision,
            "reference_count": artifact["reference_count"],
        },
        "reference_snapshot": snapshot,
    }


SETTING_ID = 1


def ensure_setting():
    """Create the single settings row before traffic; id=1 also serialises concurrency."""
    from .database import Session

    with Session.begin() as s:
        if not s.get(DensityPlanSetting, SETTING_ID):
            s.add(DensityPlanSetting(id=SETTING_ID, active_plan_id=None, revision=0, updated_by="system", updated_at=now()))


def _setting(s, lock=False):
    q = select(DensityPlanSetting).where(DensityPlanSetting.id == SETTING_ID)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = s.scalar(q)
    if not row:
        row = DensityPlanSetting(id=SETTING_ID, active_plan_id=None, revision=0, updated_by="system", updated_at=now())
        s.add(row)
        s.flush()
    return row


def active_plan(s, lock=False):
    setting = _setting(s, lock=lock)
    if not setting or not setting.active_plan_id:
        return None, setting
    return s.get(DensityPlan, setting.active_plan_id), setting


def _models(s, plan_id):
    return {m.grade: m for m in s.scalars(select(DensityPlanModel).where(DensityPlanModel.plan_id == plan_id))}


def model_view(m):
    return {
        "grade": m.grade,
        "ready": m.ready,
        "reference_count": m.reference_count,
        "threshold": m.threshold,
        "artifact_sha256": m.artifact_sha256,
        "artifact_ok": artifact_ok(m),
        "error": m.error,
    }


def plan_view(s, plan):
    setting = _setting(s)
    active_id = setting.active_plan_id if setting else None
    models = _models(s, plan.id)
    events = s.scalars(
        select(DensityPlanEvent).where(DensityPlanEvent.plan_id == plan.id).order_by(DensityPlanEvent.id.desc()).limit(8)
    ).all()
    data = obj(plan)
    artifacts_ok = all(models.get(g) and artifact_ok(models[g]) for g in GRADES)
    data.update(
        {
            "is_active": plan.id == active_id,
            "models": [model_view(models[g]) for g in GRADES if g in models],
            "can_retrain": not plan.published_at
            and (plan.train_state == "failed" or (plan.train_state == "ready" and not artifacts_ok)),
            "can_publish": plan.train_state == "ready"
            and plan.id != active_id
            and artifacts_ok,
            "can_rollback": bool(plan.published_at)
            and plan.train_state == "ready"
            and plan.id != active_id
            and artifacts_ok,
            "events": [
                {
                    "action": e.action,
                    "actor": e.actor,
                    "at": e.at,
                    "detail": e.detail,
                    "from_plan_id": e.from_plan_id,
                    "to_plan_id": e.to_plan_id,
                    "revision": e.revision,
                }
                for e in events
            ],
        }
    )
    return data


def setting_view(s):
    setting = _setting(s)
    plan = s.get(DensityPlan, setting.active_plan_id) if setting and setting.active_plan_id else None
    return {
        "active_plan_id": setting.active_plan_id if setting else None,
        "revision": setting.revision if setting else 0,
        "updated_by": setting.updated_by if setting else "",
        "updated_at": setting.updated_at if setting else "",
        "active_plan": plan_view(s, plan) if plan else None,
    }


def create_plan(s, data, u):
    if s.scalar(select(DensityPlan.id).where(DensityPlan.code == data.code)):
        raise HTTPException(409, "方案编号已存在，请换一个候选编号")
    setting = _setting(s)
    baseline_id = data.baseline_plan_id or (setting.active_plan_id if setting else None)
    if baseline_id and not s.get(DensityPlan, baseline_id):
        raise HTTPException(404, "基线方案不存在")
    seen = set()
    rows = []
    for grade in GRADES:
        ids = list(data.references.get(grade, []))
        if len(ids) != len(set(ids)):
            raise HTTPException(409, f"{grade} 参考样重复选择")
        for sid in ids:
            if sid in seen:
                raise HTTPException(409, f"参考样 id={sid} 不能跨等级或重复使用")
            seen.add(sid)
            sample = get(s, FuelSample, sid)
            if not sample.reference:
                raise HTTPException(409, f"油样 {sample.code} 不是参考样，不能用于训练")
            tank = get(s, Tank, sample.tank_id)
            if tank.grade != grade:
                raise HTTPException(409, f"油样 {sample.code} 属于 {tank.grade}，不能放入 {grade} 参考集")
            rows.append(
                DensityPlanReference(
                    plan_id=None,
                    sample_id=sample.id,
                    grade=grade,
                    code=sample.code,
                    density_15=sample.density_15,
                    viscosity=sample.viscosity,
                    sulfur=sample.sulfur,
                )
            )
    plan = DensityPlan(
        code=data.code,
        note=data.note,
        train_state="training",
        train_epoch=1,
        baseline_plan_id=baseline_id,
        created_by=u["email"],
        created_at=now(),
        error="",
    )
    s.add(plan)
    s.flush()
    for grade in GRADES:
        refs = [x for x in rows if x.grade == grade]
        s.add(DensityPlanModel(plan_id=plan.id, grade=grade, ready=False, reference_count=len(refs), error=""))
        for x in refs:
            x.plan_id = plan.id
            s.add(x)
    s.add(
        DensityPlanEvent(
            plan_id=plan.id,
            action="created",
            actor=u["email"],
            at=now(),
            detail={"references": {g: len([x for x in rows if x.grade == g]) for g in GRADES}},
            revision=setting.revision if setting else 0,
        )
    )
    audit(s, u, "density_plan_created", f"{plan.code} / " + " / ".join(f"{g}:{len([x for x in rows if x.grade == g])}" for g in GRADES))
    return plan_view(s, plan)


def retrain_plan(s, plan_id, u):
    plan = get(s, DensityPlan, plan_id, True)
    if plan.published_at:
        raise HTTPException(409, "已发布方案作为历史版本冻结，不能重训覆盖；请新建候选方案")
    if plan.train_state == "ready" and all(artifact_ok(m) for m in _models(s, plan.id).values()):
        raise HTTPException(409, "候选方案已双等级训练完成且产物校验通过，无需重试")
    models = _models(s, plan.id)
    plan.train_state = "training"
    plan.train_epoch += 1
    plan.error = ""
    for grade in GRADES:
        m = models.get(grade)
        if m:
            m.error = ""
            if m.ready and not artifact_ok(m):
                m.ready = False
    s.add(
        DensityPlanEvent(
            plan_id=plan.id,
            action="retry",
            actor=u["email"],
            at=now(),
            detail={"epoch": plan.train_epoch},
            revision=(_setting(s).revision if _setting(s) else 0),
        )
    )
    audit(s, u, "density_plan_retried", f"{plan.code} / epoch {plan.train_epoch}")
    return plan_view(s, plan)


def train_plan(s, plan_id):
    """Train every missing grade from the immutable reference snapshot.

    Lock order is always settings -> plan, matching publish/rollback, so a
    worker training a candidate cannot deadlock with an approval.
    """
    setting = _setting(s, lock=True)
    plan = s.scalar(
        select(DensityPlan)
        .where(DensityPlan.id == plan_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not plan or plan.train_state != "training":
        return False
    models = _models(s, plan.id)
    errors = []
    for grade in GRADES:
        m = models.get(grade)
        if not m:
            m = DensityPlanModel(plan_id=plan.id, grade=grade, ready=False, reference_count=0, error="")
            s.add(m)
            s.flush()
            models[grade] = m
        if artifact_ok(m):
            continue
        refs = s.scalars(
            select(DensityPlanReference)
            .where(DensityPlanReference.plan_id == plan.id, DensityPlanReference.grade == grade)
            .order_by(DensityPlanReference.sample_id)
        ).all()
        try:
            artifact, threshold, path, sha = _store_artifact(plan, grade, refs)
            m.ready = True
            m.reference_count = artifact["reference_count"]
            m.threshold = threshold
            m.artifact = artifact
            m.artifact_path = path
            m.artifact_sha256 = sha
            m.error = ""
        except Exception as exc:  # one bad grade keeps the whole candidate unusable
            m.ready = False
            m.threshold = None
            m.artifact = None
            m.artifact_path = ""
            m.artifact_sha256 = ""
            m.error = str(exc)
            errors.append(f"{grade}: {exc}")
    s.flush()
    if errors:
        plan.train_state = "failed"
        plan.error = "；".join(errors)
        plan.trained_at = now()
        s.add(
            DensityPlanEvent(
                plan_id=plan.id,
                action="train_failed",
                actor="worker",
                at=now(),
                detail={"error": plan.error, "epoch": plan.train_epoch},
            )
        )
        return False
    plan.train_state = "ready"
    plan.error = ""
    plan.trained_at = now()
    s.add(
        DensityPlanEvent(
            plan_id=plan.id,
            action="trained",
            actor="worker",
            at=now(),
            detail={
                "epoch": plan.train_epoch,
                "models": {g: {"reference_count": models[g].reference_count, "threshold": models[g].threshold} for g in GRADES},
            },
        )
    )
    _bootstrap_if_needed(s, plan, setting)
    return True


def _bootstrap_if_needed(s, plan, setting):
    """First deployment: the seeded baseline becomes active only after both grades train."""
    if plan.created_by != "seed" or plan.published_at:
        return
    if setting.active_plan_id is not None:
        return
    plan.published_at = now()
    plan.published_by = "system"
    setting.active_plan_id = plan.id
    setting.revision = 1
    setting.updated_by = "system"
    setting.updated_at = now()
    s.add(
        DensityPlanEvent(
            plan_id=plan.id,
            action="bootstrap",
            actor="system",
            at=now(),
            detail={"reason": "首次部署基线方案，双等级训练完成后启用"},
            to_plan_id=plan.id,
            revision=setting.revision,
        )
    )


def _check_publishable(s, plan):
    if plan.train_state != "ready":
        raise HTTPException(409, "候选方案尚未双等级训练完成，保留原在用方案")
    models = _models(s, plan.id)
    for grade in GRADES:
        m = models.get(grade)
        if not m or not m.ready:
            raise HTTPException(409, f"{grade} 模型未就绪，整套方案不能发布，保留原在用方案")
        if not artifact_ok(m):
            raise HTTPException(409, f"{grade} 训练产物缺失或校验失败，请重试训练，保留原在用方案")


def _swap_active(s, setting, plan, data, u, action, stale_message):
    """Move the in-use pointer; caller holds the settings row lock.

    The settings row is the single serialization point (locked before any plan
    row), so stale approvals queue behind the winner and then fail their
    revision check.  The conditional UPDATE repeats the check at write time.
    """
    old_id = setting.active_plan_id
    if old_id == plan.id:
        raise HTTPException(409, "该方案已是在用版本，无需重复操作")
    _check_publishable(s, plan)
    if action == "rollback" and not plan.published_at:
        raise HTTPException(409, "只能回滚到曾由主管发布的方案版本")
    old = get(s, DensityPlan, old_id, True) if old_id else None
    result = s.execute(
        update(DensityPlanSetting)
        .where(DensityPlanSetting.id == SETTING_ID, DensityPlanSetting.revision == data.expected_revision)
        .values(
            active_plan_id=plan.id,
            revision=DensityPlanSetting.revision + 1,
            updated_by=u["email"],
            updated_at=now(),
        )
    )
    if result.rowcount != 1:
        raise HTTPException(409, stale_message)
    s.expire_all()
    revision = data.expected_revision + 1
    if old:
        old.retired_at = now()
    plan.retired_at = None
    if action == "publish":
        plan.published_at = now()
        plan.published_by = u["email"]
    detail = {"note": data.note, "reference_counts": {g: m.reference_count for g, m in _models(s, plan.id).items()}}
    if action == "rollback":
        detail["from_code"] = old.code if old else ""
    s.add(
        DensityPlanEvent(
            plan_id=plan.id,
            action=action,
            actor=u["email"],
            at=now(),
            detail=detail,
            from_plan_id=old_id,
            to_plan_id=plan.id,
            revision=revision,
        )
    )
    audit(
        s,
        u,
        "density_plan_published" if action == "publish" else "density_plan_rolled_back",
        f"{plan.code} / revision {revision} / from {old.code if old else 'none'}",
    )
    return {"revision": revision, "active_plan_id": plan.id, "plan": plan_view(s, plan)}


def publish_plan(s, plan_id, data, u):
    setting = _setting(s, lock=True)
    if data.expected_revision != setting.revision:
        raise HTTPException(409, "在用方案修订已变化，当前审批基于过期版本，请刷新后重试")
    plan = get(s, DensityPlan, plan_id, True)
    return _swap_active(s, setting, plan, data, u, "publish", "在用方案修订已变化，当前审批基于过期版本，请刷新后重试")


def rollback_plan(s, plan_id, data, u):
    setting = _setting(s, lock=True)
    if data.expected_revision != setting.revision:
        raise HTTPException(409, "在用方案修订已变化，当前回滚基于过期版本，请刷新后重试")
    plan = get(s, DensityPlan, plan_id, True)
    return _swap_active(s, setting, plan, data, u, "rollback", "在用方案修订已变化，当前回滚基于过期版本，请刷新后重试")


def accept_comparison(s, sample, u):
    if sample.reference or sample.status not in ["received", "failed"]:
        raise HTTPException(409, "该样品已比对或不可重新计算")
    setting = _setting(s, lock=True)
    if not setting or not setting.active_plan_id:
        raise HTTPException(409, "尚无主管发布的在用密度方案，不能受理比对")
    plan = s.get(DensityPlan, setting.active_plan_id)
    if not plan or plan.train_state != "ready":
        raise HTTPException(409, "在用方案尚未完成双等级训练，请稍后重试")
    tank = get(s, Tank, sample.tank_id)
    model = s.scalar(
        select(DensityPlanModel).where(DensityPlanModel.plan_id == plan.id, DensityPlanModel.grade == tank.grade)
    )
    if not model or not model.ready:
        raise HTTPException(409, f"在用方案缺少 {tank.grade} 模型，不能受理比对")
    if not artifact_ok(model):
        raise HTTPException(409, f"在用方案 {tank.grade} 训练产物缺失，不能受理比对")
    refs = s.scalars(
        select(DensityPlanReference)
        .where(DensityPlanReference.plan_id == plan.id, DensityPlanReference.grade == tank.grade)
        .order_by(DensityPlanReference.sample_id)
    ).all()
    snapshot = {
        "plan_id": plan.id,
        "plan_code": plan.code,
        "plan_revision": setting.revision,
        "grade": tank.grade,
        "reference_count": len(refs),
        "references": [
            {
                "sample_id": r.sample_id,
                "code": r.code,
                "density_15": float(r.density_15),
                "viscosity": float(r.viscosity),
                "sulfur": float(r.sulfur),
            }
            for r in refs
        ],
    }
    job = ComparisonJob(
        sample_id=sample.id,
        plan_id=plan.id,
        plan_code=plan.code,
        plan_revision=setting.revision,
        accepted_by=u["email"],
        accepted_at=now(),
        state="queued",
        reference_snapshot=snapshot,
        attempts=0,
        error="",
    )
    sample.status = "queued"
    s.add(job)
    s.flush()
    audit(s, u, "sample_compared", f"{sample.code} / 方案{plan.code} rev{setting.revision}")
    return job


def process_job(job_id):
    from .database import Session

    with Session.begin() as s:
        job = s.scalar(
            select(ComparisonJob)
            .where(ComparisonJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not job or job.state not in ("queued", "running"):
            return False
        return _process_locked(s, job)


def process_next_job():
    from .database import Session

    with Session.begin() as s:
        job = s.scalar(
            select(ComparisonJob)
            .where(ComparisonJob.state == "queued")
            .order_by(ComparisonJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if not job:
            return False
        return _process_locked(s, job)


def _process_locked(s, job):
    job.state = "running"
    job.attempts += 1
    sample = s.get(FuelSample, job.sample_id)
    plan = s.get(DensityPlan, job.plan_id)
    try:
        if not sample:
            raise ValueError("油样不存在")
        if not plan:
            raise ValueError("受理时固定的方案不存在")
        tank = s.get(Tank, sample.tank_id)
        model = s.scalar(
            select(DensityPlanModel).where(
                DensityPlanModel.plan_id == job.plan_id, DensityPlanModel.grade == tank.grade
            )
        )
        artifact = load_artifact(model)
        score, _, _ = score_artifact(artifact, sample.density_15)
        passed = score >= artifact["threshold"] and within_spec(
            tank.grade, sample.density_15, sample.viscosity, sample.sulfur
        )
        evidence = evidence_for(artifact, sample.density_15, score, job.plan_revision, job.reference_snapshot)
        sample.log_density = score
        sample.threshold = artifact["threshold"]
        sample.status = "qualified" if passed else "anomaly"
        sample.evidence = json.dumps(evidence, ensure_ascii=False)
        job.score = score
        job.threshold = artifact["threshold"]
        job.evidence = evidence
        job.state = "done"
        job.error = ""
        job.finished_at = now()
        path = ARTIFACTS_DIR / f"sample-{sample.id}-evidence.json"
        _write_atomic(path, sample.evidence)
        return True
    except Exception as exc:
        job.state = "failed"
        job.error = str(exc)
        job.finished_at = now()
        if sample:
            sample.status = "failed"
            sample.evidence = json.dumps(
                {
                    "error": str(exc),
                    "plan": {"id": job.plan_id, "code": job.plan_code, "revision": job.plan_revision},
                    "reference_snapshot": job.reference_snapshot,
                },
                ensure_ascii=False,
            )
        return False


def recover_jobs():
    """Single-worker recovery: a job left running by a crash is retried, bounded."""
    from .database import Session

    with Session.begin() as s:
        for job in s.scalars(
            select(ComparisonJob).where(ComparisonJob.state == "running").with_for_update()
        ).all():
            if job.attempts >= 3:
                job.state = "failed"
                job.error = "比对任务中断超过重试上限"
                job.finished_at = now()
                sample = s.get(FuelSample, job.sample_id)
                if sample and sample.status == "queued":
                    sample.status = "failed"
                    sample.evidence = json.dumps(
                        {"error": job.error, "plan": {"id": job.plan_id, "code": job.plan_code, "revision": job.plan_revision}},
                        ensure_ascii=False,
                    )
            else:
                job.state = "queued"
        # Upgrade/cleanup: samples left queued without a live job can never finish.
        live = select(ComparisonJob.sample_id).where(ComparisonJob.state.in_(["queued", "running"]))
        for sample in s.scalars(
            select(FuelSample)
            .where(FuelSample.status == "queued", FuelSample.id.not_in(live))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all():
            sample.status = "failed"
            sample.evidence = json.dumps(
                {"error": "比对任务缺失，请重新受理"}, ensure_ascii=False
            )


def pending_training_plan():
    from .database import Session

    with Session.begin() as s:
        return s.scalar(
            select(DensityPlan.id)
            .where(DensityPlan.train_state == "training")
            .order_by(DensityPlan.id)
            .limit(1)
        )


def train_next():
    plan_id = pending_training_plan()
    if not plan_id:
        return False
    from .database import Session

    with Session.begin() as s:
        return train_plan(s, plan_id)


def comparison_view(s, job):
    sample = s.get(FuelSample, job.sample_id)
    tank = s.get(Tank, sample.tank_id) if sample else None
    data = obj(job)
    # The full reference snapshot stays on the job/evidence; lists only need its shape.
    data.pop("reference_snapshot", None)
    data.pop("evidence", None)
    data.update(
        {
            "sample_code": sample.code if sample else "",
            "grade": tank.grade if tank else "",
            "reference_count": (job.reference_snapshot or {}).get("reference_count", 0),
            "scored": job.state == "done",
        }
    )
    return data


def compute_diff(s, new_plan, old_plan):
    new_models, old_models = _models(s, new_plan.id), (_models(s, old_plan.id) if old_plan else {})
    new_artifacts = {g: load_artifact(new_models[g]) for g in GRADES if g in new_models and artifact_ok(new_models[g])}
    old_artifacts = {g: load_artifact(old_models[g]) for g in GRADES if old_plan and g in old_models and artifact_ok(old_models[g])}
    grade_diffs = {}
    for grade in GRADES:
        new_a, old_a = new_artifacts.get(grade), old_artifacts.get(grade)
        if not new_a:
            grade_diffs[grade] = {"ready": False, "old_ready": bool(old_a)}
            continue
        new_refs = {x["code"]: x for x in new_a["references"]}
        old_refs = {x["code"]: x for x in old_a["references"]} if old_a else {}
        added = [new_refs[c] for c in new_refs.keys() - old_refs.keys()]
        removed = [old_refs[c] for c in old_refs.keys() - new_refs.keys()]
        changed = [
            {"code": c, "old_density_15": old_refs[c]["density_15"], "new_density_15": new_refs[c]["density_15"]}
            for c in new_refs.keys() & old_refs.keys()
            if old_refs[c]["density_15"] != new_refs[c]["density_15"]
        ]
        grade_diffs[grade] = {
            "ready": True,
            "old_ready": bool(old_a),
            "new_count": new_a["reference_count"],
            "old_count": old_a["reference_count"] if old_a else None,
            "threshold_new": new_a["threshold"],
            "threshold_old": old_a["threshold"] if old_a else None,
            "threshold_delta": (new_a["threshold"] - old_a["threshold"]) if old_a else None,
            "mean_new": new_a["mean"],
            "mean_old": old_a["mean"] if old_a else None,
            "scale_new": new_a["scale"],
            "scale_old": old_a["scale"] if old_a else None,
            "added": added,
            "removed": removed,
            "changed": changed,
        }
    items = []
    for sample in s.scalars(select(FuelSample).where(FuelSample.reference == False).order_by(FuelSample.id)).all():
        tank = s.get(Tank, sample.tank_id)
        new_a, old_a = new_artifacts.get(tank.grade), old_artifacts.get(tank.grade)
        if not new_a:
            continue
        new_score = score_artifact(new_a, sample.density_15)[0]
        spec = within_spec(tank.grade, sample.density_15, sample.viscosity, sample.sulfur)
        new_passed = new_score >= new_a["threshold"] and spec
        old_score = score_artifact(old_a, sample.density_15)[0] if old_a else None
        old_passed = (old_score >= old_a["threshold"] and spec) if old_a else None
        items.append(
            {
                "sample_id": sample.id,
                "sample_code": sample.code,
                "grade": tank.grade,
                "density_15": float(sample.density_15),
                "spec": spec,
                "old_score": old_score,
                "old_threshold": old_a["threshold"] if old_a else None,
                "old_passed": old_passed,
                "new_score": new_score,
                "new_threshold": new_a["threshold"],
                "new_passed": new_passed,
                "changed": None if old_a is None else old_passed != new_passed,
            }
        )
    return {
        "plan_id": new_plan.id,
        "plan_code": new_plan.code,
        "against_plan_id": old_plan.id if old_plan else None,
        "against_plan_code": old_plan.code if old_plan else None,
        "grades": grade_diffs,
        "decisions": {
            "changed": sum(1 for x in items if x["changed"]),
            "total": len(items),
            "items": items,
        },
        "note": "判定差异按业务油样原样密度重算旧/新方案KDE阈值与等级规格；复检样仍沿用手工复检流程。",
    }
