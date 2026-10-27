"""合成固定test身份边界；管理能力变化使用正式受控管理函数并恢复viewer授权。"""
import json
from datetime import UTC, datetime, timedelta

from cs_agent.core.context import Role
from cs_agent.db.operations_identity_tables import OperationsCapabilityGrant as Grant
from cs_agent.identity.operations import grant_capability, revoke_capability
from joint_http import OUT, RUN, JointClient, advance, now
from joint_recovery import EvidenceDB, service_request
from sqlalchemy import select
from sqlalchemy.orm import Session


def main():
    db, viewer, admin = EvidenceDB(), JointClient('support_a'), JointClient('shop_admin')
    report = {'integration_run_id':RUN,'started_at':datetime.now(UTC).isoformat(),
              'boundary':'real HTTP plus controlled capability management functions; no raw business fixture writes'}
    def grant(until=None):
        with Session(db.engine) as s, s.begin():
            return grant_capability(s,subject_id='support_a',store_id='store_sim',role=Role.SUPPORT,
                capability='metrics.read',now=now(),valid_to=until,actor_ref=RUN,reason='合成联调身份边界后恢复查看权限').grant_id
    try:
        before = viewer.ops('GET','/context')
        snapshot = viewer.ops('GET','/snapshots')['items'][0]
        assert all('cost_minor' not in row and 'variable_fee_minor' not in row for row in snapshot['skus'])
        assert 'provenance' not in snapshot and 'source_metadata' not in snapshot
        report['cost_redaction']={'sku_count':len(snapshot['skus']),'cost_fee_provenance_metadata_removed':True}
        with Session(db.engine) as s, s.begin():
            ids = list(s.scalars(select(Grant.grant_id).where(Grant.subject_id=='support_a',Grant.capability=='metrics.read',Grant.grant_status=='active')))
            for gid in ids:
                revoke_capability(s,subject_id='support_a',store_id='store_sim',grant_id=gid,now=now(),actor_ref=RUN,reason='合成联调在线撤权')
        # 同一cookie不得沿用02旧投影。
        denied = viewer.ops('GET','/dashboard',expected=403)
        grant(now()+timedelta(seconds=1))
        current = viewer.ops('GET','/context')
        assert current['capability_revision']>before['capability_revision']
        viewer.ops('GET','/dashboard')
        advance(timedelta(seconds=2))
        expired = viewer.ops('GET','/dashboard',expected=403)
        report['online_revoke_and_expiry']={'revoked_status':403,'revoked_error':denied['error'],
            'expired_status':403,'expired_error':expired['error'],'new_revision':current['capability_revision']}
        report['cross_store'] = service_request('cs','GET','/internal/stores/other_synthetic_store/operations-snapshot',expected=404)['error']
        cookies = dict(admin.http.cookies)
        advance(timedelta(hours=9))
        admin.http.cookies.update(cookies)
        admin.ops('GET','/context',expected=401)
        report['expired_session'] = 401
        report['status']='completed'
    except Exception as exc:
        report.update(status='failed',error_type=type(exc).__name__)
        raise
    finally:
        # 将查看者恢复为长期metrics.read；保留撤销/过期审计，不恢复旧会话。
        with Session(db.engine) as s, s.begin():
            ids = list(s.scalars(select(Grant.grant_id).where(Grant.subject_id=='support_a',Grant.capability=='metrics.read',Grant.grant_status=='active')))
            for gid in ids:
                revoke_capability(s,subject_id='support_a',store_id='store_sim',grant_id=gid,now=now(),actor_ref=RUN,reason='合成边界结束归一查看授权')
        report['restored_viewer_grant_id']=grant()
        report['request_chains']=[{'actor':c.actor,'calls':c.calls} for c in [viewer,admin]]
        target=OUT/('identity-edges-attempt-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        for c in [viewer,admin]:
            c.close()
        db.engine.dispose()
        print(json.dumps({'status':report['status'],'evidence':str(target)}))


if __name__=='__main__':
    main()
