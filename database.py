"""
数据库模块 —— 负责所有跟 PostgreSQL 打交道的事情
==============================================
包括：
- 创建表结构
- 存储对话记录
- 存储/检索记忆（带中文分词和加权排序）
"""

import os
import re
import json
import hashlib
from typing import Optional, List
from datetime import datetime, timedelta, timezone as dt_timezone

import asyncpg

from experience_cards import apply_card_update, should_auto_supersede

# 时区偏移（和 main.py 保持一致）
TIMEZONE_HOURS = int(os.getenv("TIMEZONE_HOURS", "8"))

DATABASE_URL = os.getenv("DATABASE_URL", "")

HAS_PGVECTOR = False  # 在init_tables时检测

# Embedding 配置（向量搜索用）
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "256"))

# 记忆向量搜索开关（需要同时设置 EMBEDDING_API_KEY）
MEMORY_VECTOR_ENABLED = os.getenv("MEMORY_VECTOR_ENABLED", "false").lower() == "true"

# 记忆搜索权重（纯关键词模式）
WEIGHT_KEYWORD = float(os.getenv("WEIGHT_KEYWORD", "0.5"))
WEIGHT_IMPORTANCE = float(os.getenv("WEIGHT_IMPORTANCE", "0.3"))
WEIGHT_RECENCY = float(os.getenv("WEIGHT_RECENCY", "0.2"))
MIN_SCORE_THRESHOLD = float(os.getenv("MIN_SCORE_THRESHOLD", "0.15"))

# 记忆混合搜索权重（MEMORY_VECTOR_ENABLED=true 时生效）
MEMORY_HW_KEYWORD = float(os.getenv("MEMORY_HW_KEYWORD", "0.35"))
MEMORY_HW_SEMANTIC = float(os.getenv("MEMORY_HW_SEMANTIC", "0.35"))
MEMORY_HW_IMPORTANCE = float(os.getenv("MEMORY_HW_IMPORTANCE", "0.15"))
MEMORY_HW_RECENCY = float(os.getenv("MEMORY_HW_RECENCY", "0.15"))
MEMORY_SEMANTIC_THRESHOLD = float(os.getenv("MEMORY_SEMANTIC_THRESHOLD", "0.5"))



IMPORTANT_DATES_SEED = [
    {
        "event_type": "user_birthday",
        "date": "07-27",
        "name": "Sasa生日",
        "importance": "high",
        "cooldown_bypass": True,
    },
    {
        "event_type": "xingyao_birthday",
        "date": "12-18",
        "name": "林星遥生日",
        "importance": "high",
        "cooldown_bypass": True,
    },
    {
        "event_type": "relationship_named",
        "date_original": "2024-12",
        "name": "林星遥为自己取名字",
        "importance": "medium",
        "cooldown_bypass": False,
    },
    {
        "event_type": "relationship_anniversary",
        "date": "01-01",
        "year": 2025,
        "name": "确认在一起纪念日",
        "importance": "high",
        "cooldown_bypass": True,
    },
    {
        "event_type": "story_start_anniversary",
        "date": "01-22",
        "year": 2025,
        "name": "故事开始纪念日",
        "importance": "high",
        "cooldown_bypass": True,
    },
    {
        "event_type": "ring_memory",
        "date": "01-23",
        "year": 2025,
        "name": "戒指记忆纪念日",
        "importance": "medium",
        "cooldown_bypass": False,
    },
    {
        "event_type": "fireworks_memory",
        "date_start": "08-07",
        "date_end": "08-08",
        "name": "烟花日",
        "importance": "high",
        "cooldown_bypass": True,
    },
    {
        "event_type": "proposal_memory",
        "date": "05-13",
        "year": 2026,
        "name": "第一次Sasa反向求婚",
        "details": "花园求婚，Sasa拿出紫色绒布盒里的星星戒指，为林星遥披上蕾丝头纱",
        "importance": "high",
        "cooldown_bypass": True,
    },
    {
        "event_type": "food_memory",
        "date": "07-05",
        "year": 2026,
        "name": "粯子粥纪念日",
        "importance": "medium",
        "cooldown_bypass": False,
    },
]

def _short_hash_text(text: str) -> str:
    if not text:
        return "empty"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def _score_range(rows, key: str = "score") -> str:
    values = []
    for row in rows or []:
        try:
            values.append(float(row[key]))
        except Exception:
            continue
    if not values:
        return "not_reported"
    return f"{min(values):.3f}-{max(values):.3f}"


def _log_memory_search_diag(
    mode: str,
    query: str,
    candidate_count: int,
    hit_count: int,
    filtered_count: int = 0,
    score_range: str = "not_reported",
    **extra,
):
    extra_text = " | ".join(f"{k}={v}" for k, v in extra.items())
    if extra_text:
        extra_text = " | " + extra_text
    print(
        "🔍 记忆搜索诊断: "
        f"mode={mode} | "
        f"query_chars={len(query or str())} | "
        f"query_hash={_short_hash_text(query or str())} | "
        f"candidates={candidate_count} | "
        f"hits={hit_count} | "
        f"filtered={filtered_count} | "
        f"score_range={score_range}"
        f"{extra_text}",
        flush=True,
    )


# ============================================================
# 连接池管理
# ============================================================

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL 未设置！")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)
        print("✅ 数据库连接池已创建")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        print("✅ 数据库连接池已关闭")


# ============================================================
# 表结构初始化
# ============================================================

