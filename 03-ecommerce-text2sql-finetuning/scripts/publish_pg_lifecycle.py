"""Publish only this run's evidence; original/R1/R2 files remain immutable."""
import ast
from datetime import datetime, timezone
import difflib
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
RUN='INT-20261026-01'
PRIVATE=ROOT/'.runtime'/RUN
OUT=ROOT/'docs/07-iteration/ITER-20261025-01/review-output'/('integration-'+RUN)
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def dump(name,data):(OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')

raw=read(PRIVATE/'run-lifecycle.json');inspection=read(OUT/'post-exit-inspection.json')
assert raw['status']=='completed_awaiting_coordinator_reverification'
assert len(raw['resources'])==6 and not raw['pending_cleanup'] and not raw['forbidden_calls']
assert all(all(resource['cleanup_checks'].values()) for resource in raw['resources'].values())
assert all(row['original_exited'] for row in inspection['processes'])
assert inspection['run_root_removed']
before=read(OUT/'source-before.json')
for row in before:assert sha(ROOT/row['path'])==row['sha256'],row['path']
for row in raw['historical_indexes']:assert sha(ROOT/row['path'])==row['sha256']
for row in raw['dependencies']['files']:assert sha(Path(row['file']))==row['sha256']
dump('dependency-after.json',raw['dependencies'])
logs=OUT/'logs';logs.mkdir(exist_ok=True)
shutil.copyfile(PRIVATE/'run-lifecycle.json',logs/'raw-driver-record.json')
shutil.copyfile(PRIVATE/'registry.json',logs/'final-registry.json')
shutil.copyfile(PRIVATE/'helper-exit.json',logs/'helper-exit.json')
shutil.copyfile(PRIVATE/'helper-stderr.log',logs/'helper-stderr.log')
(logs/'events.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in raw['events']))

commands=[{'name':'syntax-before-real-run','argv':['sandbox/.venv/bin/python','-m','py_compile','scripts/verify_pg_lifecycle.py'],
    'exit_code':0,'scope':'syntax only; no server startup'},
 {'name':'real-driver','argv':['sandbox/.venv/bin/python','scripts/verify_pg_lifecycle.py'],
    'exit_code':0,'started_at':raw['started_at'],'finished_at':raw['finished_at'],
    'log':'logs/raw-driver-record.json','script_sha256':sha(ROOT/'scripts/verify_pg_lifecycle.py')},
 {'name':'owned-helper','argv':['sandbox/.venv/bin/python','scripts/verify_pg_lifecycle.py','--hold',
        raw['resources']['cooperative-holder']['pgdata'],str(PRIVATE/'helper-exit.json')],
    'exit_code':0,'log':'logs/helper-exit.json'},
 {'name':'post-exit-read-only','argv':['sandbox/.venv/bin/python','scripts/inspect_pg_lifecycle.py'],
    'exit_code':0,'log':'post-exit-inspection.json'},
 {'name':'publish-and-syntax','argv':['sandbox/.venv/bin/python','scripts/publish_pg_lifecycle.py'],
    'exit_code':0,'scope':'artifacts, syntax and hash assertions only'}]
for row in commands:row['cwd']=str(ROOT)
public=dict(raw)
public.update({'commands':commands,'raw_record':'logs/raw-driver-record.json',
    'phase_boundary':{'actual':'pgserver constructor, initdb/start, pg_ctl stop, psutil process identity/wait, file checks, psycopg SELECT 1',
       'substituted':'only a controlled connection.close proxy for one failure case',
       'observer':'atexit register/unregister forward the real calls and record owned callback identity; not a shutdown-condition substitute'},
    'command_exit_scope':'driver/helper/inspector recorded from actual tool/subprocess completion; individual pg_ctl/initdb exit codes not separately captured; dependency check=True requires success',
    'raw_field_clarification':'expected-error event real_stop means the stop implementation is real/unmocked; it does NOT mean stop was invoked for other-handle refusal',
    'stage_classification':[
       {'case':'normal-evaluator','result':'real start/query/eval_business cleanup/repeated close passed'},
       {'case':'normal-direct','result':'second sequential independent real lifecycle passed'},
       {'case':'cooperative-holder','result':'real other-handle refusal preserved live PG; helper cleanup/atexit/exit then real recovery passed','stop_executed_on_refusal':False},
       {'case':'connection-close-proxy','result':'proxy close failed; real PG stop completed; directory and original exception preserved; retry removed owned residue','stop_executed_on_first_attempt':True},
       {'case':'reward-close','result':'real SQLReward constructor+close with two explicit real leases/connections; __enter__/fixture DDL/scoring not run'}]})
dump('run-lifecycle.json',public)

handoff=ROOT/'HANDOFF.MD';readme=ROOT/'README.md'
(OUT/'before/HANDOFF.MD').write_bytes(handoff.read_bytes())
(OUT/'before/README.md').write_bytes(readme.read_bytes())
header=f'''# 03 — {RUN} 真实 PG 生命周期（2026-10-26）

> **实施已完成，待统筹独立验收。** 统筹前一回执已按本地代码与替身范围关闭 R1/R2；本轮新授权仅允许现有 WSL 非 root 环境的新建独立临时 PG 生命周期，不允许训练/模型/共享业务库。
> 本轮实际 PostgreSQL 16.2 + pgserver 0.1.4 + psutil 7.2.2；新建 6 个 PG 沙箱、2 个顺序正常生命周期、真实辅助句柄拒绝/释放恢复、受控连接 close 异常后的真实停止/恢复、评测清理入口及 SQLReward 两 lease 真实 close 均通过。运行根目录 `{raw['owner']['run_root']}` 已移除，pending_cleanup=[]；驱动退出后只读检查 39 条进程身份/存在观测和 6 个资源路径无残留。
> 入口：`docs/07-iteration/ITER-20261025-01/review-output/integration-{RUN}/review-report.md`（同名 HTML）、`verification.json`、`run-lifecycle.json`、`post-exit-inspection.json`。配置/registry/日志在 `.runtime/{RUN}`；原 R1/R2 与历史 RED/索引不覆盖。
> 驱动内部计时 {raw['elapsed_seconds']:.3f}s；最大已单独计时 close 调用 {raw['maximum_observed_close_seconds']:.3f}s（含调用方开销，不等于纯 PG wait；不是性能/容量验收）。未发现运行代码缺陷；owned_pg/eval_business/rewards 与 R2 摘要一致，未重跑已被统筹复验的85替身测试。
> 本轮未安装依赖、sudo/创建用户、扩大共享权限、加载模型、训练、下载、租卡、付费、Docker部署或连接 Debian/01/02。冻结152/12/20、manifest、expected不变。SQLReward只检验明确绑定的真实lease/连接close，未执行全fixture DDL、奖励评分或训练。
> 避坑：其他句柄存在时，辅助进程 cleanup(None) 释放磁盘PID登记后，其本进程缓存实例可保留直至进程退出；本轮已核对辅助退出回执及剩余父PID登记，再由父lease正式清理。受控连接close异常时真实停止成功仍保留目录，恢复必须再次关闭代理连接后按同一回执检查删除；不能把该错误算模型负奖。
> 仍deferred：PID复用、真实权限/磁盘故障、不可控孤儿/异常中断、持续负载、GPU/模型/训练与全业务fixture集成。当前短生命周期不代替生产稳定性或完整V1验收。等待统筹读取，无反向消息、新写者或下一阶段动作。

## 以下为历史阶段，当前状态以上述入口为准

'''
handoff.write_text(header+handoff.read_text())
text=readme.read_text();a=text.index('当前阶段：');b=text.index('已具备 **96GB',a)
readme.write_text(text[:a]+f'''当前阶段：**{RUN} 真实自有 PG 生命周期已执行，待统筹验收**。6 个新建沙箱的正常、合作句柄拒绝/恢复、连接 close 故障保留/恢复及调用方 close 通过；无 pending_cleanup。入口：[本轮报告](docs/07-iteration/ITER-20261025-01/review-output/integration-{RUN}/review-report.md)。未训练、加载模型或连接业务库。

R1/R2 已由统筹按本地替身范围关闭，历史报告/RED保留。本轮没有改训练运行代码、冻结数据或依赖；真实生命周期的边界与未验证项见报告。

'''+text[b:])

new_files=['scripts/verify_pg_lifecycle.py','scripts/inspect_pg_lifecycle.py','scripts/publish_pg_lifecycle.py']
paths={row['path'] for row in before}|set(new_files)
after=[{'path':rel,'sha256':sha(ROOT/rel),'bytes':(ROOT/rel).stat().st_size} for rel in sorted(paths)]
dump('source-after.json',after)
changes=[];patches=[]
for rel in ['HANDOFF.MD','README.md',*new_files]:
    path=ROOT/rel;prior=OUT/'before'/rel
    old=prior.read_text().splitlines(keepends=True) if prior.exists() else []
    changes.append({'path':rel,'kind':'modified' if prior.exists() else 'added',
        'before_sha256':sha(prior) if prior.exists() else None,'after_sha256':sha(path)})
    patches += list(difflib.unified_diff(old,path.read_text().splitlines(keepends=True),fromfile='before/'+rel,tofile='after/'+rel))
dump('changes.json',changes);(OUT/'changes.patch').write_text(''.join(patches))
core=['training/owned_pg.py','training/eval_business.py','training/rewards.py']
core_proof=[{'path':rel,'sha256':sha(ROOT/rel),'unchanged':sha(ROOT/rel)==next(row['sha256'] for row in before if row['path']==rel)} for rel in core]
assert all(row['unchanged'] for row in core_proof)
frozen=[row for row in before if row['path'].startswith('data/sft_v1/')]
assert all(sha(ROOT/row['path'])==row['sha256'] for row in frozen)
for rel in new_files:compile(ast.parse((ROOT/rel).read_text()),rel,'exec')
verification={'run':RUN,'status':'implemented_awaiting_coordinator_independent_acceptance',
    'generated_at':datetime.now(timezone.utc).isoformat(),'commands':commands,
    'real_pg_clusters':6,'normal_sequential_lifecycles':2,'real_helper_processes':1,
    'driver_elapsed_seconds':raw['elapsed_seconds'],'maximum_timed_close_call_seconds':raw['maximum_observed_close_seconds'],
    'reward_two_lease_close_seconds':next(row['elapsed_seconds'] for row in raw['events'] if row['stage']=='reward-caller-close'),
    'timing_scope':'maximum is individually timed close calls; first connection-proxy close not separately timed, reward pair has its own interval; no per-pg_ctl pure wait timing',
    'unexpected_runtime_failures':0,'expected_environment_failures':2,'pending_cleanup':[],
    'post_exit_process_observations':len(inspection['processes']),'post_exit_directory_checks':6,
    'training_core_unchanged':core_proof,'frozen_data_unchanged':frozen,
    'dependency_before':'dependency-before.json','dependency_after':'dependency-after.json',
    'historical_indexes_unchanged':raw['historical_indexes'],'source_before':'source-before.json','source_after':'source-after.json',
    'prior_85_tests':'coordinator previously independently passed; not repeated in this real-lifecycle run; relevant application source unchanged',
    'forbidden_calls':raw['forbidden_calls'],'nonroot':True,'deps_installed':False,'system_users_created':False,
    'shared_permissions_expanded':False,'model_loaded':False,'training_executed':False,'tiny_training_executed':False,
    'weights_downloaded':False,'paid_calls':0,'debian_or_business_db_connected':False,'docker_deployed':False,
    'runtime_code_modified':False,'caller_boundary':'eval_business cleanup helper and SQLReward constructor/close on explicit fresh real leases; not eval main/full reward __enter__/fixture/scoring',
    'deferred':['real PID reuse','disk/diagnostic failure','permissions/inaccessible process faults',
       'uncontrolled orphan/interrupt','sustained performance','full business fixtures','model/GPU/training'],
    'new_stage_started_after_report':False}
dump('verification.json',verification)
report=f'''# 03 · {RUN} 真实 PG 生命周期

**本轮实施完成，待统筹独立验收。** 使用既有 WSL 非 root 用户 zzh 和 sandbox/.venv，不安装依赖、不操作 Docker 或业务库。R1/R2 原本地替身结论由统筹关闭；本轮补充真实生命周期证据，不能签收全 V1 或模型训练。

## 实际结果

| 场景 | 实际行为 | 结果 |
|---|---|---|
| normal-evaluator | 新建空目录→真实 PG→Unix socket SELECT 1→eval_business 清理入口→重复 close | 通过 |
| normal-direct | 第二个顺序独立新目录/PG→真实查询→OwnedSandbox.close→重复 close | 通过 |
| cooperative-holder | 自有辅助 Python 持同一真实 PG 句柄，首次 close 拒绝并保留运行 PG/目录/诊断 | 预期失败通过 |
| auxiliary release/recovery | 辅助 cleanup(None)、进程退出及 atexit 回执；父句柄再次正式清理 | 通过 |
| connection-close-proxy | 仅 close 用受控异常代理；PG/pg_ctl/psutil/文件条件全真实；停止完成仍保留目录与原异常 | 预期失败通过 |
| proxy recovery | 再次关闭真实连接，按已存停机回执复查并删除自有残留 | 通过 |
| SQLReward.close | 真实构造，绑定2个新PG的真实 lease/连接，执行 close、父目录清理、重复 close | 通过；未执行 __enter__/全fixture DDL/评分 |

共 **6 个新建 PostgreSQL 16.2 沙箱**，一个自有辅助 Python。全部 SHOW listen_addresses 为空、inet_server_addr 为 NULL，使用自有 Unix socket；端口是各自 socket 中的5432，不监听外部网络。依赖 pgserver0.1.4、psutil7.2.2、psycopg3.2.9，现有 bin/source 前后摘要一致。

资源根 `{raw['owner']['run_root']}` 及6个PGDATA、PID文件、socket、所有权文件均已回收。驱动记录 registry 移除及 atexit unregister 的真实转发观察；进程退出后另一个只读解释器核对 **39条进程身份/存在观测、6个资源路径**，pending_cleanup=[]。39条含重复及仅PID存在性佐证，不冒充39个独立进程；退出主要证据是实际停止协议、捕获对象wait回执与带创建时间身份检查，而非孤立 pid_exists=False。

## 时间与证据

- 驱动内部总计时 **{raw['elapsed_seconds']:.3f}秒**，含目录/摘要与事件落盘；不含解释器导入前开销。
- 最大单独计时 close 调用 **{raw['maximum_observed_close_seconds']:.3f}秒**，包含调用方开销；SQLReward两lease关闭合计 **{verification['reward_two_lease_close_seconds']:.3f}秒**。连接故障首次close没有单独计时，不能把最大值说成全部pg_ctl纯等待峰值；各事件时间戳保留。不是吞吐或生产容量结论。
- 原始记录：logs/raw-driver-record.json；逐阶段/命令/替身边界：run-lifecycle.json；事件日志：logs/events.jsonl；实际失败诊断：failures/下两个原始JSON；辅助退出/atexit：logs/helper-exit.json；独立残留检查：post-exit-inspection.json。
- 驱动、辅助和独立检查命令退出码均0。pg_ctl/initdb单次退出码未另行捕获；真实依赖check=True在非零时会抛错，阶段成功回执保留，不能假造逐个命令的原始stdout/退出日志。
- raw expected-error里的real_stop表示实现未经Mock；合作句柄拒绝阶段并未发起stop，状态保持PG运行；run-lifecycle.json补充逐阶段分类，避免误读。

## 源码与边界

没有发现需要修补运行代码的真实缺陷。owned_pg/eval_business/rewards与R2 hash一致；冻结152/12/20、manifest、历史expected及依赖未改。仅新增三个本轮检查/报告脚本并更新HANDOFF/README；source-before/after、changes.patch、依赖摘要与历史索引证明在本目录。既有85替身测试已被统筹独立复跑，本轮沿用相同运行代码基线，不重复执行它们替代真实验证。

atexit注册/注销仅用转发观察器记录真实函数调用，不替换停机条件。其他句柄存在时，辅助cleanup(None)后其进程内缓存实例仍可存在，退出时None模式回调只处理登记；本轮辅助退出回执显示磁盘列表只剩父PID，最终由父lease完成真实清理。这不等于需要强杀或删除未知缓存。

评测器只验证实际清理helper；奖励器只验证真实构造和显式绑定资源后的close，未走完整评测main、reward.__enter__、业务fixture、训练gold、评分或模型。不把该调用方连接检查说成完整业务评测。

未sudo、创建系统用户、扩大共享目录权限、安装依赖、下载权重、加载模型、训练（含tiny）、租卡、付费、Docker部署或连接Debian/01/02库。import/subprocess/共享权限guard无违规记录。所有PG都在新建本run路径；只回收已确证退出的自有资源。

PID复用、磁盘/诊断故障、权限不可访问、不可控孤儿/异常中断等保留原Mock证据并deferred；持续负载、GPU、模型效果、训练和完整V1仍未验收。等待统筹独立读取本次产物，不反向发消息或开始下一阶段。
'''
(OUT/'review-report.md').write_text(report)
table=''.join(f'<tr><td>{html.escape(name)}</td><td>{r["pg_pid"]}</td><td>{r["startup_seconds"]:.3f}s</td><td>已回收</td></tr>' for name,r in raw['resources'].items())
page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>03 真实PG生命周期</title><style>body{{max-width:980px;margin:32px auto;padding:20px;background:#f3f6f8;color:#20364a;font:16px/1.8 system-ui}}main{{background:white;padding:30px;border-radius:12px}}pre{{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}}nav{{display:flex;gap:18px;flex-wrap:wrap}}a{{color:#176b97}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;text-align:left;border-bottom:1px solid #e1e8ed}}</style><main><h1>03 · 真实 PG 生命周期</h1><p>6 个自有沙箱已回收 · pending_cleanup=[] · 待统筹独立验收</p><nav><a href="verification.json">验证记录</a><a href="run-lifecycle.json">逐阶段记录</a><a href="post-exit-inspection.json">退出后检查</a><a href="changes.patch">代码差异</a></nav><table><thead><tr><th>资源</th><th>PG PID</th><th>启动</th><th>状态</th></tr></thead><tbody>{table}</tbody></table><pre>{html.escape(report)}</pre></main></html>'''
(OUT/'review-report.html').write_text(page)
class Links(HTMLParser):
    def handle_starttag(self,tag,attrs):
        if tag=='a':
            href=dict(attrs).get('href','')
            if href and not href.startswith(('http','#')):assert (OUT/href).exists(),href
Links().feed(page)
dump('html-check.json',{'parser':'stdlib HTMLParser','local_links_exist':True,'browser_rendered':False})
index=[{'path':str(p.relative_to(OUT)),'sha256':sha(p),'bytes':p.stat().st_size}
    for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='evidence-index.json' and '__pycache__' not in str(p)]
dump('evidence-index.json',index)
print(json.dumps({'run':RUN,'source_items':len(after),'real_pg_clusters':6,'pending_cleanup':[],
    'application_code_changed':False,'training':False,'report':'review-report.html'}))
