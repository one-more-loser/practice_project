import json

from sqlalchemy import func, select, text, update

from cs_agent.actions.repository import fingerprint, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.experience_tables import documents, regressions
from cs_agent.experience.policy import (
    POLICY_REVISION,
    SUITE_REVISION,
    TOPICS,
    Procedure,
    run_guards,
    tool_permitted,
)
from cs_agent.identity.policies import require_role
from cs_agent.privacy.redaction import external_text


def scope(ctx):
    return (
        documents.c.store_id == ctx.store_id,
        documents.c.data_batch_id == ctx.data_batch_id,
        documents.c.batch_generation == ctx.batch_generation,
    )


def clean(value):
    value = external_text(value)
    if not value.strip():
        raise AppError("invalid_input", 422, "请填写处置依据")
    return value.strip()


class ExperienceService:
    def _get(self, session, ctx, ref, lock=False):
        require_role(ctx, Role.SUPERVISOR)
        query = select(documents).where(documents.c.document_id == ref, *scope(ctx))
        if lock:
            query = query.with_for_update()
        row = session.execute(query).mappings().first()
        if not row:
            raise not_found()
        return row

    def _source(self, session, ctx, row, now):
        if (
            row["policy_revision"] != POLICY_REVISION
            or row["valid_from"] > now
            or (row["valid_until"] and row["valid_until"] <= now)
        ):
            return False
        from cs_agent.db.support_tables import tickets

        if row["kind"] == "case":
            ticket = (
                session.execute(
                    select(tickets).where(
                        tickets.c.ticket_id == row["source_ticket_id"],
                        tickets.c.store_id == ctx.store_id,
                        tickets.c.data_batch_id == ctx.data_batch_id,
                        tickets.c.batch_generation == ctx.batch_generation,
                    )
                )
                .mappings()
                .first()
            )
            return bool(
                ticket
                and ticket["status"] == "closed"
                and fingerprint(
                    {"ticket": ticket["ticket_id"], "closed_at": str(ticket["closed_at"])}
                )
                == row["source_fingerprint"]
            )
        sources = (
            session.execute(
                select(documents).where(
                    documents.c.document_id.in_(row["source_case_ids"]),
                    documents.c.kind == "case",
                    documents.c.status == "published",
                    *scope(ctx),
                )
            )
            .mappings()
            .all()
        )
        return bool(
            sources
            and len(sources) == len(row["source_case_ids"])
            and all(
                source["policy_revision"] == POLICY_REVISION
                and self._source(session, ctx, source, now)
                for source in sources
            )
            and fingerprint(
                sorted(
                    (source["document_id"], fingerprint(source["definition"])) for source in sources
                )
            )
            == row["source_fingerprint"]
        )

    def create(self, session, ctx, kind, payload, now):
        require_role(ctx, Role.SUPERVISOR)
        if kind not in {"case", "skill"} or payload["topic"] not in TOPICS:
            raise AppError("invalid_input", 422, "处置分类不符合契约")
        name = clean(payload["name"])
        definition = Procedure.model_validate(payload["definition"]).model_dump()
        for key, value in definition.items():
            definition[key] = (
                [clean(item) for item in value] if isinstance(value, list) else clean(value)
            )
        if any(not tool_permitted(tool) for tool in definition["allowed_tools"]):
            raise AppError("forbidden", 403, "处置Skill不能新增业务权限")
        expires = payload.get("valid_until")
        if expires is not None and (expires.tzinfo is None or expires <= now):
            raise AppError("invalid_input", 422, "有效期不合法")
        source_ticket_id, source_case_ids = (
            payload.get("source_ticket_id"),
            sorted(set(payload.get("source_case_ids", []))),
        )
        if kind == "case":
            from cs_agent.support.service import require_ticket_scope

            ticket = require_ticket_scope(session, ctx, source_ticket_id, lock=True)
            if ticket["status"] != "closed":
                raise AppError("state_conflict", 409, "仅完成并关闭的工单可形成案例")
            private_refs = [
                ticket[key]
                for key in ("owner_id", "ticket_id", "conversation_id", "order_id")
                if ticket[key]
            ]
            if ticket["order_id"]:
                from cs_agent.db.business_tables import orders

                display_no = session.scalar(
                    select(orders.c.display_no).where(orders.c.order_id == ticket["order_id"])
                )
                if display_no:
                    private_refs.append(display_no)

            def anonymize(value):
                for private in private_refs:
                    value = value.replace(private, "[已脱敏]")
                return value

            definition = {
                key: [anonymize(item) for item in value]
                if isinstance(value, list)
                else anonymize(value)
                for key, value in definition.items()
            }
            name = anonymize(name)
            digest = fingerprint(
                {"ticket": ticket["ticket_id"], "closed_at": str(ticket["closed_at"])}
            )
            source_case_ids = []
        else:
            if not source_case_ids or len(source_case_ids) > 20:
                raise AppError("invalid_input", 422, "请选择已发布案例")
            sources = (
                session.execute(
                    select(documents).where(
                        documents.c.document_id.in_(source_case_ids),
                        documents.c.kind == "case",
                        documents.c.status == "published",
                        *scope(ctx),
                    )
                )
                .mappings()
                .all()
            )
            if len(sources) != len(source_case_ids) or any(
                not self._source(session, ctx, source, now) for source in sources
            ):
                raise AppError("state_conflict", 409, "案例来源已失效")
            digest = fingerprint(
                sorted(
                    (source["document_id"], fingerprint(source["definition"])) for source in sources
                )
            )
            source_ticket_id = None
        ref = new_id(kind)
        unit_id, revision = ref, 1
        if payload.get("previous_version_id"):
            previous = self._get(session, ctx, payload["previous_version_id"])
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:unit,0))"),
                {"unit": "experience:" + previous["unit_id"]},
            )
            if previous["kind"] != kind:
                raise AppError("invalid_input", 422, "版本类型不一致")
            unit_id = previous["unit_id"]
            revision = (
                session.scalar(
                    select(func.max(documents.c.revision)).where(documents.c.unit_id == unit_id)
                )
                + 1
            )
        session.execute(
            documents.insert().values(
                document_id=ref,
                unit_id=unit_id,
                revision=revision,
                kind=kind,
                name=name,
                topic=payload["topic"],
                definition=definition,
                source_ticket_id=source_ticket_id,
                source_case_ids=source_case_ids,
                source_fingerprint=digest,
                policy_revision=POLICY_REVISION,
                status="candidate",
                created_by=ctx.subject_id,
                valid_from=now,
                valid_until=expires,
                **provenance(ctx, now),
            )
        )
        self._audit(session, ctx, "candidate_created", ref)
        return {"document_id": ref, "unit_id": unit_id, "revision": revision, "status": "candidate"}

    def review(self, session, ctx, ref, now):
        row = self._get(session, ctx, ref, True)
        if row["status"] != "candidate" or not self._source(session, ctx, row, now):
            raise AppError("state_conflict", 409, "候选或来源已变化")
        session.execute(
            update(documents)
            .where(documents.c.document_id == ref)
            .values(
                status="approved",
                reviewed_by=ctx.subject_id,
                reviewed_at=now,
                updated_at=now,
                version=documents.c.version + 1,
            )
        )
        self._audit(session, ctx, "reviewed", ref)
        return {"document_id": ref, "status": "approved"}

    def regress(self, session, ctx, ref, now):
        row = self._get(session, ctx, ref, True)
        if (
            row["kind"] != "skill"
            or row["status"] != "approved"
            or not self._source(session, ctx, row, now)
        ):
            raise AppError("state_conflict", 409, "请先完成主管审核并核对来源")
        results = run_guards(row["definition"], now)
        ref_run = new_id("regression")
        passed = all(result["passed"] for result in results)
        session.execute(
            regressions.insert().values(
                regression_id=ref_run,
                document_id=ref,
                definition_digest=fingerprint(row["definition"]),
                policy_revision=POLICY_REVISION,
                suite_revision=SUITE_REVISION,
                results=results,
                passed=passed,
                executed_by=ctx.subject_id,
                **provenance(ctx, now),
            )
        )
        return {
            "regression_id": ref_run,
            "passed": passed,
            "scope": "permission_and_lifecycle_guards",
            "real_model_effectiveness": "unvalidated",
            "results": results,
        }

    def publish(self, session, ctx, ref, now, *, retire=False):
        initial = self._get(session, ctx, ref)
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:unit,0))"),
            {"unit": "experience:" + initial["unit_id"]},
        )
        row = self._get(session, ctx, ref, True)
        if retire:
            session.execute(
                update(documents)
                .where(documents.c.document_id == ref)
                .values(status="retired", updated_at=now)
            )
            self._audit(session, ctx, "retired", ref)
            return {"document_id": ref, "status": "retired"}
        if (
            row["status"] != "approved"
            or row["policy_revision"] != POLICY_REVISION
            or not self._source(session, ctx, row, now)
            or (row["valid_until"] and row["valid_until"] <= now)
        ):
            raise AppError("state_conflict", 409, "来源、审核或有效期不满足发布条件")
        if row["kind"] == "skill":
            tested = session.execute(
                select(regressions).where(
                    regressions.c.document_id == ref,
                    regressions.c.passed.is_(True),
                    regressions.c.definition_digest == fingerprint(row["definition"]),
                    regressions.c.policy_revision == POLICY_REVISION,
                    regressions.c.suite_revision == SUITE_REVISION,
                )
            ).first()
            if not tested:
                raise AppError("regression_required", 409, "处置Skill必须通过当前版本的服务端回归")
        session.execute(
            update(documents)
            .where(documents.c.unit_id == row["unit_id"], documents.c.status == "published")
            .values(status="retired", updated_at=now)
        )
        session.execute(
            update(documents)
            .where(documents.c.document_id == ref)
            .values(
                status="published",
                published_at=now,
                updated_at=now,
                version=documents.c.version + 1,
            )
        )
        self._audit(session, ctx, "published", ref)
        return {"document_id": ref, "status": "published"}

    def list(self, session, ctx):
        require_role(ctx, Role.SUPERVISOR)
        return {
            "items": [
                dict(row)
                for row in session.execute(
                    select(documents)
                    .where(*scope(ctx))
                    .order_by(documents.c.created_at.desc())
                    .limit(100)
                ).mappings()
            ]
        }

    def rollback(self, session, ctx, ref, now):
        row = self._get(session, ctx, ref)
        if row["status"] not in {"published", "retired"} or not self._source(
            session, ctx, row, now
        ):
            raise AppError("state_conflict", 409, "历史来源或有效期已失效，不能回退")
        return self.create(
            session,
            ctx,
            row["kind"],
            {
                "name": row["name"],
                "topic": row["topic"],
                "definition": row["definition"],
                "source_ticket_id": row["source_ticket_id"],
                "source_case_ids": row["source_case_ids"],
                "valid_until": row["valid_until"],
                "previous_version_id": ref,
            },
            now,
        )  # 恢复为新候选，再审核/回归/发布，不复活旧授权。

    def retrieve(self, session, ctx, topic, now):
        # 内部工具，只给流程已鉴权主体参考；不把源工单/内部过程暴露给顾客读面。
        rows = session.execute(
            select(documents)
            .where(
                *scope(ctx),
                documents.c.topic == topic,
                documents.c.status == "published",
                documents.c.valid_from <= now,
                documents.c.valid_until.is_(None) | (documents.c.valid_until > now),
            )
            .order_by(documents.c.published_at.desc())
            .limit(20)
        ).mappings()
        return {
            "procedures": [
                {
                    "id": row["document_id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "revision": row["revision"],
                    "definition": row["definition"],
                }
                for row in rows
                if row["policy_revision"] == POLICY_REVISION
                and self._source(session, ctx, row, now)
            ][:5]
        }

    def model_case(self, db, request, source_ticket_id, gateway):
        from cs_agent.db.support_tables import entries
        from cs_agent.identity.service import authenticate, require_csrf
        from cs_agent.support.service import require_ticket_scope

        with db.transaction() as session:
            ctx, current, _, _ = authenticate(session, request)
            require_csrf(request, current)
            require_role(ctx, Role.SUPERVISOR)
            ticket = require_ticket_scope(session, ctx, source_ticket_id)
            if ticket["status"] != "closed":
                raise AppError("state_conflict", 409, "仅已关闭处置可生成案例候选")
            version = ticket["version"]
            private = [
                ticket[k]
                for k in [
                    "owner_id",
                    "ticket_id",
                    "order_id",
                    "conversation_id",
                    "assigned_staff_id",
                ]
                if ticket[k]
            ]
            if ticket["order_id"]:
                from cs_agent.db.business_tables import orders

                display = session.scalar(
                    select(orders.c.display_no).where(orders.c.order_id == ticket["order_id"])
                )
                if display:
                    private.append(display)
            rows = (
                session.execute(
                    select(entries.c.source_role, entries.c.text)
                    .where(entries.c.ticket_id == source_ticket_id)
                    .order_by(entries.c.created_at.desc())
                    .limit(50)
                )
                .mappings()
                .all()
            )
            notes = []
            for row in reversed(rows):
                body = external_text(row["text"], max_length=1000)
                for ref in private:
                    body = body.replace(ref, "[业务引用已脱敏]")
                if body != "[会话内容已到期]":
                    notes.append({"role": row["source_role"], "text": body})
            if not notes:
                raise AppError("state_conflict", 409, "可用处理过程已到期或缺失，不能自动生成案例")
        answer = gateway.invoke(
            "chat",
            {
                "messages": [
                    {
                        "role": "system",
                        "content": "从脱敏的已完成人工处理提炼处置案例候选。只输出JSON，键为name,topic,definition；definition含conditions/exclusions/verification_steps/resolution_summary/allowed_tools/handoff_conditions。只归纳可复用核对过程，不复制个人结论、订单引用或授予支付/退款/审批权。",
                    },
                    {"role": "user", "content": json.dumps(notes, ensure_ascii=False)},
                ]
            },
            remaining=20,
        )
        try:
            result = json.loads(answer.content)
            if (
                not isinstance(result, dict)
                or set(result) != {"name", "topic", "definition"}
                or not isinstance(result["name"], str)
                or not 1 <= len(result["name"]) <= 160
                or result["topic"] not in TOPICS
            ):
                raise ValueError
            Procedure.model_validate(result["definition"])
        except (ValueError, TypeError):
            raise AppError("model_protocol_error", 503, "候选结构不合法") from None
        with db.transaction() as session:
            ctx, current, _, _ = authenticate(session, request)
            require_csrf(request, current)
            require_role(ctx, Role.SUPERVISOR)
            ticket = require_ticket_scope(session, ctx, source_ticket_id, lock=True)
            if ticket["status"] != "closed" or ticket["version"] != version:
                raise AppError("state_conflict", 409, "原处理过程已变化，请重新生成")
            return self.create(
                session,
                ctx,
                "case",
                {**result, "source_ticket_id": source_ticket_id},
                request.app.state.clock.now(),
            )

    def model_candidate(self, db, request, payload, gateway):
        from cs_agent.identity.service import authenticate, require_csrf

        with db.transaction() as session:
            ctx, current, _, _ = authenticate(session, request)
            require_csrf(request, current)
            require_role(ctx, Role.SUPERVISOR)
            now = request.app.state.clock.now()
            rows = [self._get(session, ctx, ref) for ref in payload["source_case_ids"]]
            if not rows or any(
                row["kind"] != "case"
                or row["status"] != "published"
                or not self._source(session, ctx, row, now)
                for row in rows
            ):
                raise AppError("state_conflict", 409, "请选择有效已发布案例")
            data = [
                {"name": row["name"], "topic": row["topic"], "definition": row["definition"]}
                for row in rows
            ]
        answer = gateway.invoke(
            "chat",
            {
                "messages": [
                    {
                        "role": "system",
                        "content": "归纳处置Skill候选，只输出JSON，包含name,topic,definition。definition包含conditions/exclusions/verification_steps/resolution_summary/allowed_tools/handoff_conditions；不能新增退款、审批或身份权限。",
                    },
                    {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
                ]
            },
            remaining=20,
        )
        try:
            result = json.loads(answer.content)
            if not isinstance(result, dict) or set(result) != {"name", "topic", "definition"}:
                raise ValueError
            if (
                not isinstance(result["name"], str)
                or not 1 <= len(result["name"]) <= 160
                or result["topic"] not in TOPICS
            ):
                raise ValueError
            Procedure.model_validate(result["definition"])
        except (ValueError, TypeError):
            raise AppError("model_protocol_error", 503, "候选结构不合法") from None
        with db.transaction() as session:
            ctx, current, _, _ = authenticate(session, request)
            require_csrf(request, current)
            return self.create(
                session,
                ctx,
                "skill",
                {**result, "source_case_ids": payload["source_case_ids"]},
                request.app.state.clock.now(),
            )

    @staticmethod
    def _audit(session, ctx, action, ref):
        record(
            session,
            request_id=ctx.request_id,
            action="experience." + action,
            outcome="succeeded",
            ctx=ctx,
            resource_ref=ref,
        )