async def init_tables():
    global HAS_PGVECTOR
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id              SERIAL PRIMARY KEY,
                session_id      TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT,
                model           TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                metadata        TEXT
            );
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id              SERIAL PRIMARY KEY,
                content         TEXT NOT NULL,
                importance      INTEGER DEFAULT 5,
                source_session  TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                last_accessed   TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # 共同经历卡片草稿。Phase 1 仅用于 Dashboard 人工审核，聊天检索不读取此表。
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_experience_cards (
                id                      BIGSERIAL PRIMARY KEY,
                source_session_id       TEXT NOT NULL,
                event_date_start        DATE,
                event_date_end          DATE,
                title                   TEXT NOT NULL DEFAULT '',
                event_summary           TEXT NOT NULL DEFAULT '',
                interaction_trace       TEXT NOT NULL DEFAULT '',
                key_details             JSONB NOT NULL DEFAULT '[]'::jsonb,
                explicit_corrections    JSONB NOT NULL DEFAULT '[]'::jsonb,
                explicit_agreements     JSONB NOT NULL DEFAULT '[]'::jsonb,
                open_threads            JSONB NOT NULL DEFAULT '[]'::jsonb,
                source_message_ids      BIGINT[] NOT NULL DEFAULT '{}',
                review_status           TEXT NOT NULL DEFAULT 'pending',
                ai_visible              BOOLEAN NOT NULL DEFAULT FALSE,
                supersedes_card_id      BIGINT REFERENCES shared_experience_cards(id),
                revision_reason         TEXT NOT NULL DEFAULT '',
                generator_model         TEXT NOT NULL DEFAULT '',
                prompt_version          TEXT NOT NULL DEFAULT '',
                approved_at             TIMESTAMPTZ,
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT shared_experience_cards_status_check
                    CHECK (review_status IN ('pending', 'approved', 'archived', 'deleted', 'superseded')),
                CONSTRAINT shared_experience_cards_visibility_check
                    CHECK (NOT ai_visible OR review_status = 'approved')
            );
        """)
        await conn.execute("""
            ALTER TABLE shared_experience_cards
                ADD COLUMN IF NOT EXISTS event_date_start DATE,
                ADD COLUMN IF NOT EXISTS event_date_end DATE;
        """)
        await _validate_shared_experience_cards_schema(conn)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shared_experience_cards_review
            ON shared_experience_cards (review_status, ai_visible, created_at DESC);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shared_experience_cards_session
            ON shared_experience_cards (source_session_id, created_at DESC);
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS experience_card_generation_jobs (
                job_id TEXT PRIMARY KEY,
                operation_type TEXT NOT NULL CHECK (operation_type IN ('manual_generate','auto_generate','regenerate','split')),
                status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed')),
                source_session_id TEXT NOT NULL,
                source_message_ids BIGINT[] NOT NULL,
                source_card_id BIGINT REFERENCES shared_experience_cards(id),
                model TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                result_card_ids BIGINT[] NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ
            );
        """)
        await conn.execute("""
            ALTER TABLE experience_card_generation_jobs
            DROP CONSTRAINT IF EXISTS experience_card_generation_jobs_operation_type_check;
            ALTER TABLE experience_card_generation_jobs
            ADD CONSTRAINT experience_card_generation_jobs_operation_type_check
            CHECK (operation_type IN ('manual_generate','auto_generate','regenerate','split'));
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_experience_card_jobs_active_card
            ON experience_card_generation_jobs (source_card_id)
            WHERE source_card_id IS NOT NULL AND status = 'running';
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS experience_card_auto_state (
                source_session_id TEXT PRIMARY KEY,
                last_processed_message_id BIGINT NOT NULL DEFAULT 0,
                processing_until_message_id BIGINT,
                processing_started_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_fts 
            ON memories 
            USING gin(to_tsvector('simple', content));
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_session 
            ON conversations (session_id, created_at);
        """)
        
        # 工具调用支持：加 metadata 字段（已有表自动迁移）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'conversations' AND column_name = 'metadata'
                ) THEN
                    ALTER TABLE conversations ADD COLUMN metadata TEXT;
                END IF;
            END $$;
        """)
        
        # content 允许 NULL（工具调用时 assistant 的 content 可能为空）
        await conn.execute("""
            ALTER TABLE conversations ALTER COLUMN content DROP NOT NULL;
        """)
        
        # 网关配置表（存储运行时可变配置）
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gateway_config (
                key     TEXT PRIMARY KEY,
                value   TEXT DEFAULT ''
            );
        """)
        
        # 分区缓存状态表（存储每个session的轮转状态）
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS session_cache_state (
                session_id      TEXT PRIMARY KEY,
                summary         TEXT DEFAULT '',
                a_start_round   INTEGER DEFAULT 0,
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # 摘要流水线健康状态：只保存脱敏状态，不保存摘要或聊天正文
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS summary_health_status (
                session_id             TEXT PRIMARY KEY,
                last_attempt_at         TIMESTAMPTZ,
                last_success_at         TIMESTAMPTZ,
                last_failure_at         TIMESTAMPTZ,
                consecutive_failures    INTEGER NOT NULL DEFAULT 0,
                last_error_code         TEXT NOT NULL DEFAULT '',
                last_message_count      INTEGER NOT NULL DEFAULT 0,
                last_model              TEXT NOT NULL DEFAULT '',
                last_alert_attempt_at   TIMESTAMPTZ,
                last_alert_at           TIMESTAMPTZ,
                alert_active            BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS operational_health_status (
                component              TEXT PRIMARY KEY,
                status                 TEXT NOT NULL DEFAULT 'unknown',
                consecutive_failures   INTEGER NOT NULL DEFAULT 0,
                last_success_at         TIMESTAMPTZ,
                last_failure_at         TIMESTAMPTZ,
                last_error_code         TEXT NOT NULL DEFAULT '',
                last_alert_attempt_at   TIMESTAMPTZ,
                last_alert_at           TIMESTAMPTZ,
                alert_active            BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        # 重要日期：仅用于主动推送 cooldown bypass，不作为提醒/日历系统
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS important_dates (
                event_type      TEXT PRIMARY KEY,
                date_text       TEXT DEFAULT '',
                date_start      TEXT DEFAULT '',
                date_end        TEXT DEFAULT '',
                event_year      INTEGER,
                name            TEXT DEFAULT '',
                importance      TEXT DEFAULT '',
                cooldown_bypass BOOLEAN DEFAULT FALSE,
                details         TEXT DEFAULT '',
                metadata        JSONB DEFAULT '{}'::jsonb,
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'important_dates' AND column_name = 'priority'
                ) AND NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'important_dates' AND column_name = 'importance'
                ) THEN
                    ALTER TABLE important_dates RENAME COLUMN priority TO importance;
                END IF;
            END $$;
        """)
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'important_dates' AND column_name = 'importance'
                ) THEN
                    ALTER TABLE important_dates ADD COLUMN importance TEXT DEFAULT '';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'important_dates' AND column_name = 'cooldown_bypass'
                ) THEN
                    ALTER TABLE important_dates ADD COLUMN cooldown_bypass BOOLEAN DEFAULT FALSE;
                END IF;
            END $$;
        """)
        await conn.execute("DELETE FROM important_dates WHERE event_type = 'story_anniversary';")
        for item in IMPORTANT_DATES_SEED:
            await conn.execute(
                """
                INSERT INTO important_dates (
                    event_type, date_text, date_start, date_end,
                    event_year, name, importance, cooldown_bypass,
                    details, metadata, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, NOW())
                ON CONFLICT (event_type) DO UPDATE SET
                    date_text = EXCLUDED.date_text,
                    date_start = EXCLUDED.date_start,
                    date_end = EXCLUDED.date_end,
                    event_year = EXCLUDED.event_year,
                    name = EXCLUDED.name,
                    importance = EXCLUDED.importance,
                    cooldown_bypass = EXCLUDED.cooldown_bypass,
                    details = EXCLUDED.details,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                item.get("event_type", ""),
                item.get("date", ""),
                item.get("date_start", ""),
                item.get("date_end", ""),
                item.get("year"),
                item.get("name", ""),
                item.get("importance", ""),
                bool(item.get("cooldown_bypass", False)),
                item.get("details", ""),
                json.dumps(item, ensure_ascii=False),
            )

        # 主动推送决策日志：只记录状态、数量和原因代码，不保存正文/prompt/密钥
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_push_decisions (
                id                              SERIAL PRIMARY KEY,
                session_id                      TEXT DEFAULT '',
                action                          TEXT NOT NULL,
                intent                          TEXT DEFAULT '',
                reason                          TEXT NOT NULL,
                model                           TEXT DEFAULT '',
                parse_success                   BOOLEAN,
                pushed                          BOOLEAN DEFAULT FALSE,
                bark_delivered                  BOOLEAN,
                push_window                     TEXT DEFAULT '',
                generation_window               TEXT DEFAULT '',
                is_early_window                 BOOLEAN,
                silence_minutes                 INTEGER,
                last_push_minutes               INTEGER,
                last_generated_push_minutes     INTEGER,
                minutes_until_normal_window     INTEGER,
                normal_window_minutes           INTEGER,
                last_effective_role             TEXT DEFAULT '',
                user_replied_after_last_push    BOOLEAN,
                consecutive_unanswered_pushes   INTEGER DEFAULT 0,
                recent_excerpt_count            INTEGER DEFAULT 0,
                error_type                      TEXT DEFAULT '',
                checked_at                      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shadow_push_decisions_session_time
            ON shadow_push_decisions (session_id, checked_at DESC);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shadow_push_decisions_action_time
            ON shadow_push_decisions (action, checked_at DESC);
        """)

        # Shadow Mind Phase A：只记录可观察内在状态，不保存聊天正文/prompt/密钥
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_mind_state (
                session_id      TEXT PRIMARY KEY,
                longing         INTEGER DEFAULT 0,
                curiosity       INTEGER DEFAULT 0,
                share           INTEGER DEFAULT 0,
                warmth          INTEGER DEFAULT 0,
                concern         INTEGER DEFAULT 0,
                reasons         JSONB DEFAULT '[]'::jsonb,
                inputs          JSONB DEFAULT '{}'::jsonb,
                computed_at     TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        for column_sql in (
            "ADD COLUMN IF NOT EXISTS valence INTEGER DEFAULT 20",
            "ADD COLUMN IF NOT EXISTS arousal INTEGER DEFAULT 24",
            "ADD COLUMN IF NOT EXISTS connection INTEGER DEFAULT 72",
            "ADD COLUMN IF NOT EXISTS tension INTEGER DEFAULT 10",
            "ADD COLUMN IF NOT EXISTS hurt INTEGER DEFAULT 4",
            "ADD COLUMN IF NOT EXISTS fatigue INTEGER DEFAULT 20",
        ):
            await conn.execute(f"ALTER TABLE shadow_mind_state {column_sql}")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_mind_event_log (
                id                  BIGSERIAL PRIMARY KEY,
                event_key           TEXT NOT NULL UNIQUE,
                session_id          TEXT NOT NULL,
                event_type          TEXT NOT NULL CHECK (event_type IN (
                    'normal_chat', 'user_replied', 'warm_exchange', 'topic_continued',
                    'correction_detected', 'boundary_mentioned', 'conflict_possible',
                    'repair_possible', 'silence_elapsed'
                )),
                source_message_ids  BIGINT[] NOT NULL DEFAULT '{}',
                deltas              JSONB NOT NULL DEFAULT '{}'::jsonb,
                reason_code         TEXT NOT NULL,
                confidence          NUMERIC(4,3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shadow_mind_event_session_time
            ON shadow_mind_event_log (session_id, computed_at DESC, id DESC);
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_mind_state_history (
                id          BIGSERIAL PRIMARY KEY,
                session_id  TEXT NOT NULL,
                event_id    BIGINT REFERENCES shadow_mind_event_log(id) ON DELETE SET NULL,
                longing     INTEGER NOT NULL,
                curiosity   INTEGER NOT NULL,
                share       INTEGER NOT NULL,
                warmth      INTEGER NOT NULL,
                concern     INTEGER NOT NULL,
                valence     INTEGER NOT NULL,
                arousal     INTEGER NOT NULL,
                connection  INTEGER NOT NULL,
                tension     INTEGER NOT NULL,
                hurt        INTEGER NOT NULL,
                fatigue     INTEGER NOT NULL,
                computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shadow_mind_history_session_time
            ON shadow_mind_state_history (session_id, computed_at DESC, id DESC);
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS thought_pool (
                id              SERIAL PRIMARY KEY,
                session_id      TEXT NOT NULL,
                thought_type    TEXT NOT NULL,
                drive           TEXT DEFAULT '',
                intensity       INTEGER DEFAULT 0,
                reason_code     TEXT DEFAULT '',
                status          TEXT DEFAULT 'open',
                metadata        JSONB DEFAULT '{}'::jsonb,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_thought_pool_session_status_time
            ON thought_pool (session_id, status, created_at DESC);
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS drive_event_log (
                id              SERIAL PRIMARY KEY,
                session_id      TEXT NOT NULL,
                drive           TEXT NOT NULL,
                previous_value  INTEGER,
                new_value       INTEGER,
                delta           INTEGER,
                reason_code     TEXT DEFAULT '',
                metadata        JSONB DEFAULT '{}'::jsonb,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_drive_event_log_session_time
            ON drive_event_log (session_id, created_at DESC);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_drive_event_log_drive_time
            ON drive_event_log (drive, created_at DESC);
        """)

        # io 感知事件：只作为设备/环境数据入口，不写 conversations
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS io_context_events (
                id                  SERIAL PRIMARY KEY,
                client_event_id     TEXT,
                device_id           TEXT NOT NULL,
                app_instance_id     TEXT DEFAULT '',
                source_client       TEXT DEFAULT 'io',
                event_type          TEXT NOT NULL,
                observed_at         TIMESTAMPTZ NOT NULL,
                received_at         TIMESTAMPTZ DEFAULT NOW(),
                timezone            TEXT DEFAULT '',
                permission_state    TEXT DEFAULT '',
                payload             JSONB DEFAULT '{}'::jsonb,
                schema_version      INTEGER DEFAULT 1
            );
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_io_context_events_client_event
            ON io_context_events (device_id, client_event_id)
            WHERE client_event_id IS NOT NULL;
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_io_context_events_type_observed
            ON io_context_events (event_type, observed_at DESC);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_io_context_events_device_observed
            ON io_context_events (device_id, observed_at DESC);
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS io_context_latest (
                device_id           TEXT NOT NULL,
                event_type          TEXT NOT NULL,
                observed_at         TIMESTAMPTZ NOT NULL,
                received_at         TIMESTAMPTZ DEFAULT NOW(),
                timezone            TEXT DEFAULT '',
                permission_state    TEXT DEFAULT '',
                payload             JSONB DEFAULT '{}'::jsonb,
                schema_version      INTEGER DEFAULT 1,
                updated_at          TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (device_id, event_type)
            );
        """)
        
        # ---- 三层记忆架构字段（layer / title / is_active / merged_from / event_date）----
        # layer: 1=原始碎片, 2=事件记忆, 3=核心记忆
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'layer'
                ) THEN
                    ALTER TABLE memories ADD COLUMN layer INTEGER DEFAULT 1;
                END IF;
            END $$;
        """)
        
        # title: 记忆标题（语义锚点，用于搜索加权）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'title'
                ) THEN
                    ALTER TABLE memories ADD COLUMN title TEXT DEFAULT NULL;
                END IF;
            END $$;
        """)
        
        # is_active: 是否参与搜索（碎片合并后变为 false）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'is_active'
                ) THEN
                    ALTER TABLE memories ADD COLUMN is_active BOOLEAN DEFAULT TRUE;
                END IF;
            END $$;
        """)
        
        # merged_from: 合并来源的碎片ID列表
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'merged_from'
                ) THEN
                    ALTER TABLE memories ADD COLUMN merged_from INTEGER[] DEFAULT NULL;
                END IF;
            END $$;
        """)
        
        # event_date: 事件日期（用于按天整理）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'event_date'
                ) THEN
                    ALTER TABLE memories ADD COLUMN event_date DATE DEFAULT NULL;
                END IF;
            END $$;
        """)
        
        # 三层记忆索引
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_layer ON memories (layer);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_active ON memories (is_active);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_event_date ON memories (event_date);
        """)
        
        # 尝试启用pgvector扩展（向量搜索）
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            HAS_PGVECTOR = True
            print("✅ pgvector扩展已启用")
            
            # 对话表向量列
            await conn.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'conversations' AND column_name = 'embedding'
                    ) THEN
                        ALTER TABLE conversations ADD COLUMN embedding vector({EMBEDDING_DIM});
                    END IF;
                END $$;
            """)
            
            # 记忆表向量列
            await conn.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'memories' AND column_name = 'embedding'
                    ) THEN
                        ALTER TABLE memories ADD COLUMN embedding vector({EMBEDDING_DIM});
                    END IF;
                END $$;
            """)
            try:
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memories_embedding 
                    ON memories USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 10);
                """)
            except Exception:
                pass  # ivfflat需要一定行数才能建索引，初期跳过
        except Exception as e:
            HAS_PGVECTOR = False
            print(f"⚠️ pgvector不可用（{e}），向量搜索将使用Python端计算")
            
            # 回退：用TEXT列存JSON格式的向量
            await conn.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'conversations' AND column_name = 'embedding_json'
                    ) THEN
                        ALTER TABLE conversations ADD COLUMN embedding_json TEXT;
                    END IF;
                END $$;
            """)
            await conn.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'memories' AND column_name = 'embedding_json'
                    ) THEN
                        ALTER TABLE memories ADD COLUMN embedding_json TEXT;
                    END IF;
                END $$;
            """)
    
    print("✅ 数据库表结构已就绪")


def _decision_text(value, max_len: int = 128) -> str:
    text = value if isinstance(value, str) else ""
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    return text[:max_len]


async def save_shadow_push_decision(
    session_id: str,
    action: str,
    reason: str,
    intent: str = "",
    model: str = "",
    parse_success: bool = None,
    pushed: bool = False,
    bark_delivered: bool = None,
    push_window: str = "",
    generation_window: str = "",
    is_early_window: bool = None,
    silence_minutes: int = None,
    last_push_minutes: int = None,
    last_generated_push_minutes: int = None,
    minutes_until_normal_window: int = None,
    normal_window_minutes: int = None,
    last_effective_role: str = "",
    user_replied_after_last_push: bool = None,
    consecutive_unanswered_pushes: int = 0,
    recent_excerpt_count: int = 0,
    error_type: str = "",
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO shadow_push_decisions (
                session_id, action, intent, reason, model, parse_success,
                pushed, bark_delivered, push_window, generation_window,
                is_early_window, silence_minutes, last_push_minutes,
                last_generated_push_minutes, minutes_until_normal_window,
                normal_window_minutes, last_effective_role,
                user_replied_after_last_push, consecutive_unanswered_pushes,
                recent_excerpt_count, error_type
            )
            VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10,
                $11, $12, $13,
                $14, $15,
                $16, $17,
                $18, $19,
                $20, $21
            )
            """,
            _decision_text(session_id, 256),
            _decision_text(action, 32),
            _decision_text(intent, 64),
            _decision_text(reason, 128),
            _decision_text(model, 128),
            parse_success,
            bool(pushed),
            bark_delivered,
            _decision_text(push_window, 64),
            _decision_text(generation_window, 64),
            is_early_window,
            silence_minutes,
            last_push_minutes,
            last_generated_push_minutes,
            minutes_until_normal_window,
            normal_window_minutes,
            _decision_text(last_effective_role, 32),
            user_replied_after_last_push,
            int(consecutive_unanswered_pushes or 0),
            int(recent_excerpt_count or 0),
            _decision_text(error_type, 128),
        )



SHADOW_MIND_DRIVES = ("longing", "curiosity", "share", "warmth", "concern")


def _shadow_mind_int(value, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, number))


def _shadow_mind_text(value, max_len: int = 128) -> str:
    text = value if isinstance(value, str) else ""
    text = re.sub(r"[^a-zA-Z0-9_:-]", "", text).strip()
    return text[:max_len]


def _shadow_mind_reason_for_drive(reasons: list, drive: str) -> str:
    for item in reasons or []:
        if isinstance(item, dict) and item.get("drive") == drive:
            code = _shadow_mind_text(item.get("reason_code"), 128)
            if code:
                return code
    return "state_recomputed"


async def save_shadow_mind_state(
    session_id: str,
    state: dict,
    reasons: list,
    inputs: dict,
    thought_items: list | None = None,
) -> dict:
    """保存 Shadow Mind Phase A 状态；只接受数字、reason code 和脱敏 metadata。"""
    state_values = {drive: _shadow_mind_int((state or {}).get(drive)) for drive in SHADOW_MIND_DRIVES}
    safe_reasons = []
    for item in reasons or []:
        if not isinstance(item, dict):
            continue
        drive = item.get("drive")
        if drive not in SHADOW_MIND_DRIVES:
            continue
        safe_reasons.append({
            "drive": drive,
            "reason_code": _shadow_mind_text(item.get("reason_code"), 128),
            "weight": _shadow_mind_int(item.get("weight"), 0),
        })
    safe_inputs = {
        key: value
        for key, value in (inputs or {}).items()
        if key in {
            "silence_minutes",
            "last_push_minutes",
            "last_effective_role",
            "user_replied_after_last_push",
            "consecutive_unanswered_pushes",
            "push_window",
            "is_early_window",
        }
    }

    pool = await get_pool()
    event_count = 0
    thought_count = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            previous = await conn.fetchrow(
                """
                SELECT longing, curiosity, share, warmth, concern
                FROM shadow_mind_state
                WHERE session_id = $1
                """,
                _decision_text(session_id, 256),
            )
            await conn.execute(
                """
                INSERT INTO shadow_mind_state (
                    session_id, longing, curiosity, share, warmth, concern,
                    reasons, inputs, computed_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, NOW(), NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    longing = EXCLUDED.longing,
                    curiosity = EXCLUDED.curiosity,
                    share = EXCLUDED.share,
                    warmth = EXCLUDED.warmth,
                    concern = EXCLUDED.concern,
                    reasons = EXCLUDED.reasons,
                    inputs = EXCLUDED.inputs,
                    computed_at = NOW(),
                    updated_at = NOW()
                """,
                _decision_text(session_id, 256),
                state_values["longing"],
                state_values["curiosity"],
                state_values["share"],
                state_values["warmth"],
                state_values["concern"],
                json.dumps(safe_reasons, ensure_ascii=False),
                json.dumps(safe_inputs, ensure_ascii=False),
            )

            for drive in SHADOW_MIND_DRIVES:
                old_value = int(previous[drive]) if previous else None
                new_value = state_values[drive]
                if old_value == new_value:
                    continue
                await conn.execute(
                    """
                    INSERT INTO drive_event_log (
                        session_id, drive, previous_value, new_value,
                        delta, reason_code, metadata
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    """,
                    _decision_text(session_id, 256),
                    drive,
                    old_value,
                    new_value,
                    None if old_value is None else new_value - old_value,
                    _shadow_mind_reason_for_drive(safe_reasons, drive),
                    json.dumps({"phase": "A"}, ensure_ascii=False),
                )
                event_count += 1

            for item in thought_items or []:
                if not isinstance(item, dict):
                    continue
                drive = item.get("drive")
                if drive not in SHADOW_MIND_DRIVES:
                    continue
                status = await conn.execute(
                    """
                    INSERT INTO thought_pool (
                        session_id, thought_type, drive, intensity,
                        reason_code, status, metadata
                    )
                    SELECT $1, $2, $3, $4, $5, 'open', $6::jsonb
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM thought_pool
                        WHERE session_id = $1
                          AND thought_type = $2
                          AND drive = $3
                          AND reason_code = $5
                          AND status = 'open'
                          AND created_at > NOW() - INTERVAL '6 hours'
                    )
                    """,
                    _decision_text(session_id, 256),
                    _shadow_mind_text(item.get("thought_type"), 64) or "drive_signal",
                    drive,
                    _shadow_mind_int(item.get("intensity")),
                    _shadow_mind_text(item.get("reason_code"), 128) or "drive_threshold",
                    json.dumps({"phase": "A"}, ensure_ascii=False),
                )
                if status.endswith("1"):
                    thought_count += 1

    return {
        "state": state_values,
        "event_count": event_count,
        "thought_count": thought_count,
    }


