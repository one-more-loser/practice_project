import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ActivityPanel from './ActivityPanel.vue'
import { api } from '../api'
import type { Context } from '../types'

afterEach(() => { vi.unstubAllGlobals(); api.resetSession() })
const context: Context = { actor_ref: 'manager', store_id: 'shop', display_name: '店长', capabilities: ['metrics.read','cost.read','plan.edit','plan.confirm','activity.publish','activity.manage'], csrf_token: 'csrf', mode: 'integration', clock: '2026-10-23T00:00:00Z', dependencies: { identity: 'connected', activity: 'configured', model: 'disabled' } }
const plan = { plan_id: 'p1', store_id: 'shop', version: 2, status: 'confirmed', approval: { plan_ref: 'p1', plan_version: 2, mode: 'integration', evidence_kind: 'integration', approval_ref: 'a1', manifest_digest: 'digest', preview: { contract_version: 'XOPS-ACTIVITY-v0.1', store_id: 'shop', plan_id: 'p1', plan_version: 2, manifest_digest: 'digest', status: 'passed', evidence_kind: 'integration', preview_ref: 'v1' } }, draft: { monitoring: { interval_minutes: 5 } } }

it('同源发布发送审批版本，未知响应显示待核对并沿原键重试', async () => {
  const writes: {path: string; body: unknown; key: string | null}[] = []
  vi.stubGlobal('fetch', async (path: string, init: RequestInit) => {
    if (init.method === 'POST') { writes.push({path, body: JSON.parse(String(init.body)), key: new Headers(init.headers).get('Idempotency-Key')}); return new Response(JSON.stringify({data: {status:'unknown',intent_id:'i1'}}), {status:202}) }
    return new Response(JSON.stringify({data: path.includes('/monitoring?') ? {plan,actual:null,freshness:null,deviations:[],status:'awaiting_actual'} : path.endsWith('/plans/p1') ? plan : {items: path.endsWith('/plans') ? [plan] : []}}))
  })
  const view = mount(ActivityPanel, {props:{context,initialPlanId:'p1'}})
  try {
    await flushPromises(); const button = view.findAll('button').find(item=>item.text()==='发布活动')!
    expect(button.attributes('disabled')).toBeUndefined(); await button.trigger('click'); await flushPromises()
    expect(writes[0].path).toBe('/api/v1/shop/activities')
    expect(writes[0].body).toEqual({plan_id:'p1',plan_version:2,approval_ref:'a1',manifest_digest:'digest',preview_ref:'v1'})
    expect(view.text()).toContain('发布结果待核对')
    await button.trigger('click'); await flushPromises(); expect(writes[1].key).toBe(writes[0].key)
  } finally {view.unmount()}
})

it('活动暂停发送01修订和理由，ended不允许恢复', async () => {
  const writes: {path:string;body:unknown}[]=[]
  const activity = {activity_id:'act1',phase:'active',revision:3}
  vi.stubGlobal('fetch', async(path:string, init:RequestInit)=> {
    if(init.method==='POST') {writes.push({path,body:JSON.parse(String(init.body))});return new Response(JSON.stringify({data:{status:'committed',activity_revision:4,phase:'paused'}}))}
    return new Response(JSON.stringify({data: {items:path.endsWith('/plans')?[]:[activity]}}))
  })
  const view=mount(ActivityPanel,{props:{context}})
  try { await flushPromises(); await view.get('input').setValue('库存核对'); const pause=view.findAll('button').find(item=>item.text()==='暂停')!; await pause.trigger('click');await flushPromises();expect(writes[0]).toEqual({path:'/api/v1/shop/activities/act1/pause',body:{expected_revision:3,reason:'库存核对'}}) } finally {view.unmount()}
})

