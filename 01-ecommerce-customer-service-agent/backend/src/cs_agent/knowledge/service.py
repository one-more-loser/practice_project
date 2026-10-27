import hashlib
import json
import re
import unicodedata
from datetime import datetime
from functools import lru_cache

import jieba
from sqlalchemy import and_, delete, func, select, text, update

from cs_agent.actions.repository import new_id
from cs_agent.audit.service import record
from cs_agent.config import PROJECT_ROOT
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError
from cs_agent.db.business_tables import products, skus
from cs_agent.db.knowledge_tables import chunks, faqs, revisions, units
from cs_agent.db.models import SourceBatch, Store
from cs_agent.identity.policies import require_role
from cs_agent.identity.service import authenticate, require_csrf
from cs_agent.privacy.redaction import external_text


def _scope(principal):
    return (
        units.c.store_id == principal.store_id,
        units.c.data_batch_id == principal.data_batch_id,
        units.c.batch_generation == principal.batch_generation,
    )


def _public_text(value):
    normalized = unicodedata.normalize("NFKC", value).strip()
    if (
        not normalized
        or len(normalized) > 12000
        or external_text(normalized, max_length=12000) != normalized
    ):
        raise AppError("invalid_knowledge_text", 422, "知识内容为空、超长或包含不适合发布的信息")
    return normalized


def split_text(value, size=1000):
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", value) if part.strip()]
    chunks_out = []
    for paragraph in paragraphs:
        remaining = paragraph
        while len(remaining) > size:
            cut = max(remaining.rfind(mark, 0, size) for mark in ("。", "；", "！", "？", "\n"))
            if cut < size // 2:
                cut = size
            chunks_out.append(remaining[: cut + 1].strip())
            remaining = remaining[cut + 1 :].strip()
        if remaining:
            chunks_out.append(remaining)
    return chunks_out


@lru_cache(maxsize=8)
def _tokenizer(dictionary):
    tokenizer = jieba.Tokenizer()
    tokenizer.tmp_dir = str(PROJECT_ROOT / ".runtime/tmp")
    tokenizer.cache_file = "jieba-cs.cache"
    for term in dictionary:
        if term and len(term) > 1:
            tokenizer.add_word(term)
    return tokenizer