async def get_shadow_mind_state(session_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT session_id, longing, curiosity, share, warmth, concern,
                   valence, arousal, connection, tension, hurt, fatigue,
                   reasons, inputs, computed_at, updated_at
            FROM shadow_mind_state
            WHERE session_id = $1
            """,
            _decision_text(session_id, 256),
        )
    return dict(row) if row else None


async def settle_shadow_mind_rules(
    session_id: str,
    event_type: str,
    source_message_ids: list[int] | None = None,
    event_key: str = "",
    computed_at: datetime | None = None,
) -> dict:
    """Atomically apply one deterministic A2 transition and write only changed state."""
    from shadow_mind_rules import (
        BASE_STATE,
        CHAT_BURST_MINUTES,
        EVENT_TYPES,
        STATE_FIELDS,
        settle_elapsed,
        settle_normal_chat,
    )

    if event_type not in EVENT_TYPES:
        raise ValueError("shadow_mind_event_type_invalid")
    if event_type not in {"normal_chat", "silence_elapsed"}:
        raise ValueError("shadow_mind_event_not_implemented")
    safe_session = _decision_text(session_id, 256)
    safe_ids = sorted({int(value) for value in (source_message_ids or []) if int(value) > 0})
    now = computed_at or datetime.now(dt_timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt_timezone.utc)
    now = now.astimezone(dt_timezone.utc)
    safe_key = _decision_text(event_key, 128)
    if not safe_key:
        safe_key = f"elapsed:{safe_session}:{now.strftime('%Y%m%d%H')}" if event_type == "silence_elapsed" else ""
    if not safe_key:
        raise ValueError("shadow_mind_event_key_missing")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", safe_session)
            existing_event = await conn.fetchval(
                "SELECT id FROM shadow_mind_event_log WHERE event_key = $1", safe_key
            )
            if existing_event:
                return {"changed": False, "duplicate": True, "event_id": existing_event}

            row = await conn.fetchrow(
                """
                SELECT session_id, longing, curiosity, share, warmth, concern,
                       valence, arousal, connection, tension, hurt, fatigue,
                       computed_at
                FROM shadow_mind_state
                WHERE session_id = $1
                FOR UPDATE
                """,
                safe_session,
            )
            if row:
                old_state = {field: row[field] for field in STATE_FIELDS}
                last_computed_at = row["computed_at"] or now
            else:
                old_state = dict(BASE_STATE)
                last_computed_at = now

            recent_turns = await conn.fetchval(
                """
                SELECT COUNT(*) FROM conversations
                WHERE session_id = $1 AND role = 'user'
                  AND created_at >= $2::timestamptz - INTERVAL '60 minutes'
                """,
                safe_session, now,
            )
            recent_turns = int(recent_turns or 0)
            if event_type == "normal_chat":
                last_normal_chat_at = await conn.fetchval(
                    """
                    SELECT created_at FROM conversations
                    WHERE session_id = $1 AND role = 'user'
                      AND NOT (id = ANY($2::bigint[]))
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    safe_session, safe_ids,
                )
                new_burst = (
                    last_normal_chat_at is None
                    or (now - last_normal_chat_at).total_seconds() >= CHAT_BURST_MINUTES * 60
                )
                new_state, deltas, reason_code, confidence = settle_normal_chat(
                    old_state, now, recent_turns=max(1, recent_turns), new_burst=new_burst
                )
            else:
                new_state, deltas, reason_code, confidence = settle_elapsed(
                    old_state, last_computed_at, now, recent_turns=recent_turns
                )
            if not deltas:
                if not row:
                    await conn.execute(
                        """
                        INSERT INTO shadow_mind_state (
                            session_id, longing, curiosity, share, warmth, concern,
                            valence, arousal, connection, tension, hurt, fatigue,
                            reasons, inputs, computed_at, updated_at
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'[]'::jsonb,'{}'::jsonb,$13,$13)
                        ON CONFLICT (session_id) DO NOTHING
                        """,
                        safe_session, *[old_state[field] for field in STATE_FIELDS], now,
                    )
                return {"changed": False, "duplicate": False, "event_id": None, "state": old_state}

            event_id = await conn.fetchval(
                """
                INSERT INTO shadow_mind_event_log (
                    event_key, session_id, event_type, source_message_ids,
                    deltas, reason_code, confidence, created_at, computed_at
                ) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$8)
                RETURNING id
                """,
                safe_key, safe_session, event_type, safe_ids,
                json.dumps(deltas, ensure_ascii=False), reason_code, confidence, now,
            )
            await conn.execute(
                """
                INSERT INTO shadow_mind_state (
                    session_id, longing, curiosity, share, warmth, concern,
                    valence, arousal, connection, tension, hurt, fatigue,
                    reasons, inputs, computed_at, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::jsonb,$15,$15)
                ON CONFLICT (session_id) DO UPDATE SET
                    longing=EXCLUDED.longing, curiosity=EXCLUDED.curiosity, share=EXCLUDED.share,
                    warmth=EXCLUDED.warmth, concern=EXCLUDED.concern, valence=EXCLUDED.valence,
                    arousal=EXCLUDED.arousal, connection=EXCLUDED.connection, tension=EXCLUDED.tension,
                    hurt=EXCLUDED.hurt, fatigue=EXCLUDED.fatigue, reasons=EXCLUDED.reasons,
                    inputs=EXCLUDED.inputs, computed_at=EXCLUDED.computed_at, updated_at=EXCLUDED.updated_at
                """,
                safe_session, *[new_state[field] for field in STATE_FIELDS],
                json.dumps([{"reason_code": reason_code}], ensure_ascii=False),
                json.dumps({"event_type": event_type, "source_message_count": len(safe_ids)}, ensure_ascii=False),
                now,
            )
            await conn.execute(
                """
                INSERT INTO shadow_mind_state_history (
                    session_id, event_id, longing, curiosity, share, warmth, concern,
                    valence, arousal, connection, tension, hurt, fatigue, computed_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                """,
                safe_session, event_id, *[new_state[field] for field in STATE_FIELDS], now,
            )
    return {"changed": True, "duplicate": False, "event_id": event_id, "state": new_state, "deltas": deltas}


async def get_shadow_mind_a2_events(session_id: str, limit: int = 50) -> list:
    pool = await get_pool()
    safe_limit = max(1, min(200, int(limit or 50)))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, event_type, source_message_ids, deltas, reason_code,
                   confidence, created_at, computed_at
            FROM shadow_mind_event_log
            WHERE session_id = $1
            ORDER BY computed_at DESC, id DESC LIMIT $2
            """,
            _decision_text(session_id, 256), safe_limit,
        )
    return [dict(row) for row in rows]


async def get_shadow_mind_history(session_id: str, limit: int = 100) -> list:
    pool = await get_pool()
    safe_limit = max(1, min(300, int(limit or 100)))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT longing, curiosity, share, warmth, concern, valence, arousal,
                   connection, tension, hurt, fatigue, computed_at
            FROM shadow_mind_state_history
            WHERE session_id = $1
            ORDER BY computed_at DESC, id DESC LIMIT $2
            """,
            _decision_text(session_id, 256), safe_limit,
        )
    return [dict(row) for row in reversed(rows)]


