"""Versioned density schemes: one release carries both fuel-grade KDE models."""
from decimal import Decimal
from sqlalchemy import String,Numeric,ForeignKey,JSON,Integer,Boolean,Float,CheckConstraint,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base

class DensityPlan(Base):
    __tablename__='density_plans'
    __table_args__=(CheckConstraint("train_state IN ('training','ready','failed')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    code:Mapped[str]=mapped_column(String(70),unique=True)
    note:Mapped[str]=mapped_column(String(600),default='')
    train_state:Mapped[str]=mapped_column(String(20),default='training')
    train_epoch:Mapped[int]=mapped_column(Integer,default=1)
    baseline_plan_id:Mapped[int|None]=mapped_column(ForeignKey('density_plans.id'),nullable=True)
    created_by:Mapped[str]=mapped_column(String(120))
    created_at:Mapped[str]=mapped_column(String(40))
    trained_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    error:Mapped[str]=mapped_column(String(600),default='')
    published_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    published_by:Mapped[str|None]=mapped_column(String(120),nullable=True)
    retired_at:Mapped[str|None]=mapped_column(String(40),nullable=True)

class DensityPlanModel(Base):
    __tablename__='density_plan_models'
    __table_args__=(UniqueConstraint('plan_id','grade'),CheckConstraint("grade IN ('VLSFO','MGO')"))
    id:Mapped[int]=mapped_column(primary_key=True)
    plan_id:Mapped[int]=mapped_column(ForeignKey('density_plans.id'))
    grade:Mapped[str]=mapped_column(String(20))
    ready:Mapped[bool]=mapped_column(Boolean,default=False)
    reference_count:Mapped[int]=mapped_column(Integer,default=0)
    threshold:Mapped[float|None]=mapped_column(Float,nullable=True)
    artifact:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    artifact_path:Mapped[str]=mapped_column(String(300),default='')
    artifact_sha256:Mapped[str]=mapped_column(String(64),default='')
    error:Mapped[str]=mapped_column(String(600),default='')

class DensityPlanReference(Base):
    __tablename__='density_plan_references'
    __table_args__=(UniqueConstraint('plan_id','sample_id'),CheckConstraint("grade IN ('VLSFO','MGO')"))
    id:Mapped[int]=mapped_column(primary_key=True)
    plan_id:Mapped[int]=mapped_column(ForeignKey('density_plans.id'))
    sample_id:Mapped[int]=mapped_column(ForeignKey('fuel_samples.id'))
    grade:Mapped[str]=mapped_column(String(20))
    code:Mapped[str]=mapped_column(String(70))
    density_15:Mapped[Decimal]=mapped_column(Numeric(10,3))
    viscosity:Mapped[Decimal]=mapped_column(Numeric(10,3))
    sulfur:Mapped[Decimal]=mapped_column(Numeric(7,4))

class DensityPlanSetting(Base):
    __tablename__='density_plan_settings'
    id:Mapped[int]=mapped_column(primary_key=True)
    active_plan_id:Mapped[int|None]=mapped_column(ForeignKey('density_plans.id'),nullable=True)
    revision:Mapped[int]=mapped_column(Integer,default=0)
    updated_by:Mapped[str]=mapped_column(String(120),default='')
    updated_at:Mapped[str]=mapped_column(String(40),default='')

class DensityPlanEvent(Base):
    __tablename__='density_plan_events'
    id:Mapped[int]=mapped_column(primary_key=True)
    plan_id:Mapped[int]=mapped_column(ForeignKey('density_plans.id'))
    action:Mapped[str]=mapped_column(String(40))
    actor:Mapped[str]=mapped_column(String(120))
    at:Mapped[str]=mapped_column(String(40))
    detail:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    from_plan_id:Mapped[int|None]=mapped_column(Integer,nullable=True)
    to_plan_id:Mapped[int|None]=mapped_column(Integer,nullable=True)
    revision:Mapped[int]=mapped_column(Integer,default=0)

class ComparisonJob(Base):
    __tablename__='comparison_jobs'
    __table_args__=(CheckConstraint("state IN ('queued','running','done','failed')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    sample_id:Mapped[int]=mapped_column(ForeignKey('fuel_samples.id'))
    plan_id:Mapped[int]=mapped_column(ForeignKey('density_plans.id'))
    plan_code:Mapped[str]=mapped_column(String(70))
    plan_revision:Mapped[int]=mapped_column(Integer,default=0)
    accepted_by:Mapped[str]=mapped_column(String(120))
    accepted_at:Mapped[str]=mapped_column(String(40))
    state:Mapped[str]=mapped_column(String(20),default='queued')
    score:Mapped[float|None]=mapped_column(Float,nullable=True)
    threshold:Mapped[float|None]=mapped_column(Float,nullable=True)
    evidence:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    reference_snapshot:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    error:Mapped[str]=mapped_column(String(600),default='')
    attempts:Mapped[int]=mapped_column(Integer,default=0)
    finished_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
