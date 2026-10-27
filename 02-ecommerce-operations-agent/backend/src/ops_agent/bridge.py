"""02授权桥。01拥有交易事实；网络请求始终在02数据库事务之外。"""
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ops_agent import db, domain
from ops_agent.errors import AppError

CONTRACT = 'XOPS-ACTIVITY-v0.1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _reject(message='执行契约不符，已停止操作', status=409):
    raise AppError('execution_contract_conflict', message, status)


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        _reject('执行时间必须为带时区ISO时间', 422)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 100


def _positive(value):
    return type(value) is int and 0 < value <= 9223372036854775807


def _money(value):
    return (isinstance(value, str) and value.isascii() and value.isdigit() and
            len(value) <= 19 and int(value) <= 9223372036854775807)


class Bridge:
    def __init__(self, database, clock, base_url=None, service_token=None,
                 service_stores=(), transport=None, origin=None):
        self.database, self.clock = database, clock
        self.service_token, self.service_stores = service_token, tuple(service_stores)
        self.configured = bool(base_url)
        parsed = urlsplit(base_url or 'http://127.0.0.1')
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            raise ValueError('01执行地址配置无效')
        self.origin = origin or f'{parsed.scheme}://{parsed.netloc}'
        self.evidence_kind = 'contract_stub' if transport is not None else 'integration'
        self.dependency_status = ('contract_stub' if transport is not None else
                                  'configured' if self.configured else 'not_connected')
        self.client = httpx.Client(base_url=base_url or 'http://127.0.0.1', timeout=3,
                                   follow_redirects=False, transport=transport)

    def pull_snapshot(self, store_id):
        if not self.configured or not self.service_token or store_id not in self.service_stores:
            raise AppError('snapshot_source_unavailable', '经营源或本店服务授权未配置', 503)
        try:
            response = self.client.get('/api/v1/internal/stores/' + quote(store_id, safe='') + '/operations-snapshot',
                headers={'Authorization': 'Bearer ' + self.service_token})
            response.raise_for_status()
            value = response.json()['data']
            if not isinstance(value, dict) or value.get('store_id') != store_id:
                raise ValueError('snapshot scope')
            return domain.validate_snapshot(value)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise AppError('snapshot_source_unavailable', '01经营源读取失败；未导入任何数据', 503) from exc

    def authenticate(self, headers, store_id):
        # 只接收部署配置的服务密钥与店铺范围；不从payload扩权。
        token = headers.get('Authorization', headers.get('authorization', ''))
        if not self.service_token or not hmac.compare_digest(token.encode(), ('Bearer '+self.service_token).encode()):
            raise AppError('unauthenticated', '服务身份无效', 401)
        if store_id not in self.service_stores:
            raise AppError('forbidden', '服务身份无此店铺权限', 403)

    def manifest(self, plan):
        spec, source = plan['draft'], domain.validate_snapshot(plan['snapshot'])
        if spec['selected_candidate'] != 'direct':
            _reject('首发只允许独立直降')
        if source['store_id'] != plan['store_id']:
            _reject('快照店铺不符')
        if (source['snapshot_id'] != spec['snapshot_id'] or
                source['revision'] != spec['expected_snapshot_revision']):
            _reject('快照修订不符')
        sku_by_id = {row['sku_id']: row for row in source['skus']}
        scenario = spec['goal']['scenario_policy']
        if scenario not in ('low', 'base', 'high', 'all'):
            _reject('情景政策无效')
        lines = []
        for line in spec['lines']:
            sku = sku_by_id[line['sku_id']]
            quantity = line['quantities']['high' if scenario == 'all' else scenario]
            if not _positive(quantity) or not _money(line['direct_price_minor']):
                _reject('执行数量须大于0，活动单价须为整数分')
            if not 0 < int(line['direct_price_minor']) <= int(sku['base_price_minor']):
                _reject('活动价超出允许范围')
            lines.append(dict(sku_id=line['sku_id'], base_price_minor=sku['base_price_minor'],
                activity_price_minor=line['direct_price_minor'], quantity_cap=quantity,
                price_version=sku['price_version']))
        budget, fixed = spec['goal']['budget_minor'], spec['fixed_fee_minor']
        if not _money(budget) or not _money(fixed) or int(budget) < int(fixed):
            _reject('活动预算或固定费预算无效')
        if _time(spec['start_at']) >= _time(spec['end_at']):
            _reject('活动时间窗无效')
        return dict(contract_version=CONTRACT, store_id=plan['store_id'],
            plan_id=plan.get('plan_id', plan.get('id')), plan_version=plan['version'],
            rule='direct', currency='CNY', start_at=spec['start_at'], end_at=spec['end_at'],
            scenario_policy=scenario, budget_cap_minor=budget, fixed_fee_minor=fixed,
            variable_budget_cap_minor=str(int(budget)-int(fixed)),
            lines=sorted(lines, key=lambda row: row['sku_id']),
            source_snapshot_id=source['snapshot_id'], source_snapshot_revision=source['revision'],
            source_kind=source['source_kind'])

    def preview(self, plan, principal, request):
        principal.require('activity.publish')
        if principal.mode != 'integration' or principal.store_id != plan['store_id']:
            _reject('演示身份或跨店身份不可请求真实预检', 403)
        if not self.configured:
            raise AppError('execution_unavailable', '01活动预检尚未配置', 503)
        cookie = request.cookies.get('cs_session')
        if not cookie:
            raise AppError('unauthenticated', '缺少商城会话', 401)
        manifest = self.manifest(plan)
        expected = dict(contract_version=CONTRACT, store_id=plan['store_id'],
            plan_id=manifest['plan_id'], plan_version=plan['version'], manifest_digest=digest(manifest))
        try:
            response = self.client.post('/api/v1/shop/activity-previews',
                headers={'Cookie': 'cs_session='+cookie, 'X-CSRF-Token': principal.csrf_token,
                         'Origin': self.origin},
                json={**expected, 'manifest': manifest})
            response.raise_for_status()
            value = response.json()['data']
            if (not isinstance(value, dict) or type(value.get('plan_version')) is not int or
                    any(value.get(k) != v for k,v in expected.items()) or
                    value.get('status') != 'passed' or not _text(value.get('preview_ref')) or
                    not self.clock() < _time(value.get('expires_at')) <= self.clock()+timedelta(minutes=5)):
                _reject('01预检绑定、状态或有效期不符', 503)
            return {**expected, 'preview_ref':value['preview_ref'],'status':'passed',
                    'expires_at':value['expires_at'],'evidence_kind': self.evidence_kind}
        except AppError:
            raise
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise AppError('execution_unavailable', '01活动预检未就绪', 503) from exc

    def _load(self, c, store_id, plan_id):
        head = c.execute(select(db.plans).where(db.plans.c.plan_id == plan_id,
            db.plans.c.store_id == store_id)).mappings().one_or_none()
        if not head:
            raise AppError('not_found', '方案不存在或不可访问', 404)
        revision = c.execute(select(db.revisions.c.body).where(db.revisions.c.plan_id == plan_id,
            db.revisions.c.store_id == store_id, db.revisions.c.version == head['version'])).scalar_one()
        spec = revision.get('draft', revision)
        source = c.execute(select(db.snapshots.c.body).where(db.snapshots.c.store_id == store_id,
            db.snapshots.c.snapshot_id == spec['snapshot_id'],
            db.snapshots.c.revision == spec['expected_snapshot_revision'])).scalar_one_or_none()
        if source is None:
            _reject('方案快照缺失')
        return dict(head, draft=spec, snapshot=source)

    def projection(self, store_id, plan_id):
        with self.database.transaction() as c:
            plan = self._load(c, store_id, plan_id)
            approval = c.execute(select(db.approvals).where(db.approvals.c.plan_id == plan_id,
                db.approvals.c.store_id == store_id, db.approvals.c.version == plan['version'])
                .order_by(db.approvals.c.created_at.desc(), db.approvals.c.approval_ref.desc())).mappings().first()
            manifest = self.manifest(plan)
            return dict(contract_version=CONTRACT, store_id=store_id, plan_id=plan_id,
                plan_version=plan['version'], status=plan['status'], manifest=manifest,
                manifest_digest=digest(manifest), approval_ref=approval['approval_ref'] if approval else None)

    def grant(self, store_id, plan_id, payload):
        required = {'approval_ref','intent_id','manifest_digest','actor_ref','auth_version',
                    'capability_revision','preview_ref'}
        if (not isinstance(payload, dict) or set(payload) != required or
                any(not _text(payload.get(k)) for k in required-{'auth_version','capability_revision'}) or
                not _positive(payload.get('auth_version')) or not _positive(payload.get('capability_revision'))):
            _reject('授权请求字段无效', 422)
        try:
            with self.database.transaction('store:'+store_id) as c:
                previous = c.execute(select(db.grants).where(
                    db.grants.c.approval_ref == payload['approval_ref'])).mappings().one_or_none()
                if previous:
                    if (previous['store_id'] != store_id or previous['plan_id'] != plan_id or
                            previous['body']['request_digest'] != digest(payload)):
                        _reject('同一审批只能绑定原意图与原请求')
                    return previous['body']['grant']
                plan = self._load(c, store_id, plan_id)
                approval = c.execute(select(db.approvals).where(
                    db.approvals.c.approval_ref == payload['approval_ref'],
                    db.approvals.c.store_id == store_id, db.approvals.c.plan_id == plan_id,
                    db.approvals.c.version == plan['version'])).mappings().one_or_none()
                if not approval or plan['status'] != 'confirmed':
                    _reject('方案尚未真实确认')
                body = approval['body']
                if (body.get('mode') != 'integration' or
                        body.get('evidence_kind') != 'integration'):
                    _reject('合同替身或演示审批不可签发真实授权')
                if any(body.get(k) != payload[k] for k in
                       ('actor_ref','auth_version','capability_revision','manifest_digest')):
                    _reject('审批身份、能力版本或摘要不符')
                preview = body.get('preview', {})
                preview_binding = dict(contract_version=CONTRACT,store_id=store_id,plan_id=plan_id,
                    plan_version=plan['version'],manifest_digest=payload['manifest_digest'],
                    preview_ref=payload['preview_ref'],status='passed',evidence_kind='integration')
                if (any(preview.get(k) != v for k,v in preview_binding.items()) or
                        not self.clock() < _time(preview.get('expires_at')) <= self.clock()+timedelta(minutes=5)):
                    _reject('审批预检绑定、状态或有效期失效')
                manifest = self.manifest(plan)
                if digest(manifest) != payload['manifest_digest']:
                    _reject('审批执行摘要已变化')
                latest = c.execute(select(db.snapshots.c.revision).where(
                    db.snapshots.c.store_id == store_id,
                    db.snapshots.c.snapshot_id == plan['draft']['snapshot_id'])
                    .order_by(db.snapshots.c.revision.desc())).scalars().first()
                if latest != plan['draft']['expected_snapshot_revision']:
                    _reject('经营快照已更新，须重新测算确认')
                evaluations = c.execute(select(db.evaluations).where(
                    db.evaluations.c.plan_id == plan_id, db.evaluations.c.store_id == store_id,
                    db.evaluations.c.version == plan['version'])).mappings().all()
                if not body.get('evaluation_id') or not any(
                        row['evaluation_id'] == body['evaluation_id'] and
                        row['body'].get('feasibility') == 'checks_passed' for row in evaluations):
                    _reject('审批测算证据缺失')
                current = domain.evaluate_plan(plan['draft'], plan['snapshot'], self.clock(), ['direct'])
                if current['feasibility'] != 'checks_passed':
                    _reject('经营、资源或任务约束已失效')
                grant = dict(contract_version=CONTRACT, grant_ref='grant_'+secrets.token_hex(16),
                    approval_ref=payload['approval_ref'], intent_id=payload['intent_id'],
                    store_id=store_id, plan_id=plan_id, plan_version=plan['version'],
                    manifest_digest=payload['manifest_digest'], manifest=manifest,
                    actor_ref=payload['actor_ref'], auth_version=payload['auth_version'],
                    capability_revision=payload['capability_revision'], preview_ref=payload['preview_ref'],
                    issued_at=self.clock().isoformat(), expires_at=(self.clock()+timedelta(seconds=60)).isoformat())
                c.execute(db.grants.insert().values(approval_ref=payload['approval_ref'],
                    grant_ref=grant['grant_ref'], intent_id=payload['intent_id'], store_id=store_id,
                    plan_id=plan_id, version=plan['version'], digest=payload['manifest_digest'],
                    body={'request_digest':digest(payload),'grant':grant}, state='result_unknown',
                    created_at=self.clock().isoformat()))
                c.execute(db.plans.update().where(db.plans.c.plan_id == plan_id,
                    db.plans.c.store_id == store_id).values(status='publishing',
                    updated_at=self.clock().isoformat()))
                return grant
        except domain.DomainError as exc:
            raise AppError(exc.code, exc.message, 409) from exc
        except IntegrityError as exc:
            raise AppError('execution_conflict', '此审批或意图已绑定授权', 409) from exc

    def receipt(self, store_id, payload):
        required = {'contract_version','event_id','store_id','intent_id','approval_ref',
                    'manifest_digest','status','terminal_revision','activity_id','activity_revision','phase'}
        if (not isinstance(payload,dict) or set(payload) != required or
                payload.get('contract_version') != CONTRACT or payload.get('store_id') != store_id or
                any(not _text(payload.get(k)) for k in ('event_id','intent_id','approval_ref','manifest_digest')) or
                payload.get('status') not in ('committed','closed_no_effect') or
                not _positive(payload.get('terminal_revision'))):
            _reject('持久终态回执字段无效', 422)
        committed = payload['status'] == 'committed'
        if committed:
            if (not _text(payload['activity_id']) or not _positive(payload['activity_revision']) or
                    payload['phase'] not in ('scheduled','active','paused','ended')):
                _reject('已提交回执必须有活动与修订', 422)
        elif (payload['activity_id'] is not None or payload['activity_revision'] is not None or
                payload['phase'] is not None):
            _reject('无效果终态不可携带活动', 422)
        with self.database.transaction('store:'+store_id) as c:
            previous = c.execute(select(db.receipts).where(db.receipts.c.event_id == payload['event_id'])).mappings().one_or_none()
            if previous:
                if previous['store_id'] != store_id or previous['digest'] != digest(payload):
                    _reject('回执事件重复但内容冲突')
                return previous['body']
            grant = c.execute(select(db.grants).where(db.grants.c.intent_id == payload['intent_id'],
                db.grants.c.store_id == store_id)).mappings().one_or_none()
            if not grant or grant['approval_ref'] != payload['approval_ref'] or grant['digest'] != payload['manifest_digest']:
                _reject('回执未绑定本店授权')
            last = grant['body'].get('terminal')
            if last:
                if (payload['status'] != last['status'] or payload['activity_id'] != last['activity_id'] or
                        payload['terminal_revision'] != last['terminal_revision']):
                    _reject('持久终态不可反转或覆盖')
                if (committed and payload['activity_revision'] == last['activity_revision'] and
                        payload['phase'] != last['phase']):
                    _reject('同活动修订状态冲突')
                if (committed and last['phase'] == 'ended' and
                        payload['activity_revision'] > last['activity_revision'] and payload['phase'] != 'ended'):
                    _reject('结束活动不可通过新修订重新开启')
            latest_terminal = (last if committed and last and
                payload['activity_revision'] < last['activity_revision'] else payload)
            body = {**grant['body'], 'terminal':latest_terminal}
            c.execute(db.grants.update().where(db.grants.c.approval_ref == grant['approval_ref'])
                      .values(state=payload['status'], body=body))
            c.execute(db.plans.update().where(db.plans.c.plan_id == grant['plan_id'],
                db.plans.c.store_id == store_id).values(status=(latest_terminal['phase'] if committed
                    else 'closed_no_effect'), updated_at=self.clock().isoformat()))
            c.execute(db.receipts.insert().values(event_id=payload['event_id'],store_id=store_id,
                intent_id=payload['intent_id'], digest=digest(payload),body=payload,
                created_at=self.clock().isoformat()))
            if committed:
                activity = c.execute(select(db.activities).where(
                    db.activities.c.activity_id == payload['activity_id'])).mappings().one_or_none()
                if activity and (activity['store_id'] != store_id or activity['plan_id'] != grant['plan_id']):
                    _reject('活动已绑定其他方案')
                if not activity:
                    c.execute(db.activities.insert().values(activity_id=payload['activity_id'],
                        store_id=store_id,plan_id=grant['plan_id'],revision=payload['activity_revision'],body=payload))
                elif payload['activity_revision'] > activity['revision']:
                    c.execute(db.activities.update().where(db.activities.c.activity_id == payload['activity_id'])
                        .values(revision=payload['activity_revision'],body=payload))
            return payload

    def actuals(self, store_id, payload):
        required = {'contract_version','store_id','snapshot_id','activity_id','revision','source_kind',
                    'watermark','payment_minor','refund_minor','consumed_discount_minor',
                    'reserved_discount_minor','paid_units','window_start','window_end','bucket'}
        if (not isinstance(payload,dict) or set(payload) != required or
                payload.get('contract_version') != CONTRACT or payload.get('store_id') != store_id or
                not all(_text(payload.get(k)) for k in ('snapshot_id','activity_id')) or
                not _positive(payload.get('revision')) or payload.get('bucket') != 'activity_cumulative' or payload.get('source_kind') not in
                ('synthetic','business_import','derived') or type(payload.get('paid_units')) is not int or
                not 0 <= payload['paid_units'] <= 9223372036854775807 or
                any(not _money(payload.get(k)) for k in ('payment_minor','refund_minor',
                    'consumed_discount_minor','reserved_discount_minor'))):
            _reject('活动实际契约字段无效',422)
        if _time(payload['watermark']) > self.clock():
            _reject('活动实际时点不可来自未来',422)
        if _time(payload['window_start']) >= _time(payload['window_end']):
            _reject('活动实际时间窗必须开始早于结束',422)
        if (_time(payload['watermark']) < _time(payload['window_start']) and
                (payload['paid_units'] or any(int(payload[k]) for k in ('payment_minor','refund_minor',
                    'consumed_discount_minor','reserved_discount_minor')))):
            _reject('活动开始前的实际快照只能是零；不能提前记成交或占用',422)
        with self.database.transaction('store:'+store_id) as c:
            activity = c.execute(select(db.activities).where(db.activities.c.store_id == store_id,
                db.activities.c.activity_id == payload['activity_id'])).mappings().one_or_none()
            if not activity:
                _reject('活动尚无可信执行回执')
            grant = c.execute(select(db.grants.c.body).where(db.grants.c.store_id == store_id,
                db.grants.c.intent_id == activity['body']['intent_id'])).scalar_one()
            manifest = grant['grant']['manifest']
            if payload['source_kind'] != manifest['source_kind']:
                _reject('活动实际来源必须与原授权Manifest一致')
            if (_time(payload['window_start']) != _time(manifest['start_at']) or
                    _time(payload['window_end']) != _time(manifest['end_at'])):
                _reject('实际累计时间窗必须与原授权活动窗一致')
            identity = c.execute(select(db.actuals.c.snapshot_id).where(
                db.actuals.c.store_id == store_id,
                db.actuals.c.activity_id == payload['activity_id'])).scalars().first()
            if identity is not None and identity != payload['snapshot_id']:
                _reject('同一活动累计窗须使用固定snapshot_id及递增revision，不可换ID重开')
            rows = c.execute(select(db.actuals).where(db.actuals.c.store_id == store_id,
                db.actuals.c.snapshot_id == payload['snapshot_id'])
                .order_by(db.actuals.c.revision.desc())).mappings().all()
            if rows and rows[0]['activity_id'] != payload['activity_id']:
                _reject('实际快照不可换绑活动')
            same = next((row for row in rows if row['revision']==payload['revision']),None)
            if same and same['digest'] != digest(payload):
                _reject('实际修订重复但内容冲突')
            if rows and payload['revision'] <= rows[0]['revision']:
                return rows[0]['body']
            if rows and _time(payload['watermark']) < _time(rows[0]['body']['watermark']):
                _reject('新修订不可倒退数据时点')
            c.execute(db.actuals.insert().values(store_id=store_id, snapshot_id=payload['snapshot_id'],
                revision=payload['revision'], activity_id=payload['activity_id'], digest=digest(payload),
                body=payload, created_at=self.clock().isoformat()))
            return payload

    def reconcile(self, store_id, intent_id, finalize=False):
        with self.database.transaction() as c:
            grant = c.execute(select(db.grants).where(db.grants.c.store_id == store_id,
                db.grants.c.intent_id == intent_id)).mappings().one_or_none()
            if not grant:
                raise AppError('not_found', '执行意图不存在或不可访问',404)
            if grant['body'].get('terminal'):
                return grant['body']['terminal']
            expires_at = _time(grant['body']['grant']['expires_at'])
        unknown = {'contract_version':CONTRACT,'store_id':store_id,'intent_id':intent_id,
                   'status':'result_unknown'}
        if not self.configured or not self.service_token or store_id not in self.service_stores:
            return unknown
        if finalize and self.clock() < expires_at:
            _reject('授权尚未到期，不可终态封闭')
        path = f'/api/v1/internal/stores/{quote(store_id,safe="")}/execution-intents/{quote(intent_id,safe="")}'
        try:
            response = self.client.request('POST' if finalize else 'GET',
                path+('/finalize-expired' if finalize else ''),
                headers={'Authorization':'Bearer '+self.service_token})
            response.raise_for_status()
            value = response.json()['data']
            if (not isinstance(value, dict) or
                    value.get('status') not in ('committed','closed_no_effect')):
                return unknown
            if value.get('intent_id') != intent_id:
                return unknown
            return self.receipt(store_id,value)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AppError):
            return unknown

    def cancel(self, store_id, intent_id, actor_context: dict) -> dict:
        """请求01锁slot封闭未决意图；01持久终态之外的结果都仍待核对。"""
        required = {'actor_ref', 'auth_version', 'capability_revision', 'reason'}
        if (not isinstance(actor_context, dict) or set(actor_context) != required or
                not _text(actor_context.get('actor_ref')) or
                not _positive(actor_context.get('auth_version')) or
                not _positive(actor_context.get('capability_revision')) or
                not isinstance(actor_context.get('reason'), str) or
                not actor_context['reason'].strip() or len(actor_context['reason']) > 1000):
            _reject('撤销身份断言或原因无效', 422)
        # actor_context只能由已鉴权的上层服务构造；grant_ref不接受任何调用方覆盖。
        with self.database.transaction() as c:
            grant = c.execute(select(db.grants).where(db.grants.c.store_id == store_id,
                db.grants.c.intent_id == intent_id)).mappings().one_or_none()
            if not grant:
                raise AppError('not_found', '执行意图不存在或不可访问', 404)
            if grant['body'].get('terminal'):
                # 已发布仅返回事实，暂停/结束仍需01另一次显式有权操作。
                return grant['body']['terminal']
            grant_ref = grant['grant_ref']
        unknown = {'contract_version': CONTRACT, 'store_id': store_id,
                   'intent_id': intent_id, 'status': 'result_unknown'}
        if not self.configured or not self.service_token or store_id not in self.service_stores:
            return unknown
        path = (f'/api/v1/internal/stores/{quote(store_id, safe="")}'
                f'/execution-intents/{quote(intent_id, safe="")}/cancel-request')
        try:
            response = self.client.post(path, headers={'Authorization': 'Bearer '+self.service_token},
                json={**actor_context, 'grant_ref': grant_ref})
            response.raise_for_status()
            value = response.json()['data']
            if (not isinstance(value, dict) or value.get('intent_id') != intent_id or
                    value.get('status') not in ('committed', 'closed_no_effect')):
                return unknown
            return self.receipt(store_id, value)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AppError):
            return unknown

    def close(self):
        self.client.close()