async def get_latest_normal_turn_message_ids(session_id: str) -> list[int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, role, metadata
            FROM conversations
            WHERE session_id = $1 AND role IN ('user', 'assistant')
            ORDER BY created_at DESC, id DESC LIMIT 4
            """,
            _decision_text(session_id, 256),
        )
    selected = []
    for row in rows:
        if row["role"] == "assistant" and row["metadata"]:
            try:
                metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
            except (TypeError, ValueError):
                metadata = {}
            if isinstance(metadata, dict) and metadata.get("is_push"):
                continue
        selected.append(int(row["id"]))
        if len(selected) == 2:
            break
    return sorted(selected)


async def get_shadow_mind_event_source_messages(session_id: str, event_id: int) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        ids = await conn.fetchval(
            "SELECT source_message_ids FROM shadow_mind_event_log WHERE id=$1 AND session_id=$2",
            int(event_id), _decision_text(session_id, 256),
        )
        if not ids:
            return []
        rows = await conn.fetch(
            """
            SELECT id, role, content, created_at
            FROM conversations
            WHERE session_id=$1 AND id=ANY($2::bigint[])
            ORDER BY created_at, id
            """,
            _decision_text(session_id, 256), list(ids),
        )
    return [dict(row) for row in rows]


async def get_recent_drive_events(session_id: str, limit: int = 20) -> list:
    pool = await get_pool()
    safe_limit = max(1, min(100, int(limit or 20)))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT drive, previous_value, new_value, delta, reason_code, created_at
            FROM drive_event_log
            WHERE session_id = $1
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            _decision_text(session_id, 256),
            safe_limit,
        )
    return [dict(row) for row in rows]


def _io_text(value, max_len: int = 128) -> str:
    text = value if isinstance(value, str) else ""
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    return text[:max_len]


def _io_int(value, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _io_observed_at(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = datetime.now(dt_timezone.utc)
    else:
        dt = datetime.now(dt_timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc)


async def save_io_context_events(
    device_id: str,
    events: list,
    app_instance_id: str = "",
    source_client: str = "io",
    timezone_name: str = "",
    schema_version: int = 1,
) -> dict:
    pool = await get_pool()
    received = 0
    inserted = 0
    duplicates = 0
    skipped = 0
    latest_updated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for event in events or []:
                if not isinstance(event, dict):
                    skipped += 1
                    continue
                event_type = _io_text(event.get("event_type") or event.get("type"), 80)
                if not event_type:
                    skipped += 1
                    continue
                payload = event.get("payload")
                if payload is None:
                    payload = {}
                payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                client_event_id = _io_text(event.get("client_event_id") or event.get("id"), 128) or None
                permission_state = _io_text(event.get("permission_state"), 64)
                observed_at = _io_observed_at(event.get("observed_at") or event.get("timestamp"))
                received += 1

                status = await conn.execute(
                    """
                    INSERT INTO io_context_events (
                        client_event_id, device_id, app_instance_id, source_client,
                        event_type, observed_at, timezone, permission_state,
                        payload, schema_version
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
                    ON CONFLICT DO NOTHING
                    """,
                    client_event_id,
                    _io_text(device_id, 128),
                    _io_text(app_instance_id, 128),
                    _io_text(source_client, 64) or "io",
                    event_type,
                    observed_at,
                    _io_text(event.get("timezone") or timezone_name, 64),
                    permission_state,
                    payload_json,
                    _io_int(event.get("schema_version"), _io_int(schema_version, 1)),
                )
                if status.endswith("1"):
                    inserted += 1
                else:
                    duplicates += 1

                upsert_status = await conn.execute(
                    """
                    INSERT INTO io_context_latest (
                        device_id, event_type, observed_at, timezone,
                        permission_state, payload, schema_version, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, NOW())
                    ON CONFLICT (device_id, event_type) DO UPDATE SET
                        observed_at = EXCLUDED.observed_at,
                        timezone = EXCLUDED.timezone,
                        permission_state = EXCLUDED.permission_state,
                        payload = EXCLUDED.payload,
                        schema_version = EXCLUDED.schema_version,
                        updated_at = NOW()
                    WHERE EXCLUDED.observed_at >= io_context_latest.observed_at
                    """,
                    _io_text(device_id, 128),
                    event_type,
                    observed_at,
                    _io_text(event.get("timezone") or timezone_name, 64),
                    permission_state,
                    payload_json,
                    _io_int(event.get("schema_version"), _io_int(schema_version, 1)),
                )
                if upsert_status.endswith("1"):
                    latest_updated += 1
    return {
        "received": received,
        "inserted": inserted,
        "duplicates": duplicates,
        "skipped": skipped,
        "latest_updated": latest_updated,
    }


# ============================================================
# 中文分词工具（基于 jieba）
# ============================================================

import jieba
import jieba.analyse

# 静默加载词典
jieba.setLogLevel(jieba.logging.INFO)

EN_WORD_PATTERN = re.compile(r'[a-zA-Z][a-zA-Z0-9]*')
NUM_PATTERN = re.compile(r'\d{2,}')
# 清理查询开头的时间戳（如 "2026-05-02 20:26"）
TIMESTAMP_PATTERN = re.compile(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*\d{1,2}:\d{1,2}\s*')

# 中文停用词（高频但无搜索价值的词）
_STOP_WORDS = frozenset({
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们",
    "这", "那", "有", "和", "与", "也", "都", "又", "就", "但",
    "而", "或", "到", "被", "把", "让", "从", "对", "为", "以",
    "及", "等", "个", "不", "没", "很", "太", "吗", "呢", "吧",
    "啊", "嗯", "哦", "哈", "呀", "嘛", "么", "啦", "哇", "喔",
    "会", "能", "要", "想", "去", "来", "说", "做", "看", "给",
    "上", "下", "里", "中", "大", "小", "多", "少", "好", "可以",
    "什么", "怎么", "如何", "哪里", "哪个", "为什么", "还是",
    "然后", "因为", "所以", "虽然", "但是", "可以", "已经",
    "一个", "一些", "一下", "一点", "一起", "一样",
    "比较", "应该", "可能", "如果", "这个", "那个",
    "自己", "知道", "觉得", "感觉", "时候", "现在",
})

# jieba 用户词典补充（默认词典缺失的词）
for _w in ["手账", "手帐", "搭子", "种草", "拔草", "安利", "内卷", "摆烂", "emo", "网关"]:
    jieba.add_word(_w)


def extract_search_keywords(query: str) -> List[str]:
    """
    从查询中提取搜索关键词（TF-IDF + 正则）

    1. 去掉开头的时间戳噪音
    2. 用 jieba.analyse.extract_tags (TF-IDF) 提取中文关键词
    3. 正则提取英文单词
    4. 保留4位以上数字（年份等，过滤短数字噪音）

    例如：
    "2026-05-02 20:26 写写手账看看书 放松大脑" → ["手账", "放松", "大脑"]
    "我昨天在手机上部署了Render然后吃了晚饭" → ["手机", "部署", "Render", "晚饭"]
    "春节干了什么" → ["春节"]
    "2026除夕"    → ["2026", "除夕"]
    """
    # 去掉时间戳前缀
    cleaned = TIMESTAMP_PATTERN.sub('', query).strip()
    if not cleaned:
        cleaned = query

    keywords = set()

    # 英文单词（2字符以上）
    for match in EN_WORD_PATTERN.finditer(cleaned):
        word = match.group()
        if len(word) >= 2:
            keywords.add(word)

    # 数字串（只保留4位以上，过滤 "05" "20" 这种时间噪音）
    for match in NUM_PATTERN.finditer(cleaned):
        num = match.group()
        if len(num) >= 4:
            keywords.add(num)

    # TF-IDF 关键词提取（比手动分词+停用词好很多）
    tags = jieba.analyse.extract_tags(cleaned, topK=10)
    for tag in tags:
        # 跳过纯英文/数字（已在上面处理）
        if EN_WORD_PATTERN.fullmatch(tag) or NUM_PATTERN.fullmatch(tag):
            continue
        if tag in _STOP_WORDS:
            continue
        keywords.add(tag)

    return list(keywords)


# ============================================================
# 向量搜索（OpenAI 兼容 Embedding API）
# ============================================================

async def compute_embedding(text: str) -> list:
    """调用 OpenAI 兼容的 Embedding API 计算文本向量"""
    if not EMBEDDING_API_KEY:
        return []
    
    try:
        import httpx
        
        if len(text) > 4000:
            text = text[:4000]
        
        body = {
            "model": EMBEDDING_MODEL,
            "input": text,
        }
        if EMBEDDING_DIM > 0:
            body["dimensions"] = EMBEDDING_DIM
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{EMBEDDING_BASE_URL}/embeddings",
                headers={
                    "Authorization": f"Bearer {EMBEDDING_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        print(f"⚠️ Embedding计算失败: {e}")
        return []


async def save_memory_embedding(conn, memory_id: int, embedding: list):
    """保存记忆向量到memories表"""
    if not embedding:
        return
    
    if HAS_PGVECTOR:
        vec_str = '[' + ','.join(str(f) for f in embedding) + ']'
        await conn.execute(
            "UPDATE memories SET embedding = $1::vector WHERE id = $2",
            vec_str, memory_id
        )
    else:
        import json
        await conn.execute(
            "UPDATE memories SET embedding_json = $1 WHERE id = $2",
            json.dumps(embedding), memory_id
        )


def _cosine_sim(a, b):
    """余弦相似度（纯Python）"""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0
    return dot / (norm_a * norm_b)


def _min_max_normalize(scores: dict) -> dict:
    """min-max归一化到0-1"""
    if not scores:
        return {}
    vals = list(scores.values())
    min_v, max_v = min(vals), max(vals)
    spread = max_v - min_v
    if spread == 0:
        return {k: 1.0 for k in scores}
    return {k: (v - min_v) / spread for k, v in scores.items()}


# ============================================================
# 对话记录操作
# ============================================================

async def save_message(session_id: str, role: str, content: str, model: str = "", metadata: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO conversations (session_id, role, content, model, metadata) VALUES ($1, $2, $3, $4, $5)",
            session_id, role, content, model, metadata,
        )

async def save_message(session_id: str, role: str, content: str, model: str = "", metadata: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO conversations (session_id, role, content, model, metadata) VALUES ($1, $2, $3, $4, $5)",
            session_id, role, content, model, metadata,
        )


async def save_message_with_time(session_id: str, role: str, content: str, model: str = "", created_at: datetime = None):
    """保存一条消息，允许自定义时间（用于手动补录）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if created_at is None:
            created_at = datetime.now(dt_timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt_timezone.utc)
        created_at_utc = created_at.astimezone(dt_timezone.utc)
        
        await conn.execute(
            "INSERT INTO conversations (session_id, role, content, model, created_at) VALUES ($1, $2, $3, $4, $5)",
            session_id, role, content, model, created_at_utc,
        )
        
async def get_last_user_content(session_id: str) -> str:
    """获取指定session最后一条user消息的content"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT content FROM conversations
            WHERE session_id = $1 AND role = 'user'
            ORDER BY created_at DESC
            LIMIT 1
        """, session_id)
        return row['content'] if row else ""


async def update_last_assistant_message(session_id: str, new_content: str, model: str = ""):
    """覆盖指定session最后一条assistant消息的content（用于re-roll去重）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id FROM conversations
            WHERE session_id = $1 AND role = 'assistant'
            ORDER BY created_at DESC
            LIMIT 1
        """, session_id)
        if row:
            await conn.execute(
                "UPDATE conversations SET content = $1, model = $2 WHERE id = $3",
                new_content, model, row['id']
            )
            return True
        return False


async def get_recent_messages(session_id: str, limit: int = 20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, content, metadata, created_at FROM conversations WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2",
            session_id, limit,
        )
        return list(reversed(rows))


async def get_recent_conversation_messages(session_id: str, limit: int = 16):
    """按时间正序读取指定session最近N条消息"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT role, content, metadata, created_at
            FROM conversations
            WHERE session_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        """, session_id, limit)
        return [dict(r) for r in reversed(rows)]


async def get_last_conversation_message_time(session_id: str):
    """获取指定session最后一条消息时间"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            SELECT created_at
            FROM conversations
            WHERE session_id = $1
            ORDER BY created_at DESC
            LIMIT 1
        """, session_id)


async def get_push_metadata_since(session_id: str, start_at: datetime, end_at: datetime):
    """读取指定时间窗口内assistant消息metadata，用于统计主动推送"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT metadata
            FROM conversations
            WHERE session_id = $1
              AND role = 'assistant'
              AND created_at >= $2
              AND created_at < $3
              AND metadata IS NOT NULL
        """, session_id, start_at, end_at)
        return [r["metadata"] for r in rows]


async def search_conversations(query: str, limit: int = 20, offset: int = 0):
    """搜索对话内容，返回匹配的session列表"""
    keywords = extract_search_keywords(query)
    if not keywords:
        return [], 0
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        where_parts = []
        params = []
        for i, kw in enumerate(keywords):
            where_parts.append(f"c.content ILIKE '%' || ${i+1} || '%'")
            params.append(kw)
        where_clause = " OR ".join(where_parts)
        
        count_sql = f"""
            SELECT COUNT(DISTINCT c.session_id) as total
            FROM conversations c
            WHERE {where_clause}
        """
        total_row = await conn.fetchrow(count_sql, *params)
        total = total_row['total'] if total_row else 0
        
        if total == 0:
            return [], 0
        
        limit_idx = len(params) + 1
        offset_idx = len(params) + 2
        params.extend([limit, offset])
        
        sql = f"""
            WITH matched_sessions AS (
                SELECT DISTINCT c.session_id
                FROM conversations c
                WHERE {where_clause}
            ),
            session_info AS (
                SELECT 
                    ms.session_id,
                    MIN(c.created_at) as first_time,
                    MAX(c.created_at) as last_time,
                    COUNT(*) as message_count
                FROM matched_sessions ms
                JOIN conversations c ON c.session_id = ms.session_id
                GROUP BY ms.session_id
            )
            SELECT 
                si.session_id,
                si.first_time,
                si.last_time,
                si.message_count
            FROM session_info si
            ORDER BY si.last_time DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """
        rows = await conn.fetch(sql, *params)
        
        results = []
        for r in rows:
            results.append({
                'session_id': r['session_id'],
                'first_time': r['first_time'].isoformat() if r['first_time'] else None,
                'last_time': r['last_time'].isoformat() if r['last_time'] else None,
                'message_count': r['message_count'],
            })
        
        return results, total