def tokenize(value, dictionary=()):
    tokenizer = _tokenizer(tuple(sorted(set(dictionary))))
    terms = []
    for item in tokenizer.cut_for_search(value):
        item = unicodedata.normalize("NFKC", item).lower().strip()
        terms.extend(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", item))
    return list(dict.fromkeys(terms))


def _common(ctx, now):
    return {
        "store_id": ctx.store_id,
        "source_kind": "synthetic" if getattr(ctx, "synthetic", False) else "derived",
        "data_batch_id": ctx.data_batch_id,
        "batch_generation": ctx.batch_generation,
        "created_at": now,
        "updated_at": now,
        "version": 1,
    }


def _audit(session, ctx, action, ref, revision=1):
    record(
        session,
        request_id=ctx.request_id,
        action="knowledge." + action,
        outcome="succeeded",
        ctx=ctx,
        resource_ref=ref,
        summary={"version": revision},
    )


class KnowledgeService:
    def __init__(self, *, test_index=False):
        self.test_index = test_index

    def _check_index_target(self, db):
        if self.test_index and db.engine.url.database != "cs_agent_b0_test":
            raise AppError("test_index_forbidden", 403, "测试索引只能用于固定测试库")

    @staticmethod
    def _check_product(session, principal, product_id):
        if (
            product_id is not None
            and not session.execute(
                select(products.c.product_id).where(
                    products.c.product_id == product_id,
                    products.c.store_id == principal.store_id,
                    products.c.data_batch_id == principal.data_batch_id,
                    products.c.batch_generation == principal.batch_generation,
                    products.c.status == "active",
                )
            ).first()
        ):
            raise AppError("not_found", 404, "资源不存在或不可访问")

    @staticmethod
    def _faq_source_valid(session, principal, row, now):
        if row["valid_from"] > now or (row["valid_until"] and row["valid_until"] <= now):
            return False
        if row["knowledge_revision_id"]:
            source = (
                session.execute(
                    select(revisions, units.c.visibility, units.c.product_id)
                    .join(units, units.c.unit_id == revisions.c.unit_id)
                    .where(
                        revisions.c.revision_id == row["knowledge_revision_id"],
                        *_scope(principal),
                        units.c.status == "published",
                        revisions.c.status == "published",
                        units.c.active_revision == revisions.c.revision,
                        revisions.c.valid_from <= now,
                        revisions.c.valid_until.is_(None) | (revisions.c.valid_until > now),
                    )
                )
                .mappings()
                .first()
            )
            return bool(
                source
                and source["source_ref"] == row["source_ref"]
                and source["source_revision"] == row["source_revision"]
                and (source["visibility"] == "public" or row["visibility"] == "customer")
                and source["product_id"] == row["product_id"]
            )
        # 已确认规则是单独的稳定来源；不得让任意source_ref绕过版本/可见性校验。
        registry = json.loads(
            (PROJECT_ROOT / "contracts/knowledge_sources.json").read_text(encoding="utf-8")
        )
        return row["product_id"] is None and any(
            item["source_ref"] == row["source_ref"]
            and item["source_revision"] == row["source_revision"]
            and item["status"] == "active"
            and (item["visibility"] == "public" or row["visibility"] == "customer")
            for item in registry["rule_sources"]
        )

    def create_revision(
        self,
        session,
        ctx,
        unit_id,
        *,
        body,
        source_ref,
        source_revision,
        valid_from,
        valid_until,
        now,
    ):
        require_role(ctx, Role.KNOWLEDGE_ADMIN)
        unit = (
            session.execute(
                select(units).where(units.c.unit_id == unit_id, *_scope(ctx)).with_for_update()
            )
            .mappings()
            .first()
        )
        if not unit:
            raise AppError("not_found", 404, "资源不存在或不可访问")
        if valid_from.tzinfo is None or (
            valid_until is not None and (valid_until.tzinfo is None or valid_until <= valid_from)
        ):
            raise AppError("invalid_knowledge", 422, "有效期不合法")
        number = (
            session.scalar(
                select(func.max(revisions.c.revision)).where(revisions.c.unit_id == unit_id)
            )
            + 1
        )
        ref = new_id("knrev")
        session.execute(
            revisions.insert().values(
                revision_id=ref,
                unit_id=unit_id,
                revision=number,
                body=_public_text(body),
                source_ref=_public_text(source_ref),
                source_revision=_public_text(source_revision),
                valid_from=valid_from,
                valid_until=valid_until,
                status="draft",
                synthetic=False,
                created_by=ctx.subject_id,
                **_common(ctx, now),
            )
        )
        return {"revision_id": ref, "revision": number, "status": "draft"}

    def retire_unit(self, session, ctx, unit_id, now):
        require_role(ctx, Role.KNOWLEDGE_ADMIN)
        row = (
            session.execute(
                select(units).where(units.c.unit_id == unit_id, *_scope(ctx)).with_for_update()
            )
            .mappings()
            .first()
        )
        if not row:
            raise AppError("not_found", 404, "资源不存在或不可访问")
        session.execute(
            update(units).where(units.c.unit_id == unit_id).values(status="retired", updated_at=now)
        )
        source_ids = select(revisions.c.revision_id).where(revisions.c.unit_id == unit_id)
        session.execute(
            update(faqs)
            .where(faqs.c.knowledge_revision_id.in_(source_ids), faqs.c.status == "published")
            .values(status="retired", retired_at=now, updated_at=now)
        )
        session.execute(
            update(revisions)
            .where(revisions.c.unit_id == unit_id, revisions.c.status == "published")
            .values(status="retired", updated_at=now)
        )
        _audit(session, ctx, "retired", unit_id)
        return {"unit_id": unit_id, "status": "retired"}

    def create_faq(self, session, ctx, payload, now, faq_id=None):
        require_role(ctx, Role.KNOWLEDGE_ADMIN)
        self._check_product(session, ctx, payload.get("product_id"))
        if payload["valid_from"].tzinfo is None or (
            payload.get("valid_until")
            and (
                payload["valid_until"].tzinfo is None
                or payload["valid_until"] <= payload["valid_from"]
            )
        ):
            raise AppError("invalid_knowledge", 422, "有效期不合法")
        row = {
            **payload,
            "question": _public_text(payload["question"]),
            "answer": _public_text(payload["answer"]),
            "variants": [_public_text(value) for value in payload["variants"]],
        }
        ref = faq_id or new_id("faq")
        number = 1
        if faq_id:
            prior = (
                session.execute(
                    select(faqs)
                    .where(
                        faqs.c.faq_id == faq_id,
                        faqs.c.store_id == ctx.store_id,
                        faqs.c.data_batch_id == ctx.data_batch_id,
                        faqs.c.batch_generation == ctx.batch_generation,
                    )
                    .order_by(faqs.c.revision.desc())
                    .limit(1)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if not prior:
                raise AppError("not_found", 404, "资源不存在或不可访问")
            number = (
                session.scalar(select(func.max(faqs.c.revision)).where(faqs.c.faq_id == faq_id)) + 1
            )
        session.execute(
            faqs.insert().values(
                **row,
                faq_id=ref,
                faq_revision_id=new_id("faqrev"),
                revision=number,
                status="draft",
                synthetic=False,
                created_by=ctx.subject_id,
                **_common(ctx, now),
            )
        )
        _audit(session, ctx, "faq_draft_created", ref, number)
        return {"faq_id": ref, "status": "draft", "revision": number}

    def create_draft(
        self,
        session,
        ctx,
        *,
        title,
        topic,
        body,
        visibility,
        source_ref,
        source_revision,
        valid_from,
        valid_until,
        now,
        product_id=None,
    ):
        require_role(ctx, Role.KNOWLEDGE_ADMIN)
        if visibility not in {"public", "customer"} or not title.strip() or not topic.strip():
            raise AppError("invalid_knowledge", 422, "知識字段不符合契约")
        body = _public_text(body)
        self._check_product(session, ctx, product_id)
        if not isinstance(valid_from, datetime) or valid_from.tzinfo is None:
            raise AppError("invalid_knowledge", 422, "生效时间必须包含时区")
        if valid_until is not None and (valid_until.tzinfo is None or valid_until <= valid_from):
            raise AppError("invalid_knowledge", 422, "失效时间范围不合法")
        unit_id, revision_id = new_id("know"), new_id("knrev")
        base = _common(ctx, now)
        session.execute(
            units.insert().values(
                unit_id=unit_id,
                title=_public_text(title),
                topic=_public_text(topic),
                visibility=visibility,
                product_id=product_id,
                status="draft",
                active_revision=None,
                **base,
            )
        )
        session.execute(
            revisions.insert().values(
                revision_id=revision_id,
                unit_id=unit_id,
                revision=1,
                body=body,
                source_ref=_public_text(source_ref),
                source_revision=_public_text(source_revision),
                valid_from=valid_from,
                valid_until=valid_until,
                status="draft",
                synthetic=False,
                created_by=ctx.subject_id,
                **base,
            )
        )
        _audit(session, ctx, "draft_created", unit_id)
        return {"unit_id": unit_id, "revision_id": revision_id, "status": "draft", "indexed": False}

    def review_draft(self, session, ctx, revision_id, now):
        require_role(ctx, Role.KNOWLEDGE_ADMIN)
        row = (
            session.execute(
                select(revisions)
                .join(units, units.c.unit_id == revisions.c.unit_id)
                .where(revisions.c.revision_id == revision_id, *_scope(ctx))
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if not row:
            raise AppError("not_found", 404, "资源不存在或不可访问")
        if row["status"] != "draft":
            raise AppError("conflict", 409, "知识版本状态已变化")
        session.execute(
            update(revisions)
            .where(revisions.c.revision_id == revision_id)
            .values(
                status="approved",
                reviewed_by=ctx.subject_id,
                reviewed_at=now,
                updated_at=now,
                version=revisions.c.version + 1,
            )
        )
        _audit(session, ctx, "reviewed", revision_id, row["revision"])
        return {"revision_id": revision_id, "status": "approved", "reviewed_at": now.isoformat()}

    def publish_revision(self, db, request, revision_id, gateway, now):
        self._check_index_target(db)
        if getattr(gateway, "test_double", False) and not self.test_index:
            raise AppError("test_index_forbidden", 403, "测试向量不能发布到在线索引")
        # Model work runs between two short transactions; source and approval are rechecked before commit.
        with db.transaction() as session:
            ctx, current, _, _ = authenticate(session, request)
            require_csrf(request, current)
            require_role(ctx, Role.KNOWLEDGE_ADMIN)
            row = (
                session.execute(
                    select(
                        revisions,
                        units.c.title,
                        units.c.topic,
                        units.c.visibility,
                        units.c.active_revision,
                    )
                    .join(units, units.c.unit_id == revisions.c.unit_id)
                    .where(revisions.c.revision_id == revision_id, *_scope(ctx))
                )
                .mappings()
                .first()
            )
            if not row or row["status"] != "approved":
                raise AppError("conflict", 409, "僅已審核版本可發布")
            source = dict(row)
            segments = split_text(source["body"])
            if not segments:
                raise AppError("invalid_knowledge", 422, "知識內容沒有可索引文本")
        embedded = gateway.invoke(
            "embedding", {"texts": segments, "purpose": "document"}, remaining=20
        )
        with db.transaction() as session:
            dictionary = self._dictionary(session, ctx)
        token_rows = [" ".join(tokenize(segment, dictionary)) for segment in segments]
        digest = hashlib.sha256(
            json.dumps(
                {
                    "model": embedded["model"],
                    "revision": embedded["service_revision"],
                    "texts": segments,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:24]
        index_revision = f"idx_{digest}"
        with db.transaction() as session:
            ctx, current, _, _ = authenticate(session, request)
            require_csrf(request, current)
            require_role(ctx, Role.KNOWLEDGE_ADMIN)
            committed_at = request.app.state.clock.now()
            current_row = (
                session.execute(
                    select(revisions, units.c.active_revision.label("current_active_revision"))
                    .join(units, units.c.unit_id == revisions.c.unit_id)
                    .where(revisions.c.revision_id == revision_id, *_scope(ctx))
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if (
                not current_row
                or current_row["status"] != "approved"
                or current_row["body"] != source["body"]
                or (current_row["valid_until"] and current_row["valid_until"] <= committed_at)
                or (
                    current_row["current_active_revision"] is not None
                    and current_row["revision"] <= current_row["current_active_revision"]
                )
            ):
                raise AppError("conflict", 409, "知識版本在索引期間已變化")
            session.execute(delete(chunks).where(chunks.c.revision_id == revision_id))
            base = _common(ctx, committed_at)
            session.execute(
                chunks.insert(),
                [
                    {
                        "chunk_id": new_id("knch"),
                        "revision_id": revision_id,
                        "unit_id": source["unit_id"],
                        "ordinal": index,
                        "text": segment,
                        "keyword_text": token_rows[index],
                        "embedding": embedded["vectors"][index],
                        "embedding_model": embedded["model"],
                        "embedding_service_revision": embedded["service_revision"],
                        "index_revision": index_revision,
                        "test_only": self.test_index,
                        **base,
                    }
                    for index, segment in enumerate(segments)
                ],
            )
            active_revision = current_row["current_active_revision"]
            if active_revision:
                old_ids = select(revisions.c.revision_id).where(
                    revisions.c.unit_id == source["unit_id"],
                    revisions.c.revision == active_revision,
                )
                session.execute(
                    update(faqs)
                    .where(faqs.c.knowledge_revision_id.in_(old_ids), faqs.c.status == "published")
                    .values(status="retired", retired_at=committed_at)
                )
                session.execute(
                    update(revisions)
                    .where(
                        revisions.c.unit_id == source["unit_id"],
                        revisions.c.revision == active_revision,
                        revisions.c.status == "published",
                    )
                    .values(status="retired", updated_at=now, version=revisions.c.version + 1)
                )
            session.execute(
                update(revisions)
                .where(revisions.c.revision_id == revision_id)
                .values(
                    status="published",
                    published_at=committed_at,
                    updated_at=committed_at,
                    version=revisions.c.version + 1,
                )
            )
            session.execute(
                update(units)
                .where(units.c.unit_id == source["unit_id"])
                .values(
                    status="published",
                    active_revision=source["revision"],
                    updated_at=committed_at,
                    version=units.c.version + 1,
                )
            )
            _audit(session, ctx, "published", source["unit_id"], source["revision"])
        return {
            "revision_id": revision_id,
            "status": "published",
            "index_revision": index_revision,
            "chunk_count": len(segments),
            "published_at": committed_at.isoformat(),
        }

    def publish_faq(self, session, ctx, faq_id, now):
        require_role(ctx, Role.KNOWLEDGE_ADMIN)
        # FAQ写入量低；店铺级短锁防止两个新条目并发发布同一问法。
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {
                "scope": "faq-publish:"
                + ctx.store_id
                + ":"
                + ctx.data_batch_id
                + ":"
                + str(ctx.batch_generation)
            },
        )
        row = (
            session.execute(
                select(faqs)
                .where(
                    faqs.c.faq_id == faq_id,
                    faqs.c.store_id == ctx.store_id,
                    faqs.c.data_batch_id == ctx.data_batch_id,
                    faqs.c.batch_generation == ctx.batch_generation,
                )
                .order_by(faqs.c.revision.desc())
                .with_for_update()
                .limit(1)
            )
            .mappings()
            .first()
        )
        if (
            not row
            or row["status"] != "approved"
            or not self._faq_source_valid(session, ctx, row, now)
        ):
            raise AppError("conflict", 409, "僅已審核FAQ可發布")

        def normalize(value):
            return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()

        forms = {normalize(value) for value in [row["question"], *row["variants"]]}
        peers = session.execute(
            select(faqs).where(
                faqs.c.store_id == ctx.store_id,
                faqs.c.data_batch_id == ctx.data_batch_id,
                faqs.c.batch_generation == ctx.batch_generation,
                faqs.c.faq_id != faq_id,
                faqs.c.status == "published",
                faqs.c.product_id == row["product_id"],
            )
        ).mappings()
        for peer in peers:
            if forms & {normalize(value) for value in [peer["question"], *peer["variants"]]}:
                raise AppError("conflict", 409, "标准问题或同义问法与已发布FAQ冲突，请先停用旧条目")
        session.execute(
            update(faqs)
            .where(faqs.c.faq_revision_id == row["faq_revision_id"])
            .values(
                status="published", published_at=now, updated_at=now, version=faqs.c.version + 1
            )
        )
        session.execute(
            update(faqs)
            .where(
                faqs.c.faq_id == faq_id,
                faqs.c.faq_revision_id != row["faq_revision_id"],
                faqs.c.status == "published",
            )
            .values(status="retired", retired_at=now, updated_at=now, version=faqs.c.version + 1)
        )
        _audit(session, ctx, "faq_published", faq_id, row["revision"])
        return {
            "faq_id": faq_id,
            "revision": row["revision"],
            "status": "published",
            "published_at": now.isoformat(),
        }

    def review_faq(self, session, ctx, faq_id, now):
        require_role(ctx, Role.KNOWLEDGE_ADMIN)
        row = (
            session.execute(
                select(faqs)
                .where(
                    faqs.c.faq_id == faq_id,
                    faqs.c.store_id == ctx.store_id,
                    faqs.c.data_batch_id == ctx.data_batch_id,
                    faqs.c.batch_generation == ctx.batch_generation,
                )
                .order_by(faqs.c.revision.desc())
                .with_for_update()
                .limit(1)
            )
            .mappings()
            .first()
        )
        if not row or row["status"] != "draft":
            raise AppError("conflict", 409, "僅FAQ草稿可審核")
        session.execute(
            update(faqs)
            .where(faqs.c.faq_revision_id == row["faq_revision_id"])
            .values(
                status="approved",
                reviewed_by=ctx.subject_id,
                reviewed_at=now,
                updated_at=now,
                version=faqs.c.version + 1,
            )
        )
        _audit(session, ctx, "faq_reviewed", faq_id, row["revision"])
        return {
            "faq_id": faq_id,
            "revision": row["revision"],
            "status": "approved",
            "reviewed_at": now.isoformat(),
        }

    def retire_faq(self, session, ctx, faq_id, now):
        require_role(ctx, Role.KNOWLEDGE_ADMIN)
        result = session.execute(
            update(faqs)
            .where(
                faqs.c.faq_id == faq_id,
                faqs.c.store_id == ctx.store_id,
                faqs.c.data_batch_id == ctx.data_batch_id,
                faqs.c.batch_generation == ctx.batch_generation,
                faqs.c.status == "published",
            )
            .values(status="retired", retired_at=now, updated_at=now, version=faqs.c.version + 1)
        )
        if result.rowcount == 0:
            raise AppError("not_found", 404, "資源不存在或不可訪問")
        _audit(session, ctx, "faq_retired", faq_id)
        return {"faq_id": faq_id, "status": "retired", "retired_at": now.isoformat()}

    def faq_reference(self, session, principal, revision_id, now):
        visibility = ["public"] if principal.kind == "guest" else ["public", "customer"]
        row = (
            session.execute(
                select(faqs).where(
                    faqs.c.faq_revision_id == revision_id,
                    faqs.c.store_id == principal.store_id,
                    faqs.c.data_batch_id == principal.data_batch_id,
                    faqs.c.batch_generation == principal.batch_generation,
                    faqs.c.status == "published",
                    faqs.c.visibility.in_(visibility),
                )
            )
            .mappings()
            .first()
        )
        if not row or not self._faq_source_valid(session, principal, row, now):
            raise AppError("not_found", 404, "資源不存在或不可訪問")
        return {
            "kind": "faq",
            "faq_id": row["faq_id"],
            "question": row["question"],
            "body": row["answer"],
            "revision": row["revision"],
            "source_ref": row["source_ref"],
            "source_revision": row["source_revision"],
            "published_at": row["published_at"].isoformat() if row["published_at"] else None,
        }

    def list_admin(self, session, ctx):
        require_role(ctx, Role.KNOWLEDGE_ADMIN)
        knowledge = (
            session.execute(
                select(
                    units.c.unit_id,
                    units.c.title,
                    units.c.topic,
                    units.c.visibility,
                    units.c.status,
                    units.c.active_revision,
                    units.c.product_id,
                    units.c.updated_at,
                )
                .where(*_scope(ctx))
                .order_by(units.c.updated_at.desc())
                .limit(100)
            )
            .mappings()
            .all()
        )
        faq_rows = (
            session.execute(
                select(
                    faqs.c.faq_id,
                    faqs.c.question,
                    faqs.c.status,
                    faqs.c.revision,
                    faqs.c.reviewed_at,
                    faqs.c.published_at,
                    faqs.c.synthetic,
                )
                .where(
                    faqs.c.store_id == ctx.store_id,
                    faqs.c.data_batch_id == ctx.data_batch_id,
                    faqs.c.batch_generation == ctx.batch_generation,
                )
                .order_by(faqs.c.faq_id, faqs.c.revision.desc())
                .limit(200)
            )
            .mappings()
            .all()
        )
        version_rows = (
            session.execute(
                select(
                    revisions.c.revision_id,
                    revisions.c.unit_id,
                    revisions.c.revision,
                    revisions.c.status,
                    revisions.c.valid_from,
                    revisions.c.valid_until,
                    revisions.c.source_ref,
                    revisions.c.source_revision,
                )
                .join(units, units.c.unit_id == revisions.c.unit_id)
                .where(*_scope(ctx))
                .order_by(revisions.c.created_at.desc())
                .limit(200)
            )
            .mappings()
            .all()
        )
        return {
            "units": [dict(row) for row in knowledge],
            "revisions": [dict(row) for row in version_rows],
            "faqs": [dict(row) for row in faq_rows],
        }

    def inspect_admin(self, session, ctx, kind, ref):
        require_role(ctx, Role.KNOWLEDGE_ADMIN)
        if kind == "knowledge":
            statement = (
                select(revisions, units.c.title)
                .join(units, units.c.unit_id == revisions.c.unit_id)
                .where(revisions.c.revision_id == ref, *_scope(ctx))
            )
        elif kind == "faq":
            statement = select(faqs).where(
                faqs.c.faq_id == ref,
                faqs.c.store_id == ctx.store_id,
                faqs.c.data_batch_id == ctx.data_batch_id,
                faqs.c.batch_generation == ctx.batch_generation,
            )
            statement = statement.order_by(faqs.c.revision.desc()).limit(1)
        else:
            raise AppError("not_found", 404, "资源不存在或不可访问")
        row = session.execute(statement).mappings().first()
        if not row:
            raise AppError("not_found", 404, "资源不存在或不可访问")
        return dict(row)

    @staticmethod
    def _dictionary(session, principal):
        rows = session.execute(
            select(products.c.name, products.c.aliases).where(
                products.c.store_id == principal.store_id,
                products.c.data_batch_id == principal.data_batch_id,
                products.c.batch_generation == principal.batch_generation,
                products.c.status == "active",
            )
        ).mappings()
        terms = {"退货退款", "模拟充值", "确认收货", "修改地址", "运输异常", "账户余额"}
        for row in rows:
            terms.update([row["name"], *(row["aliases"] or [])])
        return tuple(sorted(terms))

    @staticmethod
    def _resolve_product(session, principal, query):
        rows = session.execute(
            select(products.c.product_id, products.c.name, products.c.aliases, skus.c.sku_id)
            .join(skus, skus.c.product_id == products.c.product_id)
            .where(
                products.c.store_id == principal.store_id,
                products.c.data_batch_id == principal.data_batch_id,
                products.c.batch_generation == principal.batch_generation,
                products.c.status == "active",
                skus.c.status == "active",
            )
        ).mappings()
        normalized = unicodedata.normalize("NFKC", query).casefold()
        matched = {
            row["product_id"]
            for row in rows
            if any(
                unicodedata.normalize("NFKC", name).casefold() in normalized
                for name in [row["name"], row["sku_id"], *(row["aliases"] or [])]
                if name
            )
        }
        return matched

    def retrieve(self, db, principal, query, gateway, now, *, clock=None):
        self._check_index_target(db)
        current_time = (lambda: clock.now()) if clock else (lambda: now)
        if principal.kind not in {"guest", "customer"}:
            return {"items": [], "degraded": True, "reason": "principal_not_supported"}
        ctx = principal.context
        if principal.kind == "customer" and (ctx is None or ctx.role != Role.CUSTOMER):
            raise AppError("unauthenticated", 401, "請重新登錄")
        normalized = unicodedata.normalize("NFKC", external_text(query)).strip()
        if not normalized:
            return {"items": [], "degraded": True, "reason": "empty_query"}
        visibility = ["public"] if principal.kind == "guest" else ["public", "customer"]
        with db.transaction() as session:
            matched = self._resolve_product(session, principal, normalized)
            if len(matched) > 1:
                return {"items": [], "degraded": False, "reason": "ambiguous_product"}
            product_id = next(iter(matched), None)
            exact = self._faq_match(session, principal, normalized, visibility, now, product_id)
            if exact:
                return exact
            terms = tokenize(normalized, self._dictionary(session, principal))
            if not terms:
                return {"items": [], "degraded": True, "reason": "no_search_terms"}
            keyword_query = " | ".join(terms[:20])
            keyword_rows = (
                session.execute(
                    self._active_chunk_query(
                        principal, visibility, current_time(), product_id, self.test_index
                    )
                    .where(
                        func.to_tsvector("simple", chunks.c.keyword_text).op("@@")(
                            func.to_tsquery("simple", keyword_query)
                        )
                    )
                    .order_by(
                        func.ts_rank_cd(
                            func.to_tsvector("simple", chunks.c.keyword_text),
                            func.to_tsquery("simple", keyword_query),
                        ).desc()
                    )
                    .limit(20)
                )
                .mappings()
                .all()
            )
            keyword = [self._candidate(row) for row in keyword_rows]
        dense = []
        degraded = False
        reason = None
        try:
            embedded = gateway.invoke(
                "embedding", {"texts": [normalized], "purpose": "query"}, remaining=8
            )
            vector = embedded["vectors"][0]
            with db.transaction() as session:
                dense_rows = (
                    session.execute(
                        self._active_chunk_query(
                            principal, visibility, current_time(), product_id, self.test_index
                        )
                        .where(
                            chunks.c.embedding.is_not(None),
                            chunks.c.embedding_model == embedded["model"],
                            chunks.c.embedding_service_revision == embedded["service_revision"],
                        )
                        .order_by(chunks.c.embedding.cosine_distance(vector))
                        .limit(20)
                    )
                    .mappings()
                    .all()
                )
                dense = [self._candidate(row) for row in dense_rows]
        except AppError as exc:
            degraded, reason = True, exc.code
        fused = {}
        for rank, candidate in enumerate(keyword, 1):
            fused[candidate["chunk_id"]] = {**candidate, "rrf": 1 / (60 + rank)}
        for rank, candidate in enumerate(dense, 1):
            current = fused.setdefault(candidate["chunk_id"], {**candidate, "rrf": 0})
            current["rrf"] += 1 / (60 + rank)
        candidates = sorted(fused.values(), key=lambda item: (-item["rrf"], item["chunk_id"]))[:10]
        # 外发重排前再次过滤，停用/过期内容和测试索引不会进入在线模型上下文。
        if candidates:
            with db.transaction() as session:
                allowed = set(
                    session.execute(
                        self._active_chunk_query(
                            principal, visibility, current_time(), product_id, self.test_index
                        ).where(chunks.c.chunk_id.in_([item["chunk_id"] for item in candidates]))
                    ).scalars()
                )
            candidates = [item for item in candidates if item["chunk_id"] in allowed]
        reranked = False
        if candidates:
            try:
                response = gateway.invoke(
                    "rerank",
                    {
                        "query": normalized,
                        "candidates": [
                            {"id": item["chunk_id"], "text": item["text"]} for item in candidates
                        ],
                        "top_n": 10,
                    },
                    remaining=6,
                )
                if not response["results"]:
                    raise AppError("model_protocol_error", 503, "重排未返回候选")
                rank_map = {row["id"]: index for index, row in enumerate(response["results"])}
                candidates = [item for item in candidates if item["chunk_id"] in rank_map]
                candidates.sort(key=lambda item: rank_map[item["chunk_id"]])
                reranked = True
            except AppError as exc:
                degraded, reason = True, reason or exc.code
        final = []
        char_count = 0
        ids = [item["chunk_id"] for item in candidates]
        if ids:
            with db.transaction() as session:
                rows = (
                    session.execute(
                        self._active_chunk_query(
                            principal, visibility, current_time(), product_id, self.test_index
                        ).where(chunks.c.chunk_id.in_(ids))
                    )
                    .mappings()
                    .all()
                )
                current = {row["chunk_id"]: self._candidate(row) for row in rows}
                for item in candidates:
                    fresh = current.get(item["chunk_id"])
                    if not fresh or char_count + len(fresh["text"]) > 6000:
                        continue
                    char_count += len(fresh["text"])
                    final.append(self._result_item(fresh, reranked))
        return {
            "items": final[:5],
            "degraded": degraded,
            "reason": reason,
            "retrieval": "keyword_vector_rrf" if dense else "keyword_only",
            "reranked": reranked,
            "embedding_revision": embedded.get("service_revision") if dense else None,
        }

    def reference(self, session, principal, revision_id, now):
        visibility = ["public"] if principal.kind == "guest" else ["public", "customer"]
        row = (
            session.execute(
                select(
                    revisions.c.body,
                    revisions.c.revision_id,
                    revisions.c.revision,
                    revisions.c.source_ref,
                    revisions.c.source_revision,
                    revisions.c.valid_from,
                    revisions.c.valid_until,
                    units.c.unit_id,
                    units.c.title,
                    units.c.active_revision,
                    units.c.visibility,
                )
                .join(units, units.c.unit_id == revisions.c.unit_id)
                .where(
                    revisions.c.revision_id == revision_id,
                    *_scope(principal),
                    revisions.c.status == "published",
                    units.c.status == "published",
                    units.c.active_revision == revisions.c.revision,
                    units.c.visibility.in_(visibility),
                    revisions.c.valid_from <= now,
                    and_(revisions.c.valid_until.is_(None), revisions.c.valid_from <= now)
                    | (revisions.c.valid_until > now),
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise AppError("not_found", 404, "資源不存在或不可訪問")
        return {
            "unit_id": row["unit_id"],
            "title": row["title"],
            "revision": row["revision"],
            "body": row["body"],
            "source_ref": row["source_ref"],
            "source_revision": row["source_revision"],
            "valid_from": row["valid_from"].isoformat(),
            "valid_until": row["valid_until"].isoformat() if row["valid_until"] else None,
        }

    @staticmethod
    def _active_chunk_query(principal, visibility, now, product_id=None, test_index=False):
        statement = select(
            chunks.c.chunk_id,
            chunks.c.revision_id,
            chunks.c.unit_id,
            chunks.c.ordinal,
            chunks.c.text,
            chunks.c.index_revision,
            units.c.title,
            revisions.c.revision,
            revisions.c.source_ref,
            revisions.c.source_revision,
            revisions.c.valid_from,
            revisions.c.valid_until,
        )
        return (
            statement.join(units, units.c.unit_id == chunks.c.unit_id)
            .join(revisions, revisions.c.revision_id == chunks.c.revision_id)
            .where(
                units.c.store_id == principal.store_id,
                units.c.data_batch_id == principal.data_batch_id,
                units.c.batch_generation == principal.batch_generation,
                chunks.c.store_id == principal.store_id,
                chunks.c.data_batch_id == principal.data_batch_id,
                chunks.c.batch_generation == principal.batch_generation,
                revisions.c.store_id == principal.store_id,
                revisions.c.data_batch_id == principal.data_batch_id,
                revisions.c.batch_generation == principal.batch_generation,
                units.c.status == "published",
                units.c.active_revision == revisions.c.revision,
                chunks.c.test_only.is_(test_index),
                units.c.product_id.is_(None) | (units.c.product_id == product_id),
                units.c.visibility.in_(visibility),
                revisions.c.status == "published",
                revisions.c.valid_from <= now,
                (revisions.c.valid_until.is_(None) | (revisions.c.valid_until > now)),
            )
        )

    @staticmethod
    def _candidate(row):
        return {
            "chunk_id": row["chunk_id"],
            "revision_id": row["revision_id"],
            "unit_id": row["unit_id"],
            "title": row["title"],
            "text": row["text"],
            "revision": row["revision"],
            "source_ref": row["source_ref"],
            "source_revision": row["source_revision"],
            "index_revision": row["index_revision"],
        }

    @staticmethod
    def _result_item(candidate, reranked):
        return {
            "text": candidate["text"],
            "citation": {
                "kind": "knowledge",
                "reference_id": candidate["revision_id"],
                "unit_id": candidate["unit_id"],
                "title": candidate["title"],
                "revision": candidate["revision"],
                "source_ref": candidate["source_ref"],
                "source_revision": candidate["source_revision"],
                "chunk_id": candidate["chunk_id"],
                "index_revision": candidate["index_revision"],
                "reranked": reranked,
            },
        }

    @staticmethod
    def _faq_match(session, principal, query, visibility, now, product_id=None):
        normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", query)).casefold()
        rows = session.execute(
            select(faqs).where(
                faqs.c.store_id == principal.store_id,
                faqs.c.data_batch_id == principal.data_batch_id,
                faqs.c.batch_generation == principal.batch_generation,
                faqs.c.status == "published",
                faqs.c.visibility.in_(visibility),
                faqs.c.product_id.is_(None) | (faqs.c.product_id == product_id),
            )
        )
        for row in rows.mappings():
            options = [row["question"], *(row["variants"] or [])]
            if normalized in {
                re.sub(r"\s+", "", unicodedata.normalize("NFKC", option)).casefold()
                for option in options
            }:
                if not KnowledgeService._faq_source_valid(session, principal, row, now):
                    continue
                return {
                    "items": [
                        {
                            "text": row["answer"],
                            "citation": {
                                "kind": "faq",
                                "reference_id": row["faq_revision_id"],
                                "faq_id": row["faq_id"],
                                "revision": row["revision"],
                                "source_ref": row["source_ref"],
                                "source_revision": row["source_revision"],
                                "title": row["question"],
                                "reranked": False,
                            },
                        }
                    ],
                    "degraded": False,
                    "reason": None,
                    "retrieval": "faq_exact",
                    "reranked": False,
                }
        return None


def seed_faq_drafts(db, dataset, now):
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    if payload.get("dataset_id") != "FAQ-SIM-v0.2" or payload.get("initial_status") != "draft":
        raise RuntimeError("FAQ source dataset contract mismatch")
    with db.transaction() as session:
        store = session.get(Store, "store_sim")
        if not store or store.status != "active":
            raise AppError("unavailable", 503, "店鋪暫不可用")
        batch = session.get(SourceBatch, store.data_batch_id)
        if not batch or batch.status != "active" or batch.generation != store.batch_generation:
            raise AppError("unavailable", 503, "店鋪暫不可用")
        existing = set(
            session.scalars(
                select(faqs.c.faq_id).where(
                    faqs.c.store_id == store.store_id,
                    faqs.c.data_batch_id == store.data_batch_id,
                    faqs.c.batch_generation == store.batch_generation,
                )
            ).all()
        )
        values = []
        for item in payload["items"]:
            if item["faq_id"] in existing:
                continue
            values.append(
                {
                    "faq_revision_id": new_id("faqrev"),
                    "faq_id": item["faq_id"],
                    "revision": 1,
                    "question": item["question"],
                    "variants": item["variants"],
                    "answer": item["answer"],
                    "source_ref": item["source_ref"],
                    "source_revision": item["source_revision"],
                    "topic": item["topic"],
                    "visibility": "public",
                    "status": "draft",
                    "synthetic": True,
                    "valid_from": now,
                    "valid_until": None,
                    "product_id": None,
                    "knowledge_revision_id": None,
                    "created_by": "synthetic-curation",
                    "reviewed_by": None,
                    "reviewed_at": None,
                    "published_at": None,
                    "retired_at": None,
                    "store_id": store.store_id,
                    "source_kind": "synthetic",
                    "data_batch_id": store.data_batch_id,
                    "batch_generation": store.batch_generation,
                    "version": 1,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        if values:
            session.execute(faqs.insert(), values)
    return {"inserted": len(values), "total": len(payload["items"]), "status": "draft"}
