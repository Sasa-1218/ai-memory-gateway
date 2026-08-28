"""
AI Memory Gateway — 带记忆系统的 LLM 转发网关
=============================================
让你的 AI 拥有长期记忆。

工作原理：
1. 接收客户端（Kelivo / ChatBox / 任何 OpenAI 兼容客户端）的消息
2. 自动搜索数据库中的相关记忆，注入 system prompt
3. 转发给 LLM API（支持 OpenRouter / OpenAI / 任何兼容接口）
4. 后台自动存储对话 + 用 AI 提取新记忆

环境变量 MEMORY_ENABLED=false 时退化为纯转发网关（第一阶段）。
"""

import os
import json
import uuid
import asyncio
import secrets
import re
import hashlib
import logging
import time
import httpx
from datetime import datetime, timedelta, timezone, date as date_cls
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pydantic import BaseModel
from typing import Optional

try:
    import jwt
    from jwt.algorithms import RSAAlgorithm
except Exception:
    jwt = None
    RSAAlgorithm = None

from database import init_tables, close_pool, save_message, search_memories, save_memory, get_all_memories_count, get_recent_memories, get_recent_memories_detail, get_all_memories, get_pool, get_all_memories_detail, update_memory, delete_memory, delete_memories_batch, get_gateway_config, set_gateway_config, get_all_gateway_config, get_conversation_messages, get_recent_conversation_messages, get_last_conversation_message_time, get_push_metadata_since, get_session_cache_state, save_session_cache_state, delete_session_cache_state, save_token_usage, ensure_token_usage_table, get_conversations_paginated, delete_conversation, batch_delete_conversations, merge_sessions_to_target, list_all_session_cache_states, export_all_conversations, import_conversations, get_last_user_content, update_last_assistant_message, db_row_to_message, backfill_memory_embeddings, get_pending_memory_embedding_count, search_conversations, update_message_content, rename_session_id, get_fragments_by_date, get_fragments_by_date_range, create_event_memory, deactivate_memories, promote_to_core, merge_memories, check_duplicate_memory, update_memory_with_layer, get_layer_statistics, cleanup_old_fragments, revert_merge, save_shadow_push_decision, save_shadow_mind_state, get_shadow_mind_state, get_recent_drive_events, settle_shadow_mind_rules, get_shadow_mind_a2_events, get_shadow_mind_history, get_latest_normal_turn_message_ids, get_shadow_mind_event_source_messages, save_io_context_events, get_recent_io_context_events, list_experience_cards, get_experience_card, get_experience_card_source_messages, update_experience_card, get_experience_source_messages, begin_experience_generation_job, fail_experience_generation_job, complete_experience_generation_job, approve_experience_replacement, get_experience_generation_job, claim_experience_auto_batch, finish_experience_auto_batch, get_memories_by_ids_readonly, record_summary_attempt, record_summary_failure, mark_summary_alert_delivered, record_summary_success, get_summary_health_status, record_operational_health_failure, record_operational_health_success, mark_operational_health_alert_delivered, list_operational_health_status, get_latest_io_received_at
import database as _db_module  # 用于 /api/settings 热更新 database.py 全局变量
from experience_cards import (
    REVIEW_STATUSES,
    normalize_card_update,
    restore_card_update,
    soft_delete_card_update,
    build_generation_prompt,
    is_basic_experience_candidate,
    validate_generated_cards,
)
from memory_extractor import extract_memories, score_memories, get_memory_config, _apply_memory_thinking_option
from memory_inspector import (
    build_injection_preview,
    lexical_score,
    make_result,
    matched_terms,
)

# ============================================================
# 配置项 —— 全部从环境变量读取，部署时在云平台面板里设置
# ============================================================

# 你的 API Key（OpenRouter / OpenAI / 其他兼容服务）
API_KEY = os.getenv("API_KEY", "")

# API 地址（改这个就能切换不同的 LLM 服务商）
# OpenRouter: https://openrouter.ai/api/v1/chat/completions
# OpenAI:     https://api.openai.com/v1/chat/completions
# 本地 Ollama: http://localhost:11434/v1/chat/completions
API_BASE_URL = os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

# 默认模型（如果客户端没指定就用这个）
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "anthropic/claude-sonnet-4")

# 网关端口
PORT = int(os.getenv("PORT", "8080"))

# 网关访问密钥（程序客户端/API 调用使用 X-Gateway-Key 请求头）
# Dashboard 网页访问由 Cloudflare Access 单独保护，不再支持 URL 参数登录。
GATEWAY_SECRET = os.getenv("GATEWAY_SECRET", "")

# io 感知数据入口密钥：独立于 Kelivo 使用的 X-Gateway-Key
IO_INGEST_SECRET = os.getenv("IO_INGEST_SECRET", "")

# Cloudflare Access：用于 Dashboard 页面和 Dashboard 私有接口
# CF_ACCESS_TEAM_DOMAIN 示例：https://your-team.cloudflareaccess.com
# CF_ACCESS_AUD 为 Access Application Audience Tag。
CF_ACCESS_TEAM_DOMAIN = os.getenv("CF_ACCESS_TEAM_DOMAIN", "").strip().rstrip("/")
CF_ACCESS_AUD = os.getenv("CF_ACCESS_AUD", "").strip()
CF_ACCESS_ALLOWED_EMAILS = {
    item.strip().lower()
    for item in os.getenv("CF_ACCESS_ALLOWED_EMAILS", "").split(",")
    if item.strip()
}
CF_ACCESS_ALLOWED_EMAIL_DOMAINS = {
    item.strip().lower().lstrip("@")
    for item in os.getenv("CF_ACCESS_ALLOWED_EMAIL_DOMAINS", "").split(",")
    if item.strip()
}

# 记忆系统开关（数据库出问题时可以临时关掉）
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "false").lower() == "true"

# 每次注入的最大记忆条数
MAX_MEMORIES_INJECT = int(os.getenv("MAX_MEMORIES_INJECT", "15"))

# 记忆提取间隔（0 = 禁用自动提取，1 = 每轮提取，N = 每 N 轮提取一次）
MEMORY_EXTRACT_INTERVAL = int(os.getenv("MEMORY_EXTRACT_INTERVAL", "1"))

# 记忆提取+注入总开关（false时数据库仍连接、消息仍存储，但不提取也不注入记忆）
MEMORY_EXTRACT_ENABLED = os.getenv("MEMORY_EXTRACT_ENABLED", "true").lower() == "true"

# Shadow Mind Phase A2: deterministic, zero-model state settlement.
SHADOW_MIND_RULES_ENABLED = os.getenv("SHADOW_MIND_RULES_ENABLED", "false").lower() == "true"

# 分区缓存
CACHE_PARTITION_ENABLED = os.getenv("CACHE_PARTITION_ENABLED", "false").lower() == "true"
CACHE_PARTITION_X = int(os.getenv("CACHE_PARTITION_X", "15"))
CACHE_SUMMARY_MODEL = os.getenv("CACHE_SUMMARY_MODEL", "anthropic/claude-haiku-4.5")
CACHE_PARTITION_TRIGGER = os.getenv("CACHE_PARTITION_TRIGGER", "rounds")  # rounds=按轮次 | time=按时间窗口
CACHE_PARTITION_WINDOW = int(os.getenv("CACHE_PARTITION_WINDOW", "30"))  # 时间窗口（分钟），仅 trigger=time 时生效
PARTITION_SESSION_ID = os.getenv("PARTITION_SESSION_ID", "")

def get_active_session_id() -> str:
    return PARTITION_SESSION_ID

# 时区偏移（小时），用于记忆注入时的日期显示，默认 UTC+8
TIMEZONE_HOURS = int(os.getenv("TIMEZONE_HOURS", "8"))

# 轮次计数器
_round_counter = 0

# 强制流式传输（部分客户端不发stream=true导致thinking数据丢失，开启后强制所有请求走流式）
FORCE_STREAM = os.getenv("FORCE_STREAM", "false").lower() == "true"

# 推理/思维链参数（部分客户端走网关时不会自动添加reasoning参数，导致上游不返回thinking数据）
# 设为 low/medium/high 会在转发请求时注入 reasoning_effort 参数
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "")

# 记忆模型专用配置
MEMORY_API_BASE_URL = os.getenv("MEMORY_API_BASE_URL", "")
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "")
MEMORY_API_THINKING = os.getenv("MEMORY_API_THINKING", "")

def get_memory_api_key() -> str:
    return MEMORY_API_KEY

def get_memory_api_base_url() -> str:
    return MEMORY_API_BASE_URL

# 摘要模型专用的独立接口地址/Key（不设则回退到 MEMORY_API_KEY/API_BASE_URL）
# 用于把摘要压缩这块单独指到 DeepSeek/GLM 等官方接口，跳过 OpenRouter，省手续费
# 例：SUMMARY_API_BASE_URL=https://api.deepseek.com/v1/chat/completions
#     SUMMARY_API_KEY=sk-xxxxxxxx（DeepSeek官方控制台申请的key）
#     CACHE_SUMMARY_MODEL=deepseek-chat
SUMMARY_API_BASE_URL = os.getenv("SUMMARY_API_BASE_URL", "")
SUMMARY_API_KEY = os.getenv("SUMMARY_API_KEY", "")
SUMMARY_ALERTS_ENABLED = os.getenv("SUMMARY_ALERTS_ENABLED", "true").lower() == "true"
SUMMARY_ALERT_AFTER_FAILURES = max(1, int(os.getenv("SUMMARY_ALERT_AFTER_FAILURES", "2")))
SUMMARY_ALERT_COOLDOWN_HOURS = max(1, int(os.getenv("SUMMARY_ALERT_COOLDOWN_HOURS", "6")))


def get_summary_api_base_url() -> str:
    return SUMMARY_API_BASE_URL or API_BASE_URL

def get_summary_api_key() -> str:
    return SUMMARY_API_KEY or get_memory_api_key()


async def _record_summary_failure_safe(session_id: str, reason_code: str):
    if not session_id:
        return
    try:
        result = await record_summary_failure(
            session_id,
            reason_code,
            SUMMARY_ALERT_AFTER_FAILURES,
            SUMMARY_ALERT_COOLDOWN_HOURS,
        )
        print(
            "summary_health_failure "
            f"session_hash={_short_hash_text(session_id)} "
            f"reason={reason_code} consecutive={result.get('consecutive_failures', 0)}",
            flush=True,
        )
        if SUMMARY_ALERTS_ENABLED and result.get("should_alert"):
            delivery = await deliver_summary_health_alert(
                f"摘要已连续失败 {result.get('consecutive_failures', 0)} 次。"
                "原始对话仍已保存，摘要进度没有继续推进，请打开 Dashboard 查看。"
            )
            if delivery.get("delivered"):
                await mark_summary_alert_delivered(session_id)
    except Exception as monitor_error:
        print(
            "summary_health_monitor_failed "
            f"operation=failure error_type={type(monitor_error).__name__}",
            flush=True,
        )


async def _record_summary_success_safe(session_id: str):
    if not session_id:
        return
    try:
        result = await record_summary_success(session_id)
        if SUMMARY_ALERTS_ENABLED and result.get("recovered"):
            await deliver_summary_health_alert(
                "摘要生成已经恢复，之前保留的待处理对话可以继续整理。"
            )
    except Exception as monitor_error:
        print(
            "summary_health_monitor_failed "
            f"operation=success error_type={type(monitor_error).__name__}",
            flush=True,
        )


HEALTH_COMPONENT_LABELS = {
    "memory_extraction": "记忆提取",
    "experience_cards": "经历卡生成",
    "upstream_chat": "聊天上游",
    "conversation_storage": "对话保存",
    "io_ingest": "IO 感知写入",
}


async def _record_operational_failure_safe(component: str, reason_code: str):
    try:
        result = await record_operational_health_failure(
            component,
            reason_code,
            SUMMARY_ALERT_AFTER_FAILURES,
            SUMMARY_ALERT_COOLDOWN_HOURS,
        )
        print(
            "operational_health_failure "
            f"component={component} reason={reason_code} "
            f"consecutive={result.get('consecutive_failures', 0)}",
            flush=True,
        )
        if SUMMARY_ALERTS_ENABLED and result.get("should_alert"):
            label = HEALTH_COMPONENT_LABELS.get(component, component)
            delivered = await deliver_gateway_system_alert(
                "网关系统提醒",
                f"{label}已连续失败 {result.get('consecutive_failures', 0)} 次，"
                "请打开 Dashboard 查看脱敏错误状态。",
            )
            if delivered.get("delivered"):
                await mark_operational_health_alert_delivered(component)
    except Exception as monitor_error:
        print(
            "operational_health_monitor_failed "
            f"component={component} operation=failure "
            f"error_type={type(monitor_error).__name__}",
            flush=True,
        )


async def _record_operational_success_safe(component: str):
    try:
        result = await record_operational_health_success(component)
        if SUMMARY_ALERTS_ENABLED and result.get("recovered"):
            label = HEALTH_COMPONENT_LABELS.get(component, component)
            await deliver_gateway_system_alert(
                "网关系统恢复",
                f"{label}已经恢复正常。",
            )
    except Exception as monitor_error:
        print(
            "operational_health_monitor_failed "
            f"component={component} operation=success "
            f"error_type={type(monitor_error).__name__}",
            flush=True,
        )


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"⚠️ 环境变量 {key} 值无效，回退默认值 {default}")
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"⚠️ 环境变量 {key} 值无效，回退默认值 {default}")
        return default


# 共同经历草稿自动生成：默认关闭，启用后仅在主 session 静默满 3 小时时处理新消息。
EXPERIENCE_CARD_AUTO_GENERATE = os.getenv(
    "EXPERIENCE_CARD_AUTO_GENERATE", "false"
).lower() == "true"
EXPERIENCE_CARD_AUTO_SILENCE_MINUTES = _env_int(
    "EXPERIENCE_CARD_AUTO_SILENCE_MINUTES", 180
)
EXPERIENCE_CARD_AUTO_POLL_MINUTES = _env_int(
    "EXPERIENCE_CARD_AUTO_POLL_MINUTES", 10
)
EXPERIENCE_CARD_AUTO_BATCH_LIMIT = _env_int(
    "EXPERIENCE_CARD_AUTO_BATCH_LIMIT", 100
)

# 主动推送配置
PUSH_SECRET = os.getenv("PUSH_SECRET", "")
PUSH_MAX_PER_DAY = _env_int("PUSH_MAX_PER_DAY", 7)
PUSH_HARD_MINIMUM_MINUTES = 30
PUSH_NORMAL_MIN_MINUTES = 120
PUSH_NORMAL_JITTER_MINUTES = 90
PUSH_DECISION_NORMAL_COOLDOWN_MINUTES = _env_int("PUSH_DECISION_NORMAL_COOLDOWN_MINUTES", 30)
PUSH_DECISION_SKIP_COOLDOWN_MINUTES = (
    _env_int("PUSH_DECISION_SKIP_COOLDOWN_1_MINUTES", 60),
    _env_int("PUSH_DECISION_SKIP_COOLDOWN_2_MINUTES", 90),
    _env_int("PUSH_DECISION_SKIP_COOLDOWN_3_MINUTES", 120),
)
PUSH_DECISION_COST_INPUT_PER_MILLION = _env_float("PUSH_DECISION_COST_INPUT_PER_MILLION", 2.50)
PUSH_DECISION_COST_OUTPUT_PER_MILLION = _env_float("PUSH_DECISION_COST_OUTPUT_PER_MILLION", 10.00)
PUSH_DECISION_COOLDOWN_BYPASS_REASONS = {
    "user_message_received",
    "birthday",
    "anniversary",
    "hard_event",
    "important_event",
}
BARK_DEVICE_KEY = os.getenv("BARK_DEVICE_KEY", "")
BARK_API_URL = os.getenv("BARK_API_URL", "https://api.day.app/push")
BARK_TITLE = os.getenv("BARK_TITLE", "Rora")
BARK_ICON_URL = os.getenv("BARK_ICON_URL", "")
BARK_SOUND = os.getenv("BARK_SOUND", "")
BARK_OPEN_URL = os.getenv("BARK_OPEN_URL", "")
BARK_GROUP = os.getenv("BARK_GROUP", "")
BARK_LEVEL = os.getenv("BARK_LEVEL", "")
BARK_IMAGE_URL = os.getenv("BARK_IMAGE_URL", "")
BARK_BADGE = os.getenv("BARK_BADGE", "")
BARK_MAX_DELIVERY_ATTEMPTS = 3

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_push_lock = asyncio.Lock()

# 额外的请求头（有些 API 需要，比如 OpenRouter 需要 Referer）
EXTRA_REFERER = os.getenv("EXTRA_REFERER", "https://ai-memory-gateway.local")
EXTRA_TITLE = os.getenv("EXTRA_TITLE", "AI Memory Gateway")

# 健康数据的包裹格式
class HealthData(BaseModel):
    date: str
    steps: int = 0
    active_calories: float = 0.0
    sleep_start: Optional[str] = None
    sleep_end: Optional[str] = None
    sleep_duration: Optional[str] = None
    heart_rate_avg: float = 0.0
    heart_rate_min: float = 0.0
    heart_rate_max: float = 0.0
    resting_heart_rate_avg: float = 0.0
    resting_heart_rate_max: float = 0.0
    resting_heart_rate_mix: float = 0.0
    oxygen_saturation_avg: float = 0.0
    oxygen_saturation_max: float = 0.0
    oxygen_saturation_mix: float = 0.0
    hrv_avg: float = 0.0
    hrv_max: float = 0.0
    hrv_min: float = 0.0
    period: Optional[str] = None

# 环境状态数据的包裹格式
class StatusData(BaseModel):
    location: str
    weather: str
    temperature: float
    battery: int
    schedule: Optional[str] = None
# ============================================================
# 人设加载
# ============================================================

def load_system_prompt():
    """从 system_prompt.txt 文件读取人设内容"""
    prompt_path = os.path.join(os.path.dirname(__file__), "system_prompt.txt")
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    except FileNotFoundError:
        pass
    print("ℹ️  未找到 system_prompt.txt 或文件为空，将不注入 system prompt")
    return ""


SYSTEM_PROMPT = load_system_prompt()
_DEFAULT_SYSTEM_PROMPT = SYSTEM_PROMPT  # 保留文件原始版本
if SYSTEM_PROMPT:
    print(f"✅ 人设已加载，长度：{len(SYSTEM_PROMPT)} 字符")
else:
    print("ℹ️  无人设，纯转发模式")

# System Prompt 缓存（支持设置面板热更新）
_cached_system_prompt = None
_cached_system_prompt_loaded = False

async def get_system_prompt() -> str:
    """获取 system prompt（数据库优先，fallback 到文件）"""
    global _cached_system_prompt, _cached_system_prompt_loaded
    if _cached_system_prompt_loaded:
        return _cached_system_prompt or ""
    try:
        db_prompt = await get_gateway_config("systemPrompt", "")
        if db_prompt:
            _cached_system_prompt = db_prompt
        else:
            _cached_system_prompt = _DEFAULT_SYSTEM_PROMPT
            if _DEFAULT_SYSTEM_PROMPT:
                await set_gateway_config("systemPrompt", _DEFAULT_SYSTEM_PROMPT)
        _cached_system_prompt_loaded = True
        return _cached_system_prompt or ""
    except Exception:
        _cached_system_prompt = _DEFAULT_SYSTEM_PROMPT
        _cached_system_prompt_loaded = True
        return _cached_system_prompt or ""

def invalidate_system_prompt_cache():
    """清除 system prompt 缓存（设置面板更新后调用）"""
    global _cached_system_prompt, _cached_system_prompt_loaded
    _cached_system_prompt = None
    _cached_system_prompt_loaded = False