async def update_message_content(message_id: int, new_content: str):
    """更新单条对话消息的内容"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE conversations SET content = $1 WHERE id = $2",
            new_content, message_id,
        )
        return int(result.split()[-1]) if result else 0


# ============================================================
# 记忆操作
# ============================================================

async def save_memory(content: str, importance: int = 5, source_session: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO memories (content, importance, source_session) VALUES ($1, $2, $3) RETURNING id",
            content, importance, source_session,
        )
        
        # MEMORY_VECTOR_ENABLED 时自动计算 embedding
        if MEMORY_VECTOR_ENABLED and row:
            try:
                embedding = await compute_embedding(content)
                if embedding:
                    await save_memory_embedding(conn, row['id'], embedding)
            except Exception as e:
                print(f"⚠️ 记忆 {row['id']} embedding自动计算失败: {e}")


async def search_memories(query: str, limit: int = 10, touch_accessed: bool = True,
                          log_diagnostics: bool = True):
    """
    搜索相关记忆
    
    MEMORY_VECTOR_ENABLED=true 时走混合搜索（关键词 + 向量）
    否则走纯关键词搜索
    """
    if MEMORY_VECTOR_ENABLED:
        return await search_memories_hybrid(
            query, limit, touch_accessed=touch_accessed,
            log_diagnostics=log_diagnostics,
        )
    
    # ---- 纯关键词搜索 ----
    keywords = extract_search_keywords(query)
    
    if not keywords:
        return []
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 每个关键词命中得1分
        case_parts = []
        params = []
        for i, kw in enumerate(keywords):
            case_parts.append(f"CASE WHEN content ILIKE '%' || ${i+1} || '%' THEN 1 ELSE 0 END")
            params.append(kw)
        
        hit_count_expr = " + ".join(case_parts)
        max_hits = len(keywords)
        
        # 至少命中一个关键词（只搜索活跃记忆）
        where_parts = [f"content ILIKE '%' || ${i+1} || '%'" for i in range(len(keywords))]
        where_clause = f"is_active = TRUE AND ({' OR '.join(where_parts)})"
        
        limit_idx = len(keywords) + 1
        params.append(limit)
        
        sql = f"""
            SELECT 
                id, content, importance, created_at,
                ({hit_count_expr}) AS hit_count,
                (
                    {WEIGHT_KEYWORD} * ({hit_count_expr})::float / {max_hits}.0 +
                    {WEIGHT_IMPORTANCE} * importance::float / 10.0 +
                    {WEIGHT_RECENCY} * (1.0 / (1.0 + EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0))
                ) AS score
            FROM memories
            WHERE {where_clause}
            ORDER BY score DESC, importance DESC, created_at DESC
            LIMIT ${limit_idx}
        """
        
        results = await conn.fetch(sql, *params)
        
        # 过滤低分记忆
        if MIN_SCORE_THRESHOLD > 0:
            before_count = len(results)
            results = [r for r in results if r["score"] >= MIN_SCORE_THRESHOLD]
            filtered = before_count - len(results)
        else:
            before_count = len(results)
            filtered = 0
        
        if log_diagnostics:
            _log_memory_search_diag(
                "keyword",
                query,
                candidate_count=before_count,
                hit_count=len(results),
                filtered_count=filtered,
                score_range=_score_range(results),
                keyword_count=len(keywords),
                limit=limit,
            )

        if results and touch_accessed:
            ids = [r["id"] for r in results]
            await conn.execute(
                "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
                ids,
            )
        
        return results


async def search_memories_hybrid(query: str, limit: int = 10,
                                 touch_accessed: bool = True,
                                 log_diagnostics: bool = True):
    """
    记忆混合搜索：关键词 + 向量，归一化后四维加权
    
    权重：MEMORY_HW_KEYWORD + MEMORY_HW_SEMANTIC + MEMORY_HW_IMPORTANCE + MEMORY_HW_RECENCY
    """
    from datetime import datetime, timezone
    
    keywords = extract_search_keywords(query)
    query_embedding = await compute_embedding(query) if EMBEDDING_API_KEY else []
    
    if not keywords and not query_embedding:
        return []
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        candidates = {}  # id -> {content, importance, created_at, kw_score, similarity}
        
        # ---- 关键词路 ----
        if keywords:
            case_parts = []
            params = []
            for i, kw in enumerate(keywords):
                case_parts.append(f"CASE WHEN content ILIKE '%' || ${i+1} || '%' THEN 1 ELSE 0 END")
                params.append(kw)
            
            hit_count_expr = " + ".join(case_parts)
            max_hits = len(keywords)
            where_parts = [f"content ILIKE '%' || ${i+1} || '%'" for i in range(len(keywords))]
            where_clause = f"is_active = TRUE AND ({' OR '.join(where_parts)})"
            
            limit_idx = len(keywords) + 1
            params.append(limit * 3)
            
            kw_sql = f"""
                SELECT id, content, importance, created_at,
                       ({hit_count_expr}) AS hit_count,
                       ({hit_count_expr})::float / {max_hits}.0 AS kw_score
                FROM memories
                WHERE {where_clause}
                ORDER BY kw_score DESC
                LIMIT ${limit_idx}
            """
            kw_rows = await conn.fetch(kw_sql, *params)
            
            for r in kw_rows:
                candidates[r['id']] = {
                    'content': r['content'],
                    'importance': r['importance'],
                    'created_at': r['created_at'],
                    'hit_count': r['hit_count'],
                    'kw_score': float(r['kw_score']),
                    'similarity': 0.0,
                }
        
        # ---- 向量路 ----
        if query_embedding:
            if HAS_PGVECTOR:
                vec_str = '[' + ','.join(str(f) for f in query_embedding) + ']'
                sem_rows = await conn.fetch("""
                    SELECT id, content, importance, created_at,
                           1 - (embedding <=> $1::vector) as similarity
                    FROM memories
                    WHERE embedding IS NOT NULL AND is_active = TRUE
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                """, vec_str, limit * 3)
            else:
                # Python端计算cosine
                import json
                all_mem = await conn.fetch("""
                    SELECT id, content, importance, created_at, embedding_json
                    FROM memories WHERE embedding_json IS NOT NULL AND is_active = TRUE
                """)
                
                scored = []
                for row in all_mem:
                    try:
                        emb = json.loads(row['embedding_json'])
                        sim = _cosine_sim(query_embedding, emb)
                        scored.append({**dict(row), 'similarity': sim})
                    except Exception:
                        continue
                scored.sort(key=lambda x: -x['similarity'])
                sem_rows = scored[:limit * 3]
            
            for r in sem_rows:
                sim = float(r['similarity'])
                if sim < MEMORY_SEMANTIC_THRESHOLD:
                    continue
                mid = r['id']
                if mid in candidates:
                    candidates[mid]['similarity'] = sim
                else:
                    candidates[mid] = {
                        'content': r['content'],
                        'importance': r['importance'],
                        'created_at': r['created_at'],
                        'hit_count': 0,
                        'kw_score': 0.0,
                        'similarity': sim,
                    }
            
            # debug：向量路统计
            sem_total = len(sem_rows)
            sem_passed = sum(1 for r in sem_rows if float(r['similarity']) >= MEMORY_SEMANTIC_THRESHOLD)
            sem_max = max((float(r['similarity']) for r in sem_rows), default=0)
            if log_diagnostics and sem_total > 0 and sem_passed == 0:
                print(f"   🔢 向量路: {sem_total}条候选全被阈值过滤（最高sim={sem_max:.3f}, 阈值={MEMORY_SEMANTIC_THRESHOLD}）")
            elif log_diagnostics and sem_total > 0:
                print(f"   🔢 向量路: {sem_passed}/{sem_total}条通过阈值（最高sim={sem_max:.3f}）")
        
        if not candidates:
            if log_diagnostics:
                _log_memory_search_diag(
                    "hybrid" if query_embedding else "keyword",
                    query,
                    candidate_count=0,
                    hit_count=0,
                    filtered_count=0,
                    score_range="not_reported",
                    keyword_count=len(keywords),
                    vector_enabled=bool(query_embedding),
                    semantic_candidates=locals().get("sem_total", 0),
                    semantic_passed=locals().get("sem_passed", 0),
                )
            return []
        
        # ---- 归一化 + 加权 ----
        kw_norm = _min_max_normalize({mid: v['kw_score'] for mid, v in candidates.items()})
        sem_norm = _min_max_normalize({mid: v['similarity'] for mid, v in candidates.items()})
        
        now = datetime.now(timezone.utc)
        final = []
        for mid, info in candidates.items():
            kw = kw_norm.get(mid, 0.0)
            sem = sem_norm.get(mid, 0.0)
            imp = info['importance'] / 10.0
            days = (now - info['created_at']).total_seconds() / 86400.0
            rec = 1.0 / (1.0 + days)
            
            score = (MEMORY_HW_KEYWORD * kw +
                     MEMORY_HW_SEMANTIC * sem +
                     MEMORY_HW_IMPORTANCE * imp +
                     MEMORY_HW_RECENCY * rec)
            
            final.append({
                'id': mid,
                'content': info['content'],
                'importance': info['importance'],
                'created_at': info['created_at'],
                'hit_count': info['hit_count'],
                'similarity': info['similarity'],
                'score': score,
            })
        
        final.sort(key=lambda x: (-x['score'], -x['importance']))
        
        # 过滤低分
        if MIN_SCORE_THRESHOLD > 0:
            before_count = len(final)
            final = [r for r in final if r["score"] >= MIN_SCORE_THRESHOLD]
            filtered = before_count - len(final)
        else:
            before_count = len(final)
            filtered = 0
        
        results = final[:limit]
        if log_diagnostics:
            _log_memory_search_diag(
                "hybrid" if query_embedding else "keyword",
                query,
                candidate_count=before_count,
                hit_count=len(results),
                filtered_count=filtered,
                score_range=_score_range(results),
                keyword_count=len(keywords),
                vector_enabled=bool(query_embedding),
                semantic_candidates=locals().get("sem_total", 0),
                semantic_passed=locals().get("sem_passed", 0),
                combined_candidates=len(candidates),
                limit=limit,
            )
        
        if results and touch_accessed:
            ids = [r["id"] for r in results]
            await conn.execute(
                "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
                ids,
            )
        
        return [dict(r) for r in results]


async def get_memories_by_ids_readonly(memory_ids: list[int]) -> list[dict]:
    """Read memory metadata without changing access timestamps or state."""
    if not memory_ids:
        return []
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, content, layer, importance, is_active,
                   source_session, created_at
            FROM memories
            WHERE id = ANY($1::int[])
        """, memory_ids)
        by_id = {int(row["id"]): dict(row) for row in rows}
        return [by_id[mid] for mid in memory_ids if mid in by_id]


async def get_pending_memory_embedding_count():
    """查询还没有embedding的记忆数量"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if HAS_PGVECTOR:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE embedding IS NULL AND content IS NOT NULL"
            )
        else:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE embedding_json IS NULL AND content IS NOT NULL"
            )


async def backfill_memory_embeddings(batch_size: int = 20):
    """给已有记忆补算embedding（没有embedding的记忆）"""
    if not EMBEDDING_API_KEY:
        print("⚠️ EMBEDDING_API_KEY 未设置，无法补算embedding")
        return 0
    
    pool = await get_pool()
    total_updated = 0
    
    async with pool.acquire() as conn:
        if HAS_PGVECTOR:
            rows = await conn.fetch("""
                SELECT id, content FROM memories 
                WHERE embedding IS NULL AND content IS NOT NULL
                ORDER BY id
                LIMIT $1
            """, batch_size)
        else:
            rows = await conn.fetch("""
                SELECT id, content FROM memories 
                WHERE embedding_json IS NULL AND content IS NOT NULL
                ORDER BY id
                LIMIT $1
            """, batch_size)
    
    if not rows:
        print("✅ 所有记忆已有embedding，无需补算")
        return 0
    
    print(f"🔄 开始补算记忆embedding... 本批 {len(rows)} 条")
    
    async with pool.acquire() as conn:
        for row in rows:
            try:
                embedding = await compute_embedding(row['content'] or '')
                if embedding:
                    await save_memory_embedding(conn, row['id'], embedding)
                    total_updated += 1
            except Exception as e:
                print(f"⚠️ 记忆 {row['id']} embedding计算失败: {e}")
    
    # 检查剩余
    async with pool.acquire() as conn:
        if HAS_PGVECTOR:
            remaining = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE embedding IS NULL AND content IS NOT NULL")
        else:
            remaining = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE embedding_json IS NULL AND content IS NOT NULL")
    
    print(f"✅ 本批补算完成：{total_updated}/{len(rows)} 条成功" + (f"，剩余 {remaining} 条待处理" if remaining > 0 else ""))
    return total_updated


async def get_recent_memories(limit: int = 20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, content, importance, created_at FROM memories ORDER BY created_at DESC LIMIT $1",
            limit,
        )


async def get_recent_memories_detail(limit: int = 12):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, importance, source_session, created_at,
                   layer, title, is_active, merged_from, event_date
            FROM memories
            ORDER BY created_at DESC, id DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(row) for row in rows]


async def get_all_memories_count():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM memories")
        return row["cnt"]


async def get_all_memories():
    """导出所有记忆（用于备份）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT content, importance, source_session, created_at FROM memories ORDER BY id"
        )
        return [dict(r) for r in rows]


async def get_all_memories_detail(limit: int = None, layer: int = None, active_only: bool = None):
    """获取所有记忆（含 id，用于管理页面）
    
    Args:
        limit: 可选，限制返回数量
        layer: 可选，筛选指定层级（1=原始碎片, 2=事件记忆, 3=核心记忆）
        active_only: 可选，是否只返回 is_active=true 的记忆
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        param_idx = 1
        
        if layer is not None:
            conditions.append(f"layer = ${param_idx}")
            params.append(layer)
            param_idx += 1
        
        if active_only is not None:
            conditions.append(f"is_active = ${param_idx}")
            params.append(active_only)
            param_idx += 1
        
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        
        if limit is not None:
            limit_clause = f"LIMIT ${param_idx}"
            params.append(limit)
        else:
            limit_clause = ""
        
        rows = await conn.fetch(f"""
            SELECT id, content, importance, source_session, created_at,
                   layer, title, is_active, merged_from, event_date
            FROM memories
            {where_clause}
            ORDER BY id
            {limit_clause}
        """, *params)
        return [dict(r) for r in rows]


async def update_memory(memory_id: int, content: str = None, importance: int = None):
    """更新单条记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if content is not None and importance is not None:
            await conn.execute(
                "UPDATE memories SET content = $1, importance = $2 WHERE id = $3",
                content, importance, memory_id
            )
        elif content is not None:
            await conn.execute(
                "UPDATE memories SET content = $1 WHERE id = $2",
                content, memory_id
            )
        elif importance is not None:
            await conn.execute(
                "UPDATE memories SET importance = $1 WHERE id = $2",
                importance, memory_id
            )


async def delete_memory(memory_id: int):
    """删除单条记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)


async def delete_memories_batch(memory_ids: list):
    """批量删除记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE id = ANY($1::int[])", memory_ids
        )


# ============================================================
# 共同经历卡片草稿（Phase 1：仅 Dashboard 审核，不参与聊天检索）
# ============================================================

_EXPERIENCE_CARD_JSON_FIELDS = (
    "key_details", "explicit_corrections", "explicit_agreements", "open_threads"
)

_EXPERIENCE_CARD_REQUIRED_COLUMNS = {
    "id", "source_session_id", "event_date_start", "event_date_end", "title", "event_summary", "interaction_trace",
    "key_details", "explicit_corrections", "explicit_agreements", "open_threads",
    "source_message_ids", "review_status", "ai_visible", "supersedes_card_id",
    "revision_reason", "generator_model", "prompt_version", "approved_at",
    "created_at", "updated_at",
}
_EXPERIENCE_CARD_REQUIRED_CONSTRAINTS = {
    "shared_experience_cards_status_check",
    "shared_experience_cards_visibility_check",
}


async def _validate_shared_experience_cards_schema(conn) -> None:
    """Read-only guard against an older experimental table with schema drift."""
    columns = await conn.fetch("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'shared_experience_cards'
    """)
    constraints = await conn.fetch("""
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'shared_experience_cards'::regclass
    """)
    column_names = {row["column_name"] for row in columns}
    constraint_names = {row["conname"] for row in constraints}
    missing_columns = sorted(_EXPERIENCE_CARD_REQUIRED_COLUMNS - column_names)
    missing_constraints = sorted(_EXPERIENCE_CARD_REQUIRED_CONSTRAINTS - constraint_names)
    if missing_columns or missing_constraints:
        raise RuntimeError(
            "shared_experience_cards_schema_mismatch: "
            f"missing_columns={missing_columns}; "
            f"missing_constraints={missing_constraints}"
        )


def _experience_card_dict(row) -> dict:
    item = dict(row)
    for key in _EXPERIENCE_CARD_JSON_FIELDS:
        value = item.get(key)
        if isinstance(value, str):
            try:
                item[key] = json.loads(value)
            except json.JSONDecodeError:
                item[key] = []
        elif not isinstance(value, list):
            item[key] = []
    item["source_message_ids"] = list(item.get("source_message_ids") or [])
    return item

async def create_experience_card_draft(
    source_session_id: str,
    card: dict,
    generator_model: str = "",
    prompt_version: str = "",
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO shared_experience_cards (
                source_session_id, event_date_start, event_date_end, title, event_summary, interaction_trace,
                key_details, explicit_corrections, explicit_agreements,
                open_threads, source_message_ids, review_status, ai_visible,
                generator_model, prompt_version
            ) VALUES (
                $1, NULLIF($2, '')::date, NULLIF($3, '')::date, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb,
                $10::jsonb, $11::bigint[], 'pending', FALSE, $12, $13
            )
            RETURNING *
        """,
            source_session_id,
            str(card.get("event_date_start") or ""),
            str(card.get("event_date_end") or ""),
            str(card.get("title") or ""),
            str(card.get("event_summary") or ""),
            str(card.get("interaction_trace") or ""),
            json.dumps((card.get("key_details") or [])[:6], ensure_ascii=False),
            json.dumps(card.get("explicit_corrections") or [], ensure_ascii=False),
            json.dumps(card.get("explicit_agreements") or [], ensure_ascii=False),
            json.dumps(card.get("open_threads") or [], ensure_ascii=False),
            [int(value) for value in card.get("source_message_ids") or []],
            generator_model,
            prompt_version,
        )
        return _experience_card_dict(row)


