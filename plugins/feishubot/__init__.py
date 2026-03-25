"""
飞书机器人插件 v2.5.1
支持影视搜索、资源下载、订阅管理，可选 Agent 智能模式
"""
import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

# ---------------------------------------------------------------------------
# Agent 工具定义
# ---------------------------------------------------------------------------
_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_media",
            "description": "搜索影视作品信息（电影、电视剧），返回 TMDB 信息列表",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词，如电影名或电视剧名"}
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_resources",
            "description": "搜索种子/资源，返回可下载的资源列表（包含分辨率、音轨、编码等信息）",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "资源搜索关键词"}
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "download_resource",
            "description": "下载指定序号的资源（需先调用 search_resources）",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "资源序号（从1开始）"}
                },
                "required": ["index"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "subscribe_media",
            "description": "订阅影视，自动监控并在有新资源时下载",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要订阅的影视名称"},
                    "tmdb_id": {"type": "integer", "description": "TMDB ID（可选，如果之前搜索过可直接指定）"}
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_downloading",
            "description": "获取当前正在下载的任务列表",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "向用户发送一条消息（用于发送中间状态通知）",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要发送的消息内容"}
                },
                "required": ["text"]
            }
        }
    }
]

# ---------------------------------------------------------------------------
# Agent System Prompt
# ---------------------------------------------------------------------------
_AGENT_SYSTEM_PROMPT = """你是 MoviePilot 飞书影视助手，帮助用户搜索、下载和订阅影视资源。

## 工作流程
1. 理解用户意图 — 搜索影视信息、搜索种子资源、下载、订阅、查看下载状态
2. 调用合适的工具完成任务
3. 根据工具返回的结果，向用户给出清晰的回复

## 偏好映射（用户常见表达 → 技术筛选条件）
- "4K" / "2160p" → 分辨率 4K/2160p
- "1080p" / "高清" → 分辨率 1080p
- "5.1声道" / "5.1" → 音频 Dolby Digital 5.1 或 DTS 5.1
- "全景声" / "Atmos" → Dolby Atmos
- "HDR" / "杜比视界" / "DV" → HDR/Dolby Vision
- "原盘" / "Remux" → Remux 格式

## 工作规则
- 当用户要求下载资源时，先用 search_resources 搜索，然后根据用户偏好筛选最佳结果
- 如果用户指定了画质、音轨等偏好，在搜索结果中优先筛选匹配的资源
- 发送 search_resources 后，整理结果并告知用户找到了哪些资源，推荐最佳选择
- 等用户确认后再执行下载，除非用户明确说"直接下载"
- 订阅时先 search_media 确认影视信息，再调用 subscribe_media

## 回复风格
- 简洁专业，使用 emoji 增加可读性
- 列出资源时标注关键信息：分辨率、大小、做种数、音频格式
- 给出推荐理由"""

_MAX_AGENT_ITERATIONS = 8