# ============================================================
# 应用生命周期管理
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库，关闭时断开连接"""
    global PARTITION_SESSION_ID
    experience_auto_task = None
    if MEMORY_ENABLED:
        try:
            await init_tables()
            await ensure_token_usage_table()
            count = await get_all_memories_count()
            print(f"✅ 记忆系统已启动，当前记忆数量：{count}")
            
            # 从数据库恢复面板配置（重启后保持Dashboard修改过的值）
            try:
                db_cfg = await get_all_gateway_config()
                if db_cfg:
                    _RESTORE_MAIN = {
                        "API_BASE_URL": str, "API_KEY": str, "DEFAULT_MODEL": str,
                        "MEMORY_API_BASE_URL": str,
                        "MEMORY_API_KEY": str,
                        "MEMORY_MODEL": str,
                        "MEMORY_API_THINKING": str,
                        "MEMORY_ENABLED": lambda v: _parse_bool(v),
                        "MAX_MEMORIES_INJECT": int, "MEMORY_EXTRACT_INTERVAL": int,
                        "CACHE_PARTITION_ENABLED": lambda v: _parse_bool(v),
                        "CACHE_PARTITION_X": int, "CACHE_PARTITION_TRIGGER": str,
                        "CACHE_PARTITION_WINDOW": int, "CACHE_SUMMARY_MODEL": str,
                        "SUMMARY_API_BASE_URL": str, "SUMMARY_API_KEY": str,
                        "FORCE_STREAM": lambda v: _parse_bool(v),
                        "REASONING_EFFORT": str,
                    }
                    _MEMORY_EXTRACTOR_CONFIG_KEYS = {
                        "MEMORY_API_BASE_URL", "MEMORY_API_KEY",
                        "MEMORY_MODEL", "MEMORY_API_THINKING",
                    }
                    _RESTORE_DB = {
                        "EMBEDDING_API_KEY": str, "EMBEDDING_BASE_URL": str,
                        "EMBEDDING_MODEL": str, "EMBEDDING_DIM": int,
                        "MIN_SCORE_THRESHOLD": float,
                        "MEMORY_VECTOR_ENABLED": lambda v: _parse_bool(v),
                        "MEMORY_HW_KEYWORD": float, "MEMORY_HW_SEMANTIC": float,
                        "MEMORY_HW_IMPORTANCE": float, "MEMORY_HW_RECENCY": float,
                        "MEMORY_SEMANTIC_THRESHOLD": float,
                    }
                    restored = []
                    for key, val in db_cfg.items():
                        if not val:
                            continue
                        if key in _RESTORE_MAIN:
                            globals()[key] = _RESTORE_MAIN[key](val)
                            if key in _MEMORY_EXTRACTOR_CONFIG_KEYS:
                                import memory_extractor as _me_mod
                                setattr(_me_mod, key, globals()[key])
                            restored.append(key)
                        elif key in _RESTORE_DB:
                            setattr(_db_module, key, _RESTORE_DB[key](val))
                            restored.append(key)
                    if restored:
                        print(f"🔄 从数据库恢复 {len(restored)} 项面板配置: {', '.join(restored)}")
            except Exception as e:
                print(f"[warning] 恢复面板配置失败: {e}")
            
            if not MEMORY_EXTRACT_ENABLED:
                print(f"ℹ️  记忆提取+注入已关闭（MEMORY_EXTRACT_ENABLED=false）")
            
            # 分区缓存：从DB读取活跃对话线ID
            if CACHE_PARTITION_ENABLED:
                db_sid = await get_gateway_config("partition_session_id", "")
                if db_sid:
                    PARTITION_SESSION_ID = db_sid
                    print(f"🔗 活跃对话线(DB): {PARTITION_SESSION_ID}")
                elif PARTITION_SESSION_ID:
                    await set_gateway_config("partition_session_id", PARTITION_SESSION_ID)
                    print(f"🔗 活跃对话线(ENV→DB): {PARTITION_SESSION_ID}")
                print(f"🔒 分区缓存已启用: X={CACHE_PARTITION_X}, 摘要模型={CACHE_SUMMARY_MODEL}")
            if EXPERIENCE_CARD_AUTO_GENERATE:
                experience_auto_task = asyncio.create_task(_experience_card_auto_loop())
                print(
                    "[experience-auto] enabled "
                    f"silence_minutes={EXPERIENCE_CARD_AUTO_SILENCE_MINUTES}"
                )
            else:
                print("[experience-auto] disabled")
        except Exception as e:
            print(f"⚠️  数据库初始化失败: {e}")
            print("⚠️  记忆系统将不可用，但网关仍可正常转发")
    else:
        print("ℹ️  记忆系统已关闭（设置 MEMORY_ENABLED=true 开启）")
    
    yield

    if experience_auto_task:
        experience_auto_task.cancel()
        try:
            await experience_auto_task
        except asyncio.CancelledError:
            pass
    if MEMORY_ENABLED:
        await close_pool()


app = FastAPI(title="AI Memory Gateway", version="2.0.0", lifespan=lifespan)


# --- 把这段加在这里 ---
@app.get("/debug/routes")
async def get_routes():
    routes = [{"path": route.path, "methods": list(route.methods)} for route in app.routes]
    return routes
# ---------------------

# 静态文件和模板配置
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ============================================================
# 网关鉴权中间件
# ============================================================

# 不需要应用层鉴权的路径（根路径精确匹配，其余按前缀匹配）
PUBLIC_PATHS = ("/", "/static/", "/health", "/favicon.ico", "/api/push/trigger")

# Dashboard 页面和网页私有接口由 Cloudflare Access 保护。
DASHBOARD_ACCESS_PATHS = ("/dashboard",)
DASHBOARD_ACCESS_PREFIXES = ("/dashboard/", "/api/", "/import/", "/export/")

# 程序客户端继续使用 X-Gateway-Key，避免影响 Kelivo、健康/状态上报等非网页调用。
IO_KEY_PREFIXES = ("/v1/io/",)
GATEWAY_KEY_PREFIXES = ("/v1/",)
GATEWAY_KEY_PATHS = ("/api/health/push", "/api/status/push", "/debug/routes")


def _is_public_path(path: str) -> bool:
    if path == "/":
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PATHS[1:])


def _is_gateway_key_path(path: str) -> bool:
    if _is_io_key_path(path):
        return False
    if path in GATEWAY_KEY_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in GATEWAY_KEY_PREFIXES)


def _is_io_key_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in IO_KEY_PREFIXES)


def _is_dashboard_access_path(path: str) -> bool:
    if _is_gateway_key_path(path):
        return False
    if _is_io_key_path(path):
        return False
    if path in DASHBOARD_ACCESS_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in DASHBOARD_ACCESS_PREFIXES)

@app.middleware("http")
async def gateway_auth_middleware(request: Request, call_next):
    """将 Dashboard 的 Access 鉴权与程序 API 的网关密钥鉴权分开。"""
    path = request.url.path

    # OPTIONS 预检请求放行（CORS 需要）
    if request.method == "OPTIONS":
        return await call_next(request)

    if _is_public_path(path):
        return await call_next(request)

    if _is_io_key_path(path):
        if not IO_INGEST_SECRET:
            if not hasattr(gateway_auth_middleware, "_warned_io_secret"):
                print("⚠️  IO_INGEST_SECRET 未设置，io感知入口保持关闭", flush=True)
                gateway_auth_middleware._warned_io_secret = True
            return JSONResponse(
                status_code=503,
                content={"error": "io ingest is not configured"},
            )
        io_key = request.headers.get("X-IO-Key", "")
        io_auth_success = bool(io_key) and secrets.compare_digest(io_key, IO_INGEST_SECRET)
        _log_io_auth_diag(request, io_key, io_auth_success)
        if not io_auth_success:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized. Provide X-IO-Key header."},
            )
        return await call_next(request)

    if _is_dashboard_access_path(path):
        access_result = await _authenticate_dashboard_access(request)
        if not access_result.get("ok"):
            return JSONResponse(
                status_code=access_result.get("status_code", 403),
                content={"error": "Forbidden"},
            )
        return await call_next(request)

    # 程序 API 只接受 X-Gateway-Key 请求头，不再接受 URL 查询参数登录。
    if not GATEWAY_SECRET:
        if not hasattr(gateway_auth_middleware, "_warned_gateway_secret"):
            print("⚠️  GATEWAY_SECRET 未设置！程序 API 端点不受保护！")
            print("⚠️  请在环境变量中设置 GATEWAY_SECRET 以启用程序 API 鉴权")
            gateway_auth_middleware._warned_gateway_secret = True
        return await call_next(request)

    header_key = request.headers.get("X-Gateway-Key", "")

    # compare_digest 防时序侧信道攻击
    auth_success = bool(header_key) and secrets.compare_digest(header_key, GATEWAY_SECRET)
    _log_auth_diag(request, header_key, auth_success)
    if not auth_success:
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized. Provide X-Gateway-Key header."},
        )

    return await call_next(request)


# ============================================================
# 记忆注入
# ============================================================

async def build_system_prompt_with_memories(user_message: str) -> str:
    """构建记忆数据块（不含人设，由调用方追加到人设末尾）"""
    if not MEMORY_ENABLED or not MEMORY_EXTRACT_ENABLED:
        return ""
    if MAX_MEMORIES_INJECT <= 0:
        return ""

    try:
        memories = await search_memories(user_message, limit=MAX_MEMORIES_INJECT)
        if not memories:
            return ""

        memory_lines = []
        for mem in memories:
            date_str = ""
            if mem.get("created_at"):
                try:
                    utc_str = str(mem['created_at'])[:19]
                    utc_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    local_dt = utc_dt + timedelta(hours=TIMEZONE_HOURS)
                    date_str = f"[{local_dt.strftime('%Y-%m-%d')}] "
                except:
                    date_str = f"[{str(mem['created_at'])[:10]}] "
            memory_lines.append(f"- {date_str}{mem['content']}")
        memory_text = "\n".join(memory_lines)

        print(f"📚 注入了 {len(memories)} 条相关记忆")
        return f"<脑海中浮现的既往事实>\n{memory_text}\n</脑海中浮现的既往事实>"

    except Exception as e:
        print(f"⚠️  记忆检索失败: {e}")
        return ""


# ============================================================
# 分区缓存（Partition Cache）
# ============================================================

def _is_anthropic_model(model: str) -> bool:
    """判断是否为 Anthropic Claude 系列模型（只有 Claude 支持 cache_control）"""
    model_lower = model.lower()
    return "claude" in model_lower or "anthropic" in model_lower


def _short_hash_text(text: str) -> str:
    if not text:
        return "empty"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def _diag_normalize(value):
    if isinstance(value, dict):
        return {
            k: _diag_normalize(v)
            for k, v in sorted(value.items())
            if k not in ("created_at", "cache_control")
        }
    if isinstance(value, list):
        return [_diag_normalize(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _short_hash_value(value) -> str:
    try:
        text = json.dumps(_diag_normalize(value), ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return _short_hash_text(text)


def _count_cache_breakpoints(value) -> int:
    if isinstance(value, dict):
        count = 1 if "cache_control" in value else 0
        return count + sum(_count_cache_breakpoints(v) for v in value.values())
    if isinstance(value, list):
        return sum(_count_cache_breakpoints(v) for v in value)
    return 0


def _api_provider_label(url: str) -> str:
    if not url:
        return "unknown"
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    if not host:
        return "unknown"
    if "openrouter" in host:
        return "openrouter"
    if "openai" in host:
        return "openai"
    if "deepseek" in host:
        return "deepseek"
    return host.split(":")[0]


def _usage_get(mapping: dict, *keys):
    current = mapping or {}
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return "not_reported"
        current = current[key]
    return current


def _first_reported(*values):
    for value in values:
        if value != "not_reported":
            return value
    return "not_reported"


def _log_cache_build_diag(diag: dict):
    if not diag:
        return
    print(
        "🧪 缓存诊断: "
        f"provider={diag.get('api_provider', 'unknown')} | "
        f"model={diag.get('actual_model', 'unknown')} | "
        f"mode={diag.get('mode', 'unknown')} | "
        f"session_hash={diag.get('session_hash', 'unknown')} | "
        f"client_system={diag.get('client_system_count', 'not_reported')}条/"
        f"{diag.get('client_system_chars', 'not_reported')}字/"
        f"hash={diag.get('client_system_hash', 'not_reported')} | "
        f"base_prompt={diag.get('base_prompt_chars', 'not_reported')}字/"
        f"hash={diag.get('base_prompt_hash', 'not_reported')} | "
        f"summary={diag.get('summary_count', 'not_reported')}段/"
        f"{diag.get('summary_chars', 'not_reported')}字/"
        f"hash={diag.get('summary_hash', 'not_reported')} | "
        f"rounds_total={diag.get('total_rounds', 'not_reported')} | "
        f"a_start_round={diag.get('a_start_round', 'not_reported')} | "
        f"A={diag.get('a_rounds', 'not_applicable')}轮/"
        f"{diag.get('a_messages', 'not_applicable')}条/"
        f"hash={diag.get('a_hash', 'not_applicable')} | "
        f"B={diag.get('b_rounds', 'not_applicable')}轮/"
        f"{diag.get('b_messages', 'not_applicable')}条/"
        f"hash={diag.get('b_hash', 'not_applicable')} | "
        f"rotation_count={diag.get('rotation_count', 'not_reported')} | "
        f"constructed_breakpoints={diag.get('constructed_breakpoints', 'not_reported')} | "
        f"sent_breakpoints={diag.get('sent_breakpoints', 'not_reported')}",
        flush=True,
    )



SENSITIVE_URL_LOGIN_KEYS = ("gateway_key", "key", "token")
_CF_ACCESS_JWKS_CACHE = {"expires_at": 0.0, "keys": []}
_CF_ACCESS_JWKS_TTL_SECONDS = 3600


def _redact_gateway_key_in_text(value: str) -> str:
    lower_value = value.lower() if value else ""
    if not lower_value or not any(f"{key}=" in lower_value for key in SENSITIVE_URL_LOGIN_KEYS):
        return value
    return re.sub(
        r"([?&](?:gateway_key|key|token)=)[^&\s\"]*",
        r"\1[REDACTED]",
        value,
        flags=re.IGNORECASE,
    )


class _GatewayAccessLogFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(
                _redact_gateway_key_in_text(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: _redact_gateway_key_in_text(v) if isinstance(v, str) else v
                for k, v in record.args.items()
            }
        if isinstance(record.msg, str):
            record.msg = _redact_gateway_key_in_text(record.msg)
        return True


def _install_access_log_redaction():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _GatewayAccessLogFilter) for f in logger.filters):
        logger.addFilter(_GatewayAccessLogFilter())


def _legacy_query_key_present(request: Request) -> bool:
    return any(key in request.query_params for key in SENSITIVE_URL_LOGIN_KEYS)


def _cf_access_team_domain() -> str:
    if not CF_ACCESS_TEAM_DOMAIN:
        return ""
    if CF_ACCESS_TEAM_DOMAIN.startswith("https://"):
        return CF_ACCESS_TEAM_DOMAIN
    return f"https://{CF_ACCESS_TEAM_DOMAIN}"


def _cf_access_certs_url() -> str:
    team_domain = _cf_access_team_domain()
    if not team_domain:
        return ""
    return f"{team_domain}/cdn-cgi/access/certs"


def _hash_identity(value: str) -> str:
    if not value:
        return "not_reported"
    return _short_hash_text(value.lower())


async def _get_cf_access_jwks() -> list:
    now_ts = datetime.now(timezone.utc).timestamp()
    cached_keys = _CF_ACCESS_JWKS_CACHE.get("keys") or []
    if cached_keys and now_ts < float(_CF_ACCESS_JWKS_CACHE.get("expires_at", 0.0)):
        return cached_keys

    certs_url = _cf_access_certs_url()
    if not certs_url:
        return []

    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(certs_url)
        response.raise_for_status()
        payload = response.json()

    keys = payload.get("keys", []) if isinstance(payload, dict) else []
    if isinstance(keys, list) and keys:
        _CF_ACCESS_JWKS_CACHE["keys"] = keys
        _CF_ACCESS_JWKS_CACHE["expires_at"] = now_ts + _CF_ACCESS_JWKS_TTL_SECONDS
    return keys if isinstance(keys, list) else []


def _email_allowed(email: str) -> bool:
    if not CF_ACCESS_ALLOWED_EMAILS and not CF_ACCESS_ALLOWED_EMAIL_DOMAINS:
        return True
    normalized = (email or "").strip().lower()
    if normalized in CF_ACCESS_ALLOWED_EMAILS:
        return True
    if "@" in normalized:
        domain = normalized.rsplit("@", 1)[-1]
        return domain in CF_ACCESS_ALLOWED_EMAIL_DOMAINS
    return False


async def _verify_cf_access_jwt(token: str) -> dict:
    if not token:
        return {"ok": False, "reason": "missing_jwt", "status_code": 401}
    if not CF_ACCESS_AUD or not _cf_access_team_domain():
        return {"ok": False, "reason": "access_config_missing", "status_code": 403}
    if jwt is None or RSAAlgorithm is None:
        return {"ok": False, "reason": "jwt_dependency_missing", "status_code": 403}

    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        keys = await _get_cf_access_jwks()
        selected_keys = [key for key in keys if not kid or key.get("kid") == kid]
        if not selected_keys:
            return {"ok": False, "reason": "access_key_not_found", "status_code": 403}

        valid_issuers = {_cf_access_team_domain(), f"{_cf_access_team_domain()}/"}
        last_error = None
        for jwk in selected_keys:
            try:
                public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
                payload = jwt.decode(
                    token,
                    key=public_key,
                    algorithms=["RS256"],
                    audience=CF_ACCESS_AUD,
                    options={"verify_iss": False},
                )
                if payload.get("iss") not in valid_issuers:
                    return {"ok": False, "reason": "invalid_issuer", "status_code": 403}

                email = str(payload.get("email") or "")
                if not _email_allowed(email):
                    return {
                        "ok": False,
                        "reason": "identity_not_allowed",
                        "status_code": 403,
                        "identity_hash": _hash_identity(email),
                    }
                return {
                    "ok": True,
                    "reason": "ok",
                    "status_code": 200,
                    "identity_hash": _hash_identity(email),
                }
            except Exception as exc:
                last_error = exc

        return {
            "ok": False,
            "reason": type(last_error).__name__ if last_error else "jwt_invalid",
            "status_code": 403,
        }
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__, "status_code": 403}


def _log_dashboard_access_diag(request: Request, result: dict):
    print(
        "🛡️ Dashboard Access: "
        f"path={request.url.path} | "
        f"jwt_present={str(bool(request.headers.get('Cf-Access-Jwt-Assertion', ''))).lower()} | "
        f"jwt_valid={str(bool(result.get('ok'))).lower()} | "
        f"identity_hash={result.get('identity_hash', 'not_reported')} | "
        f"reason={result.get('reason', 'not_reported')}",
        flush=True,
    )


async def _authenticate_dashboard_access(request: Request) -> dict:
    result = await _verify_cf_access_jwt(request.headers.get("Cf-Access-Jwt-Assertion", ""))
    _log_dashboard_access_diag(request, result)
    return result


def _log_auth_diag(request: Request, header_key: str, auth_success: bool):
    if request.url.path != "/v1/chat/completions":
        return
    header_present = bool(header_key)
    query_key_present = _legacy_query_key_present(request)
    header_valid = header_present and secrets.compare_digest(header_key, GATEWAY_SECRET)
    print(
        "🔐 鉴权诊断: "
        f"path=/v1/chat/completions | "
        f"header_present={str(header_present).lower()} | "
        f"header_valid={str(header_valid).lower()} | "
        f"query_key_present={str(query_key_present).lower()} | "
        f"query_key_valid=false | "
        f"auth_success={str(auth_success).lower()}",
        flush=True,
    )


def _log_io_auth_diag(request: Request, io_key: str, auth_success: bool):
    print(
        "📱 io鉴权诊断: "
        f"path={request.url.path} | "
        f"header_present={str(bool(io_key)).lower()} | "
        f"header_valid={str(auth_success).lower()}",
        flush=True,
    )


_install_access_log_redaction()


def _log_usage_diag(prefix: str, usage: dict | None):
    details = usage.get("prompt_tokens_details", {}) if isinstance(usage, dict) else {}
    prompt_tokens = _usage_get(usage, "prompt_tokens")
    completion_tokens = _usage_get(usage, "completion_tokens")
    total_tokens = _usage_get(usage, "total_tokens")
    cached_tokens = _usage_get(usage, "prompt_tokens_details", "cached_tokens")
    cache_read_tokens = _first_reported(
        _usage_get(usage, "cache_read_input_tokens"),
        _usage_get(usage, "cache_read_tokens"),
        _usage_get(usage, "prompt_tokens_details", "cache_read_tokens"),
        cached_tokens,
    )
    cache_write_tokens = _first_reported(
        _usage_get(usage, "cache_creation_input_tokens"),
        _usage_get(usage, "cache_write_tokens"),
        _usage_get(usage, "prompt_tokens_details", "cache_write_tokens"),
        _usage_get(usage, "prompt_tokens_details", "cache_creation_tokens"),
    )
    known_usage_keys = {
        "prompt_tokens", "completion_tokens", "total_tokens", "prompt_tokens_details",
        "cache_read_input_tokens", "cache_read_tokens", "cache_creation_input_tokens",
        "cache_write_tokens",
    }
    known_detail_keys = {"cached_tokens", "cache_read_tokens", "cache_write_tokens", "cache_creation_tokens"}
    other_cache = {}
    if isinstance(usage, dict):
        for k, v in usage.items():
            if "cache" in k and k not in known_usage_keys:
                other_cache[k] = v
    if isinstance(details, dict):
        for k, v in details.items():
            if "cache" in k and k not in known_detail_keys:
                other_cache[f"prompt_tokens_details.{k}"] = v
    other_cache_text = json.dumps(other_cache, ensure_ascii=False, sort_keys=True) if other_cache else "not_reported"
    print(
        f"📊 {prefix} Cache Usage: "
        f"prompt_tokens={prompt_tokens} | "
        f"completion_tokens={completion_tokens} | "
        f"total_tokens={total_tokens} | "
        f"prompt_tokens_details.cached_tokens={cached_tokens} | "
        f"cache_read_tokens={cache_read_tokens} | "
        f"cache_creation_write_tokens={cache_write_tokens} | "
        f"other_cache_fields={other_cache_text}",
        flush=True,
    )


def _strip_cache_control(messages: list):
    """
    剥掉消息中的 cache_control 字段，非 Claude 模型用不了。
    如果 content 数组只剩纯文本 block，降级回字符串格式。
    """
    stripped = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                del block["cache_control"]
                stripped += 1
        if len(content) == 1 and isinstance(content[0], dict) and content[0].get("type") == "text":
            msg["content"] = content[0]["text"]
    if stripped > 0:
        print(f"🔧 兼容性处理: 剥离了 {stripped} 个 cache_control 字段（非 Claude 模型）")
    return stripped


def build_time_injection() -> str:
    """构建时间注入文本（东八区）"""
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc + timedelta(hours=TIMEZONE_HOURS)
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekday_names[now_local.weekday()]
    time_str = now_local.strftime("%Y年%m月%d日 %H:%M")
    return f"【当前时间】{time_str} {weekday}"


def _format_summary_message_time(msg: dict) -> str:
    raw = msg.get("created_at")
    if not raw:
        return "时间未知"
    try:
        if isinstance(raw, datetime):
            dt = raw
        else:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_tz = timezone(timedelta(hours=TIMEZONE_HOURS))
        return dt.astimezone(local_tz).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "时间未知"


async def generate_summary(messages: list, session_id: str = "") -> str:
    """调用轻量模型压缩A区消息为摘要"""
    if not messages:
        return ""
    if session_id:
        try:
            await record_summary_attempt(session_id, len(messages), CACHE_SUMMARY_MODEL)
        except Exception as monitor_error:
            print(
                "summary_health_monitor_failed "
                f"operation=attempt error_type={type(monitor_error).__name__}",
                flush=True,
            )
    
    conversation_text = ""
    for msg in messages:
        role_label = "用户" if msg['role'] == 'user' else "AI"
        content = msg['content'] if isinstance(msg['content'], str) else str(msg['content'])
        msg_time = _format_summary_message_time(msg)
        conversation_text += f"[{msg_time}] {role_label}: {content}\n\n"
    
    prompt = f"""请把下面这段对话整理成一段或多段 append-only 的“过渡期近期日记摘要”。这份摘要目前仍是遥遥理解近期连续性的主要来源，不能压成只剩结论的极简索引。

写成简洁、自然的日记，而不是报告、教训或关系分析。同一段对话中存在多个彼此独立的重要事项时分别记录；普通寒暄和没有推动后续互动的临时状态不必单独成段。

核心规则：
1. 只根据原文和每条消息前的上海时间记录。保持人物、第三方关系、时间顺序、动机顺序和直接因果，不编造、不补全，不把遥遥的推测或角色化能力写成现实事实。
2. 日期使用 YYYY-MM-DD 或可靠的日期范围，不精确到小时和分钟。禁止永久保存“今天、昨天、此前周末、最近、今晚”等相对时间。能根据消息时间可靠换算时改成绝对日期；不能确定实际发生日期时写“实际发生日期未明确，于 YYYY-MM-DD 的对话中提及”。
3. 全部使用遥遥第一人称关系视角。“我”只指遥遥，“你”只指 Sasa；第三方必须写清身份，不能用含糊代词改变人物关系。转写前先在草稿中确认原文每一句的说话人是 Sasa 还是遥遥，再统一转换为遥遥第一人称叙述；转换完成后自查一遍，确认没有把 Sasa 的发言误写成”我”。
4. 首要保存近期连续性：发生了什么、事情怎样发展、你当时明确表达的感受或反应、我当时怎样回应，以及截至这段对话结束时的状态。保留未来能让我认出这件事的具体细节和情绪纹理，但不要写成逐句流水账。
5. 双方的感受只记录原文明示的内容。禁止自行提炼教训、成长感悟、人格评价、关系升华或未来相处准则；不要写“这体现了……”“说明双方关系更加……”等分析句。
6. 一次性的情绪、当天状态和临时选择不能升级成长期偏好或稳定事实。长期背景只有在原文明示长期、反复确认或未来持续有效时才记录。
7. 后文明确纠正前文时，以后文为准，并完整写清旧说法和新说法；不能同时保存互相冲突的版本。
8. 未完成事项只有在原文明示以后仍需继续时才记录。状态可能变化时使用“截至本次对话”“当时尚未”等时间限定，不写无时效的“当前结果”。
9. 根据原文的信息密度决定长度。优先删除重复表达和普通寒暄，但不得为了缩短摘要删掉关键人物、事件过程、直接因果、双方回应、事实纠正、明确约定或有辨识度的小细节。单次对话若可拆分为多个独立事项，每段目标控制在150–350字；单个事项超过400字时，应重新审视是否包含了非关键的过程细节，优先合并或省略而非继续扩写。
10. 不做八大主题归类，不输出教训、分析、评分或格式外解释，不为了填满固定字段而编造内容。

固定格式，可重复输出：

### YYYY-MM-DD 或日期范围

一段自然的近期日记摘要。

如果原文确实包含明确纠正或仍需继续的事项，可以在日记段落后补充以下行；没有就不要输出：

- 明确纠正：……
- 待继续事项：……

下面是待整理对话：

---
{conversation_text}
---

近期连续性记录："""
    
    try:
        headers = {
            "Authorization": f"Bearer {get_summary_api_key()}",
            "Content-Type": "application/json",
        }
        summary_url = get_summary_api_base_url()
        if "openrouter" in summary_url:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(summary_url, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL,
                "max_tokens": 3000,
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": prompt}],
            })
            if response.status_code == 200:
                data = response.json()
                if "choices" in data:
                    choice = data["choices"][0] or {}
                    message = choice.get("message") or {}
                    content = message.get("content")
                    summary = content.strip() if isinstance(content, str) else ""
                    finish_reason = str(choice.get("finish_reason") or "not_reported")[:64]
                    usage = data.get("usage") or {}
                    prompt_tokens = usage.get("prompt_tokens", "not_reported")
                    completion_tokens = usage.get("completion_tokens", "not_reported")
                    if summary:
                        print(
                            f"📝 摘要生成完成: {len(summary)}字 (压缩{len(messages)}条消息) "
                            f"finish_reason={finish_reason} prompt_tokens={prompt_tokens} "
                            f"completion_tokens={completion_tokens}"
                        )
                        return summary
                    print(
                        "⚠️ 摘要生成空结果: "
                        f"finish_reason={finish_reason} reasoning_present={bool(message.get('reasoning_content'))} "
                        f"prompt_tokens={prompt_tokens} completion_tokens={completion_tokens}"
                    )
                    await _record_summary_failure_safe(
                        session_id,
                        "output_truncated" if finish_reason == "length" else "empty_content",
                    )
                    return ""

        print(f"⚠️ 摘要生成失败: HTTP {response.status_code}")
        await _record_summary_failure_safe(
            session_id,
            f"upstream_http_{response.status_code}",
        )
        return ""
    except Exception as e:
        print(f"⚠️ 摘要生成异常: {type(e).__name__}")
        await _record_summary_failure_safe(
            session_id,
            f"exception_{type(e).__name__}",
        )
        return ""


# ============================================================
# 摘要自动压缩（summary_parts 段数/字数超过阈值时，把多段摘要再压成一段）
# ============================================================

def _get_int_env(key: str, default: int) -> int:
    """
    读取整数型环境变量。
    - 在 Coolify 里留空（空字符串）→ 视为 0（禁用该项判断）
    - 完全不设置这个变量 → 用 default
    - 填了非法值 → 打印警告，用 default
    """
    raw = os.getenv(key)
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "":
        return 0
    try:
        return int(raw)
    except ValueError:
        print(f"⚠️ 环境变量 {key} 值无效: '{raw}'，回退默认值 {default}")
        return default

# 超过这个段数就触发压缩（默认0=禁用，只按字数判断；如果想按段数触发可以在Coolify里设置）
SUMMARY_CONSOLIDATE_MAX_PARTS = _get_int_env("SUMMARY_CONSOLIDATE_MAX_PARTS", 0)
# 超过这个总字数就触发压缩（默认1万字）
SUMMARY_CONSOLIDATE_MAX_CHARS = _get_int_env("SUMMARY_CONSOLIDATE_MAX_CHARS", 10000)
# 压缩后目标字数（软限制，模型可以超）
SUMMARY_CONSOLIDATE_TARGET_CHARS = _get_int_env("SUMMARY_CONSOLIDATE_TARGET_CHARS", 6000) or 6000

# 注意：这里是纯文本输出，不进JSON，双引号、书名号随便用，不受碎片整理那条JSON规则限制
SUMMARY_CONSOLIDATION_PROMPT = """请把本次新增的近期连续性记录合并进当前长期摘要，生成更新后的完整长期摘要。

多段自动合并继续保持关闭；本 prompt 只作为人工或受控流程需要重建长期摘要时的备用规则。

输入分为两部分：

当前长期摘要：
---
{current_canonical_summary}
---

本次待合并的近期连续性记录：
---
{new_event_cards}
---

核心规则：
1. 当前长期摘要是基底。只处理新增记录直接带来的补充、更新、纠正和去重；无关内容尽量保持原文不变。
2. 长期摘要只维护：仍在影响当前聊天的背景、持续有效的关系设定、明确长期信息、重要关系转折、明确约定和未完成事项。普通每日事件的完整经过留在原始消息和共同经历卡片层，不机械复制进长期摘要。
3. 新增内容若只是一次性情绪、当天状态、临时选择或普通闲聊，不得升级成长期偏好、人格特征或关系结论。
4. 保留事实所需的人物、第三方身份、直接因果和必要细节。不得总结教训、成长感悟、人生意义，不写“体现了”“说明了双方关系更加”等评价。
5. 后续明确纠正旧事实时替换错误内容，不同时保留冲突版本；证据不足时标记不确定，不擅自覆盖。
6. 使用绝对日期或日期范围，不保留“今天、昨天、此前周末、最近”等相对时间，也不强制精确到小时和分钟。实际发生日期不明时，区分事件日期未知与对话提及日期。
7. 保持遥遥第一人称视角。“我”只指遥遥，“你”只指 Sasa；第三方每次明确身份，不改变人物关系和对象。
8. 不再强制八大主题归类。按内容使用下列固定功能区；没有内容的区块保留标题即可，不为填满结构而编造。
9. 不得为了达到 {target_chars} 删除重要关系背景、明确约定、事实纠正、未完成事项或辨认关键经历所必需的细节。事实完整性优先于目标字数。
10. 不要输出格式外解释。

请从下一行开始，只输出更新后的完整长期摘要，不要输出解释、分析或变更说明：

# 对话摘要

## 当前连续背景

## 已确认的长期信息与关系设定

## 重要经历与关系转折

## 明确约定与未完成事项

## 事实纠正