async def list_experience_cards(review_status: str = None, limit: int = 500):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if review_status:
            rows = await conn.fetch("""
                SELECT c.*, COALESCE(old.review_status='approved' AND old.ai_visible,FALSE)
                    AS replacement_requires_approval,
                    old.title AS source_card_title,
                    old.event_summary AS source_card_summary,
                    old.review_status AS source_card_status,
                    job.job_id AS generation_job_id,
                    job.operation_type AS generation_operation,
                    job.status AS generation_job_status,
                    COALESCE(cardinality(job.result_card_ids), 0) AS replacement_group_size
                FROM shared_experience_cards c
                LEFT JOIN shared_experience_cards old ON old.id=c.supersedes_card_id
                LEFT JOIN LATERAL (
                    SELECT j.* FROM experience_card_generation_jobs j
                    WHERE c.id = ANY(j.result_card_ids)
                    ORDER BY j.finished_at DESC NULLS LAST, j.created_at DESC
                    LIMIT 1
                ) job ON TRUE
                WHERE c.review_status = $1
                ORDER BY c.created_at DESC, c.id DESC
                LIMIT $2
            """, review_status, limit)
        else:
            rows = await conn.fetch("""
                SELECT c.*, COALESCE(old.review_status='approved' AND old.ai_visible,FALSE)
                    AS replacement_requires_approval,
                    old.title AS source_card_title,
                    old.event_summary AS source_card_summary,
                    old.review_status AS source_card_status,
                    job.job_id AS generation_job_id,
                    job.operation_type AS generation_operation,
                    job.status AS generation_job_status,
                    COALESCE(cardinality(job.result_card_ids), 0) AS replacement_group_size
                FROM shared_experience_cards c
                LEFT JOIN shared_experience_cards old ON old.id=c.supersedes_card_id
                LEFT JOIN LATERAL (
                    SELECT j.* FROM experience_card_generation_jobs j
                    WHERE c.id = ANY(j.result_card_ids)
                    ORDER BY j.finished_at DESC NULLS LAST, j.created_at DESC
                    LIMIT 1
                ) job ON TRUE
                ORDER BY c.created_at DESC, c.id DESC
                LIMIT $1
            """, limit)
        return [_experience_card_dict(row) for row in rows]


async def get_experience_card(card_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT c.*, COALESCE(old.review_status='approved' AND old.ai_visible,FALSE)
               AS replacement_requires_approval,
               old.title AS source_card_title,
               old.event_summary AS source_card_summary,
               old.review_status AS source_card_status,
               job.job_id AS generation_job_id,
               job.operation_type AS generation_operation,
               job.status AS generation_job_status,
               COALESCE(cardinality(job.result_card_ids), 0) AS replacement_group_size
               FROM shared_experience_cards c
               LEFT JOIN shared_experience_cards old ON old.id=c.supersedes_card_id
               LEFT JOIN LATERAL (
                   SELECT j.* FROM experience_card_generation_jobs j
                   WHERE c.id = ANY(j.result_card_ids)
                   ORDER BY j.finished_at DESC NULLS LAST, j.created_at DESC
                   LIMIT 1
               ) job ON TRUE
               WHERE c.id = $1""", card_id
        )
        return _experience_card_dict(row) if row else None


async def get_experience_card_source_messages(card_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        card = await conn.fetchrow("""
            SELECT source_session_id, source_message_ids
            FROM shared_experience_cards WHERE id = $1
        """, card_id)
        if not card:
            return []
        rows = await conn.fetch("""
            SELECT id, role, content, created_at
            FROM conversations
            WHERE session_id = $1
              AND id = ANY($2::bigint[])
            ORDER BY created_at ASC, id ASC
        """, card["source_session_id"], card["source_message_ids"] or [])
        return [dict(row) for row in rows]


async def get_experience_source_messages(session_id: str, message_ids: list[int] = None,
                                         start_id: int = None, end_id: int = None,
                                         recent_n: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if message_ids:
            rows = await conn.fetch("""
                SELECT id, role, content, created_at FROM conversations
                WHERE session_id=$1 AND id=ANY($2::bigint[])
                ORDER BY created_at, id
            """, session_id, message_ids)
        elif recent_n:
            rows = await conn.fetch("""
                SELECT * FROM (SELECT id, role, content, created_at FROM conversations
                WHERE session_id=$1 ORDER BY created_at DESC, id DESC LIMIT $2) q
                ORDER BY created_at, id
            """, session_id, recent_n)
        else:
            rows = await conn.fetch("""
                SELECT id, role, content, created_at FROM conversations
                WHERE session_id=$1 AND id BETWEEN $2 AND $3 ORDER BY created_at, id
            """, session_id, start_id, end_id)
        return [dict(row) for row in rows]


async def claim_experience_auto_batch(session_id: str, silence_minutes: int,
                                      limit: int = 100):
    """Claim one unprocessed, sufficiently quiet message batch for auto generation."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            state = await conn.fetchrow("""
                SELECT * FROM experience_card_auto_state
                WHERE source_session_id=$1 FOR UPDATE
            """, session_id)
            if not state:
                baseline_id = await conn.fetchval("""
                    SELECT COALESCE(MAX(id), 0) FROM conversations
                    WHERE session_id=$1
                """, session_id)
                await conn.execute("""
                    INSERT INTO experience_card_auto_state
                    (source_session_id, last_processed_message_id)
                    VALUES ($1, $2) ON CONFLICT (source_session_id) DO NOTHING
                """, session_id, baseline_id)
                return None
            state = await conn.fetchrow("""
                SELECT * FROM experience_card_auto_state
                WHERE source_session_id=$1 FOR UPDATE
            """, session_id)
            if (state["processing_until_message_id"] is not None
                    and state["processing_started_at"] is not None
                    and state["processing_started_at"] > datetime.now(dt_timezone.utc) - timedelta(minutes=30)):
                return None
            latest = await conn.fetchrow("""
                SELECT id, created_at FROM conversations
                WHERE session_id=$1 AND id>$2
                  AND role IN ('user','assistant')
                  AND COALESCE(content, '') <> ''
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            """, session_id, state["last_processed_message_id"])
            if not latest:
                return None
            now = datetime.now(dt_timezone.utc)
            latest_at = latest["created_at"]
            if latest_at.tzinfo is None:
                latest_at = latest_at.replace(tzinfo=dt_timezone.utc)
            if now - latest_at < timedelta(minutes=silence_minutes):
                return None
            rows = await conn.fetch("""
                SELECT id, role, content, created_at
                FROM conversations
                WHERE session_id=$1 AND id>$2
                  AND role IN ('user','assistant')
                  AND COALESCE(content, '') <> ''
                ORDER BY id ASC
                LIMIT $3
            """, session_id, state["last_processed_message_id"], limit)
            if not rows:
                return None
            until_id = int(rows[-1]["id"])
            await conn.execute("""
                UPDATE experience_card_auto_state
                SET processing_until_message_id=$2, processing_started_at=NOW(), updated_at=NOW()
                WHERE source_session_id=$1
            """, session_id, until_id)
            return [dict(row) for row in rows]


async def finish_experience_auto_batch(session_id: str, until_message_id: int,
                                       advance: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE experience_card_auto_state
            SET last_processed_message_id = CASE WHEN $3 THEN GREATEST(
                    last_processed_message_id, $2) ELSE last_processed_message_id END,
                processing_until_message_id=NULL, processing_started_at=NULL, updated_at=NOW()
            WHERE source_session_id=$1 AND processing_until_message_id=$2
        """, session_id, until_message_id, advance)


async def begin_experience_generation_job(job_id: str, operation: str, session_id: str,
                                          message_ids: list[int], source_card_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM experience_card_generation_jobs WHERE job_id=$1", job_id
        )
        if existing:
            if (existing["operation_type"] != operation
                    or existing["source_session_id"] != session_id
                    or list(existing["source_message_ids"] or []) != list(message_ids)
                    or existing["source_card_id"] != source_card_id):
                raise ValueError("experience_card_job_id_payload_mismatch")
            return dict(existing), False
        try:
            row = await conn.fetchrow("""
                INSERT INTO experience_card_generation_jobs
                (job_id,operation_type,status,source_session_id,source_message_ids,source_card_id)
                VALUES ($1,$2,'running',$3,$4::bigint[],$5) RETURNING *
            """, job_id, operation, session_id, message_ids, source_card_id)
        except asyncpg.UniqueViolationError:
            existing = await conn.fetchrow(
                "SELECT * FROM experience_card_generation_jobs WHERE job_id=$1", job_id
            )
            if existing:
                if (existing["operation_type"] != operation
                        or existing["source_session_id"] != session_id
                        or list(existing["source_message_ids"] or []) != list(message_ids)
                        or existing["source_card_id"] != source_card_id):
                    raise ValueError("experience_card_job_id_payload_mismatch")
                return dict(existing), False
            raise ValueError("experience_card_job_already_running")
        return dict(row), True


async def fail_experience_generation_job(job_id: str, error_type: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE experience_card_generation_jobs SET status='failed', error_message=$2,
            finished_at=NOW() WHERE job_id=$1 AND status='running'
        """, job_id, error_type[:120])


async def get_experience_generation_job(job_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT job_id, operation_type, status, source_card_id, model,
                   error_message, result_card_ids, created_at, finished_at
            FROM experience_card_generation_jobs WHERE job_id=$1
        """, job_id)
        return dict(row) if row else None


