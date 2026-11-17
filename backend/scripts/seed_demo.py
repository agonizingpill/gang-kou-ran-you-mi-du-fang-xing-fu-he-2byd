import random,json
from sqlalchemy import select,func
from src.database import Base,engine,Session
from src.models import User,Tank,FuelSample,Transfer
from src.receipt_models import TankCapacity,TankReceipt,LoadingReconciliation
from src.harbor_models import FuelLine,LinePermit,Berth,VesselWindow,RetainedSample,RetestOrder
from src.plan_models import DensityPlan,DensityPlanModel,DensityPlanReference,DensityPlanSetting,ComparisonJob
BASE_PLAN='DP-BASE-42'
from src.security import password_hash

def seed():
    Base.metadata.create_all(engine)
    with Session.begin() as s:
        for role in ['admin','chemist','viewer']:
            if not s.scalar(select(User).where(User.email==role+'@chenggang.local')):s.add(User(email=role+'@chenggang.local',password=password_hash('Harbor2026!'),role=role))
        for code,grade,liters in [('TK-06','VLSFO',50000),('TK-08','VLSFO',20000),('TK-M01','MGO',35000)]:
            if not s.scalar(select(Tank).where(Tank.code==code)):s.add(Tank(code=code,grade=grade,liters=liters,status='active'))
        s.flush();tank_rows=list(s.scalars(select(Tank)));tanks={x.code:x for x in tank_rows};tanks_by_id={x.id:x.grade for x in tank_rows};rng=random.Random(42)
        for grade,tank,mu,sigma,visc,sulfur in [('VLSFO','TK-06',975,8,130,.35),('MGO','TK-M01',850,6,4,.06)]:
            for i in range(60):
                code=f'REF-{grade}-{i:03}'
                if not s.scalar(select(FuelSample).where(FuelSample.code==code)):s.add(FuelSample(code=code,tank_id=tanks[tank].id,density_15=round(rng.gauss(mu,sigma),3),viscosity=visc,sulfur=sulfur,status='qualified',reference=True,evidence='seed42固定等级合格参考样',retest_note=''))
        for code,tank,density,visc,sulfur in [('FS-20261117-01','TK-06',980,130,.35),('FS-OFF','TK-06',1035,130,.35),('FS-MGO-01','TK-M01',852,4,.06)]:
            if not s.scalar(select(FuelSample).where(FuelSample.code==code)):s.add(FuelSample(code=code,tank_id=tanks[tank].id,density_15=density,viscosity=visc,sulfur=sulfur,status='received',reference=False,evidence='',retest_note=''))
        for code,grade in [('FL-V01','VLSFO'),('FL-M01','MGO')]:
            if not s.scalar(select(FuelLine).where(FuelLine.code==code)):s.add(FuelLine(code=code,allowed_grade=grade,capacity_lpm=1000,min_flush_liters=200,state='idle'))
        for code,draft in [('BT-01',10),('BT-02',8)]:
            if not s.scalar(select(Berth).where(Berth.code==code)):s.add(Berth(code=code,max_draft=draft,active=True))
        s.flush()
        for code,cap,density in [('TK-06',100000,975),('TK-08',60000,978),('TK-M01',70000,850)]:
            tank=tanks[code]
            if not s.scalar(select(TankCapacity.id).where(TankCapacity.tank_id==tank.id)):
                s.add(TankCapacity(tank_id=tank.id,capacity_liters=cap,density_15=density,baseline_note='演示初始计量基准，按储罐台账与等级密度建立'))
        if not s.scalar(select(TankReceipt.id).where(TankReceipt.code=='RC-DEMO-17')):
            s.add(TankReceipt(tank_id=tanks['TK-06'].id,code='RC-DEMO-17',grade='VLSFO',liters=5000,density_15=985,viscosity=130,sulfur=.35,state='planned'))
        if not s.get(DensityPlanSetting,1):
            s.add(DensityPlanSetting(id=1,active_plan_id=None,revision=0,updated_by='system',updated_at=''))
        baseline=s.scalar(select(DensityPlan).where(DensityPlan.code==BASE_PLAN))
        if not baseline:
            # Snapshot the 120 seeded references as the first reproducible scheme.
            baseline=DensityPlan(code=BASE_PLAN,note='seed42固定参考库基线方案；双等级训练成功后自动启用',train_state='training',train_epoch=1,baseline_plan_id=None,created_by='seed',created_at='2026-11-17T00:00:00+00:00',error='')
            s.add(baseline);s.flush()
            refs=s.scalars(select(FuelSample).join(Tank,FuelSample.tank_id==Tank.id).where(FuelSample.reference==True).order_by(FuelSample.id)).all()
            for grade in ['VLSFO','MGO']:
                rows=[x for x in refs if tanks_by_id[x.tank_id]==grade]
                s.add(DensityPlanModel(plan_id=baseline.id,grade=grade,ready=False,reference_count=len(rows),error=''))
                for x in rows:
                    s.add(DensityPlanReference(plan_id=baseline.id,sample_id=x.id,grade=grade,code=x.code,density_15=x.density_15,viscosity=x.viscosity,sulfur=x.sulfur))
    with Session() as s:print(json.dumps({k:s.scalar(select(func.count()).select_from(v))for k,v in [('users',User),('tanks',Tank),('samples',FuelSample),('transfers',Transfer),('tank_capacities',TankCapacity),('receipts',TankReceipt),('reconciliations',LoadingReconciliation),('fuel_lines',FuelLine),('line_permits',LinePermit),('berths',Berth),('vessel_windows',VesselWindow),('retained_samples',RetainedSample),('retest_orders',RetestOrder),('density_plans',DensityPlan),('density_plan_references',DensityPlanReference),('comparison_jobs',ComparisonJob)]}))
if __name__=='__main__':seed()