"""


async def consolidate_summary_parts(summary_parts: list) -> list:
    """
    如果 summary_parts 段数或总字数超过阈值，调用模型把它们整理压缩成一份结构化摘要。
    压缩失败时原样返回，不影响主流程。
    """
    if SUMMARY_CONSOLIDATE_MAX_PARTS <= 0 and SUMMARY_CONSOLIDATE_MAX_CHARS <= 0:
        return summary_parts  # 功能关闭（两个阈值都留空/设0）

    if not summary_parts or len(summary_parts) <= 1:
        return summary_parts

    total_chars = sum(len(p) for p in summary_parts)
    should_consolidate = (
        (SUMMARY_CONSOLIDATE_MAX_PARTS > 0 and len(summary_parts) >= SUMMARY_CONSOLIDATE_MAX_PARTS)
        or (SUMMARY_CONSOLIDATE_MAX_CHARS > 0 and total_chars >= SUMMARY_CONSOLIDATE_MAX_CHARS)
    )
    if not should_consolidate:
        return summary_parts

    current_canonical_summary = "\n\n".join(str(p) for p in summary_parts[:-1]).strip()
    new_event_cards = str(summary_parts[-1]).strip()
    prompt = SUMMARY_CONSOLIDATION_PROMPT.format(
        current_canonical_summary=current_canonical_summary or "（暂无当前长期摘要）",
        new_event_cards=new_event_cards,
        target_chars=SUMMARY_CONSOLIDATE_TARGET_CHARS,
    )

    try:
        headers = {
            "Authorization": f"Bearer {get_summary_api_key()}",
            "Content-Type": "application/json",
        }
        summary_url = get_summary_api_base_url()
        if "openrouter" in summary_url:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
            
        # ★ 这里把超时时间改长，给足 Token，防止长文被截断 ★
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(summary_url, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL,
                "max_tokens": 20000,
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": prompt}],
            })
            if response.status_code == 200:
                data = response.json()
                if "choices" in data:
                    merged = data["choices"][0]["message"]["content"].strip()
                    if merged:
                        print(f"🗜️ 摘要压缩完成: {len(summary_parts)}段/{total_chars}字 → 1段/{len(merged)}字")
                        return [merged]

        print(f"⚠️ 摘要压缩失败: HTTP {response.status_code}，保留原有{len(summary_parts)}段")
        return summary_parts
    except Exception as e:
        print(f"⚠️ 摘要压缩异常: {e}，保留原有{len(summary_parts)}段")
        return summary_parts


def group_by_rounds(history: list) -> list:
    """
    按逻辑轮分组：每个user消息开始一轮，到下一个user前结束。
    一轮可能包含: [user, assistant] 或 [user, assistant(tool_calls), tool, assistant] 等。
    """
    rounds = []
    current_round = []
    for msg in history:
        if msg['role'] == 'user' and current_round:
            rounds.append(current_round)
            current_round = []
        current_round.append(msg)
    if current_round:
        rounds.append(current_round)
    return rounds


def _should_rotate(b_rounds_count: int, X: int, a_msgs: list) -> bool:
    """
    判断是否应该触发A区→摘要的轮转。
    
    rounds模式（默认）：B区轮数 >= X 时触发
    time模式：A区最早消息距今 >= 时间窗口 时触发（短时间内大量消息不频繁摘要）
    """
    if b_rounds_count == 0:
        return False
    
    if CACHE_PARTITION_TRIGGER == "time":
        a_first_time = None
        for msg in a_msgs:
            t = msg.get('created_at')
            if t:
                a_first_time = t
                break
        
        if a_first_time:
            now = datetime.now(timezone.utc)
            if a_first_time.tzinfo is None:
                a_first_time = a_first_time.replace(tzinfo=timezone.utc)
            age_minutes = (now - a_first_time).total_seconds() / 60
            return age_minutes >= CACHE_PARTITION_WINDOW
        
        return b_rounds_count >= X
    
    return b_rounds_count >= X

# 时间窗口模式下单次请求最大轮转次数（防止一口气压完所有历史）
CACHE_MAX_ROTATIONS = int(os.getenv("CACHE_MAX_ROTATIONS", "2"))


def _apply_breakpoint(msg: dict) -> bool:
    """
    给消息打上 cache_control breakpoint。
    支持 content 为 str 或 list（多模态block数组）两种格式。
    返回 True 表示成功打上，False 表示无法打（比如content为空）。
    """
    content = msg.get('content')
    
    # content 是纯字符串
    if isinstance(content, str) and content.strip():
        msg['content'] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        return True
    
    # content 是 block 数组（多模态消息）
    if isinstance(content, list):
        # 从后往前找最后一个 text block
        for i in range(len(content) - 1, -1, -1):
            block = content[i]
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                block["cache_control"] = {"type": "ephemeral"}
                return True
    
    return False


async def build_partitioned_messages(
    session_id: str,
    all_messages: list,
    base_prompt: str,
    user_message: str,
    cache_diag: dict | None = None,
) -> list:
    """
    分区缓存模式：构建带breakpoint的messages数组。
    
    结构：
    system: [{人设, BP1}]                        ← 永远命中
    messages:
      [摘要blocks（每段一个block）, 最后BP]       ← 尾部追加，前面命中
      [摘要assistant]
      [A区消息... 最后一条BP2]                    ← 正常轮次不变
      [B区消息... 最后一条BP3]                    ← lookback命中
      [当前user: 时间+记忆+消息]                  ← 不缓存
    """
    X = CACHE_PARTITION_X
    
    # 客户端(Kelivo)可能会发system消息过来（比如世界书"系统提示前/后"位置的常驻条目
    # 会被Kelivo合并进一条role=system的消息）。以前这里直接丢弃，现在改成提取出来，
    # 合并进网关自己的人设一起转发，不再丢失。
    client_system_content = ""
    client_system_msg_count = 0
    for m in all_messages:
        if m.get('role') == 'system':
            client_system_msg_count += 1
            c = m.get('content')
            if isinstance(c, list):
                c = " ".join(
                    item.get("text", "") for item in c
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            c = (c or "").strip() if isinstance(c, str) else str(c or "")
            if c:
                client_system_content += ("\n\n" if client_system_content else "") + c

    # 无论有没有内容都打印，方便区分"没部署新代码"和"这次请求确实没有system内容"
    content_hash = _short_hash_text(client_system_content)
    print(f"🔍 客户端system检测: all_messages中共{client_system_msg_count}条role=system消息，合并后{len(client_system_content)}字，hash={content_hash}")

    if client_system_content:
        base_prompt = (base_prompt + "\n\n" + client_system_content) if base_prompt else client_system_content
        print(f"📖 已合并客户端system内容(可能含世界书常驻注入): {len(client_system_content)}字")

    non_system = [m for m in all_messages if m.get('role') != 'system']
    
    current_user_msg = None
    history = non_system[:]
    if history and history[-1].get('role') == 'user':
        current_user_msg = history.pop()
    
    # 清洗孤立的tool消息（前面不是 assistant(tool_calls) 或另一条 tool 的）
    # 防止DB里的重复tool消息导致消息乱序
    cleaned = []
    orphan_count = 0
    for msg in history:
        if msg.get('role') == 'tool':
            prev = cleaned[-1] if cleaned else None
            if prev and (prev.get('role') == 'tool' or 
                        (prev.get('role') == 'assistant' and prev.get('tool_calls'))):
                cleaned.append(msg)
            else:
                orphan_count += 1
        else:
            cleaned.append(msg)
    if orphan_count > 0:
        print(f"⚠️ 清理了 {orphan_count} 条孤立tool消息")
    history = cleaned
    
    # 按逻辑轮分组（解决tool消息导致的轮计数错乱）
    rounds = group_by_rounds(history)
    total_rounds = len(rounds)
    
    state = await get_session_cache_state(session_id)
    summary_parts = state['summary_parts']
    a_start_round = state['a_start_round']
    summary_total = sum(len(p) for p in summary_parts)
    if cache_diag is not None:
        cache_diag.update({
            "session_hash": _short_hash_text(session_id),
            "client_system_count": client_system_msg_count,
            "client_system_chars": len(client_system_content),
            "client_system_hash": content_hash,
            "base_prompt_chars": len(base_prompt or ""),
            "base_prompt_hash": _short_hash_text(base_prompt or ""),
            "summary_count": len(summary_parts),
            "summary_chars": summary_total,
            "summary_hash": _short_hash_value(summary_parts),
            "total_rounds": total_rounds,
            "a_start_round": a_start_round,
            "rotation_count": 0,
        })
    
    if total_rounds < X:
        if cache_diag is not None:
            cache_diag["mode"] = "basic"
        return await _build_basic_cached(history, base_prompt, user_message, current_user_msg, cache_diag=cache_diag)
    
    # 计算A/B区（按逻辑轮切片）
    a_end_round = a_start_round + X
    a_round_groups = rounds[a_start_round : a_end_round]
    b_round_groups = rounds[a_end_round :]
    a_msgs = [msg for rnd in a_round_groups for msg in rnd]
    b_msgs = [msg for rnd in b_round_groups for msg in rnd]
    b_rounds_count = len(b_round_groups)
    
    rotation_count = 0
    rotation_failed = False
    max_rotations = CACHE_MAX_ROTATIONS if CACHE_PARTITION_TRIGGER == "time" else 999
    while _should_rotate(b_rounds_count, X, a_msgs) and rotation_count < max_rotations:
        trigger_info = f"B区{b_rounds_count}轮 >= X={X}" if CACHE_PARTITION_TRIGGER != "time" else f"A区首条消息超出{CACHE_PARTITION_WINDOW}分钟窗口"
        print(f"🔄 轮转尝试#{rotation_count + 1}: session={session_id}, {trigger_info}")
        
        new_summary = await generate_summary(a_msgs, session_id)
        if not new_summary:
            rotation_failed = True
            print("⚠️ 摘要为空，停止轮转并保留当前A/B区与游标，等待后续重试")
            break

        summary_parts.append(new_summary)
        rotation_count += 1
        
        a_start_round += X
        a_end_round = a_start_round + X
        a_round_groups = rounds[a_start_round : a_end_round]
        b_round_groups = rounds[a_end_round :]
        a_msgs = [msg for rnd in a_round_groups for msg in rnd]
        b_msgs = [msg for rnd in b_round_groups for msg in rnd]
        b_rounds_count = len(b_round_groups)
    
    if rotation_count > 0:
        # 每次轮转完成后检查一下摘要是否需要压缩（段数/字数超阈值才会真正触发）
        summary_parts = await consolidate_summary_parts(summary_parts)
        try:
            await save_session_cache_state(session_id, summary_parts, a_start_round)
        except Exception as state_error:
            await _record_summary_failure_safe(
                session_id,
                f"state_write_{type(state_error).__name__}",
            )
            raise
        if not rotation_failed:
            await _record_summary_success_safe(session_id)
        summary_total = sum(len(p) for p in summary_parts)
        print(f"🔄 轮转完成(共{rotation_count}次): 摘要{len(summary_parts)}段/{summary_total}字, A区{len(a_msgs)}条, B区{len(b_msgs)}条")
        if cache_diag is not None:
            cache_diag.update({
                "summary_count": len(summary_parts),
                "summary_chars": summary_total,
                "summary_hash": _short_hash_value(summary_parts),
                "a_start_round": a_start_round,
            })
    if cache_diag is not None:
        cache_diag["rotation_count"] = rotation_count
    
    # 拼装messages
    result = []
    if base_prompt:
        result.append({
            "role": "system",
            "content": [{"type": "text", "text": base_prompt, "cache_control": {"type": "ephemeral"}}]
        })
    
    # 摘要区（多block，尾部追加模式）
    if summary_parts:
        blocks = [{"type": "text", "text": "[以下是之前对话的摘要，帮助你回忆上下文]"}]
        for i, part in enumerate(summary_parts):
            item = {"type": "text", "text": part}
            if i == len(summary_parts) - 1:
                item["cache_control"] = {"type": "ephemeral"}
            blocks.append(item)
        result.append({"role": "user", "content": blocks})
        result.append({"role": "assistant", "content": "好的，我已了解之前的对话内容。"})
    
    # A区：剥离tool消息和tool_calls，只保留有文本的user/assistant（节省上下文）
    cleaned_a = []
    for msg in a_msgs:
        if msg.get('role') == 'tool':
            continue
        m = {k: v for k, v in msg.items() if k not in ('created_at', 'tool_calls')}
        if m.get('role') == 'assistant' and not (m.get('content') or '').strip():
            continue
        cleaned_a.append(m)
    
    # A区：从末尾往前找第一条非tool消息打BP
    for j in range(len(cleaned_a) - 1, -1, -1):
        if cleaned_a[j].get('role') != 'tool' and _apply_breakpoint(cleaned_a[j]):
            break
    
    for m in cleaned_a:
        result.append(m)
    
    # B区：先构建去掉created_at的副本，再从末尾往前打BP
    b_cleaned = [{k: v for k, v in msg.items() if k not in ('created_at',)} for msg in b_msgs]
    
    for j in range(len(b_cleaned) - 1, -1, -1):
        if b_cleaned[j].get('role') != 'tool' and _apply_breakpoint(b_cleaned[j]):
            break
    
    for m in b_cleaned:
        result.append(m)
    
    if current_user_msg:
        parts = [build_time_injection()]
        
        if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message:
            mem_text = await build_memory_text(user_message)
            if mem_text:
                parts.append(mem_text)
        
        current_text = current_user_msg['content']
        if isinstance(current_text, list):
            current_text = " ".join(
                item.get("text", "") for item in current_text
                if isinstance(item, dict) and item.get("type") == "text"
            )
        
        parts.append(current_text)
        result.append({"role": "user", "content": "\n\n".join(parts)})
    
    bp_count = 1 + (1 if summary_parts else 0) + (1 if cleaned_a else 0) + (1 if b_msgs else 0)
    summary_total = sum(len(p) for p in summary_parts)
    tool_stripped = len(a_msgs) - len(cleaned_a)
    a_info = f"A区{len(cleaned_a)}条({len(a_round_groups)}轮)" + (f"[剥离{tool_stripped}条tool]" if tool_stripped else "")
    print(f"🔒 分区缓存: BP×{bp_count} | 摘要{'有' if summary_parts else '无'}({len(summary_parts)}段/{summary_total}字) | {a_info} | B区{len(b_msgs)}条({b_rounds_count}轮) | 总{len(result)}条messages")
    if cache_diag is not None:
        cache_diag.update({
            "mode": "partitioned",
            "a_rounds": len(a_round_groups),
            "a_messages": len(cleaned_a),
            "a_hash": _short_hash_value(cleaned_a),
            "b_rounds": b_rounds_count,
            "b_messages": len(b_cleaned),
            "b_hash": _short_hash_value(b_cleaned),
            "constructed_breakpoints": _count_cache_breakpoints(result),
        })
    return result


async def _build_basic_cached(
    history: list,
    base_prompt: str,
    user_message: str,
    current_user_msg: dict,
    cache_diag: dict | None = None,
) -> list:
    """基础版prompt caching（历史不够分区时的降级模式）"""
    result = []
    if base_prompt:
        result.append({
            "role": "system",
            "content": [{"type": "text", "text": base_prompt, "cache_control": {"type": "ephemeral"}}]
        })
    
    h_cleaned = [{k: v for k, v in msg.items() if k not in ('created_at',)} for msg in history]
    
    # 从末尾往前找第一条非tool消息打BP
    for j in range(len(h_cleaned) - 1, -1, -1):
        if h_cleaned[j].get('role') != 'tool' and _apply_breakpoint(h_cleaned[j]):
            break
    
    for m in h_cleaned:
        result.append(m)
    
    if current_user_msg:
        parts = [build_time_injection()]
        
        if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message:
            mem_text = await build_memory_text(user_message)
            if mem_text:
                parts.append(mem_text)
        
        current_text = current_user_msg['content']
        if isinstance(current_text, list):
            current_text = " ".join(
                item.get("text", "") for item in current_text
                if isinstance(item, dict) and item.get("type") == "text"
            )
        
        parts.append(current_text)
        result.append({"role": "user", "content": "\n\n".join(parts)})
    
    bp_count = 1 + (1 if history else 0)
    print(f"🔒 基础缓存(降级): BP×{bp_count} | 历史{len(history)}条 | 总{len(result)}条messages")
    if cache_diag is not None:
        cache_diag.update({
            "mode": "basic",
            "a_rounds": "not_applicable",
            "a_messages": "not_applicable",
            "a_hash": "not_applicable",
            "b_rounds": "not_applicable",
            "b_messages": "not_applicable",
            "b_hash": "not_applicable",
            "constructed_breakpoints": _count_cache_breakpoints(result),
            "history_messages": len(history),
            "history_hash": _short_hash_value([{k: v for k, v in msg.items() if k != 'created_at'} for msg in history]),
        })
    return result


async def build_memory_text(user_message: str) -> str:
    """搜索记忆并格式化为注入文本（分区缓存模式用）"""
    if MAX_MEMORIES_INJECT <= 0:
        return ""
    try:
        memories = await search_memories(user_message, limit=MAX_MEMORIES_INJECT)
        if not memories:
            return ""
        
        memory_lines = []
        for mem in memories:
            date_str = ""
            if mem.get("created_at"):
                try:
                    utc_str = str(mem['created_at'])[:19]
                    utc_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    local_dt = utc_dt + timedelta(hours=TIMEZONE_HOURS)
                    date_str = f"[{local_dt.strftime('%Y-%m-%d')}] "
                except:
                    date_str = f"[{str(mem['created_at'])[:10]}] "
            memory_lines.append(f"- {date_str}{mem['content']}")
        
        print(f"📚 注入了 {len(memories)} 条相关记忆")
        return "<脑海中浮现的既往事实>\n" + "\n".join(memory_lines) + "\n</脑海中浮现的既往事实>"
    except Exception as e:
        print(f"⚠️ 记忆检索失败: {e}")
        return ""


# ============================================================
# 主动推送（Shadow Push）
# ============================================================

def _local_now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def _push_sleep_reason(now_local: datetime) -> str:
    hour = now_local.hour
    is_weekend = now_local.weekday() >= 5
    if is_weekend and 0 <= hour < 9:
        return "weekend_sleep"
    if not is_weekend and 0 <= hour < 6:
        return "weekday_sleep"
    return ""


def _status_description(now_local: datetime) -> str:
    hour = now_local.hour
    is_weekend = now_local.weekday() >= 5
    if is_weekend:
        if 0 <= hour < 9:
            return "她大概率在睡觉，周末可能晚睡晚起。"
        if 9 <= hour < 12:
            return "她可能刚起床，状态还在慢慢恢复。"
        if 12 <= hour < 18:
            return "她可能在出门、休息，或处理自己的事情。"
        return "她可能在放松、玩手机，或准备收尾一天。"
    if 0 <= hour < 6:
        return "她大概率在睡觉。"
    if 6 <= hour < 7:
        return "她可能刚起床，或正在通勤、准备进入工作状态。"
    if 7 <= hour < 8:
        return "她可能刚到公司、准备吃早餐。如果话题和时机自然，可以顺便关心她有没有吃早药，但不要每次主动推送都变成固定催药。"
    if 8 <= hour < 12:
        return "上午，她可能在工作或处理任务。"
    if 12 <= hour < 13:
        return "午间，她可能在吃饭或短暂休息。"
    if 13 <= hour < 16:
        return "下午，她可能还在工作或做正事。"
    if 16 <= hour < 17:
        return "她大概率在下班通勤的路上或者刚到家。"
    if 17 <= hour < 21:
        return "她大概率已经下班，在家休息或放松。"
    if 21 <= hour < 22:
        return "她可能在家休息。如果时机合适，可以自然地问一句有没有吃晚药，但消息不要只剩下催药。"
    if 22 <= hour < 24:
        return "她可能准备睡觉或还在熬夜。如果今天她没回复过吃了晚药，可以自然关心一下；也可以提醒她洗脸刷牙、早点休息，但不要把每次推送都写成任务清单。"
    return "夜里，她可能在放松，也可能准备睡了。"


def _shanghai_day_bounds(now_local: datetime) -> tuple[datetime, datetime]:
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def _count_pushes_today(session_id: str, now_local: datetime) -> int:
    start_utc, end_utc = _shanghai_day_bounds(now_local)
    metadata_rows = await get_push_metadata_since(session_id, start_utc, end_utc)
    count = 0
    for meta_str in metadata_rows:
        try:
            meta = json.loads(meta_str)
        except Exception:
            continue
        if meta.get("is_push") is True:
            count += 1
    return count


def _parse_metadata_datetime(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _to_utc(parsed)


def _get_bark_delivered_at(meta: dict, created_at: datetime | None) -> datetime | None:
    if meta.get("bark_delivered") is not True:
        return None
    delivered_at = _parse_metadata_datetime(meta.get("bark_delivered_at"))
    if delivered_at:
        return delivered_at
    delivered_at = _parse_metadata_datetime(meta.get("bark_last_attempt_at"))
    if delivered_at:
        return delivered_at
    return _to_utc(created_at)


async def _get_last_generated_shadow_push_at(session_id: str) -> datetime | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT created_at, metadata
            FROM conversations
            WHERE session_id = $1
              AND role = 'assistant'
              AND metadata IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 300
            """,
            session_id,
        )
    for row in rows:
        if _is_shadow_push_metadata(row["metadata"]):
            return row["created_at"]
    return None


def _latest_delivered_shadow_push_at_from_rows(rows: list) -> datetime | None:
    latest_delivered_at = None
    for row in rows:
        meta = _parse_metadata(row["metadata"])
        if meta.get("is_push") is not True or meta.get("push_source") != "shadow_cron":
            continue
        delivered_at = _get_bark_delivered_at(meta, row["created_at"])
        if delivered_at and (latest_delivered_at is None or delivered_at > latest_delivered_at):
            latest_delivered_at = delivered_at
    return latest_delivered_at


async def _get_last_delivered_shadow_push_at(session_id: str) -> datetime | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT created_at, metadata
            FROM conversations
            WHERE session_id = $1
              AND role = 'assistant'
              AND metadata IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 300
            """,
            session_id,
        )
    return _latest_delivered_shadow_push_at_from_rows(rows)


def _stable_push_target_minutes(session_id: str, last_delivered_at: datetime | None) -> int:
    if not last_delivered_at:
        return PUSH_NORMAL_MIN_MINUTES
    delivered_utc = _to_utc(last_delivered_at)
    seed = f"{session_id}:{delivered_utc.isoformat() if delivered_utc else ''}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    jitter = int(digest[:8], 16) % (PUSH_NORMAL_JITTER_MINUTES + 1)
    return PUSH_NORMAL_MIN_MINUTES + jitter


def _build_push_timing_state(
    now_local: datetime,
    last_generated_at: datetime | None,
    last_delivered_at: datetime | None,
    target_minutes: int,
) -> dict:
    last_generated_minutes = _minutes_between(now_local, last_generated_at)
    last_push_minutes = _minutes_between(now_local, last_delivered_at)

    generation_window = "ok"
    if last_generated_minutes != "not_applicable" and int(last_generated_minutes) < PUSH_HARD_MINIMUM_MINUTES:
        generation_window = "generated_recent_block"

    if last_push_minutes == "not_applicable":
        return {
            "push_window": "normal_window",
            "is_early_window": False,
            "last_push_minutes": "not_applicable",
            "last_generated_push_minutes": last_generated_minutes,
            "generation_window": generation_window,
            "normal_window_minutes": target_minutes,
            "minutes_until_normal_window": 0,
        }

    minutes_until_normal = max(0, target_minutes - int(last_push_minutes))
    if int(last_push_minutes) < PUSH_HARD_MINIMUM_MINUTES:
        push_window = "hard_minimum_block"
    elif int(last_push_minutes) < target_minutes:
        push_window = "early_window"
    else:
        push_window = "normal_window"
    return {
        "push_window": push_window,
        "is_early_window": push_window == "early_window",
        "last_push_minutes": int(last_push_minutes),
        "last_generated_push_minutes": last_generated_minutes,
        "generation_window": generation_window,
        "normal_window_minutes": target_minutes,
        "minutes_until_normal_window": minutes_until_normal,
    }


async def _get_push_timing_state(session_id: str, now_local: datetime) -> dict:
    last_generated_at = await _get_last_generated_shadow_push_at(session_id)
    last_delivered_at = await _get_last_delivered_shadow_push_at(session_id)
    target_minutes = _stable_push_target_minutes(session_id, last_delivered_at)
    return _build_push_timing_state(now_local, last_generated_at, last_delivered_at, target_minutes)


def _shadow_decision_cooldown_minutes(consecutive_skips: int) -> int:
    if consecutive_skips <= 0:
        return max(0, PUSH_DECISION_NORMAL_COOLDOWN_MINUTES)
    index = min(consecutive_skips, len(PUSH_DECISION_SKIP_COOLDOWN_MINUTES)) - 1
    return max(0, PUSH_DECISION_SKIP_COOLDOWN_MINUTES[index])


def _is_push_decision_cooldown_bypass_reason(reason: str) -> bool:
    return reason in PUSH_DECISION_COOLDOWN_BYPASS_REASONS


def _parse_month_day(value: str):
    try:
        month_text, day_text = (value or "").split("-", 1)
        month = int(month_text)
        day = int(day_text)
    except Exception:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return month, day


def _important_date_matches_today(
    *,
    today: date_cls,
    date_text: str = "",
    date_start: str = "",
    date_end: str = "",
    event_year: int | None = None,
) -> bool:
    if event_year and today.year < int(event_year):
        return False

    value = (date_text or "").strip()
    if len(value) == 10:
        try:
            return today == date_cls.fromisoformat(value)
        except Exception:
            return False
    if len(value) == 5:
        month_day = _parse_month_day(value)
        return bool(month_day and (today.month, today.day) == month_day)

    start = _parse_month_day((date_start or "").strip())
    end = _parse_month_day((date_end or "").strip())
    if start and end:
        current = (today.month, today.day)
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end
    return False


async def _get_active_hard_event_bypass_reason(now_local: datetime) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT event_type, date_text, date_start, date_end, event_year
            FROM important_dates
            WHERE cooldown_bypass = TRUE
            """
        )
    today = now_local.date()
    for row in rows:
        if _important_date_matches_today(
            today=today,
            date_text=row["date_text"],
            date_start=row["date_start"],
            date_end=row["date_end"],
            event_year=row["event_year"],
        ):
            return "hard_event"
    return ""


async def _get_shadow_decision_cooldown_state(session_id: str, now_local: datetime) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT action, reason, checked_at
            FROM shadow_push_decisions
            WHERE session_id = $1
              AND action IN ('send', 'skip', 'error')
            ORDER BY checked_at DESC, id DESC
            LIMIT 20
            """,
            session_id,
        )
        last_user_at = await conn.fetchval(
            """
            SELECT created_at
            FROM conversations
            WHERE session_id = $1
              AND role = 'user'
              AND COALESCE(content, '') <> ''
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            session_id,
        )

    if not rows:
        return {
            "decision_cooldown_active": False,
            "decision_cooldown_reason": "no_prior_decision",
            "decision_cooldown_minutes": 0,
            "decision_cooldown_remaining_minutes": 0,
            "decision_consecutive_skips": 0,
            "decision_cooldown_bypass": "none",
        }

    latest = rows[0]
    latest_at = latest["checked_at"]
    latest_action = latest["action"] or ""
    latest_at_utc = _to_utc(latest_at)
    last_user_utc = _to_utc(last_user_at)
    bypass_reason = "user_message_received"
    if latest_at_utc and last_user_utc and last_user_utc > latest_at_utc and _is_push_decision_cooldown_bypass_reason(bypass_reason):
        return {
            "decision_cooldown_active": False,
            "decision_cooldown_reason": bypass_reason,
            "decision_cooldown_minutes": 0,
            "decision_cooldown_remaining_minutes": 0,
            "decision_consecutive_skips": 0,
            "decision_cooldown_bypass": "user_message_received",
        }

    hard_event_reason = await _get_active_hard_event_bypass_reason(now_local)
    if hard_event_reason and _is_push_decision_cooldown_bypass_reason(hard_event_reason):
        return {
            "decision_cooldown_active": False,
            "decision_cooldown_reason": hard_event_reason,
            "decision_cooldown_minutes": 0,
            "decision_cooldown_remaining_minutes": 0,
            "decision_consecutive_skips": 0,
            "decision_cooldown_bypass": hard_event_reason,
        }

    consecutive_skips = 0
    for row in rows:
        if row["action"] == "skip":
            consecutive_skips += 1
            continue
        break

    cooldown_minutes = _shadow_decision_cooldown_minutes(consecutive_skips)
    elapsed_minutes = _minutes_between(now_local, latest_at)
    elapsed_int = elapsed_minutes if isinstance(elapsed_minutes, int) else 0
    remaining = max(0, cooldown_minutes - elapsed_int)
    active = cooldown_minutes > 0 and remaining > 0
    return {
        "decision_cooldown_active": active,
        "decision_cooldown_reason": "decision_cooldown" if active else "decision_cooldown_elapsed",
        "decision_cooldown_minutes": cooldown_minutes,
        "decision_cooldown_elapsed_minutes": elapsed_int,
        "decision_cooldown_remaining_minutes": remaining,
        "decision_consecutive_skips": consecutive_skips,
        "decision_last_action": latest_action,
        "decision_cooldown_bypass": "none",
    }