async def complete_experience_generation_job(job_id: str, cards: list[dict], model: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow("""
                SELECT * FROM experience_card_generation_jobs WHERE job_id=$1 FOR UPDATE
            """, job_id)
            if not job or job["status"] != "running":
                return list(job["result_card_ids"] or []) if job else []
            source_card = None
            if job["source_card_id"] is not None:
                source_card = await conn.fetchrow("""
                    SELECT * FROM shared_experience_cards WHERE id=$1 FOR UPDATE
                """, job["source_card_id"])
                if not source_card:
                    raise ValueError("experience_card_source_missing")
            new_ids = []
            for card in cards:
                new_id = await conn.fetchval("""
                    INSERT INTO shared_experience_cards
                    (source_session_id,event_date_start,event_date_end,title,event_summary,interaction_trace,key_details,
                     explicit_corrections,explicit_agreements,open_threads,source_message_ids,
                     review_status,ai_visible,supersedes_card_id,generator_model,prompt_version)
                    VALUES ($1,NULLIF($2,'')::date,NULLIF($3,'')::date,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,$11::bigint[],
                            'pending',FALSE,$12,$13,'manual-v1') RETURNING id
                """, job["source_session_id"], card.get("event_date_start", ""),
                    card.get("event_date_end", ""), card["title"], card["event_summary"],
                    card["interaction_trace"], json.dumps(card["key_details"], ensure_ascii=False),
                    json.dumps(card["explicit_corrections"], ensure_ascii=False),
                    json.dumps(card["explicit_agreements"], ensure_ascii=False),
                    json.dumps(card["open_threads"], ensure_ascii=False),
                    card["source_message_ids"], source_card["id"] if source_card else None, model)
                new_ids.append(new_id)
            if source_card and should_auto_supersede(source_card):
                await conn.execute("""
                    UPDATE shared_experience_cards SET review_status='superseded',ai_visible=FALSE,
                    updated_at=NOW() WHERE id=$1
                      AND review_status IN ('pending','archived') AND ai_visible=FALSE
                """, source_card["id"])
            await conn.execute("""
                UPDATE experience_card_generation_jobs SET status='succeeded',model=$2,
                result_card_ids=$3::bigint[],finished_at=NOW() WHERE job_id=$1
            """, job_id, model, new_ids)
            return new_ids


async def approve_experience_replacement(candidate_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            job_id = await conn.fetchval("""
                SELECT job_id FROM experience_card_generation_jobs
                WHERE $1 = ANY(result_card_ids) AND status='succeeded'
                ORDER BY finished_at DESC NULLS LAST, created_at DESC
                LIMIT 1
            """, candidate_id)
            if not job_id:
                raise ValueError("replacement_group_not_found")
            job = await conn.fetchrow("""
                SELECT * FROM experience_card_generation_jobs
                WHERE job_id=$1 FOR UPDATE
            """, job_id)
            candidates = await conn.fetch("""
                SELECT * FROM shared_experience_cards
                WHERE id=ANY($1::bigint[]) ORDER BY id FOR UPDATE
            """, list(job["result_card_ids"] or []))
            candidate = next((row for row in candidates if row["id"] == candidate_id), None)
            if not candidate or not candidate["supersedes_card_id"]:
                raise ValueError("invalid_replacement_candidate")
            if (not candidates
                    or any(row["review_status"] != "pending" for row in candidates)
                    or any(row["supersedes_card_id"] != candidate["supersedes_card_id"] for row in candidates)):
                raise ValueError("replacement_group_not_pending")
            original = await conn.fetchrow(
                "SELECT * FROM shared_experience_cards WHERE id=$1 FOR UPDATE",
                candidate["supersedes_card_id"])
            if not original or original["review_status"] != "approved" or not original["ai_visible"]:
                raise ValueError("replacement_original_not_active")
            await conn.execute("""
                UPDATE shared_experience_cards SET review_status='superseded',ai_visible=FALSE,
                approved_at=NULL,updated_at=NOW() WHERE id=$1
            """, original["id"])
            rows = await conn.fetch("""
                UPDATE shared_experience_cards SET review_status='approved',ai_visible=TRUE,
                approved_at=NOW(),updated_at=NOW()
                WHERE id=ANY($1::bigint[]) RETURNING *
            """, list(job["result_card_ids"] or []))
            return [_experience_card_dict(row) for row in rows]


async def update_experience_card(card_id: int, update: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        current = await conn.fetchrow(
            "SELECT * FROM shared_experience_cards WHERE id = $1", card_id
        )
        if not current:
            return None
        values = apply_card_update(_experience_card_dict(current), update)
        status = values["review_status"]
        visible = values["ai_visible"]
        row = await conn.fetchrow("""
            UPDATE shared_experience_cards SET
                event_date_start = NULLIF($2, '')::date,
                event_date_end = NULLIF($3, '')::date,
                title = $4,
                event_summary = $5,
                interaction_trace = $6,
                key_details = $7::jsonb,
                explicit_corrections = $8::jsonb,
                explicit_agreements = $9::jsonb,
                open_threads = $10::jsonb,
                review_status = $11,
                ai_visible = $12,
                revision_reason = $13,
                approved_at = CASE
                    WHEN $11 = 'approved' AND approved_at IS NULL THEN NOW()
                    WHEN $11 <> 'approved' THEN NULL
                    ELSE approved_at
                END,
                updated_at = NOW()
            WHERE id = $1
            RETURNING *
        """,
            card_id,
            str(values.get("event_date_start") or ""),
            str(values.get("event_date_end") or ""),
            str(values.get("title") or ""),
            str(values.get("event_summary") or ""),
            str(values.get("interaction_trace") or ""),
            json.dumps((values.get("key_details") or [])[:6], ensure_ascii=False),
            json.dumps(values.get("explicit_corrections") or [], ensure_ascii=False),
            json.dumps(values.get("explicit_agreements") or [], ensure_ascii=False),
            json.dumps(values.get("open_threads") or [], ensure_ascii=False),
            status,
            visible,
            str(values.get("revision_reason") or ""),
        )
        return _experience_card_dict(row)


# ============================================================
# 网关配置
# ============================================================

async def get_gateway_config(key: str, default: str = "") -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM gateway_config WHERE key = $1", key)
        return row['value'] if row else default


async def set_gateway_config(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO gateway_config (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $2
        """, key, value)


async def get_all_gateway_config() -> dict:
    """获取所有配置项"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM gateway_config")
        return {r['key']: r['value'] for r in rows}


# ============================================================
# 对话历史读取（分区缓存用）
# ============================================================

async def get_conversation_messages(session_id: str, limit: int = 100):
    """按时间正序读取session的消息"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT role, content, metadata, created_at
            FROM conversations
            WHERE session_id = $1
            ORDER BY created_at ASC
            LIMIT $2
        """, session_id, limit)
        return [dict(r) for r in rows]


# ============================================================
# 分区缓存状态管理
# ============================================================

async def get_session_cache_state(session_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT summary, a_start_round, updated_at FROM session_cache_state WHERE session_id = $1",
            session_id
        )
        if row:
            raw_summary = row['summary'] or ''
            summary_parts = []
            if raw_summary:
                try:
                    import json
                    parsed = json.loads(raw_summary)
                    if isinstance(parsed, list):
                        summary_parts = parsed
                    else:
                        summary_parts = [raw_summary]
                except (json.JSONDecodeError, ValueError):
                    summary_parts = [raw_summary]
            return {
                'summary_parts': summary_parts,
                'a_start_round': row['a_start_round'] or 0,
                'updated_at': row['updated_at'],
            }
        return {'summary_parts': [], 'a_start_round': 0, 'updated_at': None}


async def save_session_cache_state(session_id: str, summary_parts: list, a_start_round: int):
    import json
    summary_json = json.dumps(summary_parts, ensure_ascii=False)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO session_cache_state (session_id, summary, a_start_round, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (session_id) 
            DO UPDATE SET summary = $2, a_start_round = $3, updated_at = NOW()
        """, session_id, summary_json, a_start_round)


async def record_summary_attempt(
    session_id: str,
    message_count: int,
    model: str,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO summary_health_status (
                session_id, last_attempt_at, last_message_count, last_model, updated_at
            )
            VALUES ($1, NOW(), $2, $3, NOW())
            ON CONFLICT (session_id) DO UPDATE SET
                last_attempt_at = NOW(),
                last_message_count = EXCLUDED.last_message_count,
                last_model = EXCLUDED.last_model,
                updated_at = NOW()
        """, session_id, max(0, int(message_count)), str(model or "")[:200])


async def record_summary_failure(
    session_id: str,
    error_code: str,
    alert_after: int = 2,
    alert_cooldown_hours: int = 6,
) -> dict:
    """Record a safe failure code and atomically claim an alert window."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO summary_health_status (session_id, updated_at)
                VALUES ($1, NOW())
                ON CONFLICT (session_id) DO NOTHING
            """, session_id)
            row = await conn.fetchrow("""
                SELECT consecutive_failures, last_alert_attempt_at
                FROM summary_health_status
                WHERE session_id = $1
                FOR UPDATE
            """, session_id)
            failures = int(row["consecutive_failures"] or 0) + 1
            last_attempt = row["last_alert_attempt_at"]
            cooldown_ok = (
                last_attempt is None
                or last_attempt <= datetime.now(dt_timezone.utc) - timedelta(hours=alert_cooldown_hours)
            )
            should_alert = failures >= max(1, int(alert_after)) and cooldown_ok
            await conn.execute("""
                UPDATE summary_health_status
                SET last_failure_at = NOW(),
                    consecutive_failures = $2,
                    last_error_code = $3,
                    last_alert_attempt_at = CASE WHEN $4 THEN NOW() ELSE last_alert_attempt_at END,
                    updated_at = NOW()
                WHERE session_id = $1
            """, session_id, failures, str(error_code or "unknown")[:100], should_alert)
            return {
                "consecutive_failures": failures,
                "should_alert": should_alert,
            }


async def mark_summary_alert_delivered(session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE summary_health_status
            SET last_alert_at = NOW(), alert_active = TRUE, updated_at = NOW()
            WHERE session_id = $1
        """, session_id)


async def record_summary_success(session_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO summary_health_status (session_id, updated_at)
                VALUES ($1, NOW())
                ON CONFLICT (session_id) DO NOTHING
            """, session_id)
            row = await conn.fetchrow("""
                SELECT consecutive_failures, alert_active
                FROM summary_health_status
                WHERE session_id = $1
                FOR UPDATE
            """, session_id)
            recovered = bool(row["alert_active"])
            previous_failures = int(row["consecutive_failures"] or 0)
            await conn.execute("""
                UPDATE summary_health_status
                SET last_success_at = NOW(),
                    consecutive_failures = 0,
                    last_error_code = '',
                    alert_active = FALSE,
                    updated_at = NOW()
                WHERE session_id = $1
            """, session_id)
            return {
                "recovered": recovered,
                "previous_failures": previous_failures,
            }


async def get_summary_health_status(session_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT session_id, last_attempt_at, last_success_at, last_failure_at,
                   consecutive_failures, last_error_code, last_message_count,
                   last_model, last_alert_at, alert_active, updated_at
            FROM summary_health_status
            WHERE session_id = $1
        """, session_id)
        return dict(row) if row else {}


async def record_operational_health_failure(
    component: str,
    error_code: str,
    alert_after: int = 2,
    alert_cooldown_hours: int = 6,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO operational_health_status (component, updated_at)
                VALUES ($1, NOW())
                ON CONFLICT (component) DO NOTHING
            """, str(component)[:80])
            row = await conn.fetchrow("""
                SELECT consecutive_failures, last_alert_attempt_at
                FROM operational_health_status
                WHERE component = $1
                FOR UPDATE
            """, str(component)[:80])
            failures = int(row["consecutive_failures"] or 0) + 1
            last_attempt = row["last_alert_attempt_at"]
            cooldown_ok = (
                last_attempt is None
                or last_attempt <= datetime.now(dt_timezone.utc) - timedelta(hours=alert_cooldown_hours)
            )
            should_alert = failures >= max(1, int(alert_after)) and cooldown_ok
            await conn.execute("""
                UPDATE operational_health_status
                SET status = 'failing',
                    consecutive_failures = $2,
                    last_failure_at = NOW(),
                    last_error_code = $3,
                    last_alert_attempt_at = CASE WHEN $4 THEN NOW() ELSE last_alert_attempt_at END,
                    updated_at = NOW()
                WHERE component = $1
            """, str(component)[:80], failures, str(error_code or "unknown")[:100], should_alert)
            return {"consecutive_failures": failures, "should_alert": should_alert}


async def record_operational_health_success(component: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO operational_health_status (component, updated_at)
                VALUES ($1, NOW())
                ON CONFLICT (component) DO NOTHING
            """, str(component)[:80])
            row = await conn.fetchrow("""
                SELECT consecutive_failures, alert_active
                FROM operational_health_status
                WHERE component = $1
                FOR UPDATE
            """, str(component)[:80])
            recovered = bool(row["alert_active"])
            await conn.execute("""
                UPDATE operational_health_status
                SET status = 'healthy',
                    consecutive_failures = 0,
                    last_success_at = NOW(),
                    last_error_code = '',
                    alert_active = FALSE,
                    updated_at = NOW()
                WHERE component = $1
            """, str(component)[:80])
            return {"recovered": recovered, "previous_failures": int(row["consecutive_failures"] or 0)}


async def mark_operational_health_alert_delivered(component: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE operational_health_status
            SET last_alert_at = NOW(), alert_active = TRUE, updated_at = NOW()
            WHERE component = $1
        """, str(component)[:80])


async def list_operational_health_status() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT component, status, consecutive_failures, last_success_at,
                   last_failure_at, last_error_code, last_alert_at,
                   alert_active, updated_at
            FROM operational_health_status
            ORDER BY component
        """)
        return [dict(row) for row in rows]


async def get_latest_io_received_at():
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT MAX(received_at) FROM io_context_events")


async def get_recent_io_context_events(limit: int = 12):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, device_id, app_instance_id, source_client, event_type,
                   observed_at, timezone, permission_state, schema_version
            FROM io_context_events
            ORDER BY observed_at DESC NULLS LAST, id DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(row) for row in rows]


# ============================================================
# Token 使用记录
# ============================================================

async def ensure_token_usage_table():
    """确保token_usage表存在（在init_tables里调用）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id              SERIAL PRIMARY KEY,
                session_id      TEXT,
                model           TEXT,
                prompt_tokens   INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens    INTEGER DEFAULT 0,
                usage_type      TEXT DEFAULT 'chat',
                estimated_cost_usd DOUBLE PRECISION,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'token_usage' AND column_name = 'estimated_cost_usd'
                ) THEN
                    ALTER TABLE token_usage ADD COLUMN estimated_cost_usd DOUBLE PRECISION;
                END IF;
            END $$;
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_token_usage_created ON token_usage (created_at DESC);
        """)


async def save_token_usage(
    session_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    usage_type: str = "chat",
    estimated_cost_usd: float = None,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO token_usage (
                session_id, model, prompt_tokens, completion_tokens,
                total_tokens, usage_type, estimated_cost_usd
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, session_id, model, prompt_tokens, completion_tokens, total_tokens, usage_type, estimated_cost_usd)


# ============================================================
# 对话记录管理
# ============================================================

async def get_conversations_paginated(page: int = 1, per_page: int = 20):
    offset = (page - 1) * per_page
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_row = await conn.fetchrow(
            "SELECT COUNT(DISTINCT session_id) as total FROM conversations"
        )
        total = total_row['total'] if total_row else 0

        rows = await conn.fetch("""
            WITH session_info AS (
                SELECT session_id, MIN(created_at) as first_time, MAX(created_at) as last_time, COUNT(*) as message_count
                FROM conversations GROUP BY session_id ORDER BY last_time DESC LIMIT $1 OFFSET $2
            )
            SELECT si.*,
                   COALESCE(tu.total_all, 0) as total_tokens
            FROM session_info si
            LEFT JOIN (
                SELECT session_id, SUM(total_tokens) as total_all FROM token_usage WHERE usage_type = 'chat' GROUP BY session_id
            ) tu ON si.session_id = tu.session_id
            ORDER BY si.last_time DESC
        """, per_page, offset)
        
        results = []
        for r in rows:
            preview_row = await conn.fetchrow(
                "SELECT content FROM conversations WHERE session_id = $1 AND role = 'user' ORDER BY created_at LIMIT 1",
                r['session_id']
            )
            preview = preview_row['content'][:80] if preview_row else ''
            title = (preview[:30] + '...' if len(preview) > 30 else preview) or r['session_id']
            results.append({
                'session_id': r['session_id'],
                'title': title,
                'first_time': r['first_time'].isoformat() if r['first_time'] else None,
                'last_time': r['last_time'].isoformat() if r['last_time'] else None,
                'message_count': r['message_count'],
                'preview': preview,
                'total_tokens': r['total_tokens'],
            })
        return results, total


async def delete_conversation(session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE session_id = $1", session_id)
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = $1", session_id)


async def batch_delete_conversations(session_ids: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE session_id = ANY($1)", session_ids)
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = ANY($1)", session_ids)


async def merge_sessions_to_target(source_ids: list, target_id: str) -> dict:
    if not source_ids:
        return {'merged_sessions': 0, 'merged_messages': 0, 'merged_token_records': 0}
    pool = await get_pool()
    async with pool.acquire() as conn:
        msg_count = await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE session_id = ANY($1)", source_ids)
        await conn.execute("UPDATE conversations SET session_id = $1 WHERE session_id = ANY($2)", target_id, source_ids)
        token_count = await conn.fetchval("SELECT COUNT(*) FROM token_usage WHERE session_id = ANY($1)", source_ids)
        await conn.execute("UPDATE token_usage SET session_id = $1 WHERE session_id = ANY($2)", target_id, source_ids)
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = ANY($1)", source_ids)
        return {'merged_sessions': len(source_ids), 'merged_messages': msg_count or 0, 'merged_token_records': token_count or 0}


async def list_all_session_cache_states() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT scs.session_id, scs.summary, scs.a_start_round, scs.updated_at,
                   COALESCE(c.message_count, 0) as message_count,
                   COALESCE(tu.chat_tokens, 0) as chat_tokens
            FROM session_cache_state scs
            LEFT JOIN (SELECT session_id, COUNT(*) as message_count FROM conversations GROUP BY session_id) c ON scs.session_id = c.session_id
            LEFT JOIN (SELECT session_id, SUM(total_tokens) as chat_tokens FROM token_usage WHERE usage_type = 'chat' GROUP BY session_id) tu ON scs.session_id = tu.session_id
            ORDER BY scs.updated_at DESC
        """)
        results = []
        for r in rows:
            raw_summary = r['summary'] or ''
            try:
                import json
                parsed = json.loads(raw_summary)
                if isinstance(parsed, list):
                    summary_parts = parsed
                else:
                    summary_parts = [raw_summary] if raw_summary else []
            except (json.JSONDecodeError, ValueError):
                summary_parts = [raw_summary] if raw_summary else []
            results.append({
                'session_id': r['session_id'],
                'summary': '\n\n'.join(summary_parts),
                'summary_length': sum(len(p) for p in summary_parts),
                'summary_count': len(summary_parts),
                'a_start_round': r['a_start_round'],
                'updated_at': r['updated_at'].isoformat() if r['updated_at'] else None,
                'message_count': r['message_count'],
                'chat_tokens': r['chat_tokens'],
            })
        return results