# ---------------------------------------------------------------------------
# OpenRouter LLM 客户端（零外部依赖）
# ---------------------------------------------------------------------------
class _OpenRouterClient:
    """零依赖 OpenRouter LLM 客户端"""

    def __init__(self, base_url: str, model: str, credential: str):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._credential = credential

    def chat(self, messages: list, tools: list = None) -> dict:
        import requests as _requests

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._credential}",
            "Content-Type": "application/json",
        }
        body: dict = {"model": self._model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        resp = _requests.post(url, headers=headers, json=body, timeout=120)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------
class FeishuBot(_PluginBase):
    """飞书机器人 — MoviePilot v2 插件"""

    # -- 插件元数据 ----------------------------------------------------------
    plugin_name = "飞书机器人"
    plugin_desc = "飞书机器人，支持影视搜索、资源下载、订阅管理，可选 Agent 智能模式"
    plugin_icon = "https://raw.githubusercontent.com/madrays/MoviePilot-Plugins/main/icons/feishu.png"
    plugin_version = "2.5.1"
    plugin_author = "madrays"
    author_url = "https://github.com/madrays"
    plugin_config_prefix = "feishubot_"
    plugin_order = 30
    auth_level = 1

    # -- 初始化 --------------------------------------------------------------
    def init_plugin(self, config: dict = None):
        logger.info(
            f"飞书机器人插件初始化, config type={type(config)}, "
            f"keys={list(config.keys()) if config else 'None'}"
        )

        # 配置项
        self._enabled: bool = False
        self._app_id: str = ""
        self._feishu_app_credential: str = ""
        self._verification_token: str = ""
        self._encrypt_key: str = ""
        self._llm_enabled: bool = False
        self._openrouter_credential: str = ""
        self._openrouter_model: str = ""

        # 运行时状态
        self._llm_client: Optional[_OpenRouterClient] = None
        self._tenant_token: str = ""
        self._token_expires: float = 0
        self._processed_events: set = set()
        self._user_media_cache: Dict[str, list] = {}
        self._user_resource_cache: Dict[str, list] = {}
        self._conversations: Dict[str, list] = {}
        self._user_locks: Dict[str, threading.Lock] = {}

        if config:
            self._enabled = config.get("enabled", False)
            self._app_id = str(config.get("app_id", "") or "").strip()
            self._feishu_app_credential = str(
                config.get("feishu_app_credential", "") or ""
            ).strip()
            self._verification_token = str(
                config.get("verification_token", "") or ""
            ).strip()
            self._encrypt_key = str(config.get("encrypt_key", "") or "").strip()
            self._openrouter_credential = str(
                config.get("openrouter_credential", "") or ""
            ).strip()
            self._openrouter_model = (
                str(config.get("openrouter_model", "") or "").strip()
                or "google/gemini-2.5-flash-preview:free"
            )

            # 健壮的布尔解析 — MoviePilot VSwitch 可能存为字符串
            llm_raw = config.get("llm_enabled")
            logger.info(f"llm_enabled raw={llm_raw!r}, type={type(llm_raw)}")
            if isinstance(llm_raw, bool):
                self._llm_enabled = llm_raw
            elif isinstance(llm_raw, str):
                self._llm_enabled = llm_raw.lower() in ("true", "1", "yes", "on")
            else:
                self._llm_enabled = bool(llm_raw) if llm_raw is not None else False

        logger.info(
            f"飞书机器人配置: enabled={self._enabled}, llm_enabled={self._llm_enabled}, "
            f"app_id={'✓' if self._app_id else '✗'}, model={self._openrouter_model}"
        )

        # 创建 Agent 客户端
        if self._llm_enabled and self._openrouter_credential:
            try:
                self._llm_client = _OpenRouterClient(
                    base_url="https://openrouter.ai/api/v1",
                    model=self._openrouter_model,
                    credential=self._openrouter_credential,
                )
                logger.info("飞书 Agent 模式已启用 ✓")
            except Exception as e:
                logger.error(f"飞书 Agent 客户端创建失败: {e}")
                self._llm_client = None
        elif self._llm_enabled:
            logger.warning("飞书 LLM 已启用但 API Key 未配置，Agent 模式未激活")

    # -- 状态 ----------------------------------------------------------------
    def get_state(self) -> bool:
        return self._enabled

    # -- API 路由 ------------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/callback",
                "endpoint": self._webhook_callback,
                "methods": ["POST"],
                "summary": "飞书回调",
            }
        ]

    # -- 配置表单 (Vuetify) --------------------------------------------------
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "llm_enabled",
                                            "label": "启用 Agent 智能模式",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "app_id",
                                            "label": "飞书 App ID",
                                            "placeholder": "cli_xxx",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "feishu_app_credential",
                                            "label": "飞书 App Secret",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "verification_token",
                                            "label": "Verification Token",
                                            "placeholder": "飞书事件回调验证令牌",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "encrypt_key",
                                            "label": "Encrypt Key (可选)",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "openrouter_credential",
                                            "label": "OpenRouter API Key",
                                            "type": "password",
                                            "placeholder": "sk-or-...",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "openrouter_model",
                                            "label": "模型",
                                            "placeholder": "google/gemini-2.5-flash-preview:free",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "app_id": "",
            "feishu_app_credential": "",
            "verification_token": "",
            "encrypt_key": "",
            "llm_enabled": False,
            "openrouter_credential": "",
            "openrouter_model": "google/gemini-2.5-flash-preview:free",
        }

    # -- 页面 ----------------------------------------------------------------
    def get_page(self) -> List[dict]:
        return []

    # -- 停止 ----------------------------------------------------------------
    def stop_service(self):
        pass

    # ========================================================================
    # 飞书 API helpers
    # ========================================================================
    def _get_tenant_token(self) -> str:
        import requests as _requests

        now = time.time()
        if self._tenant_token and now < self._token_expires:
            return self._tenant_token
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        resp = _requests.post(
            url,
            json={
                "app_id": self._app_id,
                "app_secret": self._feishu_app_credential,
            },
            timeout=10,
        )
        data = resp.json()
        self._tenant_token = data.get("tenant_access_token", "")
        expire = data.get("expire", 7200)
        self._token_expires = now + expire - 300
        return self._tenant_token

    def _feishu_send(self, chat_id: str, text: str):
        import requests as _requests

        token = self._get_tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        try:
            resp = _requests.post(
                url,
                headers=headers,
                json=body,
                params={"receive_id_type": "chat_id"},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"飞书发送失败: {resp.status_code} {resp.text[:200]}"
                )
        except Exception as e:
            logger.error(f"飞书发送异常: {e}")

    def _feishu_reply(self, msg_id: str, text: str):
        import requests as _requests

        token = self._get_tenant_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/reply"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        try:
            resp = _requests.post(url, headers=headers, json=body, timeout=10)
            if resp.status_code != 200:
                logger.warning(
                    f"飞书回复失败: {resp.status_code} {resp.text[:200]}"
                )
        except Exception as e:
            logger.error(f"飞书回复异常: {e}")

    # ========================================================================
    # Webhook 回调入口
    # ========================================================================
    def _webhook_callback(self, request_data: dict) -> dict:
        # 1. URL 验证（首次配置飞书时的 challenge）
        if "challenge" in request_data:
            return {"challenge": request_data["challenge"]}

        # 2. 事件处理
        header = request_data.get("header", {})
        event = request_data.get("event", {})

        # 去重
        event_id = header.get("event_id", "")
        if event_id in self._processed_events:
            return {"code": 0}
        self._processed_events.add(event_id)
        # 清理旧事件 ID（保留最近 50 个）
        if len(self._processed_events) > 100:
            self._processed_events = set(list(self._processed_events)[-50:])

        # 3. 消息事件
        event_type = header.get("event_type", "")
        if event_type == "im.message.receive_v1":
            threading.Thread(
                target=self._handle_message, args=(event,), daemon=True
            ).start()

        return {"code": 0}

    # ========================================================================
    # 消息分发
    # ========================================================================
    def _handle_message(self, event: dict):
        message = event.get("message", {})
        chat_id = message.get("chat_id", "")
        msg_id = message.get("message_id", "")
        msg_type = message.get("message_type", "")
        sender = event.get("sender", {})
        user_id = sender.get("sender_id", {}).get("open_id", "")

        if msg_type != "text":
            self._feishu_reply(msg_id, "暂只支持文本消息")
            return

        try:
            content = json.loads(message.get("content", "{}"))
            text = content.get("text", "").strip()
        except Exception:
            text = ""

        if not text:
            return

        logger.info(
            f"飞书收到: user={user_id}, text={text}, "
            f"agent_mode={self._llm_client is not None}"
        )

        # 并发锁
        lock = self._get_user_lock(user_id)
        if not lock.acquire(blocking=False):
            self._feishu_reply(msg_id, "⏳ 上一个请求还在处理中，请稍候")
            return
        try:
            # 诊断指令（始终可用）
            if text.startswith("/status") or text.startswith("/状态"):
                self._cmd_status(chat_id, msg_id)
                return

            # Agent 模式
            if self._llm_client:
                logger.info(f"[Agent] 路由到 Agent 处理: {text[:50]}")
                self._agent_handle(text, chat_id, msg_id, user_id)
                return

            logger.info(f"[Legacy] 路由到传统指令处理: {text[:50]}")
            self._legacy_handle(text, chat_id, msg_id, user_id)
        finally:
            lock.release()

    # ========================================================================
    # 用户锁（并发保护）
    # ========================================================================
    def _get_user_lock(self, user_id: str) -> threading.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = threading.Lock()
        return self._user_locks[user_id]

    # ========================================================================
    # Legacy 模式
    # ========================================================================
    def _legacy_handle(self, text: str, chat_id: str, msg_id: str, user_id: str):
        if text.startswith("/搜索") or text.startswith("/search"):
            keyword = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
            if keyword:
                self._cmd_search(keyword, chat_id, msg_id, user_id)
            else:
                self._feishu_reply(msg_id, "请输入搜索关键词，例如: /搜索 三体")
        elif text.startswith("/下载") or text.startswith("/download"):
            arg = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
            self._cmd_download(arg, chat_id, msg_id, user_id)
        elif text.startswith("/订阅") or text.startswith("/subscribe"):
            keyword = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
            if keyword:
                self._cmd_subscribe(keyword, chat_id, msg_id, user_id)
            else:
                self._feishu_reply(msg_id, "请输入订阅关键词，例如: /订阅 三体")
        elif text.startswith("/正在下载") or text.startswith("/downloading"):
            self._cmd_downloading(chat_id, msg_id)
        elif text.startswith("/帮助") or text.startswith("/help"):
            self._cmd_help(chat_id, msg_id)
        else:
            # 默认当作搜索
            self._cmd_search(text, chat_id, msg_id, user_id)

    # -- 搜索影视 ------------------------------------------------------------
    def _cmd_search(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        try:
            self._feishu_reply(msg_id, f"🔍 正在搜索: {keyword}")
            from app.chain.media import MediaChain

            media_chain = MediaChain()
            meta, medias = media_chain.search(title=keyword)
            if not medias:
                self._feishu_send(chat_id, f"未找到与 \u201c{keyword}\u201d 相关的影视")
                return
            valid = [m for m in medias if hasattr(m, "title") and hasattr(m, "type")]
            if not valid:
                self._feishu_send(chat_id, "未找到有效的影视结果")
                return
            self._user_media_cache[user_id] = valid
            lines = [f"🎬 搜索 \u201c{keyword}\u201d 找到 {len(valid)} 个结果:\n"]
            for i, m in enumerate(valid[:20]):
                title = getattr(m, "title", "未知")
                year = getattr(m, "year", "")
                mtype = getattr(m, "type", "")
                rating = getattr(m, "vote_average", "")
                overview = getattr(m, "overview", "")
                if overview and len(overview) > 60:
                    overview = overview[:60] + "…"
                line = f"{i + 1}. {title}"
                if year:
                    line += f" ({year})"
                if mtype:
                    line += f" [{mtype.value if hasattr(mtype, 'value') else mtype}]"
                if rating:
                    line += f" ⭐{rating}"
                if overview:
                    line += f"\n   {overview}"
                lines.append(line)
            self._feishu_send(chat_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"飞书搜索异常: {e}", exc_info=True)
            self._feishu_send(chat_id, f"搜索出错: {str(e)}")

    # -- 下载 ----------------------------------------------------------------
    def _cmd_download(self, arg: str, chat_id: str, msg_id: str, user_id: str):
        try:
            if not arg:
                self._feishu_reply(msg_id, "请输入资源序号，例如: /下载 1")
                return
            try:
                index = int(arg.strip())
            except ValueError:
                self._feishu_reply(msg_id, "请输入有效的数字序号")
                return
            contexts = self._user_resource_cache.get(user_id, [])
            if not contexts:
                self._feishu_reply(msg_id, "没有缓存的搜索结果，请先搜索资源")
                return
            if index < 1 or index > len(contexts):
                self._feishu_reply(
                    msg_id, f"序号超出范围，有效范围 1-{len(contexts)}"
                )
                return
            ctx = contexts[index - 1]
            self._feishu_reply(msg_id, f"⬇️ 正在下载第 {index} 个资源...")
            from app.chain.download import DownloadChain

            dl = DownloadChain()
            result = dl.download_single(context=ctx, userid=user_id)
            if result:
                self._feishu_send(chat_id, "✅ 下载任务已添加")
            else:
                self._feishu_send(chat_id, "❌ 下载失败，可能是资源不可用")
        except Exception as e:
            logger.error(f"飞书下载异常: {e}", exc_info=True)
            self._feishu_send(chat_id, f"下载出错: {str(e)}")

    # -- 订阅 ----------------------------------------------------------------
    def _cmd_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        try:
            self._feishu_reply(msg_id, f"📡 正在订阅: {keyword}")
            from app.chain.media import MediaChain

            media_chain = MediaChain()
            meta, medias = media_chain.search(title=keyword)
            if not medias:
                self._feishu_send(
                    chat_id, f"未找到 \u201c{keyword}\u201d 相关影视，无法订阅"
                )
                return
            valid = [m for m in medias if hasattr(m, "title") and hasattr(m, "type")]
            if not valid:
                self._feishu_send(chat_id, "未找到有效影视结果")
                return
            media = valid[0]
            from app.chain.subscribe import SubscribeChain

            sub_chain = SubscribeChain()
            sid, msg = sub_chain.add(
                title=media.title,
                year=getattr(media, "year", None),
                mtype=media.type,
                tmdbid=getattr(media, "tmdb_id", None),
                userid=user_id,
            )
            if sid:
                self._feishu_send(chat_id, f"✅ 已订阅: {media.title}")
            else:
                self._feishu_send(chat_id, f"订阅结果: {msg}")
        except Exception as e:
            logger.error(f"飞书订阅异常: {e}", exc_info=True)
            self._feishu_send(chat_id, f"订阅出错: {str(e)}")

    # -- 正在下载 ------------------------------------------------------------
    def _cmd_downloading(self, chat_id: str, msg_id: str):
        try:
            from app.chain.download import DownloadChain

            dl = DownloadChain()
            try:
                torrents = dl.list_downloading() if hasattr(dl, "list_downloading") else []
            except Exception:
                torrents = []
            if not torrents:
                self._feishu_send(chat_id, "当前没有正在下载的任务")
                return
            lines = [f"⬇️ 正在下载 {len(torrents)} 个任务:\n"]
            for i, t in enumerate(torrents[:10]):
                title = getattr(t, "title", "未知")
                progress = getattr(t, "progress", 0)
                speed = getattr(t, "dlspeed", "")
                lines.append(f"{i + 1}. {title} - {progress}% {speed}")
            self._feishu_send(chat_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"飞书查看下载异常: {e}", exc_info=True)
            self._feishu_send(chat_id, f"查看下载出错: {str(e)}")

    # -- 帮助 ----------------------------------------------------------------
    def _cmd_help(self, chat_id: str, msg_id: str):
        help_text = (
            "🤖 飞书机器人帮助\n\n"
            "/搜索 <关键词> - 搜索影视\n"
            "/下载 <序号> - 下载资源\n"
            "/订阅 <关键词> - 订阅影视\n"
            "/正在下载 - 查看下载中\n"
            "/status - 查看机器人状态\n"
            "/帮助 - 显示此帮助\n\n"
            "也可以直接输入影视名称进行搜索"
        )
        self._feishu_send(chat_id, help_text)

    # -- 状态诊断 ------------------------------------------------------------
    def _cmd_status(self, chat_id: str, msg_id: str):
        lines = [
            "🔧 飞书机器人状态",
            f"- 版本: {self.plugin_version}",
            f"- 启用: {self._enabled}",
            f"- LLM 配置: {self._llm_enabled}",
            f"- Agent 客户端: {'✅ 已创建' if self._llm_client else '❌ 未创建'}",
        ]
        if self._llm_enabled:
            lines.append(f"- 模型: {self._openrouter_model or '(未设置)'}")
            lines.append(
                f"- API Key: {'✅ 已配置' if self._openrouter_credential else '❌ 未配置'}"
            )
        self._feishu_reply(msg_id, "\n".join(lines))

    # ========================================================================
    # Agent 模式
    # ========================================================================
    def _agent_handle(self, text: str, chat_id: str, msg_id: str, user_id: str):
        """Agent 模式入口"""
        # 获取或创建会话历史
        if user_id not in self._conversations:
            self._conversations[user_id] = []
        history = self._conversations[user_id]

        # 保留最近 10 轮对话
        if len(history) > 20:
            history = history[-20:]
            self._conversations[user_id] = history

        messages = [{"role": "system", "content": _AGENT_SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

        try:
            reply = self._agent_loop(messages, chat_id, user_id)
            self._conversations[user_id].append({"role": "user", "content": text})
            self._conversations[user_id].append(
                {"role": "assistant", "content": reply}
            )
            self._feishu_send(chat_id, reply)
        except Exception as e:
            logger.error(f"[Agent] 处理异常: {e}", exc_info=True)
            self._feishu_send(chat_id, f"处理出错: {str(e)}")

    def _agent_loop(self, messages: list, chat_id: str, user_id: str) -> str:
        for iteration in range(_MAX_AGENT_ITERATIONS):
            logger.info(
                f"[Agent] iteration={iteration + 1}, messages_count={len(messages)}"
            )
            result = self._llm_client.chat(messages=messages, tools=_AGENT_TOOLS)
            choice = result.get("choices", [{}])[0]
            message = choice.get("message", {})
            tool_calls = message.get("tool_calls")

            if not tool_calls:
                reply = message.get("content", "")
                return reply or "（无回复）"

            # 有 tool_calls — 执行工具并继续循环
            messages.append(message)
            for tc in tool_calls:
                fn_name = tc.get("function", {}).get("name", "")
                fn_args_str = tc.get("function", {}).get("arguments", "{}")
                try:
                    fn_args = json.loads(fn_args_str)
                except Exception:
                    fn_args = {}
                logger.info(f"[Agent] tool_call: {fn_name}({fn_args})")
                tool_result = self._execute_tool(fn_name, fn_args, chat_id, user_id)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": json.dumps(
                            tool_result, ensure_ascii=False, default=str
                        ),
                    }
                )

        return "⚠️ 处理步骤过多，请尝试简化请求。"

    # ========================================================================
    # Tool 执行分发
    # ========================================================================
    def _execute_tool(
        self, name: str, args: dict, chat_id: str, user_id: str
    ) -> dict:
        try:
            if name == "search_media":
                return self._tool_search_media(args.get("keyword", ""), user_id)
            elif name == "search_resources":
                return self._tool_search_resources(args.get("keyword", ""), user_id)
            elif name == "download_resource":
                return self._tool_download_resource(args.get("index", 0), user_id)
            elif name == "subscribe_media":
                return self._tool_subscribe_media(
                    args.get("keyword", ""), args.get("tmdb_id"), user_id
                )
            elif name == "get_downloading":
                return self._tool_get_downloading()
            elif name == "send_message":
                self._feishu_send(chat_id, args.get("text", ""))
                return {"status": "sent"}
            else:
                return {"error": f"未知工具: {name}"}
        except Exception as e:
            logger.error(f"[Agent] 工具执行错误 {name}: {e}", exc_info=True)
            return {"error": str(e)}

    # ========================================================================
    # Tool 实现 — 返回结构化数据给 LLM
    # ========================================================================
    def _tool_search_media(self, keyword: str, user_id: str) -> dict:
        from app.chain.media import MediaChain

        media_chain = MediaChain()
        meta, medias = media_chain.search(title=keyword)
        if not medias:
            return {"keyword": keyword, "total_found": 0, "results": []}
        valid = [m for m in medias if hasattr(m, "title") and hasattr(m, "type")]
        self._user_media_cache[user_id] = valid
        results = []
        for i, m in enumerate(valid[:10]):
            results.append(
                {
                    "index": i + 1,
                    "title": getattr(m, "title", "未知"),
                    "year": getattr(m, "year", ""),
                    "type": str(getattr(m, "type", "")),
                    "rating": getattr(m, "vote_average", ""),
                    "overview": (getattr(m, "overview", "") or "")[:100],
                    "tmdb_id": getattr(m, "tmdb_id", None),
                }
            )
        return {"keyword": keyword, "total_found": len(valid), "results": results}

    def _tool_search_resources(self, keyword: str, user_id: str) -> dict:
        from app.chain.search import SearchChain

        search_chain = SearchChain()
        contexts = search_chain.search_by_title(title=keyword)
        if not contexts:
            return {"keyword": keyword, "total_found": 0, "results": []}
        self._user_resource_cache[user_id] = contexts
        results = []
        for i, ctx in enumerate(contexts[:20]):
            torrent = ctx.torrent_info if hasattr(ctx, "torrent_info") else None
            meta_info = ctx.meta_info if hasattr(ctx, "meta_info") else None
            title = ""
            site = ""
            size = 0
            seeders = 0
            tags: dict = {}
            if torrent:
                title = (
                    getattr(torrent, "title", "")
                    or getattr(torrent, "description", "")
                    or ""
                )
                site = getattr(torrent, "site_name", "") or ""
                size = getattr(torrent, "size", 0) or 0
                seeders = getattr(torrent, "seeders", 0) or 0
            if meta_info:
                res = getattr(meta_info, "resource_pix", "") or ""
                vc = getattr(meta_info, "video_encode", "") or ""
                ac = getattr(meta_info, "audio_encode", "") or ""
                tags = {
                    "resolution": res,
                    "video_codec": vc,
                    "audio": ac,
                }
            # 格式化大小
            if isinstance(size, (int, float)) and size > 0:
                if size > 1024**3:
                    size_str = f"{size / 1024**3:.2f} GB"
                elif size > 1024**2:
                    size_str = f"{size / 1024**2:.1f} MB"
                else:
                    size_str = f"{size} B"
            else:
                size_str = str(size)
            results.append(
                {
                    "index": i + 1,
                    "title": title[:80],
                    "site": site,
                    "size": size_str,
                    "seeders": seeders,
                    "tags": tags,
                }
            )
        return {
            "keyword": keyword,
            "total_found": len(contexts),
            "showing": min(20, len(contexts)),
            "results": results,
        }

    def _tool_download_resource(self, index: int, user_id: str) -> dict:
        contexts = self._user_resource_cache.get(user_id, [])
        if not contexts:
            return {"error": "没有缓存的搜索结果，请先搜索资源"}
        if index < 1 or index > len(contexts):
            return {"error": f"序号超出范围，有效范围 1-{len(contexts)}"}
        ctx = contexts[index - 1]
        from app.chain.download import DownloadChain

        dl = DownloadChain()
        result = dl.download_single(context=ctx, userid=user_id)
        if result:
            title = ""
            if hasattr(ctx, "torrent_info") and ctx.torrent_info:
                title = getattr(ctx.torrent_info, "title", "") or ""
            return {"status": "success", "title": title, "message": "下载任务已添加"}
        else:
            return {"status": "failed", "message": "下载失败，可能是资源不可用"}

    def _tool_subscribe_media(
        self, keyword: str, tmdb_id: int = None, user_id: str = ""
    ) -> dict:
        from app.chain.media import MediaChain

        media_chain = MediaChain()
        meta, medias = media_chain.search(title=keyword)
        if not medias:
            return {"error": f"未找到 \u201c{keyword}\u201d 相关影视"}
        valid = [m for m in medias if hasattr(m, "title") and hasattr(m, "type")]
        if not valid:
            return {"error": "未找到有效影视结果"}
        target = valid[0]
        if tmdb_id:
            for m in valid:
                if getattr(m, "tmdb_id", None) == tmdb_id:
                    target = m
                    break
        from app.chain.subscribe import SubscribeChain

        sub = SubscribeChain()
        sid, msg = sub.add(
            title=target.title,
            year=getattr(target, "year", None),
            mtype=target.type,
            tmdbid=getattr(target, "tmdb_id", None),
            userid=user_id,
        )
        if sid:
            return {
                "status": "success",
                "title": target.title,
                "subscribe_id": sid,
            }
        else:
            return {"status": "info", "title": target.title, "message": msg}

    def _tool_get_downloading(self) -> dict:
        from app.chain.download import DownloadChain

        dl = DownloadChain()
        try:
            torrents = dl.list_downloading() if hasattr(dl, "list_downloading") else []
        except Exception:
            torrents = []
        if not torrents:
            return {"total": 0, "tasks": []}
        tasks = []
        for t in torrents[:10]:
            tasks.append(
                {
                    "title": getattr(t, "title", "未知"),
                    "progress": getattr(t, "progress", 0),
                    "speed": getattr(t, "dlspeed", ""),
                    "size": getattr(t, "size", ""),
                }
            )
        return {"total": len(torrents), "tasks": tasks}