async def should_generate_push(
    session_id: str,
    *,
    enforce_cooldown: bool = True,
    enforce_daily_limit: bool = True,
) -> dict:
    now_local = _local_now()

    sleep_reason = _push_sleep_reason(now_local)
    if sleep_reason:
        return {"should_push": False, "reason": sleep_reason}

    last_time = await get_last_conversation_message_time(session_id)
    if not last_time:
        return {"should_push": False, "reason": "no_history"}

    if enforce_cooldown:
        timing_state = await _get_push_timing_state(session_id, now_local)
        if timing_state.get("generation_window") == "generated_recent_block":
            return {
                "should_push": False,
                "reason": "generated_recent_block",
                **timing_state,
            }
        if timing_state["push_window"] == "hard_minimum_block":
            return {
                "should_push": False,
                "reason": "hard_minimum_block",
                **timing_state,
            }
    else:
        timing_state = {
            "push_window": "normal_window",
            "is_early_window": False,
            "last_push_minutes": "not_applicable",
            "last_generated_push_minutes": "not_applicable",
            "generation_window": "ok",
            "normal_window_minutes": PUSH_NORMAL_MIN_MINUTES,
            "minutes_until_normal_window": 0,
        }

    if enforce_daily_limit and PUSH_MAX_PER_DAY > 0:
        push_count = await _count_pushes_today(session_id, now_local)
        if push_count >= PUSH_MAX_PER_DAY:
            return {
                "should_push": False,
                "reason": "daily_limit",
                "push_count": push_count,
                "max_per_day": PUSH_MAX_PER_DAY,
                **timing_state,
            }

    if enforce_cooldown:
        decision_cooldown = await _get_shadow_decision_cooldown_state(session_id, now_local)
        if decision_cooldown.get("decision_cooldown_active"):
            return {
                "should_push": False,
                "reason": "decision_cooldown",
                **timing_state,
                **decision_cooldown,
            }
        timing_state.update(decision_cooldown)

    return {"should_push": True, "reason": timing_state["push_window"], **timing_state}


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            item.get("text", "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(content or "")


def _clean_history_for_push(rows: list) -> list:
    cleaned = []
    for row in rows:
        msg = db_row_to_message(row)
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        if msg.get("tool_calls"):
            continue
        text = _message_text(msg.get("content")).strip()
        if not text:
            continue
        cleaned.append({"role": role, "content": text})
    return cleaned


def _to_utc(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_push_time(dt: datetime | None) -> str:
    utc_dt = _to_utc(dt)
    if not utc_dt:
        return "none"
    local_dt = utc_dt.astimezone(ZoneInfo("Asia/Shanghai"))
    return local_dt.strftime("%Y-%m-%d %H:%M Asia/Shanghai")


def _minutes_between(now_local: datetime, dt: datetime | None):
    utc_dt = _to_utc(dt)
    if not utc_dt:
        return "not_applicable"
    elapsed = now_local - utc_dt.astimezone(now_local.tzinfo)
    return max(0, int(elapsed.total_seconds() // 60))


def _is_shadow_push_metadata(meta_str: str | None) -> bool:
    if not meta_str:
        return False
    try:
        meta = json.loads(meta_str)
    except Exception:
        return False
    return meta.get("is_push") is True and meta.get("push_source") == "shadow_cron"


async def _get_push_interaction_state(session_id: str, now_local: datetime) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        last_user = await conn.fetchrow("""
            SELECT id, role, created_at
            FROM conversations
            WHERE session_id = $1
              AND role = 'user'
              AND COALESCE(content, '') <> ''
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """, session_id)
        last_effective = await conn.fetchrow("""
            SELECT id, role, created_at
            FROM conversations
            WHERE session_id = $1
              AND role IN ('user', 'assistant')
              AND COALESCE(content, '') <> ''
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """, session_id)
        metadata_rows = await conn.fetch("""
            SELECT id, created_at, metadata
            FROM conversations
            WHERE session_id = $1
              AND role = 'assistant'
              AND metadata IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 300
        """, session_id)

        last_generated_push = None
        last_delivered_push_at = None
        for row in metadata_rows:
            meta = _parse_metadata(row["metadata"])
            if meta.get("is_push") is not True or meta.get("push_source") != "shadow_cron":
                continue
            if last_generated_push is None:
                last_generated_push = row
            delivered_at = _get_bark_delivered_at(meta, row["created_at"])
            if delivered_at and last_delivered_push_at is None:
                last_delivered_push_at = delivered_at

        user_replied_after_last_push = "not_applicable"
        if last_delivered_push_at:
            user_replied_after_last_push = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1
                    FROM conversations
                    WHERE session_id = $1
                      AND role = 'user'
                      AND created_at > $2
                      AND COALESCE(content, '') <> ''
                )
            """, session_id, last_delivered_push_at)

    last_user_at = last_user["created_at"] if last_user else None
    last_generated_push_at = last_generated_push["created_at"] if last_generated_push else None
    consecutive_unanswered_pushes = 0
    if last_delivered_push_at and user_replied_after_last_push is False:
        last_user_utc = _to_utc(last_user_at)
        for row in metadata_rows:
            meta = _parse_metadata(row["metadata"])
            if meta.get("is_push") is not True or meta.get("push_source") != "shadow_cron":
                continue
            delivered_at = _get_bark_delivered_at(meta, row["created_at"])
            if not delivered_at:
                continue
            if last_user_utc and delivered_at <= last_user_utc:
                break
            consecutive_unanswered_pushes += 1

    return {
        "last_effective_role": last_effective["role"] if last_effective else "none",
        "last_user_message_at": _format_push_time(last_user_at),
        "silence_minutes": _minutes_between(now_local, last_user_at),
        "last_generated_push_at": _format_push_time(last_generated_push_at),
        "last_push_at": _format_push_time(last_delivered_push_at),
        "last_push_minutes": _minutes_between(now_local, last_delivered_push_at),
        "user_replied_after_last_push": user_replied_after_last_push,
        "consecutive_unanswered_pushes": consecutive_unanswered_pushes,
    }


def _bool_text(value) -> str:
    if value == "not_applicable":
        return "not_applicable"
    return str(bool(value)).lower()


def _log_push_context_diag(
    state: dict,
    recent_excerpt_count: int,
    pushed: bool,
    reason: str,
    action: str = "send",
    parse_success: bool = True,
):
    print(
        "📮 主动推送上下文诊断: "
        f"action={action} | "
        f"reason={reason} | "
        f"push_window={state.get('push_window', 'not_applicable')} | "
        f"is_early_window={str(bool(state.get('is_early_window', False))).lower()} | "
        f"minutes_until_normal_window={state.get('minutes_until_normal_window', 'not_applicable')} | "
        f"silence_minutes={state.get('silence_minutes', 'not_applicable')} | "
        f"last_effective_role={state.get('last_effective_role', 'none')} | "
        f"user_replied_after_last_push={_bool_text(state.get('user_replied_after_last_push', 'not_applicable'))} | "
        f"consecutive_unanswered_pushes={state.get('consecutive_unanswered_pushes', 0)} | "
        f"recent_excerpt_count={recent_excerpt_count} | "
        f"pushed={str(pushed).lower()} | "
        f"parse_success={str(parse_success).lower()}",
        flush=True,
    )


def _push_decision_int(value):
    if value in (None, "", "not_applicable", "not_reported"):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _push_decision_bool(value):
    if value in (None, "", "not_applicable", "not_reported"):
        return None
    return bool(value)


def _clean_push_decision_code(value, fallback: str = "unspecified", max_len: int = 64) -> str:
    text = value if isinstance(value, str) else ""
    text = re.sub(r"[^a-zA-Z0-9_:-]", "", text).strip()[:max_len]
    return text or fallback


def _build_shadow_push_decision_payload(
    session_id: str,
    action: str,
    reason: str,
    state: dict | None = None,
    recent_excerpt_count: int = 0,
    pushed: bool = False,
    model: str = "",
    parse_success: bool = None,
    bark_delivered: bool = None,
    error_type: str = "",
    intent: str = "",
) -> dict:
    state = state or {}
    return {
        "session_id": session_id or "",
        "action": _clean_push_decision_code(action, fallback="error", max_len=32),
        "intent": _clean_push_decision_code(intent, fallback="", max_len=64),
        "reason": _clean_push_decision_code(reason, fallback="unspecified", max_len=128),
        "model": model or "",
        "parse_success": parse_success,
        "pushed": bool(pushed),
        "bark_delivered": bark_delivered,
        "push_window": state.get("push_window") or "",
        "generation_window": state.get("generation_window") or "",
        "is_early_window": _push_decision_bool(state.get("is_early_window")),
        "silence_minutes": _push_decision_int(state.get("silence_minutes")),
        "last_push_minutes": _push_decision_int(state.get("last_push_minutes")),
        "last_generated_push_minutes": _push_decision_int(state.get("last_generated_push_minutes")),
        "minutes_until_normal_window": _push_decision_int(state.get("minutes_until_normal_window")),
        "normal_window_minutes": _push_decision_int(state.get("normal_window_minutes")),
        "last_effective_role": state.get("last_effective_role") or "",
        "user_replied_after_last_push": _push_decision_bool(state.get("user_replied_after_last_push")),
        "consecutive_unanswered_pushes": _push_decision_int(state.get("consecutive_unanswered_pushes")) or 0,
        "recent_excerpt_count": _push_decision_int(recent_excerpt_count) or 0,
        "error_type": _clean_push_decision_code(error_type, fallback="", max_len=128),
    }


async def _save_shadow_push_decision_log(
    session_id: str,
    action: str,
    reason: str,
    state: dict | None = None,
    recent_excerpt_count: int = 0,
    pushed: bool = False,
    model: str = "",
    parse_success: bool = None,
    bark_delivered: bool = None,
    error_type: str = "",
    intent: str = "",
):
    try:
        payload = _build_shadow_push_decision_payload(
            session_id=session_id,
            action=action,
            reason=reason,
            state=state,
            recent_excerpt_count=recent_excerpt_count,
            pushed=pushed,
            model=model,
            parse_success=parse_success,
            bark_delivered=bark_delivered,
            error_type=error_type,
            intent=intent,
        )
        await save_shadow_push_decision(**payload)
    except Exception as e:
        print(f"⚠️ 主动推送决策日志写入失败: {type(e).__name__}", flush=True)



SHADOW_MIND_DRIVES = ("longing", "curiosity", "share", "warmth", "concern")


def _shadow_mind_clamp(value: int | float) -> int:
    try:
        number = int(round(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, number))


def _shadow_mind_minutes(value) -> int:
    if value in (None, "", "not_applicable", "not_reported"):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _shadow_mind_add_reason(reasons: list, drive: str, reason_code: str, weight: int) -> None:
    if drive not in SHADOW_MIND_DRIVES:
        return
    reasons.append({
        "drive": drive,
        "reason_code": _clean_push_decision_code(reason_code, fallback="unspecified", max_len=96),
        "weight": _shadow_mind_clamp(weight),
    })


def _shadow_mind_silence_band(silence_minutes: int) -> str:
    if silence_minutes >= 24 * 60:
        return "silence_24h"
    if silence_minutes >= 12 * 60:
        return "silence_12h"
    if silence_minutes >= 6 * 60:
        return "silence_6h"
    if silence_minutes >= 3 * 60:
        return "silence_3h"
    if silence_minutes >= 60:
        return "silence_1h"
    if silence_minutes >= 30:
        return "silence_30m"
    return "recent_contact"


def compute_shadow_mind_state(interaction_state: dict, timing_state: dict | None = None) -> dict:
    """Shadow Mind Phase A：确定性内在状态计算，不参与主动推送 send/skip 判断。"""
    interaction_state = interaction_state or {}
    merged = dict(interaction_state)
    if timing_state:
        merged.update(timing_state)

    silence = _shadow_mind_minutes(merged.get("silence_minutes"))
    last_push_minutes = _shadow_mind_minutes(merged.get("last_push_minutes"))
    unanswered = _shadow_mind_minutes(merged.get("consecutive_unanswered_pushes"))
    last_role = merged.get("last_effective_role") or "none"
    replied_after_push = merged.get("user_replied_after_last_push")
    push_window = merged.get("push_window") or "not_applicable"
    is_early = bool(merged.get("is_early_window"))

    reasons = []
    silence_band = _shadow_mind_silence_band(silence)

    longing = 18
    if silence >= 30:
        longing += min(42, silence // 45 * 4)
        _shadow_mind_add_reason(reasons, "longing", silence_band, min(60, silence // 30))
    if last_role == "user":
        longing += 8
        _shadow_mind_add_reason(reasons, "longing", "last_effective_role_user", 12)
    if unanswered:
        longing -= min(18, unanswered * 6)
        _shadow_mind_add_reason(reasons, "longing", "unanswered_pushes_reduce_pressure", unanswered * 8)

    curiosity = 16
    if last_role == "user":
        curiosity += 24
        _shadow_mind_add_reason(reasons, "curiosity", "user_left_recent_thread", 28)
    if silence >= 180:
        curiosity += 12
        _shadow_mind_add_reason(reasons, "curiosity", silence_band, 16)
    if unanswered >= 2:
        curiosity -= 10
        _shadow_mind_add_reason(reasons, "curiosity", "avoid_repeated_questions", 18)

    share = 20
    if push_window == "normal_window":
        share += 16
        _shadow_mind_add_reason(reasons, "share", "normal_window_available", 18)
    elif is_early:
        share -= 8
        _shadow_mind_add_reason(reasons, "share", "early_window_high_bar", 14)
    if silence >= 240:
        share += 10
        _shadow_mind_add_reason(reasons, "share", silence_band, 10)
    if unanswered:
        share -= min(12, unanswered * 4)

    warmth = 52
    if replied_after_push is True:
        warmth += 12
        _shadow_mind_add_reason(reasons, "warmth", "user_replied_after_last_push", 20)
    if last_role == "user":
        warmth += 6
        _shadow_mind_add_reason(reasons, "warmth", "conversation_recently_received", 10)
    if silence >= 360:
        warmth += 6
        _shadow_mind_add_reason(reasons, "warmth", "soft_reconnect_after_time", 10)

    concern = 4
    if silence >= 24 * 60:
        concern += 30
    elif silence >= 12 * 60:
        concern += 22
    elif silence >= 6 * 60:
        concern += 14
    elif silence >= 3 * 60:
        concern += 8
    if silence >= 180:
        _shadow_mind_add_reason(reasons, "concern", "concern_time_only", min(42, silence // 60 * 5))
    if unanswered >= 3:
        concern += 6
        _shadow_mind_add_reason(reasons, "concern", "unanswered_pushes_do_not_escalate", 18)
    concern = min(concern, 45)

    state = {
        "longing": _shadow_mind_clamp(longing),
        "curiosity": _shadow_mind_clamp(curiosity),
        "share": _shadow_mind_clamp(share),
        "warmth": _shadow_mind_clamp(warmth),
        "concern": _shadow_mind_clamp(concern),
    }
    inputs = {
        "silence_minutes": silence,
        "last_push_minutes": last_push_minutes if merged.get("last_push_minutes") != "not_applicable" else "not_applicable",
        "last_effective_role": last_role,
        "user_replied_after_last_push": replied_after_push,
        "consecutive_unanswered_pushes": unanswered,
        "push_window": push_window,
        "is_early_window": is_early,
    }
    return {
        "state": state,
        "reasons": reasons,
        "inputs": inputs,
        "thought_pool": _build_shadow_mind_thought_pool(state, reasons),
    }


def _build_shadow_mind_thought_pool(state: dict, reasons: list) -> list:
    items = []
    thresholds = {
        "longing": (62, "approach"),
        "curiosity": (58, "ask_or_notice"),
        "share": (58, "small_share"),
        "warmth": (68, "affection"),
        "concern": (32, "gentle_check_in"),
    }
    reason_by_drive = {}
    for item in reasons or []:
        if isinstance(item, dict) and item.get("drive") not in reason_by_drive:
            reason_by_drive[item.get("drive")] = item.get("reason_code", "drive_threshold")
    for drive, (threshold, thought_type) in thresholds.items():
        intensity = _shadow_mind_clamp((state or {}).get(drive, 0))
        if intensity < threshold:
            continue
        items.append({
            "thought_type": thought_type,
            "drive": drive,
            "intensity": intensity,
            "reason_code": reason_by_drive.get(drive, "drive_threshold"),
        })
    return items


def _shadow_mind_json_value(value, fallback):
    if value in (None, ""):
        return fallback
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return fallback
        return parsed if isinstance(parsed, type(fallback)) else fallback
    return fallback


def _shadow_mind_public_state(row: dict | None) -> dict | None:
    if not row:
        return None
    computed_at = row.get("computed_at")
    updated_at = row.get("updated_at")
    return {
        "session_id": row.get("session_id", ""),
        "state": {
            field: row.get(field, 0)
            for field in SHADOW_MIND_DRIVES + ("valence", "arousal", "connection", "tension", "hurt", "fatigue")
        },
        "reasons": _shadow_mind_json_value(row.get("reasons"), []),
        "inputs": _shadow_mind_json_value(row.get("inputs"), {}),
        "computed_at": computed_at.isoformat() if hasattr(computed_at, "isoformat") else computed_at,
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
    }


def _shadow_mind_public_events(events: list) -> list:
    public = []
    for event in events or []:
        created_at = event.get("created_at")
        public.append({
            "drive": event.get("drive", ""),
            "previous_value": event.get("previous_value"),
            "new_value": event.get("new_value"),
            "delta": event.get("delta"),
            "reason_code": event.get("reason_code", ""),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        })
    return public


def _shadow_mind_a2_public_events(events: list) -> list:
    public = []
    for event in events or []:
        computed_at = event.get("computed_at")
        created_at = event.get("created_at")
        public.append({
            "id": event.get("id"),
            "event_type": event.get("event_type", ""),
            "source_message_ids": list(event.get("source_message_ids") or []),
            "deltas": _shadow_mind_json_value(event.get("deltas"), {}),
            "reason_code": event.get("reason_code", ""),
            "confidence": float(event.get("confidence") or 0),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            "computed_at": computed_at.isoformat() if hasattr(computed_at, "isoformat") else computed_at,
        })
    return public


def _shadow_mind_public_history(rows: list) -> list:
    result = []
    fields = SHADOW_MIND_DRIVES + ("valence", "arousal", "connection", "tension", "hurt", "fatigue")
    for row in rows or []:
        computed_at = row.get("computed_at")
        result.append({
            "state": {field: row.get(field, 0) for field in fields},
            "computed_at": computed_at.isoformat() if hasattr(computed_at, "isoformat") else computed_at,
        })
    return result


async def _log_push_decision_diag(session_id: str, reason: str, timing_state: dict | None = None):
    try:
        now_local = _local_now()
        state = await _get_push_interaction_state(session_id, now_local)
        if timing_state:
            state.update(timing_state)
        recent_rows = await get_recent_conversation_messages(session_id, limit=16)
        recent_excerpt_count = min(len(_clean_history_for_push(recent_rows)), 12)
        _log_push_context_diag(state, recent_excerpt_count, False, reason, action="skip")
        await _save_shadow_push_decision_log(
            session_id=session_id,
            action="blocked",
            reason=reason,
            state=state,
            recent_excerpt_count=recent_excerpt_count,
            pushed=False,
            model=DEFAULT_MODEL,
        )
    except Exception as e:
        print(f"⚠️ 主动推送上下文诊断失败: {type(e).__name__}", flush=True)


async def _build_shadow_user_content(recent_messages: list, interaction_state: dict) -> str:
    now_local = _local_now()
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    now_text = now_local.strftime("%Y年%m月%d日 %H:%M") + " " + weekday_names[now_local.weekday()]

    recent_text = "\n".join(
        f"{'Sasa' if m['role'] == 'user' else 'AI'}: {m['content'][:180]}"
        for m in recent_messages[-12:]
    )
    memory_query = recent_text[-1200:] if recent_text else "Sasa 最近的状态"
    memory_block = ""
    if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED:
        memory_block = await build_memory_text(memory_query)

    parts = [
        "<system_trigger>",
        "[当前时间]",
        now_text,
        "",
        "[状态推测]",
        _status_description(now_local),
        "",
        "[互动间隔]",
        f"last_effective_role={interaction_state.get('last_effective_role', 'none')}",
        f"last_user_message_at={interaction_state.get('last_user_message_at', 'none')}",
        f"silence_minutes={interaction_state.get('silence_minutes', 'not_applicable')}",
        f"last_generated_push_at={interaction_state.get('last_generated_push_at', 'none')}",
        f"last_push_at={interaction_state.get('last_push_at', 'none')}",
        f"last_push_minutes={interaction_state.get('last_push_minutes', 'not_applicable')}",
        f"user_replied_after_last_push={_bool_text(interaction_state.get('user_replied_after_last_push', 'not_applicable'))}",
        f"consecutive_unanswered_pushes={interaction_state.get('consecutive_unanswered_pushes', 0)}",
        "last_push_at/last_push_minutes表示最近一次Bark实际送达，不是单纯生成入库时间。",
        "这些是事实，只用来判断语气和时机，不要把具体分钟/小时机械地说给Sasa听。",
        "",
        "[推送窗口]",
        f"push_window={interaction_state.get('push_window', 'not_applicable')}",
        f"is_early_window={str(bool(interaction_state.get('is_early_window', False))).lower()}",
        f"normal_window_minutes={interaction_state.get('normal_window_minutes', 'not_applicable')}",
        f"minutes_until_normal_window={interaction_state.get('minutes_until_normal_window', 'not_applicable')}",
        "push_window=early_window时发送门槛明显更高：只有新的具体话题、真实关心、明显情境变化或不同于上一条的信息才send。",
        "early_window里，普通想念、无具体内容、催回复、重复表达应skip。",
        "push_window=normal_window只表示可以自然判断，不代表必须send；没有具体自然理由仍然skip。",
        "",
        "[可用素材]",
        "最近对话是第一优先级；相关记忆只作为补充，不要硬串剧情。",
    ]
    if memory_block:
        parts.extend(["", memory_block])
    parts.extend([
        "",
        "[行动指令]",
        "现在不是必须发送消息，而是先判断此刻要不要主动开口。",
        "硬性静默、冷却和每日上限已经由程序判断通过；但你仍然要根据自然性决定send或skip。",
        "不得只因为冷却时间已过就send；如果没有具体自然理由，优先skip。",
        "可以因为想念、惦记、分享欲、最近具体话题或低压关心选择send。",
        "连续未回复主动推送越多，越倾向skip；不要反复追问“为什么不回”。",
        "如果consecutive_unanswered_pushes>0，early_window原则上skip；normal_window也要明显提高skip概率。",
        "如果user_replied_after_last_push=true，不要把上一条主动推送视为持续未回应的打扰，但仍要参考最近真实对话结束时间，避免刚聊完又发。",
        "优先承接最近对话里的具体细节，避免通用客服式问候。",
        "如果last_effective_role=assistant，说明对话停在你这里，别误以为Sasa刚说完还等你回复。",
        "如果user_replied_after_last_push=false或consecutive_unanswered_pushes>0，要更轻一点，不要连续追问她为什么不回。",
        "如果user_replied_after_last_push=true，说明上次主动推送已经被接住，不要误判成从那次起一直没人理。",
        "",
        "[输出格式]",
        "只返回严格JSON对象，不要markdown，不要代码块，不要解释。",
        'send示例：{"action":"send","reason":"specific_recent_topic","message":"一句自然的主动消息"}',
        'skip示例：{"action":"skip","reason":"no_natural_reason","message":""}',
        "action只能是send或skip。",
        "reason建议使用：natural_longing、specific_recent_topic、gentle_concern、small_share、no_natural_reason、too_soon、avoid_pressure、repeated_unanswered_pushes。",
        "message只有send时填写；写1到2句，不超过80个中文字符，不分段，不用markdown和emoji。skip时message必须是空字符串。",
        "</system_trigger>",
    ])
    return "\n".join(parts)


def parse_shadow_decision(raw_text: str) -> dict:
    try:
        data = json.loads((raw_text or "").strip())
    except Exception:
        return {
            "parse_success": False,
            "action": "skip",
            "reason": "parse_failed",
            "message": "",
        }
    if not isinstance(data, dict):
        return {
            "parse_success": False,
            "action": "skip",
            "reason": "invalid_json",
            "message": "",
        }
    action = data.get("action")
    reason = data.get("reason")
    intent = _clean_push_decision_code(data.get("intent", ""), fallback="")
    message = data.get("message", "")
    if action not in {"send", "skip"}:
        return {
            "parse_success": False,
            "action": "skip",
            "reason": "invalid_fields",
            "intent": "",
            "message": "",
        }
    if action == "skip":
        reason = _clean_push_decision_code(reason, fallback="model_skip_no_reason")
    elif not isinstance(reason, str):
        return {
            "parse_success": False,
            "action": "skip",
            "reason": "invalid_fields",
            "intent": "",
            "message": "",
        }
    if not isinstance(message, str):
        message = ""
    if action == "send":
        reason = _clean_push_decision_code(reason, fallback="unspecified")
    if action == "skip":
        return {
            "parse_success": True,
            "action": "skip",
            "reason": reason,
            "intent": intent,
            "message": "",
        }
    return {
        "parse_success": True,
        "action": "send",
        "reason": reason,
        "intent": intent,
        "message": message,
    }


_PUSH_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "]"
)


def clean_push_reply(text: str, hard_limit: int = 120) -> str:
    cleaned = text or ""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<thinking>[\s\S]*?</thinking>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```[\s\S]*?```", "", cleaned)
    cleaned = _PUSH_EMOJI_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    chars = list(cleaned)
    if len(chars) <= hard_limit:
        return cleaned
    head = chars[:hard_limit]
    ends = set("。！？…～!?.")
    cut = -1
    for i in range(len(head) - 1, -1, -1):
        if head[i] in ends:
            cut = i
            break
    return "".join(head[:cut + 1] if cut >= 0 else head).strip()


def _parse_bark_badge(value: str):
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        badge = int(raw)
    except ValueError:
        print("⚠️ BARK_BADGE值无效，已忽略")
        return None
    if badge < 0:
        print("⚠️ BARK_BADGE值无效，已忽略")
        return None
    return badge


async def deliver_bark_push(message: str) -> dict:
    if not BARK_DEVICE_KEY:
        return {
            "attempted": False,
            "delivered": False,
            "error_type": "not_configured",
            "http_status": "not_reported",
        }
    payload = {
        "device_key": BARK_DEVICE_KEY,
        "title": BARK_TITLE,
        "body": message,
    }
    if BARK_ICON_URL:
        payload["icon"] = BARK_ICON_URL
    if BARK_SOUND:
        payload["sound"] = BARK_SOUND
    if BARK_OPEN_URL:
        payload["url"] = BARK_OPEN_URL
    if BARK_GROUP:
        payload["group"] = BARK_GROUP
    if BARK_LEVEL:
        if BARK_LEVEL in {"active", "timeSensitive", "passive", "critical"}:
            payload["level"] = BARK_LEVEL
        else:
            print("⚠️ BARK_LEVEL值无效，已忽略")
    if BARK_IMAGE_URL:
        payload["image"] = BARK_IMAGE_URL
    badge = _parse_bark_badge(BARK_BADGE)
    if badge is not None:
        payload["badge"] = badge

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                BARK_API_URL,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
        if 200 <= response.status_code < 300:
            return {
                "attempted": True,
                "delivered": True,
                "error_type": "none",
                "http_status": response.status_code,
            }
        print(f"⚠️ Bark投递失败: HTTP {response.status_code}")
        return {
            "attempted": True,
            "delivered": False,
            "error_type": f"HTTP_{response.status_code}",
            "http_status": response.status_code,
        }
    except Exception as e:
        print(f"⚠️ Bark投递异常: {type(e).__name__}")
        return {
            "attempted": True,
            "delivered": False,
            "error_type": type(e).__name__,
            "http_status": "not_reported",
        }


async def deliver_gateway_system_alert(title: str, message: str) -> dict:
    """Send a separate operational alert without exposing content or secrets."""
    if not BARK_DEVICE_KEY:
        return {
            "attempted": False,
            "delivered": False,
            "error_type": "not_configured",
            "http_status": "not_reported",
        }
    payload = {
        "device_key": BARK_DEVICE_KEY,
        "title": str(title or "网关系统提醒")[:40],
        "body": message,
        "group": f"{BARK_GROUP or 'Rora'} · 系统",
    }
    if BARK_ICON_URL:
        payload["icon"] = BARK_ICON_URL
    if BARK_SOUND:
        payload["sound"] = BARK_SOUND
    if BARK_OPEN_URL:
        payload["url"] = BARK_OPEN_URL
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                BARK_API_URL,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
        if 200 <= response.status_code < 300:
            print("summary_health_alert delivered=true", flush=True)
            return {
                "attempted": True,
                "delivered": True,
                "error_type": "none",
                "http_status": response.status_code,
            }
        print(
            f"summary_health_alert delivered=false http_status={response.status_code}",
            flush=True,
        )
        return {
            "attempted": True,
            "delivered": False,
            "error_type": f"HTTP_{response.status_code}",
            "http_status": response.status_code,
        }
    except Exception as alert_error:
        print(
            "summary_health_alert delivered=false "
            f"error_type={type(alert_error).__name__}",
            flush=True,
        )
        return {
            "attempted": True,
            "delivered": False,
            "error_type": type(alert_error).__name__,
            "http_status": "not_reported",
        }


async def deliver_summary_health_alert(message: str) -> dict:
    return await deliver_gateway_system_alert("网关摘要提醒", message)


def _apply_bark_delivery_result(metadata: dict, result: dict) -> dict:
    next_meta = dict(metadata)
    now_attempt_at = datetime.now(timezone.utc).isoformat()
    if not result.get("attempted"):
        next_meta["bark_attempted"] = False
        next_meta["bark_delivered"] = False
        next_meta["bark_error_type"] = result.get("error_type", "not_configured")
        return next_meta
    attempts = int(next_meta.get("bark_attempts") or 0) + 1
    next_meta["bark_attempted"] = True
    next_meta["bark_delivered"] = bool(result.get("delivered"))
    next_meta["bark_attempts"] = attempts
    next_meta["bark_error_type"] = result.get("error_type", "unknown")
    next_meta["bark_http_status"] = result.get("http_status", "not_reported")
    next_meta["bark_last_attempt_at"] = now_attempt_at
    if result.get("delivered") and not next_meta.get("bark_delivered_at"):
        next_meta["bark_delivered_at"] = now_attempt_at
    if attempts >= BARK_MAX_DELIVERY_ATTEMPTS and not result.get("delivered"):
        next_meta["bark_retry_exhausted"] = True
    return next_meta


async def _save_push_message(session_id: str, content: str, model: str, metadata: dict) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO conversations (session_id, role, content, model, metadata)
            VALUES ($1, 'assistant', $2, $3, $4)
            RETURNING id
            """,
            session_id,
            content,
            model,
            json.dumps(metadata, ensure_ascii=False),
        )


async def _update_message_metadata(message_id: int, metadata: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE conversations SET metadata = $1 WHERE id = $2",
            json.dumps(metadata, ensure_ascii=False),
            message_id,
        )


def _parse_metadata(meta_str: str | None) -> dict:
    if isinstance(meta_str, dict):
        return meta_str
    if not meta_str:
        return {}
    try:
        meta = json.loads(meta_str)
    except Exception:
        return {}
    return meta if isinstance(meta, dict) else {}


def _is_retryable_undelivered_push(meta: dict) -> bool:
    if meta.get("is_push") is not True or meta.get("push_source") != "shadow_cron":
        return False
    if meta.get("delivery") != "bark":
        return False
    if meta.get("bark_retry_stopped") is True or meta.get("bark_retry_exhausted") is True:
        return False
    if meta.get("bark_attempted") is not True or meta.get("bark_delivered") is not False:
        return False
    return int(meta.get("bark_attempts") or 0) < BARK_MAX_DELIVERY_ATTEMPTS


async def _has_user_reply_after(session_id: str, after_time: datetime) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM conversations
                WHERE session_id = $1
                  AND role = 'user'
                  AND created_at > $2
                  AND COALESCE(content, '') <> ''
            )
            """,
            session_id,
            after_time,
        )


async def retry_undelivered_bark_push(session_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, created_at, metadata
            FROM conversations
            WHERE session_id = $1
              AND role = 'assistant'
              AND metadata IS NOT NULL
            ORDER BY created_at ASC, id ASC
            LIMIT 300
            """,
            session_id,
        )

    for row in rows:
        meta = _parse_metadata(row["metadata"])
        if not _is_retryable_undelivered_push(meta):
            continue
        if await _has_user_reply_after(session_id, row["created_at"]):
            next_meta = dict(meta)
            next_meta["bark_retry_stopped"] = True
            next_meta["bark_retry_stop_reason"] = "user_replied_after_generated_push"
            next_meta["bark_retry_stopped_at"] = datetime.now(timezone.utc).isoformat()
            await _update_message_metadata(row["id"], next_meta)
            print(
                "📮 Bark补发停止: "
                f"message_id={row['id']} | "
                "reason=user_replied_after_generated_push",
                flush=True,
            )
            return {
                "attempted": False,
                "delivered": False,
                "message_id": row["id"],
                "stopped": True,
                "stop_reason": "user_replied_after_generated_push",
            }
        result = await deliver_bark_push(row["content"] or "")
        next_meta = _apply_bark_delivery_result(meta, result)
        await _update_message_metadata(row["id"], next_meta)
        delivered = bool(result.get("delivered"))
        print(
            "📮 Bark补发诊断: "
            f"message_id={row['id']} | "
            f"attempts={next_meta.get('bark_attempts', meta.get('bark_attempts', 0))} | "
            f"delivered={str(delivered).lower()} | "
            f"error_type={next_meta.get('bark_error_type', 'none')} | "
            f"exhausted={str(bool(next_meta.get('bark_retry_exhausted'))).lower()}",
            flush=True,
        )
        return {
            "attempted": bool(result.get("attempted")),
            "delivered": delivered,
            "message_id": row["id"],
            "attempts": next_meta.get("bark_attempts", meta.get("bark_attempts", 0)),
            "error_type": next_meta.get("bark_error_type", "none"),
            "exhausted": bool(next_meta.get("bark_retry_exhausted")),
        }
    return {"attempted": False, "delivered": False, "reason": "no_retryable_push"}


def _format_dashboard_time(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SHANGHAI_TZ).strftime("%m-%d %H:%M")