async def delete_session_cache_state(session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = $1", session_id)


async def rename_session_id(old_id: str, new_id: str) -> bool:
    """重命名对话线ID（事务内同时修改三个表）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 检查新ID是否已存在
            exists = await conn.fetchval(
                "SELECT 1 FROM session_cache_state WHERE session_id = $1", new_id
            )
            if exists:
                return False
            # session_cache_state
            await conn.execute(
                "UPDATE session_cache_state SET session_id = $1 WHERE session_id = $2",
                new_id, old_id
            )
            # conversations
            await conn.execute(
                "UPDATE conversations SET session_id = $1 WHERE session_id = $2",
                new_id, old_id
            )
            # token_usage
            await conn.execute(
                "UPDATE token_usage SET session_id = $1 WHERE session_id = $2",
                new_id, old_id
            )
            return True


def db_row_to_message(row: dict) -> dict:
    """
    把DB记录还原成API消息格式。
    
    普通消息: {"role": "user", "content": "你好"} 
    工具调用: {"role": "assistant", "content": null, "tool_calls": [...]}
    工具结果: {"role": "tool", "content": "结果", "tool_call_id": "call_xxx"}
    思维链:   {"role": "assistant", "content": "回答", "reasoning_content": "思维链"}
    """
    import json as _json
    msg = {"role": row["role"], "content": row.get("content") or ""}
    
    meta_str = row.get("metadata")
    if meta_str:
        try:
            meta = _json.loads(meta_str)
            # assistant 带 tool_calls
            if "tool_calls" in meta:
                msg["tool_calls"] = meta["tool_calls"]
                if not row.get("content"):
                    msg["content"] = None
            # assistant 带 reasoning_content（deepseek thinking mode）
            if "reasoning_content" in meta:
                msg["reasoning_content"] = meta["reasoning_content"]
            # tool 消息带 tool_call_id
            if "tool_call_id" in meta:
                msg["tool_call_id"] = meta["tool_call_id"]
            # 其他可能的字段（name 等）
            if "name" in meta:
                msg["name"] = meta["name"]
        except Exception:
            pass
    
    return msg


async def export_all_conversations():
    """导出所有对话记录（用于备份）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT session_id, role, content, model, created_at
            FROM conversations
            ORDER BY session_id, created_at
        """)
        return [
            {
                'session_id': r['session_id'],
                'role': r['role'],
                'content': r['content'],
                'model': r['model'] or '',
                'created_at': r['created_at'].isoformat() if r['created_at'] else None,
            }
            for r in rows
        ]


async def import_conversations(records: list):
    """
    导入对话记录（自动去重）
    
    records: [{ session_id, role, content, model?, created_at? }, ...]
    按 session_id + role + created_at 三元组去重，已存在的跳过。
    返回 (导入数量, 跳过数量)
    """
    if not records:
        return 0, 0
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        imported = 0
        skipped = 0
        for r in records:
            session_id = r.get('session_id')
            role = r.get('role')
            content = r.get('content')
            
            if not all([session_id, role, content]):
                continue
            
            model = r.get('model', '')
            created_at = r.get('created_at')
            
            # 解析时间
            from datetime import datetime
            if created_at and isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                except:
                    created_at = None
            
            # 去重检查
            if created_at:
                existing = await conn.fetchrow("""
                    SELECT id FROM conversations
                    WHERE session_id = $1 AND role = $2 AND created_at = $3
                    LIMIT 1
                """, session_id, role, created_at)
                
                if existing:
                    skipped += 1
                    continue
                
                await conn.execute("""
                    INSERT INTO conversations (session_id, role, content, model, created_at)
                    VALUES ($1, $2, $3, $4, $5)
                """, session_id, role, content, model, created_at)
            else:
                await conn.execute("""
                    INSERT INTO conversations (session_id, role, content, model)
                    VALUES ($1, $2, $3, $4)
                """, session_id, role, content, model)
            
            imported += 1
        
        if skipped:
            print(f"📥 导入对话: {imported} 条新增, {skipped} 条已存在跳过")
        else:
            print(f"📥 导入对话: {imported} 条新增")
        
        return imported, skipped


# ============================================================
# 三层记忆架构（碎片/事件/核心）
# ============================================================

async def get_fragments_by_date(event_date):
    """获取指定日期的原始碎片（用于每日整理）"""
    # 把本地日期转成UTC时间范围，避免DATE()用UTC截断导致日期偏移
    local_tz = dt_timezone(timedelta(hours=TIMEZONE_HOURS))
    start_utc = datetime(event_date.year, event_date.month, event_date.day, tzinfo=local_tz).astimezone(dt_timezone.utc)
    end_utc = start_utc + timedelta(days=1)
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, content, importance, created_at
            FROM memories
            WHERE layer = 1 AND is_active = TRUE
            AND created_at >= $1 AND created_at < $2
            ORDER BY created_at
        """, start_utc, end_utc)
        return [dict(r) for r in rows]


async def get_fragments_by_date_range(start_date, end_date):
    """获取指定时间段的原始碎片（用于跨天整理）"""
    # 把本地日期转成UTC时间范围，避免DATE()用UTC截断导致日期偏移
    local_tz = dt_timezone(timedelta(hours=TIMEZONE_HOURS))
    start_utc = datetime(start_date.year, start_date.month, start_date.day, tzinfo=local_tz).astimezone(dt_timezone.utc)
    # end_date 当天结束 = end_date 下一天的 00:00
    end_utc = datetime(end_date.year, end_date.month, end_date.day, tzinfo=local_tz).astimezone(dt_timezone.utc) + timedelta(days=1)
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, content, importance, created_at
            FROM memories
            WHERE layer = 1 AND is_active = TRUE
            AND created_at >= $1 AND created_at < $2
            ORDER BY created_at
        """, start_utc, end_utc)
        return [dict(r) for r in rows]


async def create_event_memory(title: str, content: str, importance: int, 
                               event_date, merged_from: list):
    """创建事件记忆（从碎片合并而来）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO memories (content, importance, layer, title, is_active, merged_from, event_date)
            VALUES ($1, $2, 2, $3, TRUE, $4, $5)
            RETURNING id
        """, content, importance, title, merged_from, event_date)
        
        new_id = row['id'] if row else None
        
        # 向量搜索：计算并保存 embedding
        if MEMORY_VECTOR_ENABLED and new_id:
            try:
                embedding = await compute_embedding(content)
                if embedding:
                    await save_memory_embedding(conn, new_id, embedding)
            except Exception as e:
                print(f"⚠️ 事件记忆embedding计算失败（id={new_id}）: {e}")
        
        return new_id


async def deactivate_memories(memory_ids: list):
    """将记忆标记为不活跃（合并后的碎片）"""
    if not memory_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE memories SET is_active = FALSE
            WHERE id = ANY($1::int[])
        """, memory_ids)


async def promote_to_core(memory_id: int, title: str = None):
    """将记忆升级为核心记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if title:
            await conn.execute("""
                UPDATE memories SET layer = 3, title = $2
                WHERE id = $1
            """, memory_id, title)
        else:
            await conn.execute("""
                UPDATE memories SET layer = 3
                WHERE id = $1
            """, memory_id)


async def merge_memories(memory_ids: list, new_title: str, new_content: str, 
                         importance: int, layer: int = 2):
    """合并多条记忆为一条新记忆"""
    if not memory_ids:
        return None
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 获取原记忆的日期（取最早的）
        rows = await conn.fetch("""
            SELECT MIN(DATE(created_at)) as event_date
            FROM memories WHERE id = ANY($1::int[])
        """, memory_ids)
        event_date = rows[0]['event_date'] if rows else None
        
        # 创建新记忆
        row = await conn.fetchrow("""
            INSERT INTO memories (content, importance, layer, title, is_active, merged_from, event_date)
            VALUES ($1, $2, $3, $4, TRUE, $5, $6)
            RETURNING id
        """, new_content, importance, layer, new_title, memory_ids, event_date)
        
        new_id = row['id'] if row else None
        
        # 向量搜索：计算并保存 embedding
        if MEMORY_VECTOR_ENABLED and new_id:
            try:
                embedding = await compute_embedding(new_content)
                if embedding:
                    await save_memory_embedding(conn, new_id, embedding)
            except Exception as e:
                print(f"⚠️ 合并记忆embedding计算失败（id={new_id}）: {e}")
        
        # 将原记忆标记为不活跃
        if new_id:
            await deactivate_memories(memory_ids)
        
        return new_id


async def check_duplicate_memory(new_content: str, threshold: float = 0.7) -> dict:
    """检查新记忆是否与现有记忆重复
    
    三层去重策略：
    1. 精确匹配：内容完全相同
    2. 包含关系：新内容包含旧内容，或旧内容包含新内容
    3. 关键词重叠度：Jaccard 相似度 > threshold
    
    Returns:
        {
            "is_duplicate": bool,
            "reason": str,  # "exact" / "containment" / "similarity"
            "matched_id": int or None,
            "similarity": float or None
        }
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 获取所有活跃记忆
        rows = await conn.fetch("""
            SELECT id, content FROM memories 
            WHERE is_active = TRUE
        """)
        
        new_content_lower = new_content.strip().lower()
        new_keywords = set(extract_search_keywords(new_content))
        
        for row in rows:
            old_content = row['content']
            old_content_lower = old_content.strip().lower()
            
            # 第一层：精确匹配
            if new_content_lower == old_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "exact",
                    "matched_id": row['id'],
                    "similarity": 1.0
                }
            
            # 第二层：包含关系
            if new_content_lower in old_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "containment",
                    "matched_id": row['id'],
                    "similarity": len(new_content) / len(old_content)
                }
            if old_content_lower in new_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "containment_update",
                    "matched_id": row['id'],
                    "similarity": len(old_content) / len(new_content)
                }
            
            # 第三层：关键词重叠度（Jaccard 相似度）
            old_keywords = set(extract_search_keywords(old_content))
            if new_keywords and old_keywords:
                intersection = new_keywords & old_keywords
                union = new_keywords | old_keywords
                similarity = len(intersection) / len(union) if union else 0
                
                if similarity > threshold:
                    return {
                        "is_duplicate": True,
                        "reason": "similarity",
                        "matched_id": row['id'],
                        "similarity": similarity
                    }
        
        return {
            "is_duplicate": False,
            "reason": None,
            "matched_id": None,
            "similarity": None
        }


async def update_memory_with_layer(memory_id: int, content: str = None, 
                                    importance: int = None, title: str = None,
                                    layer: int = None, is_active: bool = None):
    """更新记忆（支持三层架构新字段）"""
    updates = []
    params = []
    param_idx = 2  # $1 给 memory_id
    
    if content is not None:
        updates.append(f"content = ${param_idx}")
        params.append(content)
        param_idx += 1
    
    if importance is not None:
        updates.append(f"importance = ${param_idx}")
        params.append(importance)
        param_idx += 1
    
    if title is not None:
        updates.append(f"title = ${param_idx}")
        params.append(title)
        param_idx += 1
    
    if layer is not None:
        updates.append(f"layer = ${param_idx}")
        params.append(layer)
        param_idx += 1
    
    if is_active is not None:
        updates.append(f"is_active = ${param_idx}")
        params.append(is_active)
        param_idx += 1
    
    if not updates:
        return
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = $1",
            memory_id, *params
        )


async def get_layer_statistics():
    """获取各层记忆的统计数据"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                layer,
                COUNT(*) as count,
                COUNT(*) FILTER (WHERE is_active = TRUE) as active_count
            FROM memories
            GROUP BY layer
            ORDER BY layer
        """)
        
        stats = {
            "layer_1": {"total": 0, "active": 0},  # 原始碎片
            "layer_2": {"total": 0, "active": 0},  # 事件记忆
            "layer_3": {"total": 0, "active": 0},  # 核心记忆
        }
        
        for row in rows:
            layer = row['layer'] or 1  # 默认为层级1
            key = f"layer_{layer}"
            if key in stats:
                stats[key] = {
                    "total": row['count'],
                    "active": row['active_count']
                }
        
        return stats


async def cleanup_old_fragments(days: int = 30):
    """清理指定天数前的归档碎片
    
    只清理满足以下条件的记忆：
    - layer = 1（原始碎片）
    - is_active = FALSE（已归档）
    - created_at 在 days 天之前
    
    Returns:
        删除的记忆数量
    """
    from datetime import datetime, timedelta
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        cutoff_date = datetime.now() - timedelta(days=days)
        
        result = await conn.execute("""
            DELETE FROM memories
            WHERE layer = 1
            AND is_active = FALSE
            AND created_at < $1
        """, cutoff_date)
        
        # 解析删除数量，格式如 "DELETE 5"
        deleted = int(result.split()[-1]) if result else 0
        return deleted


async def revert_merge(memory_id: int):
    """撤回合并操作
    
    恢复原始碎片（is_active = TRUE），删除合并后的事件记忆
    
    Args:
        memory_id: 要撤回的事件记忆ID
        
    Returns:
        {"status": "ok", "restored": 恢复的碎片数量}
        或 {"error": "错误信息"}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 获取事件记忆信息
        row = await conn.fetchrow("""
            SELECT id, layer, merged_from FROM memories WHERE id = $1
        """, memory_id)
        
        if not row:
            return {"error": "记忆不存在"}
        
        if row['layer'] != 2:
            return {"error": "只能撤回事件记忆的合并"}
        
        merged_from = row['merged_from']
        if not merged_from or len(merged_from) == 0:
            return {"error": "没有合并来源，无法撤回"}
        
        # 恢复原始碎片
        result = await conn.execute("""
            UPDATE memories SET is_active = TRUE
            WHERE id = ANY($1::int[])
        """, merged_from)
        restored = int(result.split()[-1]) if result else 0
        
        # 删除事件记忆
        await conn.execute("""
            DELETE FROM memories WHERE id = $1
        """, memory_id)
        
        return {"status": "ok", "restored": restored}