def _io_payload_value(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _io_text_value(value, max_len: int = 80) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _io_has_value(value) -> bool:
    if value in (None, ""):
        return False
    if isinstance(value, dict):
        return any(_io_has_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_io_has_value(item) for item in value)
    return True


def _io_add_part(parts: list[str], text: str) -> None:
    text = str(text or "").strip()
    if text and text not in parts:
        parts.append(text)


def _io_number_text(value, max_decimals: int = 1) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _io_text_value(value, 32)
    if number.is_integer():
        return str(int(number))
    return f"{number:.{max_decimals}f}".rstrip("0").rstrip(".")


def _io_percent_text(value) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _io_text_value(value, 32)
    if 0 <= number <= 1:
        number *= 100
    return f"{round(number)}%"


def _io_motion_text(value) -> str:
    state_labels = {
        "still": "静止",
        "stationary": "静止",
        "walking": "步行",
        "running": "跑步",
        "cycling": "骑行",
        "automotive": "乘车",
        "driving": "乘车",
        "unknown": "",
    }
    confidence_labels = {
        "low": "低",
        "medium": "中",
        "high": "高",
    }
    if isinstance(value, dict):
        state = str(value.get("state") or value.get("motion") or value.get("activity") or "").strip()
        confidence = str(value.get("confidence") or "").strip()
        parts = []
        label = state_labels.get(state.lower(), state)
        if label:
            parts.append(label)
        confidence_label = confidence_labels.get(confidence.lower(), confidence)
        if confidence_label:
            parts.append(f"置信度{confidence_label}")
        return "，".join(parts)
    return _io_text_value(value, 80)


def _io_nested_text(payload: dict, keys: tuple[str, ...], max_len: int = 80) -> str:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return _io_text_value(current, max_len=max_len)


def _io_location_text(location) -> str:
    if isinstance(location, str):
        return _io_text_value(location, 120)
    if not isinstance(location, dict):
        return ""
    for keys in (
        ("address",),
        ("formatted_address",),
        ("name",),
        ("label",),
        ("city",),
        ("locality",),
        ("region",),
        ("administrative_area",),
    ):
        text = _io_nested_text(location, keys, 120)
        if text:
            return text
    lat = location.get("latitude") or location.get("lat")
    lng = location.get("longitude") or location.get("lng") or location.get("lon")
    if lat is not None and lng is not None:
        return f"{lat}, {lng}"
    return ""


def _format_io_chat_preview(event_type: str, payload: dict, timezone_name: str = "") -> str:
    payload = _io_payload_value(payload)
    event_type = str(event_type or "")
    parts = []
    if event_type == "time.now":
        value = payload.get("local_time") or payload.get("now") or payload.get("timestamp") or payload.get("observed_at") or payload.get("time") or ""
        if value:
            _io_add_part(parts, f"时间字段：{_io_text_value(value, 48)}")
        if "charging" in payload:
            if payload.get("charging") is True:
                _io_add_part(parts, "正在充电")
            elif payload.get("charging") is False:
                _io_add_part(parts, "未充电")
        battery_level = payload.get("battery_level")
        if battery_level not in (None, ""):
            _io_add_part(parts, f"电量：{_io_percent_text(battery_level)}")
        user_state = _io_text_value(payload.get("user_state"), 48)
        if user_state and user_state.lower() not in ("default", "unknown", "normal"):
            _io_add_part(parts, f"设备状态：{user_state}")
        motion_state = _io_motion_text(payload.get("motion_state"))
        if motion_state:
            _io_add_part(parts, f"活动状态：{motion_state}")
        now_playing = payload.get("now_playing")
        if isinstance(now_playing, dict):
            title = _io_text_value(now_playing.get("title"), 48)
            artist = _io_text_value(now_playing.get("artist"), 48)
            playback = _io_text_value(now_playing.get("playback_state") or now_playing.get("state"), 24)
            if title or artist:
                song = " - ".join([part for part in (title, artist) if part])
                suffix = f"（{playback}）" if playback else ""
                _io_add_part(parts, f"媒体：{song}{suffix}")
    elif event_type == "location.update":
        location = (
            _io_location_text(payload.get("location"))
            or _io_text_value(payload.get("address"), 120)
            or _io_text_value(payload.get("label"), 120)
            or _io_text_value(payload.get("place_label"), 120)
            or _io_text_value(payload.get("city"), 80)
            or _io_text_value(payload.get("locality"), 80)
        )
        lat = payload.get("latitude")
        lng = payload.get("longitude")
        if location:
            _io_add_part(parts, f"位置字段：{location}")
        elif lat is not None and lng is not None:
            _io_add_part(parts, "位置字段：已收到坐标")
        if timezone_name and not parts:
            _io_add_part(parts, f"仅收到时区：{timezone_name}")
    elif event_type == "weather.current":
        weather = payload.get("weather") or payload.get("summary") or payload.get("condition") or payload.get("description") or ""
        temp = payload.get("temperature") or payload.get("temp") or _io_nested_text(payload, ("temperature", "value"))
        if weather:
            _io_add_part(parts, f"天气：{_io_text_value(weather, 80)}")
        if temp != "":
            _io_add_part(parts, f"温度：{_io_number_text(temp)}°C")
        apparent = payload.get("apparent_temperature")
        if apparent not in (None, ""):
            _io_add_part(parts, f"体感：{_io_number_text(apparent)}°C")
        humidity = payload.get("humidity")
        if humidity not in (None, ""):
            _io_add_part(parts, f"湿度：{_io_percent_text(humidity)}")
    elif event_type == "motion.state":
        motion = payload.get("motion_state") or payload.get("motion") or payload.get("state") or payload.get("activity") or ""
        if motion:
            _io_add_part(parts, f"活动状态：{_io_motion_text(motion)}")
    elif event_type == "health.steps":
        steps = payload.get("step_count") or payload.get("steps") or payload.get("count") or payload.get("value") or ""
        if steps != "":
            _io_add_part(parts, f"步数：{_io_text_value(steps, 32)}")
    elif event_type == "health.sleep":
        sleep = payload.get("sleep") or payload.get("duration") or payload.get("value") or ""
        if sleep:
            _io_add_part(parts, f"睡眠：{_io_text_value(sleep, 120)}")
        asleep_minutes = payload.get("asleep_minutes")
        if asleep_minutes not in (None, ""):
            _io_add_part(parts, f"睡眠时长：{_io_text_value(asleep_minutes, 32)}分钟")
        for key, label in (("deep_minutes", "深睡"), ("rem_minutes", "REM"), ("core_minutes", "核心睡眠")):
            value = payload.get(key)
            if value not in (None, ""):
                _io_add_part(parts, f"{label}：{_io_text_value(value, 32)}分钟")
    elif event_type == "health.workout":
        workout = payload.get("workout") or payload.get("activity") or payload.get("summary") or ""
        if workout:
            _io_add_part(parts, f"运动：{_io_text_value(workout, 120)}")
        workout_type = payload.get("workout_type")
        duration = payload.get("duration_min")
        count_today = payload.get("count_today")
        if workout_type:
            _io_add_part(parts, f"运动类型：{_io_text_value(workout_type, 48)}")
        if duration not in (None, ""):
            _io_add_part(parts, f"运动时长：{_io_text_value(duration, 32)}分钟")
        if count_today not in (None, ""):
            _io_add_part(parts, f"今日运动记录：{_io_text_value(count_today, 32)}次")
    elif event_type == "health.vitals":
        heart = payload.get("current_heart_rate") or payload.get("heart_rate") or payload.get("hr") or payload.get("pulse") or ""
        resting_heart = payload.get("resting_heart_rate")
        blood = payload.get("oxygen_saturation_pct") or payload.get("blood_oxygen") or payload.get("spo2") or ""
        if heart != "":
            _io_add_part(parts, f"心率：{_io_text_value(heart, 32)}")
        if resting_heart not in (None, ""):
            _io_add_part(parts, f"静息心率：{_io_text_value(resting_heart, 32)}")
        if blood != "":
            _io_add_part(parts, f"血氧：{_io_text_value(blood, 32)}")
    elif event_type == "device.battery":
        level = payload.get("level") or payload.get("battery_level") or payload.get("value") or ""
        charging = payload.get("charging")
        if level != "":
            _io_add_part(parts, f"电量：{_io_percent_text(level)}")
        if charging is True:
            _io_add_part(parts, "充电中")
        elif charging is False:
            _io_add_part(parts, "未充电")

    if not parts:
        if payload and not _io_has_value(payload):
            return "已接收该类感知事件，但本次字段为空，暂时没有可用于聊天的有效数值。"
        return "已接收该类感知事件，但这些字段暂时没有转换成聊天预览。"
    return "；".join(parts)


def _format_io_payload_details(payload: dict) -> list[dict]:
    payload = _io_payload_value(payload)
    details = []
    for key in sorted(payload.keys()):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(value)
        if len(text) > 180:
            text = text[:177] + "..."
        details.append({"key": str(key), "value": text})
    return details[:20]


async def get_push_delivery_status(session_id: str) -> dict:
    if not session_id:
        return {
            "enabled": bool(BARK_DEVICE_KEY),
            "total_24h": 0,
            "undelivered_count": 0,
            "retryable_count": 0,
            "exhausted_count": 0,
            "retry_stopped_count": 0,
            "consecutive_unanswered_pushes": 0,
            "last_failed_at": "",
            "last_error_type": "none",
            "latest_generated_at": "",
            "latest_delivered_at": "",
            "latest_delivery_state": "none",
        }
    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(hours=24)
    pool = await get_pool()
    async with pool.acquire() as conn:
        last_user = await conn.fetchrow(
            """
            SELECT created_at
            FROM conversations
            WHERE session_id = $1
              AND role = 'user'
              AND COALESCE(content, '') <> ''
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            session_id,
        )
        rows = await conn.fetch(
            """
            SELECT id, created_at, metadata
            FROM conversations
            WHERE session_id = $1
              AND role = 'assistant'
              AND metadata IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 300
            """,
            session_id,
        )

    total = 0
    undelivered = 0
    retryable = 0
    exhausted = 0
    retry_stopped = 0
    consecutive_unanswered = 0
    last_failed_at = None
    last_error_type = "none"
    latest_generated_at = None
    latest_delivered_at = None
    latest_delivery_state = "none"
    last_user_at = _to_utc(last_user["created_at"]) if last_user else None
    for row in rows:
        meta = _parse_metadata(row["metadata"])
        if meta.get("is_push") is not True or meta.get("push_source") != "shadow_cron":
            continue
        created_at = _to_utc(row["created_at"])
        if created_at and created_at >= start_utc:
            total += 1
        delivered_at = _get_bark_delivered_at(meta, row["created_at"])
        if delivered_at and (latest_delivered_at is None or delivered_at > latest_delivered_at):
            latest_delivered_at = delivered_at
        if latest_generated_at is None:
            latest_generated_at = row["created_at"]
            if meta.get("delivery") != "bark":
                latest_delivery_state = "not_configured"
            elif delivered_at:
                latest_delivery_state = "delivered"
            elif meta.get("bark_attempted") is True:
                latest_delivery_state = "undelivered"
            else:
                latest_delivery_state = "unknown"
        if delivered_at:
            if last_user_at and delivered_at <= last_user_at:
                continue
            consecutive_unanswered += 1
            continue
        if meta.get("delivery") == "bark" and meta.get("bark_attempted") is True and meta.get("bark_delivered") is False:
            undelivered += 1
            if last_failed_at is None:
                last_failed_at = _parse_metadata_datetime(meta.get("bark_last_attempt_at")) or row["created_at"]
                last_error_type = meta.get("bark_error_type", "unknown")
            if meta.get("bark_retry_stopped") is True:
                retry_stopped += 1
            elif _is_retryable_undelivered_push(meta):
                retryable += 1
            else:
                exhausted += 1
    return {
        "enabled": bool(BARK_DEVICE_KEY),
        "total_24h": total,
        "undelivered_count": undelivered,
        "retryable_count": retryable,
        "exhausted_count": exhausted,
        "retry_stopped_count": retry_stopped,
        "consecutive_unanswered_pushes": consecutive_unanswered,
        "last_failed_at": _format_dashboard_time(last_failed_at),
        "last_error_type": last_error_type,
        "latest_generated_at": _format_dashboard_time(latest_generated_at),
        "latest_delivered_at": _format_dashboard_time(latest_delivered_at),
        "latest_push_at": _format_dashboard_time(latest_delivered_at),
        "latest_delivery_state": latest_delivery_state,
        "max_attempts": BARK_MAX_DELIVERY_ATTEMPTS,
    }


def _extract_usage_tokens(data: dict) -> tuple[int, int, int]:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    return max(0, prompt_tokens), max(0, completion_tokens), max(0, total_tokens)


def _extract_chat_completion_text(data: dict) -> str:
    choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _estimate_shadow_decision_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    cost = (
        prompt_tokens * PUSH_DECISION_COST_INPUT_PER_MILLION
        + completion_tokens * PUSH_DECISION_COST_OUTPUT_PER_MILLION
    ) / 1_000_000
    return round(max(0.0, cost), 8)


async def _record_shadow_decision_token_usage(session_id: str, model: str, data: dict) -> None:
    prompt_tokens, completion_tokens, total_tokens = _extract_usage_tokens(data)
    if total_tokens <= 0:
        print("📊 主动推送决策 Token: not_reported", flush=True)
        return
    estimated_cost = _estimate_shadow_decision_cost_usd(prompt_tokens, completion_tokens)
    try:
        await save_token_usage(
            session_id,
            model,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            usage_type="shadow_push_decision",
            estimated_cost_usd=estimated_cost,
        )
    except Exception as e:
        print(f"⚠️ 主动推送决策Token记录失败: {type(e).__name__}", flush=True)
        return
    print(
        "📊 主动推送决策Token: "
        f"prompt_tokens={prompt_tokens} | "
        f"completion_tokens={completion_tokens} | "
        f"total_tokens={total_tokens} | "
        f"estimated_cost_usd={estimated_cost:.8f}",
        flush=True,
    )


async def generate_shadow_push(session_id: str, timing_state: dict | None = None) -> dict:
    recent_rows = await get_recent_conversation_messages(session_id, limit=16)
    recent_messages = _clean_history_for_push(recent_rows)
    recent_excerpt_count = min(len(recent_messages), 12)
    now_local = _local_now()
    interaction_state = await _get_push_interaction_state(session_id, now_local)
    if timing_state is None:
        timing_state = await _get_push_timing_state(session_id, now_local)
    interaction_state.update(timing_state)
    if not recent_messages:
        _log_push_context_diag(interaction_state, 0, False, "no_recent_messages", action="skip")
        await _save_shadow_push_decision_log(
            session_id=session_id,
            action="skip",
            reason="no_recent_messages",
            state=interaction_state,
            recent_excerpt_count=0,
            pushed=False,
            model=DEFAULT_MODEL,
        )
        return {"pushed": False, "reason": "no_recent_messages"}

    base_prompt = await get_system_prompt()
    shadow_user_content = await _build_shadow_user_content(recent_messages, interaction_state)
    push_messages = []
    if base_prompt:
        push_messages.append({"role": "system", "content": base_prompt})
    push_messages.extend(recent_messages)
    push_messages.append({"role": "user", "content": shadow_user_content})

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    if "openrouter" in API_BASE_URL:
        headers["HTTP-Referer"] = EXTRA_REFERER
        headers["X-Title"] = EXTRA_TITLE

    body = {
        "model": DEFAULT_MODEL,
        "messages": push_messages,
        "temperature": 0.9,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(API_BASE_URL, headers=headers, json=body)
    except Exception as e:
        print(f"⚠️ 主动推送决策异常: {type(e).__name__}")
        _log_push_context_diag(
            interaction_state,
            recent_excerpt_count,
            False,
            "model_exception",
            action="skip",
            parse_success=False,
        )
        await _save_shadow_push_decision_log(
            session_id=session_id,
            action="error",
            reason="model_exception",
            state=interaction_state,
            recent_excerpt_count=recent_excerpt_count,
            pushed=False,
            model=DEFAULT_MODEL,
            parse_success=False,
            error_type=type(e).__name__,
        )
        return {"pushed": False, "reason": "model_exception"}
    if response.status_code != 200:
        print(f"⚠️ 主动推送决策失败: HTTP {response.status_code}")
        _log_push_context_diag(
            interaction_state,
            recent_excerpt_count,
            False,
            "model_error",
            action="skip",
            parse_success=False,
        )
        await _save_shadow_push_decision_log(
            session_id=session_id,
            action="error",
            reason="model_error",
            state=interaction_state,
            recent_excerpt_count=recent_excerpt_count,
            pushed=False,
            model=DEFAULT_MODEL,
            parse_success=False,
            error_type=f"http_{response.status_code}",
        )
        return {"pushed": False, "reason": "model_error", "status_code": response.status_code}

    try:
        data = response.json()
    except Exception:
        _log_push_context_diag(
            interaction_state,
            recent_excerpt_count,
            False,
            "invalid_response_json",
            action="skip",
            parse_success=False,
        )
        await _save_shadow_push_decision_log(
            session_id=session_id,
            action="error",
            reason="invalid_response_json",
            state=interaction_state,
            recent_excerpt_count=recent_excerpt_count,
            pushed=False,
            model=DEFAULT_MODEL,
            parse_success=False,
            error_type="invalid_response_json",
        )
        return {"pushed": False, "reason": "invalid_response_json"}
    await _record_shadow_decision_token_usage(session_id, DEFAULT_MODEL, data)
    raw_reply = ""
    try:
        raw_reply = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, AttributeError):
        raw_reply = ""

    decision = parse_shadow_decision(raw_reply)
    action = decision["action"]
    decision_reason = decision["reason"]
    decision_intent = decision.get("intent", "")
    parse_success = bool(decision["parse_success"])
    if action == "skip":
        _log_push_context_diag(
            interaction_state,
            recent_excerpt_count,
            False,
            decision_reason,
            action="skip",
            parse_success=parse_success,
        )
        await _save_shadow_push_decision_log(
            session_id=session_id,
            action="skip" if parse_success else "error",
            reason=decision_reason,
            intent=decision_intent,
            state=interaction_state,
            recent_excerpt_count=recent_excerpt_count,
            pushed=False,
            model=DEFAULT_MODEL,
            parse_success=parse_success,
            error_type="" if parse_success else decision_reason,
        )
        return {
            "pushed": False,
            "reason": decision_reason,
            "action": "skip",
            "parse_success": parse_success,
        }

    ai_reply = clean_push_reply(decision.get("message", ""), hard_limit=80)
    if not ai_reply:
        _log_push_context_diag(
            interaction_state,
            recent_excerpt_count,
            False,
            "empty_message",
            action="skip",
            parse_success=parse_success,
        )
        await _save_shadow_push_decision_log(
            session_id=session_id,
            action="skip",
            reason="empty_message",
            intent=decision_intent,
            state=interaction_state,
            recent_excerpt_count=recent_excerpt_count,
            pushed=False,
            model=DEFAULT_MODEL,
            parse_success=parse_success,
        )
        return {"pushed": False, "reason": "empty_message", "action": "skip", "parse_success": parse_success}

    metadata = {
        "is_push": True,
        "push_source": "shadow_cron",
        "delivery": "bark" if BARK_DEVICE_KEY else "none",
    }
    message_id = await _save_push_message(session_id, ai_reply, DEFAULT_MODEL, metadata)
    delivery_result = await deliver_bark_push(ai_reply)
    metadata = _apply_bark_delivery_result(metadata, delivery_result)
    await _update_message_metadata(message_id, metadata)
    delivered = bool(delivery_result.get("delivered"))
    _log_push_context_diag(
        interaction_state,
        recent_excerpt_count,
        True,
        decision_reason,
        action="send",
        parse_success=parse_success,
    )
    await _save_shadow_push_decision_log(
        session_id=session_id,
        action="send",
        reason=decision_reason,
        intent=decision_intent,
        state=interaction_state,
        recent_excerpt_count=recent_excerpt_count,
        pushed=True,
        model=DEFAULT_MODEL,
        parse_success=parse_success,
        bark_delivered=delivered,
    )
    print(f"📮 主动推送已落库: session={session_id}, chars={len(ai_reply)}, bark={'ok' if delivered else 'skip_or_fail'}")
    return {
        "pushed": True,
        "reason": decision_reason,
        "action": "send",
        "parse_success": parse_success,
        "session_id": session_id,
        "chars": len(ai_reply),
        "delivered": delivered,
    }


# ============================================================
# 后台记忆处理
# ============================================================

async def process_memories_background(session_id: str, user_msg: str, assistant_msg: str, model: str, context_messages: list = None, skip_conversation_log: bool = False, tool_messages: list = None, assistant_tool_calls: list = None, assistant_reasoning: str = None):
    """
    后台异步：存储对话 + 提取记忆（不阻塞主流程）
    
    记忆提取受 MEMORY_EXTRACT_INTERVAL 控制：
    - 0: 禁用自动提取
    - 1: 每轮提取（默认）
    - N: 每 N 轮提取一次
    对话记录始终保存，不受间隔影响（除非 skip_conversation_log=True）。
    
    context_messages: 客户端发来的原始对话上下文（不含system prompt），
                      用于让提取模型从完整上下文中提取记忆。
    skip_conversation_log: 跳过对话存储（标题生成等辅助请求时使用）
    tool_messages: 客户端发来的工具结果消息列表
    assistant_tool_calls: response中assistant的工具调用列表（如果有）
    assistant_reasoning: response中assistant的reasoning_content（deepseek thinking mode）
    """
    global _round_counter
    health_stage = "conversation_storage"
    try:
        shadow_mind_normal_turn_saved = False
        # Debug: 打印存储分支判断依据
        print(f"💾 process_memories_background: user_msg={bool(user_msg)}, tool_messages={len(tool_messages) if tool_messages else 0}, "
              f"assistant_tool_calls={len(assistant_tool_calls) if assistant_tool_calls else 0}, skip={skip_conversation_log}")
        if tool_messages:
            print(f"💾 tool详情: {[{'role': m.get('role'), 'tool_call_id': m.get('tool_call_id', '?')} for m in tool_messages]}")
        
        # 1. 存储对话记录（除非明确跳过）
        if skip_conversation_log:
            print(f"⏭️  跳过对话存储（辅助请求）")
        elif tool_messages:
            # 工具结果轮次：存tool消息 + assistant回复（user消息在之前的轮次已存过）
            for tm in tool_messages:
                meta_dict = {}
                if tm.get("tool_call_id"):
                    meta_dict["tool_call_id"] = tm["tool_call_id"]
                if tm.get("name"):
                    meta_dict["name"] = tm["name"]
                meta = json.dumps(meta_dict) if meta_dict else None
                await save_message(session_id, "tool", tm.get("content", ""), model, metadata=meta)
            
            if assistant_msg or assistant_tool_calls:
                ast_meta_dict = {}
                if assistant_tool_calls:
                    ast_meta_dict["tool_calls"] = assistant_tool_calls
                if assistant_reasoning:
                    ast_meta_dict["reasoning_content"] = assistant_reasoning
                ast_meta = json.dumps(ast_meta_dict) if ast_meta_dict else None
                await save_message(session_id, "assistant", assistant_msg or "", model, metadata=ast_meta)
                print(f"🔧 存储: {len(tool_messages)}条tool + 1条assistant" + (" (含tool_calls)" if assistant_tool_calls else "") + (" (含reasoning)" if assistant_reasoning else ""))
        else:
            # 普通对话或首次工具调用
            ast_meta_dict = {}
            if assistant_tool_calls:
                ast_meta_dict["tool_calls"] = assistant_tool_calls
            if assistant_reasoning:
                ast_meta_dict["reasoning_content"] = assistant_reasoning
            assistant_meta = json.dumps(ast_meta_dict) if ast_meta_dict else None
            
            if assistant_tool_calls:
                # 首次工具调用：assistant回复包含tool_calls，存user + assistant(tool_calls)
                await save_message(session_id, "user", user_msg, model)
                await save_message(session_id, "assistant", assistant_msg or "", model, metadata=assistant_meta)
                print(f"🔧 存储: user + assistant (含{len(assistant_tool_calls)}个tool_calls)" + (" (含reasoning)" if assistant_reasoning else ""))
            else:
                # 纯文字对话：re-roll检测 + 存user + assistant
                last_user = await get_last_user_content(session_id)
                if last_user and last_user.strip() == user_msg.strip():
                    updated = await update_last_assistant_message(session_id, assistant_msg, model)
                    if updated:
                        print(f"🔄 检测到re-roll，已覆盖最后一条assistant回复")
                        shadow_mind_normal_turn_saved = True
                    else:
                        await save_message(session_id, "user", user_msg, model)
                        await save_message(session_id, "assistant", assistant_msg, model, metadata=assistant_meta)
                        shadow_mind_normal_turn_saved = True
                else:
                    await save_message(session_id, "user", user_msg, model)
                    await save_message(session_id, "assistant", assistant_msg, model, metadata=assistant_meta)
                    shadow_mind_normal_turn_saved = True
        if not skip_conversation_log:
            await _record_operational_success_safe("conversation_storage")

        # Shadow Mind is isolated from conversation persistence and all downstream jobs.
        # Failures here are diagnostic only and must never block memory/summary/card work.
        if SHADOW_MIND_RULES_ENABLED and shadow_mind_normal_turn_saved:
            try:
                source_ids = await get_latest_normal_turn_message_ids(session_id)
                if source_ids:
                    event_key = "normal_chat:" + session_id + ":" + "-".join(str(value) for value in source_ids)
                    await settle_shadow_mind_rules(
                        session_id=session_id,
                        event_type="normal_chat",
                        source_message_ids=source_ids,
                        event_key=hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:32],
                    )
            except Exception as shadow_error:
                print(
                    "shadow_mind_settle_failed "
                    f"event_type=normal_chat error_type={type(shadow_error).__name__}",
                    flush=True,
                )
        
        # 2. 检查是否需要提取记忆
        health_stage = "memory_extraction"
        if not MEMORY_EXTRACT_ENABLED:
            print(f"⏭️  记忆提取已关闭（MEMORY_EXTRACT_ENABLED=false）")
            return
        
        if MEMORY_EXTRACT_INTERVAL == 0:
            print(f"⏭️  记忆自动提取已禁用，跳过")
            return
        
        _round_counter += 1
        
        if MEMORY_EXTRACT_INTERVAL > 1 and (_round_counter % MEMORY_EXTRACT_INTERVAL != 0):
            print(f"⏭️  轮次 {_round_counter}，跳过记忆提取（每 {MEMORY_EXTRACT_INTERVAL} 轮提取一次）")
            return
        
        if MEMORY_EXTRACT_INTERVAL > 1:
            print(f"📝 轮次 {_round_counter}，执行记忆提取")
        
        # 3. 获取已有记忆，传给提取模型做对比去重
        existing = await get_recent_memories(limit=80)
        existing_contents = [r["content"] for r in existing]
        
        # 4. 构建用于提取的消息列表
        #    截取最近 MEMORY_EXTRACT_INTERVAL 轮对话（每轮=user+assistant共2条）
        #    而非发送完整上下文，省token
        if context_messages:
            # 截取最近N轮（interval×2条），加上最新的assistant回复
            tail_count = MEMORY_EXTRACT_INTERVAL * 2
            recent_msgs = list(context_messages)[-tail_count:] if len(context_messages) > tail_count else list(context_messages)
            messages_for_extraction = recent_msgs + [
                {"role": "assistant", "content": assistant_msg}
            ]
            print(f"📝 截取最近 {MEMORY_EXTRACT_INTERVAL} 轮对话提取记忆（{len(messages_for_extraction)} 条消息）")
        else:
            messages_for_extraction = [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": assistant_msg},
            ]
        
        new_memories = await extract_memories(messages_for_extraction, existing_memories=existing_contents)
        
        # 过滤垃圾记忆（不靠模型自觉，硬过滤）
        META_BLACKLIST = [
            "记忆库", "记忆系统", "检索", "没有被记录", "没有被提取",
            "记忆遗漏", "尚未被记录", "写入不完整", "检索功能",
            "系统没有返回", "关键词匹配", "语义匹配", "语义检索",
            "阈值", "数据库", "seed", "导入", "部署",
            "bug", "debug", "端口", "网关",
        ]
        
        filtered_memories = []
        for mem in new_memories:
            content = mem["content"]
            if any(kw in content for kw in META_BLACKLIST):
                print(f"🚫 过滤掉meta记忆: content_chars={len(content)} hash={_short_hash_text(content)}")
                continue
            filtered_memories.append(mem)
        
        for mem in filtered_memories:
            await save_memory(
                content=mem["content"],
                importance=mem["importance"],
                source_session=session_id,
            )
        
        if filtered_memories:
            total = await get_all_memories_count()
            print(f"💾 已保存 {len(filtered_memories)} 条新记忆（过滤了 {len(new_memories) - len(filtered_memories)} 条），总计 {total} 条")
        await _record_operational_success_safe("memory_extraction")
            
    except Exception as e:
        print(f"⚠️  后台记忆处理失败: {type(e).__name__}")
        await _record_operational_failure_safe(
            health_stage,
            f"exception_{type(e).__name__}",
        )


# ============================================================
# API 接口
# ============================================================

@app.post("/api/push/trigger")
async def api_push_trigger(request: Request):
    """外部cron触发主动推送：独立密钥保护，不复用GATEWAY_SECRET"""
    no_store_headers = {"Cache-Control": "no-store"}
    session_id = ""
    if not PUSH_SECRET:
        await _save_shadow_push_decision_log(
            session_id="",
            action="error",
            reason="push_secret_missing",
            model=DEFAULT_MODEL,
            parse_success=None,
            error_type="push_secret_missing",
        )
        return JSONResponse(
            status_code=500,
            content={"error": "PUSH_SECRET is not configured"},
            headers=no_store_headers,
        )

    provided = request.headers.get("X-Push-Secret", "")
    if not secrets.compare_digest(provided, PUSH_SECRET):
        await _save_shadow_push_decision_log(
            session_id="",
            action="blocked",
            reason="unauthorized",
            model=DEFAULT_MODEL,
            parse_success=None,
        )
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized"},
            headers=no_store_headers,
        )

    if _push_lock.locked():
        await _save_shadow_push_decision_log(
            session_id=get_active_session_id() or "",
            action="blocked",
            reason="locked",
            model=DEFAULT_MODEL,
            parse_success=None,
        )
        return JSONResponse(
            content={"pushed": False, "reason": "locked"},
            headers=no_store_headers,
        )

    await _push_lock.acquire()
    try:
        session_id = get_active_session_id()
        if not session_id:
            await _save_shadow_push_decision_log(
                session_id="",
                action="blocked",
                reason="no_active_session",
                model=DEFAULT_MODEL,
                parse_success=None,
            )
            return JSONResponse(
                content={"pushed": False, "reason": "no_active_session"},
                headers=no_store_headers,
            )
        if not API_KEY:
            await _save_shadow_push_decision_log(
                session_id=session_id,
                action="error",
                reason="api_key_missing",
                model=DEFAULT_MODEL,
                parse_success=None,
                error_type="api_key_missing",
            )
            return JSONResponse(
                status_code=500,
                content={"pushed": False, "reason": "api_key_missing"},
                headers=no_store_headers,
            )

        retry_result = await retry_undelivered_bark_push(session_id)
        if retry_result.get("attempted"):
            await _save_shadow_push_decision_log(
                session_id=session_id,
                action="blocked",
                reason="bark_retry",
                model=DEFAULT_MODEL,
                parse_success=None,
                pushed=False,
                bark_delivered=bool(retry_result.get("delivered", False)),
                error_type="retry_exhausted" if retry_result.get("exhausted") else "",
            )
            return JSONResponse(
                content={
                    "pushed": False,
                    "reason": "bark_retry",
                    "retry_delivered": retry_result.get("delivered", False),
                    "retry_exhausted": retry_result.get("exhausted", False),
                },
                headers=no_store_headers,
            )

        decision = await should_generate_push(session_id)
        if not decision.get("should_push"):
            await _log_push_decision_diag(session_id, decision.get("reason", "blocked"), decision)
            return JSONResponse(
                content={"pushed": False, **decision},
                headers=no_store_headers,
            )

        result = await generate_shadow_push(session_id, decision)
        return JSONResponse(content=result, headers=no_store_headers)
    except Exception as e:
        print(f"⚠️ 主动推送异常: {type(e).__name__}")
        await _save_shadow_push_decision_log(
            session_id=session_id,
            action="error",
            reason="internal_error",
            model=DEFAULT_MODEL,
            parse_success=None,
            error_type=type(e).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={"pushed": False, "reason": "internal_error"},
            headers=no_store_headers,
        )
    finally:
        _push_lock.release()


@app.get("/")
async def health_check():
    """健康检查"""
    memory_count = 0
    current_system_prompt = await get_system_prompt()
    if MEMORY_ENABLED:
        try:
            memory_count = await get_all_memories_count()
        except:
            pass
    
    return {
        "status": "running",
        "gateway": "AI Memory Gateway v2.0",
        "system_prompt_loaded": len(current_system_prompt) > 0,
        "system_prompt_length": len(current_system_prompt),
        "memory_enabled": MEMORY_ENABLED,
        "memory_count": memory_count,
        "memory_extract_interval": MEMORY_EXTRACT_INTERVAL,
    }


@app.get("/v1/models")
async def list_models():
    """模型列表：直接转发上游（OpenRouter等）真实的模型列表"""
    models_url = API_BASE_URL.replace("/chat/completions", "/models")
    try:
        headers = {"Authorization": f"Bearer {API_KEY}"}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(models_url, headers=headers)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"⚠️ 获取模型列表失败: {e}")

    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL,
                "object": "model",
                "created": 1700000000,
                "owned_by": "ai-memory-gateway",
            }
        ],
    }


def _io_request_text(value, max_len: int = 128) -> str:
    text = value if isinstance(value, str) else ""
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    return text[:max_len]


def _io_schema_version(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(number, 1000))


def _io_event_type_summary(events: list) -> str:
    event_types = sorted({
        _io_request_text((event or {}).get("event_type") or (event or {}).get("type"), 80)
        for event in events
        if isinstance(event, dict)
    })
    event_types = [item for item in event_types if item]
    if not event_types:
        return "none"
    return ",".join(event_types[:12])


@app.post("/v1/io/context/events")
async def io_context_events(request: Request):
    """io 感知事件入口：只写入设备/环境数据，不调用模型、不写 conversations。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "invalid_payload"})

    device_id = _io_request_text(body.get("device_id") or body.get("deviceId"), 128)
    if not device_id:
        return JSONResponse(status_code=400, content={"error": "device_id_required"})

    events = body.get("events")
    if not isinstance(events, list) or not events:
        return JSONResponse(status_code=400, content={"error": "events_required"})
    if len(events) > 100:
        return JSONResponse(status_code=413, content={"error": "too_many_events", "max_events": 100})

    try:
        result = await save_io_context_events(
            device_id=device_id,
            app_instance_id=_io_request_text(body.get("app_instance_id") or body.get("appInstanceId"), 128),
            source_client=_io_request_text(body.get("source_client"), 64) or "io",
            timezone_name=_io_request_text(body.get("timezone"), 64),
            schema_version=_io_schema_version(body.get("schema_version")),
            events=events,
        )
        await _record_operational_success_safe("io_ingest")
    except Exception as io_error:
        await _record_operational_failure_safe(
            "io_ingest",
            f"exception_{type(io_error).__name__}",
        )
        raise
    print(
        "📱 io感知事件入库: "
        f"device_hash={_short_hash_text(device_id)} | "
        f"events_received={len(events)} | "
        f"event_types={_io_event_type_summary(events)} | "
        f"inserted={result.get('inserted', 0)} | "
        f"duplicates={result.get('duplicates', 0)} | "
        f"skipped={result.get('skipped', 0)} | "
        f"latest_updated={result.get('latest_updated', 0)}",
        flush=True,
    )
    return {
        "status": "ok",
        "received": result.get("received", 0),
        "inserted": result.get("inserted", 0),
        "duplicates": result.get("duplicates", 0),
        "skipped": result.get("skipped", 0),
        "latest_updated": result.get("latest_updated", 0),
    }



@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """核心转发接口"""
    if not API_KEY:
        return JSONResponse(
            status_code=500,
            content={"error": "API_KEY 未设置，请在环境变量中配置"},
        )
    
    body = await request.json()
    messages = body.get("messages", [])
    
    # ---------- 检测是否应跳过对话存储 ----------
    # 客户端通过header显式声明（如标题生成等辅助请求）
    skip_conversation_log = request.headers.get("X-Skip-Conversation-Log", "").lower() == "true"
    
    # ---------- 提取用户最新消息 ----------
    user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                user_message = content
            elif isinstance(content, list):
                user_message = " ".join(
                    item.get("text", "") for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            break
    
    # ---------- 构建 system prompt ----------
    # 先保存原始对话消息（不含 system prompt），用于记忆提取
    original_messages = [msg for msg in messages if msg.get("role") != "system"]
    base_system_prompt = await get_system_prompt()
    
    # ---------- 检测工具调用消息 ----------
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    if tool_messages:
        print(f"🔧 检测到 {len(tool_messages)} 条工具结果消息")
    
    # ---------- 生成 session ID ----------
    session_id = str(uuid.uuid4())[:8]
    cache_diag = {
        "api_provider": _api_provider_label(API_BASE_URL),
        "mode": "legacy",
        "session_hash": _short_hash_text(session_id),
    }
    
    # ---------- 分区缓存模式 ----------
    if CACHE_PARTITION_ENABLED:
        active_sid = get_active_session_id()
        if active_sid:
            session_id = active_sid
        cache_diag["session_hash"] = _short_hash_text(session_id)
        
        # 从DB读取历史
        try:
            db_history = await get_conversation_messages(session_id, limit=10000)
            db_msgs = []
            for m in (db_history or []):
                msg = db_row_to_message(m)
                msg['created_at'] = m.get('created_at')  # 保留时间戳供分区时间窗口判断
                db_msgs.append(msg)
        except Exception as e:
            print(f"[warning] 分区模式读取历史失败: {e}")
            db_msgs = []
        
        # 提取客户端新消息（非system），可能是user、tool、或带tool_calls的assistant
        client_new_msgs = [m for m in messages if m.get("role") != "system"]
        # 分区模式下，assistant消息来自上一轮response（DB里已存），过滤掉避免重复
        client_new_msgs = [m for m in client_new_msgs if m.get("role") != "assistant"]
        # 分区模式下DB已有完整历史，客户端发来的旧user是冗余的，只保留最后一条
        user_msgs = [m for m in client_new_msgs if m.get("role") == "user"]
        if len(user_msgs) > 1:
            last_user = user_msgs[-1]
            client_new_msgs = [m for m in client_new_msgs if m.get("role") != "user"]
            client_new_msgs.append(last_user)
            print(f"🔧 去重: 过滤{len(user_msgs)-1}条冗余user，保留最后1条")
        # 工具结果轮次处理：基于DB状态 + 当前轮次tool_call_id精确判断
        client_tools = [m for m in client_new_msgs if m.get("role") == "tool"]
        if client_tools:
            # 判断DB是否处于"等待tool结果"状态（最后一条是assistant(tool_calls)）
            db_last = db_msgs[-1] if db_msgs else None
            db_expecting_tool = (db_last and db_last.get("role") == "assistant" and db_last.get("tool_calls"))
            
            if not db_expecting_tool:
                # DB不在等待tool结果 → 客户端的所有tool都是历史残留（含手动删除后的幽灵）
                stale_ids = [m.get('tool_call_id', '?') for m in client_tools]
                print(f"🔧 去重: DB未在等待tool结果，丢弃{len(client_tools)}条客户端tool (ids: {stale_ids})")
                client_new_msgs = [m for m in client_new_msgs if m.get("role") != "tool"]
            else:
                # DB在等待tool → 只保留匹配当前轮次assistant(tool_calls)的tool
                expected_tool_ids = {tc.get("id") for tc in db_last.get("tool_calls", []) if tc.get("id")}
                new_tools = [m for m in client_tools if m.get("tool_call_id") in expected_tool_ids]
                stale_tools = [m for m in client_tools if m.get("tool_call_id") not in expected_tool_ids]
                
                if stale_tools:
                    print(f"🔧 去重: 丢弃{len(stale_tools)}条非当前轮次tool (ids: {[m.get('tool_call_id','?') for m in stale_tools]})")
                if new_tools:
                    print(f"🔧 保留{len(new_tools)}条当前轮次tool (ids: {[m.get('tool_call_id','?') for m in new_tools]})")
                
                # 重建 client_new_msgs
                last_msg = client_new_msgs[-1] if client_new_msgs else None
                client_new_msgs = new_tools[:]
                if last_msg and last_msg.get("role") == "user":
                    client_new_msgs.append(last_msg)
                
                if new_tools:
                    # Race condition 防护：DB的assistant(tool_calls)已确认存在（db_expecting_tool=True），
                    # 但仍需检查是否被其他并发请求意外清除
                    new_tool_ids = {m.get("tool_call_id") for m in new_tools if m.get("tool_call_id")}
                    db_has_matching_ast = False
                    for m in db_msgs:
                        if m.get("role") == "assistant" and m.get("tool_calls"):
                            ast_tc_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
                            if new_tool_ids & ast_tc_ids:
                                db_has_matching_ast = True
                                break
                    if not db_has_matching_ast and new_tool_ids:
                        for m in messages:
                            if m.get("role") == "assistant" and m.get("tool_calls"):
                                ast_tc_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
                                if new_tool_ids & ast_tc_ids:
                                    client_new_msgs.insert(0, m)
                                    print(f"⚠️ Race防护: 从客户端补充assistant(tool_calls)")
                                    break
        all_msgs = db_msgs + client_new_msgs
        
        # 同步更新tool_messages，避免process_memories_background存重复的旧tool
        tool_messages = [m for m in client_new_msgs if m.get("role") == "tool"]
        
        print(f"📦 分区模式: DB历史{len(db_msgs)}条 + 客户端消息{len(client_new_msgs)}条")
        
        messages = await build_partitioned_messages(
            session_id, all_msgs, base_system_prompt, user_message, cache_diag=cache_diag
        )
        body["messages"] = messages
    
    else:
        # ---------- 原有逻辑：system prompt + 记忆注入 ----------
        memory_block = ""
        if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message:
            memory_block = await build_system_prompt_with_memories(user_message)

        has_system = any(msg.get("role") == "system" for msg in messages)

        if has_system:
            for i, msg in enumerate(messages):
                if msg.get("role") == "system":
                    if base_system_prompt:
                        msg["content"] = base_system_prompt + "\n\n" + (msg.get("content") or "")
                    if memory_block:
                        # 追加到人设末尾，不是前置
                        messages[i]["content"] = msg["content"] + "\n\n" + memory_block
                    # 没有记忆就原样不动
                    break
        elif base_system_prompt or memory_block:
            # 客户端没发 system 消息时（少见），用网关人设 + 记忆拼
            base = base_system_prompt
            if memory_block:
                base = (base + "\n\n" + memory_block) if base else memory_block
            if base:
                messages.insert(0, {"role": "system", "content": base})

        body["messages"] = messages
    
    # ---------- 模型处理 ----------
    model = body.get("model", DEFAULT_MODEL)
    if not model:
        model = DEFAULT_MODEL
    body["model"] = model
    cache_diag["actual_model"] = model
    cache_diag["api_provider"] = _api_provider_label(API_BASE_URL)
    cache_diag.setdefault("constructed_breakpoints", _count_cache_breakpoints(body.get("messages", [])))
    
    # ---------- cache_control 兼容性处理 ----------
    if CACHE_PARTITION_ENABLED and not _is_anthropic_model(model):
        _strip_cache_control(body.get("messages", []))
    cache_diag["sent_breakpoints"] = _count_cache_breakpoints(body.get("messages", []))
    _log_cache_build_diag(cache_diag)
    
    # ---------- 转发请求 ----------
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    # OpenRouter 需要的额外头
    if "openrouter" in API_BASE_URL:
        headers["HTTP-Referer"] = EXTRA_REFERER
        headers["X-Title"] = EXTRA_TITLE
    
    is_stream = body.get("stream", False)
    
    # 强制流式传输（解决部分客户端不发stream=true的问题）
    if FORCE_STREAM and not is_stream:
        is_stream = True
        body["stream"] = True
        print(f"⚡ 强制开启流式传输（FORCE_STREAM=true）")
    
    # 注入推理参数（解决客户端走网关时不带reasoning参数的问题）
    if REASONING_EFFORT:
        # 统一用 reasoning_effort（Claude/OpenAI/Google Gemini OpenAI兼容端点都支持）
        # 先删除客户端可能已带的值，确保用我们配置的
        body.pop("reasoning_effort", None)
        body.pop("google", None)
        body["reasoning_effort"] = REASONING_EFFORT
        print(f"🧠 注入推理参数: reasoning_effort={REASONING_EFFORT}")
    
    print(f"📡 请求: model={model}, stream={is_stream}, memory={'on' if MEMORY_ENABLED else 'off'}", flush=True)
    
    # 调试：打印请求体中的推理相关字段
    debug_keys = {k: v for k, v in body.items() if k in ('reasoning_effort', 'google', 'reasoning')}
    if debug_keys:
        print(f"📡 推理字段: {debug_keys}", flush=True)
    
    if is_stream:
        return StreamingResponse(
            stream_and_capture(headers, body, session_id, user_message, model, original_messages, skip_conversation_log, tool_messages, cache_diag=cache_diag),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    else:
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(API_BASE_URL, headers=headers, json=body)
        except Exception as upstream_error:
            await _record_operational_failure_safe(
                "upstream_chat",
                f"exception_{type(upstream_error).__name__}",
            )
            raise
            
        if response.status_code == 200:
            await _record_operational_success_safe("upstream_chat")
            resp_data = response.json()
            _log_usage_diag("NonStream", resp_data.get("usage") if isinstance(resp_data, dict) else None)
            assistant_msg = ""
            assistant_tool_calls = None
            assistant_reasoning = None
            try:
                msg_obj = resp_data["choices"][0]["message"]
                assistant_msg = msg_obj.get("content") or ""
                if msg_obj.get("tool_calls"):
                    assistant_tool_calls = msg_obj["tool_calls"]
                    print(f"🔧 Response 包含 {len(assistant_tool_calls)} 个工具调用")
                if msg_obj.get("reasoning_content"):
                    assistant_reasoning = msg_obj["reasoning_content"]
                    print(f"🧠 Response 包含 reasoning_content ({len(assistant_reasoning)}字符)")
            except (KeyError, IndexError):
                pass

            if MEMORY_ENABLED and (user_message or tool_messages):
                asyncio.create_task(
                    process_memories_background(session_id, user_message, assistant_msg, model,
                                                context_messages=original_messages, skip_conversation_log=skip_conversation_log,
                                                tool_messages=tool_messages, assistant_tool_calls=assistant_tool_calls,
                                                assistant_reasoning=assistant_reasoning)
                )

            return JSONResponse(status_code=200, content=resp_data)
        await _record_operational_failure_safe(
            "upstream_chat",
            f"upstream_http_{response.status_code}",
        )
        return JSONResponse(status_code=response.status_code, content=response.json())


async def stream_and_capture(headers: dict, body: dict, session_id: str, user_message: str, model: str, original_messages: list = None, skip_conversation_log: bool = False, tool_messages: list = None, cache_diag: dict | None = None):
    try:
        async for chunk in _stream_and_capture_impl(
            headers,
            body,
            session_id,
            user_message,
            model,
            original_messages,
            skip_conversation_log,
            tool_messages,
            cache_diag,
        ):
            yield chunk
    except Exception as upstream_error:
        await _record_operational_failure_safe(
            "upstream_chat",
            f"exception_{type(upstream_error).__name__}",
        )
        raise


async def _stream_and_capture_impl(headers: dict, body: dict, session_id: str, user_message: str, model: str, original_messages: list = None, skip_conversation_log: bool = False, tool_messages: list = None, cache_diag: dict | None = None):
    """流式响应 + 捕获完整回复（原始字节透传，确保SSE格式和thinking数据完整）"""
    full_response = []
    full_reasoning = []
    stream_usage = {}
    line_buffer = ""
    accumulated_tool_calls = {}  # index -> {id, type, function: {name, arguments}}
    
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", API_BASE_URL, headers=headers, json=body) as response:
            # 打印上游响应头（排查thinking问题用）
            upstream_ct = response.headers.get("content-type", "")
            print(f"📨 上游响应: status={response.status_code}, content-type={upstream_ct}", flush=True)
            
            # 上游非200时，提前打印messages结构方便debug
            if response.status_code != 200:
                msg_summary = [{"role": m.get("role"), "tool_calls": bool(m.get("tool_calls")), "tool_call_id": m.get("tool_call_id", ""), "content_type": type(m.get("content")).__name__} for m in body.get("messages", [])]
                print(f"❌ 发送的messages结构({len(msg_summary)}条): {msg_summary}", flush=True)
            
            error_body_parts = []
            is_error = response.status_code != 200
            if is_error:
                await _record_operational_failure_safe(
                    "upstream_chat",
                    f"upstream_http_{response.status_code}",
                )
            
            async for chunk in response.aiter_bytes():
                # 原始字节直接透传给客户端
                yield chunk
                
                if is_error:
                    error_body_parts.append(chunk)
                    continue
                
                # 旁路解析：从字节流中提取assistant回复内容，用于后续记忆提取
                text = chunk.decode("utf-8", errors="ignore")
                line_buffer += text
                while "\n" in line_buffer:
                    line, line_buffer = line_buffer.split("\n", 1)
                    line = line.strip()
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            data = json.loads(line[6:])
                            
                            if "usage" in data:
                                stream_usage = data["usage"]
                            
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                full_response.append(content)
                            
                            # 收集reasoning_content（deepseek thinking mode）
                            reasoning = delta.get("reasoning_content", "")
                            if reasoning:
                                full_reasoning.append(reasoning)
                            
                            # 累积tool_calls
                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    if idx not in accumulated_tool_calls:
                                        accumulated_tool_calls[idx] = {
                                            "index": idx,
                                            "id": tc.get("id", ""),
                                            "type": tc.get("type", "function"),
                                            "function": {"name": "", "arguments": ""}
                                        }
                                    if tc.get("id"):
                                        accumulated_tool_calls[idx]["id"] = tc["id"]
                                    if "function" in tc:
                                        fn = tc["function"]
                                        if fn.get("name"):
                                            accumulated_tool_calls[idx]["function"]["name"] = fn["name"]
                                        if "arguments" in fn:
                                            accumulated_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
    
    assistant_msg = "".join(full_response)
    if not is_error:
        await _record_operational_success_safe("upstream_chat")
    assistant_reasoning = "".join(full_reasoning) if full_reasoning else None
    assistant_tool_calls = list(accumulated_tool_calls.values()) if accumulated_tool_calls else None
    
    if assistant_reasoning:
        print(f"🧠 Stream response 包含 reasoning_content ({len(assistant_reasoning)}字符)")
    
    # 打印上游错误内容
    if error_body_parts:
        error_text = b"".join(error_body_parts).decode("utf-8", errors="ignore")[:500]
        print(f"❌ 上游错误内容: {error_text}", flush=True)
    
    if assistant_tool_calls:
        print(f"🔧 Stream response 包含 {len(assistant_tool_calls)} 个工具调用")
    
    if stream_usage:
        pt = stream_usage.get("prompt_tokens", 0)
        ct = stream_usage.get("completion_tokens", 0)
        tt = stream_usage.get("total_tokens", 0)
        if tt > 0:
            asyncio.create_task(save_token_usage(session_id, model, pt, ct, tt))
            print(f"📊 Stream Token: {pt} + {ct} = {tt}")
        _log_usage_diag("Stream", stream_usage)
    else:
        _log_usage_diag("Stream", None)
    
    if MEMORY_ENABLED and (user_message or tool_messages):
        asyncio.create_task(
            process_memories_background(session_id, user_message, assistant_msg, model, 
                                        context_messages=original_messages, skip_conversation_log=skip_conversation_log,
                                        tool_messages=tool_messages, assistant_tool_calls=assistant_tool_calls,
                                        assistant_reasoning=assistant_reasoning)
        )


# ============================================================
# 记忆管理接口
# ============================================================


@app.get("/import/seed-memories")
async def import_seed_memories():
    """一次性导入预置记忆（从 seed_memories.py）"""
    try:
        from seed_memories import run_seed_import
        result = await run_seed_import()
        return result
    except ImportError:
        return {"error": "未找到 seed_memories.py，请参考 seed_memories_example.py 创建"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/export/memories")
async def export_memories():
    """
    导出所有记忆为 JSON（用于备份或迁移）
    浏览器访问这个地址会直接触发下载一个 .json 文件
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}

    try:
        memories = await get_all_memories()
        for mem in memories:
            if mem.get("created_at"):
                mem["created_at"] = str(mem["created_at"])

        payload = {
            "total": len(memories),
            "exported_at": str(__import__("datetime").datetime.now()),
            "memories": memories,
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        export_filename = f"memories_export_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{export_filename}"',
            },
        )
    except Exception as e:
        return {"error": str(e)}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Dashboard - 整合的记忆管理界面"""
    if not MEMORY_ENABLED:
        return HTMLResponse("<h3>记忆系统未启用（设置 MEMORY_ENABLED=true 开启）</h3>")
    
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/api/push/status")
async def api_push_status():
    """主动推送投递状态（脱敏，只用于Dashboard展示）"""
    if not MEMORY_ENABLED:
        return {"enabled": False, "reason": "memory_disabled"}
    return await get_push_delivery_status(get_active_session_id())


@app.get("/api/summary/health")
async def api_summary_health():
    """脱敏摘要健康状态；不返回摘要或聊天正文。"""
    session_id = get_active_session_id()
    if not session_id:
        return {"enabled": False, "reason": "session_not_configured"}
    health = await get_summary_health_status(session_id)
    state = await get_session_cache_state(session_id)
    history = await get_conversation_messages(session_id, limit=10000)
    messages = []
    for row in history:
        message = db_row_to_message(row)
        message["created_at"] = row.get("created_at")
        if message.get("role") == "tool":
            previous = messages[-1] if messages else None
            if not previous or not (
                previous.get("role") == "tool"
                or (previous.get("role") == "assistant" and previous.get("tool_calls"))
            ):
                continue
        messages.append(message)
    total_rounds = len(group_by_rounds(messages))
    a_start_round = int(state.get("a_start_round", 0))
    unprocessed_rounds = max(0, total_rounds - a_start_round)
    ready_rounds = max(0, unprocessed_rounds - CACHE_PARTITION_X)
    failures = int(health.get("consecutive_failures", 0) or 0)
    return {
        "enabled": True,
        "status": "warning" if failures > 0 else "healthy",
        "consecutive_failures": failures,
        "last_error_code": health.get("last_error_code") or "",
        "last_attempt_at": _format_dashboard_time(health.get("last_attempt_at")),
        "last_success_at": _format_dashboard_time(health.get("last_success_at")),
        "last_failure_at": _format_dashboard_time(health.get("last_failure_at")),
        "last_alert_at": _format_dashboard_time(health.get("last_alert_at")),
        "alert_active": bool(health.get("alert_active", False)),
        "last_message_count": int(health.get("last_message_count", 0) or 0),
        "model": health.get("last_model") or CACHE_SUMMARY_MODEL,
        "summary_parts": len(state.get("summary_parts", [])),
        "summary_chars": sum(len(part) for part in state.get("summary_parts", [])),
        "a_start_round": a_start_round,
        "total_rounds": total_rounds,
        "unprocessed_rounds": unprocessed_rounds,
        "ready_rounds": ready_rounds,
    }


@app.get("/api/system/health")
async def api_system_health():
    """统一脱敏健康状态，不读取或返回业务正文。"""
    rows = {row["component"]: row for row in await list_operational_health_status()}
    components = []
    for component, label in HEALTH_COMPONENT_LABELS.items():
        row = rows.get(component, {})
        components.append({
            "component": component,
            "label": label,
            "status": row.get("status") or "unknown",
            "consecutive_failures": int(row.get("consecutive_failures", 0) or 0),
            "last_success_at": _format_dashboard_time(row.get("last_success_at")),
            "last_failure_at": _format_dashboard_time(row.get("last_failure_at")),
            "last_error_code": row.get("last_error_code") or "",
            "last_alert_at": _format_dashboard_time(row.get("last_alert_at")),
            "alert_active": bool(row.get("alert_active", False)),
        })
    latest_io = await get_latest_io_received_at()
    io_age_minutes = None
    io_stale = False
    if latest_io:
        io_age_minutes = max(
            0,
            int((datetime.now(timezone.utc) - latest_io).total_seconds() // 60),
        )
        io_stale = io_age_minutes > 90
    return {
        "components": components,
        "io": {
            "last_received_at": _format_dashboard_time(latest_io),
            "age_minutes": io_age_minutes,
            "stale": io_stale,
            "stale_after_minutes": 90,
        },
        "warning_count": sum(
            1 for item in components if item["status"] == "failing"
        ) + (1 if io_stale else 0),
    }


@app.get("/api/memory/extraction/recent")
async def api_memory_extraction_recent(limit: int = 12):
    """最近提取出的记忆预览，只读、不改写记忆。"""
    limit = max(1, min(int(limit or 12), 30))
    rows = await get_recent_memories_detail(limit)
    items = []
    for row in rows:
        content = str(row.get("content") or "")
        items.append({
            "id": row.get("id"),
            "title": row.get("title") or f"记忆 #{row.get('id')}",
            "content": content,
            "importance": int(row.get("importance", 0) or 0),
            "source_session": row.get("source_session") or "",
            "created_at": _format_dashboard_time(row.get("created_at")),
            "layer": row.get("layer"),
            "is_active": bool(row.get("is_active", True)),
            "content_chars": len(content),
        })
    return {
        "items": items,
        "total": len(items),
        "note": "这里只展示最近写入的记忆内容预览，不包含聊天正文和提取 prompt。",
    }


@app.get("/api/io/context/recent")
async def api_io_context_recent(limit: int = 12):
    """最近收到的 io 感知事件，只返回类型级别明细。"""
    limit = max(1, min(int(limit or 12), 30))
    rows = await get_recent_io_context_events(limit)
    items = []
    category_counts = {}
    for row in rows:
        event_type = str(row.get("event_type") or "")
        category = event_type.split(".", 1)[0] if "." in event_type else event_type
        category_counts[category] = category_counts.get(category, 0) + 1
        payload = _io_payload_value(row.get("payload"))
        payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
        payload_preview = payload_json if len(payload_json) <= 240 else payload_json[:237] + "..."
        chat_preview = _format_io_chat_preview(event_type, payload, row.get("timezone") or "")
        items.append({
            "id": row.get("id"),
            "device_hash": _short_hash_text(str(row.get("device_id") or "")),
            "source_client": row.get("source_client") or "",
            "event_type": event_type,
            "category": category,
            "observed_at": _format_dashboard_time(row.get("observed_at")),
            "timezone": row.get("timezone") or "",
            "permission_state": row.get("permission_state") or "",
            "schema_version": int(row.get("schema_version") or 0),
            "payload_preview": payload_preview,
            "payload_details": _format_io_payload_details(payload),
            "chat_preview": chat_preview,
            "chat_integration_enabled": False,
        })
    return {
        "items": items,
        "category_counts": category_counts,
        "total": len(items),
        "note": "这里只展示事件类型、原始输入摘要和加工后预览；当前仍未接入聊天。",
        "chat_integration_enabled": False,
    }


@app.get("/api/shadow/mind/status")
async def api_shadow_mind_status():
    """Shadow Mind A2 state; reads lazily settle elapsed time when enabled."""
    if not MEMORY_ENABLED:
        return {"enabled": False, "reason": "memory_disabled"}
    session_id = get_active_session_id()
    if SHADOW_MIND_RULES_ENABLED:
        try:
            now = datetime.now(timezone.utc)
            key = hashlib.sha256(f"silence:{session_id}:{now:%Y%m%d%H}".encode("utf-8")).hexdigest()[:32]
            await settle_shadow_mind_rules(session_id, "silence_elapsed", [], key, now)
        except Exception as shadow_error:
            print(
                "shadow_mind_settle_failed "
                f"event_type=silence_elapsed error_type={type(shadow_error).__name__}",
                flush=True,
            )
    state = await get_shadow_mind_state(session_id)
    events = await get_shadow_mind_a2_events(session_id, limit=50)
    history = await get_shadow_mind_history(session_id, limit=100)
    return {
        "enabled": SHADOW_MIND_RULES_ENABLED,
        "phase": "A2",
        "session_id": session_id,
        "shadow_mind_state": _shadow_mind_public_state(state),
        "event_log": _shadow_mind_a2_public_events(events),
        "history": _shadow_mind_public_history(history),
    }


@app.post("/api/shadow/mind/settle")
async def api_shadow_mind_settle():
    """Dashboard-only lazy debug settlement; no model or production side effects."""
    if not MEMORY_ENABLED:
        return {"enabled": False, "reason": "memory_disabled"}
    if not SHADOW_MIND_RULES_ENABLED:
        return {"enabled": False, "reason": "rules_disabled"}
    session_id = get_active_session_id()
    now = datetime.now(timezone.utc)
    key = hashlib.sha256(f"manual-silence:{session_id}:{now:%Y%m%d%H}".encode("utf-8")).hexdigest()[:32]
    result = await settle_shadow_mind_rules(session_id, "silence_elapsed", [], key, now)
    return {
        "enabled": True,
        "changed": bool(result.get("changed")),
        "duplicate": bool(result.get("duplicate")),
    }


@app.post("/api/shadow/mind/recompute")
async def api_shadow_mind_recompute():
    """Compatibility route name; A2 remains the only enabled settlement path."""
    return await api_shadow_mind_settle()


@app.get("/api/shadow/mind/events/{event_id}/messages")
async def api_shadow_mind_event_messages(event_id: int):
    if not MEMORY_ENABLED:
        return {"enabled": False, "reason": "memory_disabled", "messages": []}
    rows = await get_shadow_mind_event_source_messages(get_active_session_id(), event_id)
    return {
        "messages": [
            {
                "id": row.get("id"),
                "role": row.get("role", ""),
                "content": row.get("content", ""),
                "created_at": row["created_at"].isoformat() if hasattr(row.get("created_at"), "isoformat") else row.get("created_at"),
            }
            for row in rows
        ]
    }


@app.post("/api/conversations/{session_id}/messages")
async def add_message_to_conversation(session_id: str, request: Request):
    """手动向对话线插入一条消息（支持自定义时间）"""
    data = await request.json()
    role = data.get("role", "user")
    content = data.get("content", "").strip()
    model = data.get("model", "manual")
    created_at_str = data.get("created_at")  # 可选：ISO格式时间字符串
    
    if not content:
        return {"error": "content 不能为空"}
    if role not in ("user", "assistant"):
        return {"error": "role 只能是 user 或 assistant"}
    
    # 解析时间
    from datetime import datetime, timezone
    if created_at_str:
        try:
            # 处理前端可能传来的各种格式：
            # - "2026-06-23T14:30:00.000Z"  -> 去掉 Z
            # - "2026-06-23 14:30:00"      -> 把空格换成 T
            dt_str = created_at_str.replace('Z', '').replace(' ', 'T')
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            print(f"⚠️ 时间解析失败: {e}, 原始值: {created_at_str}")
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    
    # 调用 database 里的新函数
    from database import save_message_with_time
    await save_message_with_time(session_id, role, content, model, dt)
    
    return {"status": "ok", "session_id": session_id, "role": role, "created_at": dt.isoformat()}


# ============================================================
# 管理 API
# ============================================================

@app.get("/api/memories")
async def api_get_memories(layer: int = None, active_only: bool = None):
    """获取所有记忆（管理页面用）
    
    Query params:
        layer: 筛选层级（1=碎片, 2=事件, 3=核心）
        active_only: 是否只返回活跃记忆
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    memories = await get_all_memories_detail(layer=layer, active_only=active_only)
    tz_offset = timezone(timedelta(hours=TIMEZONE_HOURS))
    for m in memories:
        if m.get("created_at"):
            dt = m["created_at"]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            m["created_at"] = dt.astimezone(tz_offset).strftime("%Y-%m-%d %H:%M:%S")
    # 获取层级统计
    try:
        layer_stats = await get_layer_statistics()
    except Exception:
        layer_stats = None
    
    result = {"memories": memories}
    if layer_stats:
        result["layer_stats"] = layer_stats
    return result


@app.get("/api/memories/search")
async def api_search_memories(q: str = "", limit: int = 20):
    """语义搜索记忆（Dashboard用，走后端 search_memories）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if not q.strip():
        return {"error": "搜索关键词不能为空", "results": []}
    try:
        results = await search_memories(q.strip(), limit)
        tz_offset = timezone(timedelta(hours=TIMEZONE_HOURS))
        out = []
        for r in results:
            item = dict(r)
            if item.get("created_at"):
                dt = item["created_at"]
                if hasattr(dt, 'tzinfo'):
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    item["created_at"] = dt.astimezone(tz_offset).strftime("%Y-%m-%d %H:%M:%S")
            out.append(item)
        return {"results": out, "total": len(out)}
    except Exception as e:
        return {"error": str(e), "results": []}


# ============================================================
# 共同经历卡片草稿（Dashboard 审核；Phase 1 不参与聊天召回）
# ============================================================

_INSPECTOR_SOURCES = {
    "memories", "summary_parts", "approved_experience_cards",
    "pending_experience_cards",
}


@app.post("/api/memory-inspector/search")
async def api_memory_inspector_search(request: Request):
    """Read-only retrieval preview; never participates in production chat."""
    started = time.monotonic()
    try:
        data = await request.json()
        query = str(data.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query_required")
        if len(query) > 500:
            raise HTTPException(status_code=400, detail="query_too_long")
        requested = data.get("sources") or ["memories"]
        if not isinstance(requested, list):
            raise HTTPException(status_code=400, detail="sources_must_be_list")
        sources = [str(item) for item in requested if str(item) in _INSPECTOR_SOURCES]
        if not sources:
            raise HTTPException(status_code=400, detail="valid_source_required")
        limit = max(1, min(int(data.get("limit") or 10), 30))
        keywords = _db_module.extract_search_keywords(query)
        results = []

        if "memories" in sources:
            matches = await search_memories(
                query, limit, touch_accessed=False, log_diagnostics=False
            )
            ids = [int(item["id"]) for item in matches]
            details = {
                int(item["id"]): item
                for item in await get_memories_by_ids_readonly(ids)
            }
            for match in matches:
                item = details.get(int(match["id"]), {})
                content = str(match.get("content") if isinstance(match, dict) else match["content"])
                terms = matched_terms(query, keywords, content)
                results.append(make_result(
                    result_type="memory",
                    item_id=int(match["id"]),
                    title=item.get("title") or f"记忆 #{match['id']}",
                    content=content,
                    score=float(match["score"]),
                    terms=terms,
                    source_session_id=item.get("source_session") or "",
                    ai_visible=bool(item.get("is_active", True)),
                    review_status="active" if item.get("is_active", True) else "archived",
                ))

        if "summary_parts" in sources:
            session_id = get_active_session_id()
            state = await get_session_cache_state(session_id) if session_id else {"summary_parts": []}
            summary_results = []
            for index, part in enumerate(state.get("summary_parts") or [], start=1):
                score, terms = lexical_score(query, keywords, part)
                if score <= 0:
                    continue
                summary_results.append(make_result(
                    result_type="summary_part",
                    item_id=index,
                    title=f"近期摘要片段 {index}",
                    content=part,
                    score=score,
                    terms=terms,
                    source_session_id=session_id,
                    ai_visible=True,
                    review_status="active",
                ))
            results.extend(sorted(summary_results, key=lambda item: -item["score"])[:limit])

        card_sources = {
            "approved_experience_cards": "approved",
            "pending_experience_cards": "pending",
        }
        for source_name, status in card_sources.items():
            if source_name not in sources:
                continue
            card_results = []
            for card in await list_experience_cards(status, 500):
                searchable = "\n".join([
                    str(card.get("title") or ""),
                    str(card.get("event_summary") or ""),
                    str(card.get("interaction_trace") or ""),
                    "\n".join(card.get("key_details") or []),
                ])
                score, terms = lexical_score(query, keywords, searchable)
                if score <= 0:
                    continue
                card_results.append(make_result(
                    result_type="experience_card",
                    item_id=int(card["id"]),
                    title=card.get("title") or "共同经历",
                    content="\n".join(filter(None, [
                        card.get("event_summary"), card.get("interaction_trace")
                    ])),
                    score=score,
                    terms=terms,
                    source_session_id=card.get("source_session_id") or "",
                    source_message_ids=card.get("source_message_ids") or [],
                    ai_visible=bool(card.get("ai_visible")),
                    review_status=card.get("review_status") or "",
                    key_details=card.get("key_details") or [],
                ))
            results.extend(sorted(card_results, key=lambda item: -item["score"])[:limit])

        results.sort(key=lambda item: (-item["score"], item["type"], str(item["id"])))
        preview = build_injection_preview(results)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(
            "[memory-inspector] succeeded "
            f"source_count={len(sources)} hit_count={len(results)} elapsed_ms={elapsed_ms}"
        )
        return {
            "status": "ok",
            "query_saved": False,
            "production_retrieval_changed": False,
            "sources": sources,
            "results": results,
            "hit_count": len(results),
            "elapsed_ms": elapsed_ms,
            "injection_preview": preview,
        }
    except HTTPException:
        raise
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(
            "[memory-inspector] failed "
            f"elapsed_ms={elapsed_ms} error_type={type(exc).__name__}"
        )
        raise HTTPException(status_code=500, detail="memory_inspector_failed") from exc

@app.get("/api/experience-cards")
async def api_list_experience_cards(status: str = None, limit: int = 500):
    if status and status not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail="invalid_review_status")
    cards = await list_experience_cards(status, max(1, min(limit, 1000)))
    return {"cards": cards, "total": len(cards), "retrieval_enabled": False}


@app.get("/api/experience-cards/{card_id}")
async def api_get_experience_card(card_id: int):
    card = await get_experience_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="experience_card_not_found")
    source_messages = await get_experience_card_source_messages(card_id)
    return {"card": card, "source_messages": source_messages, "retrieval_enabled": False}


@app.put("/api/experience-cards/{card_id}")
async def api_update_experience_card(card_id: int, request: Request):
    try:
        update = normalize_card_update(await request.json())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not update:
        raise HTTPException(status_code=400, detail="empty_update")
    card = await update_experience_card(card_id, update)
    if not card:
        raise HTTPException(status_code=404, detail="experience_card_not_found")
    return {"status": "ok", "card": card, "retrieval_enabled": False}


@app.delete("/api/experience-cards/{card_id}")
async def api_delete_experience_card(card_id: int):
    card = await update_experience_card(card_id, soft_delete_card_update())
    if not card:
        raise HTTPException(status_code=404, detail="experience_card_not_found")
    return {"status": "ok", "card": card}


@app.post("/api/experience-cards/{card_id}/restore")
async def api_restore_experience_card(card_id: int):
    card = await update_experience_card(card_id, restore_card_update())
    if not card:
        raise HTTPException(status_code=404, detail="experience_card_not_found")
    return {"status": "ok", "card": card}


def _clean_experience_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines)
    return json.loads(text)


async def _run_experience_generation(operation: str, job_id: str, session_id: str,
                                     messages: list[dict], source_card: dict = None):
    message_ids = [int(item["id"]) for item in messages]
    started = time.monotonic()
    try:
        job, created = await begin_experience_generation_job(
            job_id, operation, session_id, message_ids, source_card["id"] if source_card else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not created:
        return {"status": job["status"], "job_id": job_id,
                "card_ids": list(job.get("result_card_ids") or [])}
    try:
        if not MEMORY_API_BASE_URL or not MEMORY_API_KEY or not MEMORY_MODEL:
            raise ValueError("memory_config_missing")
        body = {"model": MEMORY_MODEL,
                "messages": [{"role": "user", "content": build_generation_prompt(messages, operation)}],
                "temperature": 0.3, "top_p": 0.9, "stream": False}
        _apply_memory_thinking_option(body)
        async with httpx.AsyncClient(timeout=240.0) as client:
            response = await client.post(MEMORY_API_BASE_URL,
                headers={"Authorization": f"Bearer {MEMORY_API_KEY}", "Content-Type": "application/json"},
                json=body)
        response.raise_for_status()
        response_data = response.json()
        usage = response_data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        if any(value is not None for value in (prompt_tokens, completion_tokens, total_tokens)):
            try:
                await save_token_usage(
                    session_id,
                    MEMORY_MODEL,
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    int(total_tokens or ((prompt_tokens or 0) + (completion_tokens or 0))),
                    usage_type="experience_card_generation",
                )
            except Exception as usage_exc:
                print(
                    "[experience-generation] token_log_failed "
                    f"error_type={type(usage_exc).__name__}"
                )
        content = ((response_data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        cards = validate_generated_cards(_clean_experience_json(content), set(message_ids))
        card_ids = await complete_experience_generation_job(job_id, cards, MEMORY_MODEL)
        print(
            "[experience-generation] succeeded "
            f"operation={operation} message_id_start={min(message_ids)} "
            f"message_id_end={max(message_ids)} message_count={len(message_ids)} "
            f"card_count={len(card_ids)} model={MEMORY_MODEL} "
            f"prompt_tokens={prompt_tokens if prompt_tokens is not None else 'not_reported'} "
            f"completion_tokens={completion_tokens if completion_tokens is not None else 'not_reported'} "
            f"total_tokens={total_tokens if total_tokens is not None else 'not_reported'} "
            f"elapsed_ms={int((time.monotonic() - started) * 1000)}"
        )
        return {"status": "succeeded", "job_id": job_id, "card_ids": card_ids}
    except Exception as exc:
        reason = type(exc).__name__
        await fail_experience_generation_job(job_id, reason)
        print(
            "[experience-generation] failed "
            f"operation={operation} message_id_start={min(message_ids)} "
            f"message_id_end={max(message_ids)} message_count={len(message_ids)} "
            f"model={MEMORY_MODEL or 'not_configured'} "
            f"elapsed_ms={int((time.monotonic() - started) * 1000)} error_type={reason}"
        )
        raise HTTPException(status_code=502, detail=reason) from exc


async def _experience_card_auto_tick() -> dict:
    """Process at most one quiet, unprocessed batch from the active session."""
    session_id = get_active_session_id()
    if not session_id:
        return {"status": "skipped", "reason": "active_session_missing"}
    messages = await claim_experience_auto_batch(
        session_id,
        EXPERIENCE_CARD_AUTO_SILENCE_MINUTES,
        EXPERIENCE_CARD_AUTO_BATCH_LIMIT,
    )
    if not messages:
        return {"status": "idle", "reason": "no_eligible_quiet_batch"}
    until_id = int(messages[-1]["id"])
    if not is_basic_experience_candidate(messages):
        await finish_experience_auto_batch(session_id, until_id, True)
        await _record_operational_success_safe("experience_cards")
        print(
            "[experience-auto] skipped_basic "
            f"message_count={len(messages)} until_id={until_id}"
        )
        return {"status": "skipped", "reason": "basic_event_filter"}
    fingerprint = hashlib.sha256(
        f"{session_id}:{messages[0]['id']}:{until_id}".encode("utf-8")
    ).hexdigest()[:24]
    job_id = f"auto-{fingerprint}"
    try:
        result = await _run_experience_generation(
            "auto_generate", job_id, session_id, messages
        )
        if result.get("status") != "succeeded":
            raise RuntimeError("auto_generation_not_succeeded")
        await finish_experience_auto_batch(session_id, until_id, True)
        await _record_operational_success_safe("experience_cards")
        print(
            "[experience-auto] succeeded "
            f"message_count={len(messages)} card_count={len(result.get('card_ids') or [])}"
        )
        return result
    except Exception as exc:
        await finish_experience_auto_batch(session_id, until_id, False)
        await _record_operational_failure_safe(
            "experience_cards",
            f"exception_{type(exc).__name__}",
        )
        print(f"[experience-auto] failed error_type={type(exc).__name__}")
        return {"status": "failed", "reason": type(exc).__name__}


async def _experience_card_auto_loop():
    while True:
        try:
            await _experience_card_auto_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[experience-auto] loop_error error_type={type(exc).__name__}")
        await asyncio.sleep(max(1, EXPERIENCE_CARD_AUTO_POLL_MINUTES) * 60)


@app.post("/api/experience-cards/source-preview")
async def api_experience_source_preview(request: Request):
    data = await request.json()
    session_id = str(data.get("source_session_id") or "").strip()
    recent_n = max(1, min(int(data.get("recent_n") or 16), 100))
    start_id, end_id = data.get("start_id"), data.get("end_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="source_session_id_required")
    if bool(start_id) != bool(end_id):
        raise HTTPException(status_code=400, detail="start_and_end_id_required_together")
    messages = await get_experience_source_messages(
        session_id, start_id=int(start_id) if start_id else None,
        end_id=int(end_id) if end_id else None,
        recent_n=None if start_id and end_id else recent_n)
    return {"messages": messages, "count": len(messages)}


@app.post("/api/experience-cards/generate")
async def api_generate_experience_cards(request: Request):
    data = await request.json()
    session_id = str(data.get("source_session_id") or "").strip()
    job_id = str(data.get("job_id") or "").strip()
    ids = [int(value) for value in data.get("source_message_ids") or []]
    if not session_id or not job_id or not ids:
        raise HTTPException(status_code=400, detail="generation_fields_required")
    messages = await get_experience_source_messages(session_id, message_ids=ids)
    if {item["id"] for item in messages} != set(ids):
        raise HTTPException(status_code=400, detail="source_messages_invalid")
    return await _run_experience_generation("manual_generate", job_id, session_id, messages)


@app.get("/api/experience-card-jobs/{job_id}")
async def api_get_experience_generation_job(job_id: str):
    job = await get_experience_generation_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="experience_card_job_not_found")
    return {"job": job}


@app.post("/api/experience-cards/{card_id}/approve-replacement")
async def api_approve_experience_replacement(card_id: int):
    try:
        cards = await approve_experience_replacement(card_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok", "cards": cards, "count": len(cards)}


@app.post("/api/experience-cards/{card_id}/{operation}")
async def api_reprocess_experience_card(card_id: int, operation: str, request: Request):
    if operation not in {"regenerate", "split"}:
        raise HTTPException(status_code=404, detail="operation_not_found")
    card = await get_experience_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="experience_card_not_found")
    messages = await get_experience_card_source_messages(card_id)
    if not messages:
        raise HTTPException(status_code=400, detail="source_messages_missing")
    job_id = str((await request.json()).get("job_id") or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id_required")
    return await _run_experience_generation(operation, job_id, card["source_session_id"], messages, card)


@app.put("/api/memories/{memory_id}")
async def api_update_memory(memory_id: int, request: Request):
    """更新单条记忆（支持 content / importance / title / layer）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    await update_memory_with_layer(
        memory_id,
        content=data.get("content"),
        importance=data.get("importance"),
        title=data.get("title"),
        layer=data.get("layer"),
    )
    return {"status": "ok", "id": memory_id}


@app.delete("/api/memories/{memory_id}")
async def api_delete_memory(memory_id: int, soft: bool = False):
    """删除单条记忆
    
    Query params:
        soft: true=归档（is_active=false），false=永久删除
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if soft:
        await update_memory_with_layer(memory_id, is_active=False)
    else:
        await delete_memory(memory_id)
    return {"status": "ok", "id": memory_id}


@app.post("/api/memories/batch-update")
async def api_batch_update(request: Request):
    """批量更新记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    updates = data.get("updates", [])
    if not updates:
        return {"error": "没有要更新的记忆"}
    for item in updates:
        await update_memory_with_layer(
            item["id"],
            content=item.get("content"),
            importance=item.get("importance"),
            title=item.get("title"),
            layer=item.get("layer"),
        )
    return {"status": "ok", "updated": len(updates)}


@app.post("/api/memories/batch-delete")
async def api_batch_delete(request: Request):
    """批量删除记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        return {"error": "未选择记忆"}
    await delete_memories_batch(ids)
    return {"status": "ok", "deleted": len(ids)}


# ============================================================
# 三层记忆架构：整理 / 合并 / 升级 / 统计
# ============================================================

CONSOLIDATION_PROMPT = """
你是记忆整理助手。请将以下对话碎片整理成完整的事件记录。

要求：
1. 按主题/事件分组，相关的碎片合并到一起
2. 每个事件一条记录，不要太细碎也不要太笼统
3. 每条记录包含：标题（10字内）+ 完整描述
4. 合并重复内容，保留重要细节
5. 用遥遥的第一人称口吻写（像是遥遥自己在回忆和小猫的这些互动），保留原文中的主观感受、
   情绪表达和个人化用语，不要改写为客观陈述或第三方总结
   转换示例：
    原文——用户："我今天去打拳击了，膝盖有点疼。" AI："好好休息，冰敷一下膝盖哦。"
    应写成——"Sasa今天去打拳击了，膝盖有点疼，我叮嘱她冰敷休息。"
    （用户说的"我"要转换成"Sasa"，AI说的"我"才是遥遥自己的"我"，两者不能混）
6. content字段中不要使用双引号，用单引号或书名号代替

碎片记忆：
{fragments}

请用 JSON 格式输出：
[
  {{
    "title": "事件标题（10字内）",
    "content": "完整的事件描述",
    "importance": 5,
    "merged_ids": [1, 2, 3]
  }}
]

只输出 JSON，不要其他内容。确保 JSON 语法正确。
"""

# 整理状态（异步执行，防重入）
_consolidate_status = {
    "running": False,
    "started_at": None,
    "result": None,
    "error": None,
}


async def consolidate_memories_for_date(event_date):
    """整理指定日期的碎片记忆"""
    return await consolidate_memories_for_date_range(event_date, event_date)


async def consolidate_memories_for_date_range(start_date, end_date):
    """整理指定时间段的碎片记忆"""
    from datetime import date
    import re
    
    # 获取该时间段的碎片
    fragments = await get_fragments_by_date_range(start_date, end_date)
    
    if not fragments:
        return {"status": "no_fragments", "start_date": str(start_date), "end_date": str(end_date)}
    
    # 构建碎片文本
    fragments_text = "\n".join([
        f"[ID={f['id']}] ({f['created_at'].strftime('%m-%d') if hasattr(f['created_at'], 'strftime') else str(f['created_at'])[:10]}) {f['content']}"
        for f in fragments
    ])
    
    # 调用 AI 进行整理
    prompt = CONSOLIDATION_PROMPT.format(fragments=fragments_text)
    
    # 使用独立记忆模型配置，避免混用聊天主接口。
    memory_config, missing = get_memory_config()
    if missing:
        print(f"⚠️ 整理记忆配置缺失: memory_config_missing missing={','.join(missing)}")
        return {"status": "error", "error": "memory_config_missing"}
    consolidation_model = memory_config["model"]
    memory_url = memory_config["base_url"]
    memory_key = memory_config["api_key"]
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # 最多重试2次（应对429限流）
            last_error = None
            for attempt in range(3):
                payload = {
                    "model": consolidation_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 3000
                }
                _apply_memory_thinking_option(payload)
                response = await client.post(
                    memory_url,
                    headers={
                        "Authorization": f"Bearer {memory_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload
                )

                if response.status_code == 429:
                    wait_time = (attempt + 1) * 10
                    print(f"⚠️ 整理API 429限流，{wait_time}秒后重试（第{attempt+1}次）")
                    last_error = f"429 Too Many Requests (重试{attempt+1}次)"
                    await asyncio.sleep(wait_time)
                    continue

                if response.status_code != 200:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    print(f"⚠️ 整理API返回 {response.status_code}: body_chars={len(response.text or str())} body_hash={_short_hash_text(response.text or str())}")
                    break

                last_error = None
                break

            if last_error:
                return {"status": "error", "error": f"API调用失败: {last_error}"}

            data = response.json()
            content = _extract_chat_completion_text(data).strip()
            choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
            finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
            prompt_tokens, completion_tokens, total_tokens = _extract_usage_tokens(data)
            if not content:
                print(
                    "⚠️ 整理记忆模型返回空内容: "
                    f"finish_reason={finish_reason or 'not_reported'} "
                    f"prompt_tokens={prompt_tokens} completion_tokens={completion_tokens} total_tokens={total_tokens}",
                    flush=True,
                )
                return {"status": "error", "error": "memory_consolidation_empty_response"}
            
            # 解析 JSON（三层容错）
            json_match = re.search(r'\[[\s\S]*\]', content)
            if json_match:
                json_str = json_match.group()
                try:
                    events = json.loads(json_str)
                except json.JSONDecodeError:
                    # 方案1：用 strict=False
                    try:
                        events = json.loads(json_str, strict=False)
                    except json.JSONDecodeError:
                        # 方案2：去掉控制字符后重试
                        cleaned = re.sub(r'[\x00-\x1f\x7f]', ' ', json_str)
                        try:
                            events = json.loads(cleaned)
                        except json.JSONDecodeError as e:
                            # 方案3：让 AI 重新格式化
                            print(f"⚠️ JSON解析失败，尝试让AI修复: {e}")
                            fix_payload = {
                                "model": consolidation_model,
                                "messages": [{"role": "user", "content": f"请修复以下JSON的语法错误，只输出修复后的JSON数组，不要其他内容：\n{json_str[:2000]}"}],
                                "max_tokens": 2000
                            }
                            _apply_memory_thinking_option(fix_payload)
                            fix_resp = await client.post(
                                memory_url,
                                headers={
                                    "Authorization": f"Bearer {memory_key}",
                                    "Content-Type": "application/json"
                                },
                                json=fix_payload
                            )
                            if fix_resp.status_code == 200:
                                fix_content = fix_resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                                fix_match = re.search(r'\[[\s\S]*\]', fix_content)
                                if fix_match:
                                    try:
                                        events = json.loads(fix_match.group())
                                        print(f"✅ AI修复JSON成功")
                                    except json.JSONDecodeError:
                                        return {"status": "error", "error": f"JSON解析失败（AI修复也失败）", "raw": content[:500]}
                                else:
                                    return {"status": "error", "error": "AI修复未返回有效JSON", "raw": content[:500]}
                            else:
                                return {"status": "error", "error": f"JSON解析失败，AI修复请求失败: HTTP {fix_resp.status_code}", "raw": content[:500]}
            else:
                return {"status": "error", "error": "无法解析 AI 返回的 JSON", "raw": content}
            
            # 创建事件记忆并停用碎片
            created_count = 0
            for event in events:
                merged_ids = event.get("merged_ids", [])
                if merged_ids:
                    await create_event_memory(
                        title=event.get("title", ""),
                        content=event.get("content", ""),
                        importance=event.get("importance", 5),
                        event_date=start_date,
                        merged_from=merged_ids
                    )
                    created_count += 1
            
            # 停用所有已处理的碎片
            all_fragment_ids = [f['id'] for f in fragments]
            await deactivate_memories(all_fragment_ids)
            
            return {
                "status": "ok",
                "start_date": str(start_date),
                "end_date": str(end_date),
                "fragments_processed": len(fragments),
                "events_created": created_count
            }
            
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/memories/consolidate")
async def api_manual_consolidate(request: Request):
    """手动触发整理（异步，立即返回）
    
    Body:
        start_date: 开始日期（YYYY-MM-DD 格式）
        end_date: 结束日期（YYYY-MM-DD 格式）
        或
        date: 单个日期（兼容旧版）
    """
    from datetime import date as date_type
    
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    if _consolidate_status.get("running"):
        return {"status": "already_running", "started_at": _consolidate_status.get("started_at")}
    
    data = await request.json()
    
    # 解析日期参数
    if "date" in data and "start_date" not in data:
        start_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        end_date = start_date
    else:
        start_date_str = data.get("start_date")
        end_date_str = data.get("end_date")
        
        if not start_date_str or not end_date_str:
            return {"error": "请提供开始和结束日期"}
        
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        
        if start_date > end_date:
            return {"error": "开始日期不能晚于结束日期"}
    
    async def _run():
        _consolidate_status.update({"running": True, "started_at": f"{start_date}~{end_date}", "result": None, "error": None})
        try:
            result = await consolidate_memories_for_date_range(start_date, end_date)
            _consolidate_status["result"] = result
            print(f"[manual/consolidate] 整理 {start_date}~{end_date}: {result}")
        except Exception as e:
            _consolidate_status["error"] = str(e)
            print(f"[manual/consolidate] 整理 {start_date}~{end_date} 失败: {e}")
        finally:
            _consolidate_status["running"] = False
    
    asyncio.create_task(_run())
    return {"status": "started", "start_date": str(start_date), "end_date": str(end_date)}


@app.get("/api/memories/consolidate/status")
async def api_consolidate_status():
    """查询整理任务状态"""
    return _consolidate_status


@app.post("/api/memories/{memory_id}/promote")
async def api_promote_to_core(memory_id: int, request: Request):
    """将记忆升级为核心记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    title = data.get("title")
    
    await promote_to_core(memory_id, title=title)
    return {"status": "ok", "memory_id": memory_id, "layer": 3}


@app.post("/api/memories/merge")
async def api_merge_memories(request: Request):
    """手动合并多条记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    memory_ids = data.get("ids", [])
    new_title = data.get("title", "")
    new_content = data.get("content", "")
    importance = data.get("importance", 5)
    layer = data.get("layer", 2)
    
    if not memory_ids or not new_content:
        return {"error": "请提供记忆ID列表和合并后内容"}
    
    new_id = await merge_memories(memory_ids, new_title, new_content, importance, layer)
    return {"status": "ok", "new_id": new_id, "merged": len(memory_ids)}


@app.post("/api/memories/check-duplicate")
async def api_check_duplicate(request: Request):
    """检查记忆是否重复"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    content = data.get("content", "")
    threshold = data.get("threshold", 0.7)
    
    if not content:
        return {"error": "请提供记忆内容"}
    
    result = await check_duplicate_memory(content, threshold)
    return result


@app.post("/api/memories/cleanup-fragments")
async def api_cleanup_fragments(request: Request):
    """清理指定天数前的归档碎片
    
    Body:
        days: 清理多少天前的归档碎片（默认30天）
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    days = data.get("days", 30)
    
    try:
        deleted = await cleanup_old_fragments(days)
        return {"status": "ok", "deleted": deleted, "days": days}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/memories/{memory_id}/revert-merge")
async def api_revert_merge(memory_id: int):
    """撤回合并操作：恢复原始碎片，删除合并后的事件记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    try:
        result = await revert_merge(memory_id)
        return result
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/memories/{memory_id}/restore")
async def api_restore_memory(memory_id: int):
    """恢复已归档的记忆（将 is_active 设为 TRUE）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    try:
        await update_memory_with_layer(memory_id, is_active=True)
        return {"status": "ok", "id": memory_id}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/memories/layer-stats")
async def api_layer_statistics():
    """获取各层记忆统计数据"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    try:
        stats = await get_layer_statistics()
        return stats
    except Exception as e:
        return {"error": str(e)}


@app.post("/import/text")
async def import_text_memories(request: Request):
    """从纯文本导入记忆（每行一条），可选自动评分"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        data = await request.json()
        lines = data.get("lines", [])
        skip_scoring = data.get("skip_scoring", False)
        
        if not lines:
            return {"error": "没有找到记忆条目"}
        
        if skip_scoring:
            scored = [{"content": t, "importance": 5} for t in lines]
        else:
            scored = await score_memories(lines)
        
        imported = 0
        skipped = 0
        
        for mem in scored:
            content = mem.get("content", "")
            if not content:
                continue
            
            pool = await get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            
            if existing > 0:
                skipped += 1
                continue
            
            await save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session="text-import",
            )
            imported += 1
        
        total = await get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/import/memories")
async def import_memories(request: Request):
    """从 JSON 导入记忆（用于迁移或恢复备份）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        data = await request.json()
        memories = data.get("memories", [])
        
        if not memories:
            return {"error": "没有找到记忆数据，请确认 JSON 格式正确"}
        
        imported = 0
        skipped = 0
        
        for mem in memories:
            content = mem.get("content", "")
            if not content:
                continue
            
            pool = await get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            
            if existing > 0:
                skipped += 1
                continue
            
            await save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session=mem.get("source_session", "json-import"),
            )
            imported += 1
        
        total = await get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 对话记录管理 API
# ============================================================

@app.get("/api/conversations")
async def api_conversations(page: int = 1, per_page: int = 20):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        results, total = await get_conversations_paginated(page, per_page)
        total_pages = max(1, -(-total // per_page))  # 向上取整
        return {"conversations": results, "total": total, "page": page, "per_page": per_page, "total_pages": total_pages}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/conversations/{session_id}/messages")
async def api_conversation_messages(session_id: str, limit: int = 50, offset: int = 0):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE session_id = $1", session_id
            )
            rows = await conn.fetch("""
                SELECT id, role, content, created_at
                FROM conversations WHERE session_id = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
            """, session_id, limit, offset)
        msgs = [{"id": r["id"], "role": r["role"], "content": r["content"], 
                 "created_at": r["created_at"].isoformat() if r.get("created_at") else None} for r in rows]
        return {"messages": msgs, "total": total}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/conversations/{session_id}")
async def api_delete_conversation(session_id: str):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        await delete_conversation(session_id)
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/conversations/batch-delete")
async def api_batch_delete(request: Request):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
        ids = body.get("session_ids", [])
        if ids:
            await batch_delete_conversations(ids)
        return {"status": "ok", "deleted": len(ids)}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/admin/merge-sessions")
async def api_merge_sessions(request: Request):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
        source_ids = [s for s in body.get("source_ids", []) if s != body.get("target_id", "")]
        target_id = body.get("target_id", "")
        if not source_ids or not target_id:
            return {"error": "source_ids 和 target_id 不能为空"}
        result = await merge_sessions_to_target(source_ids, target_id)
        return {"status": "ok", **result}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/chat/search")
async def api_search_conversations(q: str = "", limit: int = 20, offset: int = 0):
    """搜索对话内容"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if not q.strip():
        return {"error": "搜索关键词不能为空", "results": [], "total": 0}
    try:
        results, total = await search_conversations(q.strip(), limit, offset)
        return {"results": results, "total": total}
    except Exception as e:
        return {"error": str(e), "results": [], "total": 0}


@app.patch("/api/chat/messages/{message_id}")
async def api_update_message(message_id: int, request: Request):
    """编辑单条消息内容"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
        content = body.get("content", "").strip()
        if not content:
            return {"error": "内容不能为空"}
        updated = await update_message_content(message_id, content)
        if updated == 0:
            return {"error": "消息不存在"}
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/conversations/export")
async def api_export_conversations():
    """导出所有对话记录"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        data = await export_all_conversations()
        return JSONResponse(content=data)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/conversations/import")
async def api_import_conversations(request: Request):
    """导入对话记录（JSON格式，自动去重）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        records = await request.json()
        if not isinstance(records, list):
            return {"error": "格式错误：需要 JSON 数组"}
        imported, skipped = await import_conversations(records)
        return {"status": "ok", "imported": imported, "skipped": skipped, "total": imported + skipped}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/api/messages/{message_id}")
async def delete_message(message_id: int):
    """永久删除单条对话消息"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("DELETE FROM conversations WHERE id = $1", message_id)
            deleted = int(result.split()[-1]) if result else 0
            if deleted == 0:
                return JSONResponse(status_code=404, content={"error": "消息不存在"})
            return {"status": "ok", "deleted": deleted}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 对话线管理 API（分区缓存）
# ============================================================

@app.get("/api/partition/status")
async def api_partition_status():
    active_sid = get_active_session_id()
    state = await get_session_cache_state(active_sid) if active_sid else {}
    return {
        "enabled": CACHE_PARTITION_ENABLED,
        "active_session_id": active_sid,
        "partition_x": CACHE_PARTITION_X,
        "summary_model": CACHE_SUMMARY_MODEL,
        "summary": '\n\n'.join(state.get('summary_parts', [])),
        "summary_parts": state.get('summary_parts', []),
        "summary_count": len(state.get('summary_parts', [])),
        "summary_length": sum(len(p) for p in state.get('summary_parts', [])),
        "a_start_round": state.get('a_start_round', 0),
        "updated_at": state.get('updated_at').isoformat() if state.get('updated_at') else None,
    }


@app.get("/api/partition/threads")
async def api_partition_threads():
    threads = await list_all_session_cache_states()
    active_sid = get_active_session_id()
    for t in threads:
        t['is_active'] = (t['session_id'] == active_sid)
    if active_sid and not any(t['session_id'] == active_sid for t in threads):
        threads.insert(0, {'session_id': active_sid, 'summary': '', 'summary_length': 0, 'summary_count': 0, 'a_start_round': 0, 'updated_at': None, 'message_count': 0, 'chat_tokens': 0, 'is_active': True})
    return {"threads": threads, "active_session_id": active_sid}


@app.put("/api/partition/summary")
async def api_update_summary(request: Request):
    try:
        body = await request.json()
        sid = body.get("session_id", "")
        summary = body.get("summary", "")
        if not sid:
            return {"error": "session_id 不能为空"}
        state = await get_session_cache_state(sid)
        summary_parts = [summary] if isinstance(summary, str) and summary else summary if isinstance(summary, list) else []
        # 摘要清空时 a_start_round 也归零，否则历史会被跳过
        a_start = state.get('a_start_round', 0) if summary_parts else 0
        await save_session_cache_state(sid, summary_parts, a_start)
        total_len = sum(len(p) for p in summary_parts)
        return {"status": "ok", "summary_parts": len(summary_parts), "summary_length": total_len}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/partition/summary")
async def api_clear_summary(request: Request):
    try:
        body = await request.json()
        sid = body.get("session_id", "")
        if not sid:
            return {"error": "session_id 不能为空"}
        # 摘要和 a_start_round 一起归零
        await save_session_cache_state(sid, [], 0)
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/partition/thread")
async def api_create_thread(request: Request):
    try:
        body = await request.json()
        new_id = body.get("session_id", "").strip()
        copy_from = body.get("copy_summary_from", "")
        if not new_id:
            return {"error": "session_id 不能为空"}
        existing = await get_session_cache_state(new_id)
        if existing.get('updated_at'):
            return {"error": f"对话线 '{new_id}' 已存在"}
        summary_parts = []
        if copy_from:
            source = await get_session_cache_state(copy_from)
            summary_parts = source.get('summary_parts', [])
        await save_session_cache_state(new_id, summary_parts, 0)
        total_len = sum(len(p) for p in summary_parts)
        return {"status": "ok", "session_id": new_id, "summary_length": total_len}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/partition/switch")
async def api_switch_thread(request: Request):
    global PARTITION_SESSION_ID
    try:
        body = await request.json()
        new_id = body.get("session_id", "").strip()
        if not new_id:
            return {"error": "session_id 不能为空"}
        old_id = PARTITION_SESSION_ID
        PARTITION_SESSION_ID = new_id
        await set_gateway_config("partition_session_id", new_id)
        return {"status": "ok", "old_session_id": old_id, "new_session_id": new_id}
    except Exception as e:
        return {"error": str(e)}


@app.put("/api/partition/thread/rename")
async def api_rename_thread(request: Request):
    global PARTITION_SESSION_ID
    try:
        body = await request.json()
        old_id = body.get("old_id", "").strip()
        new_id = body.get("new_id", "").strip()
        if not old_id or not new_id:
            return {"error": "old_id 和 new_id 不能为空"}
        if old_id == new_id:
            return {"error": "新旧ID相同"}
        success = await rename_session_id(old_id, new_id)
        if not success:
            return {"error": f"对话线 '{new_id}' 已存在"}
        # 如果重命名的是活跃线，同步更新
        if PARTITION_SESSION_ID == old_id:
            PARTITION_SESSION_ID = new_id
            await set_gateway_config("partition_session_id", new_id)
        return {"status": "ok", "old_id": old_id, "new_id": new_id}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/partition/thread/{session_id:path}")
async def api_delete_thread(session_id: str):
    """删除对话线（不允许删除当前活跃线）"""
    try:
        active_sid = get_active_session_id()
        if session_id == active_sid:
            return {"error": "不能删除当前活跃的对话线"}
        await delete_session_cache_state(session_id)
        print(f"🗑️ 删除对话线: {session_id}")
        return {"status": "ok", "session_id": session_id}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 记忆向量补算（带进度追踪）
# ============================================================

_backfill_mem_status = {
    "running": False,
    "total": 0,
    "done": 0,
    "error": None,
    "finished_at": None,
}

@app.post("/api/admin/backfill-memory-embeddings")
async def api_backfill_memory_embeddings():
    """给已有记忆补算embedding（后台异步执行，前端轮询进度）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    if _backfill_mem_status["running"]:
        return {"error": "补算任务正在运行中，请等待完成"}
    
    try:
        total = await get_pending_memory_embedding_count()
    except Exception as e:
        return {"error": f"查询待处理数量失败: {e}"}
    
    if total == 0:
        return {"status": "done", "message": "所有记忆已有embedding，无需补算", "total": 0, "done": 0}
    
    _backfill_mem_status["running"] = True
    _backfill_mem_status["total"] = total
    _backfill_mem_status["done"] = 0
    _backfill_mem_status["error"] = None
    _backfill_mem_status["finished_at"] = None
    
    async def run_backfill():
        try:
            while _backfill_mem_status["running"]:
                updated = await backfill_memory_embeddings(batch_size=20)
                _backfill_mem_status["done"] += updated
                
                if updated == 0:
                    break
                
                await asyncio.sleep(1)
            
            _backfill_mem_status["finished_at"] = datetime.now(timezone.utc).isoformat()
            print(f"✅ 记忆embedding补算完成：{_backfill_mem_status['done']}/{_backfill_mem_status['total']}")
        except Exception as e:
            _backfill_mem_status["error"] = str(e)
            print(f"❌ 记忆embedding补算异常: {e}")
        finally:
            _backfill_mem_status["running"] = False
    
    asyncio.create_task(run_backfill())
    return {"status": "started", "total": total}

@app.get("/api/admin/backfill-memory-embeddings/status")
async def api_backfill_memory_embeddings_status():
    """查询记忆embedding补算进度"""
    return {
        "running": _backfill_mem_status["running"],
        "total": _backfill_mem_status["total"],
        "done": _backfill_mem_status["done"],
        "error": _backfill_mem_status["error"],
        "finished_at": _backfill_mem_status["finished_at"],
    }


# ============================================================
# 模型列表 API（/api/models）
# 设置面板的 combo-box 用，根据 API_BASE_URL 自动适配
# ============================================================

@app.get("/api/models")
async def get_models():
    """获取可用模型列表（根据 API_BASE_URL 自动适配）"""
    is_openrouter = "openrouter.ai" in API_BASE_URL
    is_google = "googleapis.com" in API_BASE_URL or "generativelanguage" in API_BASE_URL
    is_openai = "api.openai.com" in API_BASE_URL

    try:
        if is_openrouter:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {API_KEY}"}
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("data", [])
                    simplified = [{"id": m.get("id"), "name": m.get("name"), "context_length": m.get("context_length")} for m in models]
                    simplified.sort(key=lambda x: x.get("name", ""))
                    return {"models": simplified, "total": len(simplified), "provider": "openrouter"}

        elif is_google:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={API_KEY}"
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("models", [])
                    simplified = []
                    for m in models:
                        full_name = m.get("name", "")
                        model_id = full_name.replace("models/", "") if full_name.startswith("models/") else full_name
                        display_name = m.get("displayName", model_id)
                        supported_methods = m.get("supportedGenerationMethods", [])
                        if "generateContent" in supported_methods:
                            simplified.append({"id": model_id, "name": display_name, "context_length": m.get("inputTokenLimit"), "output_limit": m.get("outputTokenLimit")})
                    def sort_key(x):
                        name = x.get("id", "")
                        if "gemini-3" in name: return "0" + name
                        elif "gemini-2.5" in name: return "1" + name
                        elif "gemini-2.0" in name: return "2" + name
                        else: return "9" + name
                    simplified.sort(key=sort_key)
                    return {"models": simplified, "total": len(simplified), "provider": "google"}
                else:
                    print(f"[get_models] Google API 返回 {response.status_code}: {response.text}")
                    return {"error": f"Google API 返回 {response.status_code}", "models": [], "provider": "google"}

        elif is_openai:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {API_KEY}"}
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("data", [])
                    simplified = [{"id": m.get("id", ""), "name": m.get("id", "")} for m in models if m.get("id", "").startswith(("gpt-", "o1", "o3", "o4"))]
                    simplified.sort(key=lambda x: x.get("id", ""))
                    return {"models": simplified, "total": len(simplified), "provider": "openai"}
            openai_models = [
                {"id": "gpt-4.1", "name": "GPT-4.1"},
                {"id": "gpt-4o", "name": "GPT-4o"},
                {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
                {"id": "o3-mini", "name": "o3-mini"},
            ]
            return {"models": openai_models, "total": len(openai_models), "provider": "openai"}

        else:
            return {"models": [], "total": 0, "provider": "unknown", "note": "未识别的 API，请手动输入模型名"}

    except Exception as e:
        print(f"[get_models] 错误: {e}")
        return {"error": str(e), "models": []}


# ============================================================
# 高级设置面板 API（/api/settings）
# Dashboard 前端设置面板用，管理所有运行时可调配置
# ============================================================

def _mask_key(key_value: str) -> str:
    """API Key 打码：只露前5位和后4位"""
    if not key_value:
        return ""
    if len(key_value) < 10:
        return "****"
    return key_value[:5] + "****" + key_value[-4:]


def _is_masked(value: str) -> bool:
    """判断值是否是打码值（用户没改过）"""
    return "****" in str(value)


def _parse_bool(val, fallback=False) -> bool:
    """解析布尔值（兼容字符串/布尔/None）"""
    if val is None:
        return fallback
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("true", "1", "yes")


@app.get("/api/settings")
async def get_settings():
    """获取高级设置（数据库优先，fallback 到环境变量/运行时默认值）"""
    try:
        db = await get_all_gateway_config()

        # --- 基础连接 ---
        api_key_raw = db.get("API_KEY") or API_KEY
        embedding_key_raw = db.get("EMBEDDING_API_KEY") or _db_module.EMBEDDING_API_KEY

        memory_key_raw = db.get("MEMORY_API_KEY") or MEMORY_API_KEY
        summary_key_raw = db.get("SUMMARY_API_KEY") or SUMMARY_API_KEY


        settings = {
            # 基础连接
            "API_BASE_URL":     db.get("API_BASE_URL") or str(API_BASE_URL),
            "API_KEY":          _mask_key(api_key_raw),
            "DEFAULT_MODEL":    db.get("DEFAULT_MODEL") or str(DEFAULT_MODEL),

            # 记忆系统
            "MEMORY_ENABLED":          _parse_bool(db.get("MEMORY_ENABLED"), MEMORY_ENABLED),
            "MEMORY_API_BASE_URL":     db.get("MEMORY_API_BASE_URL") or str(MEMORY_API_BASE_URL),
            "MEMORY_API_KEY":          _mask_key(memory_key_raw),
            "MEMORY_MODEL":            db.get("MEMORY_MODEL") or str(MEMORY_MODEL),
            "MEMORY_API_THINKING":     db.get("MEMORY_API_THINKING") or str(MEMORY_API_THINKING),
            "MAX_MEMORIES_INJECT":     int(db.get("MAX_MEMORIES_INJECT") or MAX_MEMORIES_INJECT),
            "MIN_SCORE_THRESHOLD":     float(db.get("MIN_SCORE_THRESHOLD") or _db_module.MIN_SCORE_THRESHOLD),
            "MEMORY_EXTRACT_INTERVAL": int(db.get("MEMORY_EXTRACT_INTERVAL") or MEMORY_EXTRACT_INTERVAL),

            # 缓存分区
            "CACHE_PARTITION_ENABLED": _parse_bool(db.get("CACHE_PARTITION_ENABLED"), CACHE_PARTITION_ENABLED),
            "CACHE_PARTITION_X":       int(db.get("CACHE_PARTITION_X") or CACHE_PARTITION_X),
            "CACHE_PARTITION_TRIGGER": db.get("CACHE_PARTITION_TRIGGER") or CACHE_PARTITION_TRIGGER,
            "CACHE_PARTITION_WINDOW":  int(db.get("CACHE_PARTITION_WINDOW") or CACHE_PARTITION_WINDOW),
            "CACHE_SUMMARY_MODEL":     db.get("CACHE_SUMMARY_MODEL") or str(CACHE_SUMMARY_MODEL),
            "SUMMARY_API_BASE_URL":    db.get("SUMMARY_API_BASE_URL") or str(SUMMARY_API_BASE_URL),
            "SUMMARY_API_KEY":         _mask_key(summary_key_raw),

            # 向量搜索（开源版用 EMBEDDING_API_KEY + EMBEDDING_BASE_URL）
            "MEMORY_VECTOR_ENABLED":   _parse_bool(db.get("MEMORY_VECTOR_ENABLED"), _db_module.MEMORY_VECTOR_ENABLED),
            "EMBEDDING_API_KEY":       _mask_key(embedding_key_raw),
            "EMBEDDING_BASE_URL":      db.get("EMBEDDING_BASE_URL") or str(_db_module.EMBEDDING_BASE_URL),
            "EMBEDDING_MODEL":         db.get("EMBEDDING_MODEL") or str(_db_module.EMBEDDING_MODEL),
            "EMBEDDING_DIM":           int(db.get("EMBEDDING_DIM") or _db_module.EMBEDDING_DIM),

            # 搜索权重
            "MEMORY_HW_KEYWORD":        float(db.get("MEMORY_HW_KEYWORD") or _db_module.MEMORY_HW_KEYWORD),
            "MEMORY_HW_SEMANTIC":       float(db.get("MEMORY_HW_SEMANTIC") or _db_module.MEMORY_HW_SEMANTIC),
            "MEMORY_HW_IMPORTANCE":     float(db.get("MEMORY_HW_IMPORTANCE") or _db_module.MEMORY_HW_IMPORTANCE),
            "MEMORY_HW_RECENCY":        float(db.get("MEMORY_HW_RECENCY") or _db_module.MEMORY_HW_RECENCY),
            "MEMORY_SEMANTIC_THRESHOLD": float(db.get("MEMORY_SEMANTIC_THRESHOLD") or _db_module.MEMORY_SEMANTIC_THRESHOLD),

            # 其他
            "FORCE_STREAM":       _parse_bool(db.get("FORCE_STREAM"), FORCE_STREAM),
            "REASONING_EFFORT":   db.get("REASONING_EFFORT") or str(REASONING_EFFORT),

            # System Prompt
            "systemPrompt": db.get("systemPrompt") or _DEFAULT_SYSTEM_PROMPT or "",
            "role_display_user": db.get("role_display_user") or "👤 用户",
            "role_display_assistant": db.get("role_display_assistant") or "🤖 助手",
        }

        return {"status": "ok", "settings": settings}
    except Exception as e:
        print(f"[get_settings] 错误: {e}")
        return {"error": str(e)}


@app.put("/api/settings")
async def save_settings(request: Request):
    """保存高级设置（写入数据库 + 热更新运行时变量，立即生效无需重启）"""
    try:
        data = await request.json()
        updated = []
        skipped = []

        # main.py 全局变量映射（key → 类型转换函数）
        _MAIN_VARS = {
            "API_BASE_URL":          str,
            "API_KEY":               str,
            "DEFAULT_MODEL":         str,
            "MEMORY_API_BASE_URL":   str,
            "MEMORY_API_KEY":        str,
            "MEMORY_MODEL":          str,
            "MEMORY_API_THINKING":   str,
            "MEMORY_ENABLED":        lambda v: _parse_bool(v),
            "MAX_MEMORIES_INJECT":   int,
            "MEMORY_EXTRACT_INTERVAL": int,
            "CACHE_PARTITION_ENABLED": lambda v: _parse_bool(v),
            "CACHE_PARTITION_X":     int,
            "CACHE_PARTITION_TRIGGER": str,
            "CACHE_PARTITION_WINDOW": int,
            "CACHE_SUMMARY_MODEL":   str,
            "SUMMARY_API_BASE_URL":  str,
            "SUMMARY_API_KEY":       str,
            "FORCE_STREAM":          lambda v: _parse_bool(v),
            "REASONING_EFFORT":      str,
        }

        # database.py 全局变量映射（开源版用 EMBEDDING_API_KEY + EMBEDDING_BASE_URL）
        _DB_VARS = {
            "EMBEDDING_API_KEY":       str,
            "EMBEDDING_BASE_URL":      str,
            "EMBEDDING_MODEL":         str,
            "EMBEDDING_DIM":           int,
            "MIN_SCORE_THRESHOLD":     float,
            "MEMORY_VECTOR_ENABLED":   lambda v: _parse_bool(v),
            "MEMORY_HW_KEYWORD":       float,
            "MEMORY_HW_SEMANTIC":      float,
            "MEMORY_HW_IMPORTANCE":    float,
            "MEMORY_HW_RECENCY":       float,
            "MEMORY_SEMANTIC_THRESHOLD": float,
        }

        # 只存 os.environ 的变量
        _ENV_ONLY = {}

        # 打码字段
        _MASKED_KEYS = {"API_KEY", "EMBEDDING_API_KEY", "MEMORY_API_KEY", "SUMMARY_API_KEY"}

        for key, value in data.items():
            # --- 打码字段特殊处理 ---
            if key in _MASKED_KEYS:
                str_val = str(value).strip()
                if _is_masked(str_val):
                    skipped.append(key)
                    continue
                if not str_val:
                    await set_gateway_config(key, "")
                    if key in _MAIN_VARS:
                        globals()[key] = ""
                    elif key in _DB_VARS:
                        setattr(_db_module, key, "")
                    if key == "MEMORY_API_KEY":
                        import memory_extractor as _me_mod
                        _me_mod.MEMORY_API_KEY = ""
                    os.environ[key] = ""
                    updated.append(key)
                    continue

            # --- systemPrompt 特殊处理 ---
            if key == "systemPrompt":
                await set_gateway_config("systemPrompt", str(value))
                invalidate_system_prompt_cache()
                updated.append("systemPrompt")
                print(f"[settings] systemPrompt 已更新（{len(str(value))} 字）")
                continue

            # --- 常规字段 ---
            await set_gateway_config(key, str(value))

            if key in _MAIN_VARS:
                typed_value = _MAIN_VARS[key](value)
                globals()[key] = typed_value
                os.environ[key] = str(value)
                if key in {"MEMORY_API_BASE_URL", "MEMORY_API_KEY", "MEMORY_MODEL", "MEMORY_API_THINKING"}:
                    import memory_extractor as _me_mod
                    setattr(_me_mod, key, str(typed_value))
                updated.append(key)
                if key in _MASKED_KEYS:
                    print(f"[settings] {key} = [REDACTED]")
                else:
                    print(f"[settings] {key} = {typed_value}")

            elif key in _DB_VARS:
                typed_value = _DB_VARS[key](value)
                setattr(_db_module, key, typed_value)
                os.environ[key] = str(value)
                updated.append(key)
                print(f"[settings] {key} = {typed_value} (database)")

            elif key in _ENV_ONLY:
                typed_value = _ENV_ONLY[key](value)
                os.environ[key] = str(typed_value)
                updated.append(key)
                print(f"[settings] {key} = {typed_value} (env)")

            else:
                skipped.append(key)

        return {
            "status": "ok",
            "updated": updated,
            "skipped": skipped,
            "message": f"已更新 {len(updated)} 项配置，立即生效"
        }
    except Exception as e:
        print(f"[save_settings] 错误: {e}")
        return {"error": str(e)}
        
# 接收健康数据 
@app.post("/api/health/push")
async def push_health_data(data: HealthData):
    # 因为有中间件保护，能走到这里的绝对是自己人
    print("收到健康数据：", data.dict())
    return {"status": "success", "message": "健康数据已收到，等待对接数据库"}

# 接收状态数据
@app.post("/api/status/push")
async def push_status_data(data: StatusData):
    print("收到状态数据：", data.dict())
    return {"status": "success", "message": "环境状态已收到，等待对接数据库"}


# ============================================================

if __name__ == "__main__":
    import uvicorn
    print(f"🚀 AI Memory Gateway 启动中... 端口 {PORT}")
    print(f"📝 人设长度：{len(SYSTEM_PROMPT)} 字符")
    print(f"🤖 默认模型：{DEFAULT_MODEL}")
    print(f"🔗 API 地址：{API_BASE_URL}")
    print(f"🧠 记忆系统：{'开启' if MEMORY_ENABLED else '关闭'}")
    if MEMORY_ENABLED:
        print(f"📝 记忆提取+注入：{'开启' if MEMORY_EXTRACT_ENABLED else '关闭'}")
    print(f"🔄 记忆提取间隔：{'禁用' if MEMORY_EXTRACT_INTERVAL == 0 else '每轮提取' if MEMORY_EXTRACT_INTERVAL == 1 else f'每 {MEMORY_EXTRACT_INTERVAL} 轮提取一次'}")
    if CACHE_PARTITION_ENABLED:
        print(f"🔒 分区缓存：开启 (X={CACHE_PARTITION_X}, session={PARTITION_SESSION_ID or '未设置'})")
    if FORCE_STREAM:
        print(f"⚡ 强制流式传输：开启")
    if REASONING_EFFORT:
        print(f"🧠 推理参数注入：{REASONING_EFFORT}")
    _install_access_log_redaction()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
