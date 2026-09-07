import base64
import json
import asyncio
import time
import os
import re
import websockets
from loguru import logger
from dotenv import load_dotenv, set_key
from XianyuApis import XianyuApis
import sys
import random


from utils.xianyu_utils import generate_mid, generate_uuid, trans_cookies, generate_device_id, decrypt
from XianyuAgent import XianyuReplyBot
from context_manager import ChatContextManager
from app_store import AppStore, PolicyEngine, DEFAULT_POLICIES, find_unauthorized_promises
from privacy_guard import redact_sensitive_text


class XianyuLive:
    TEMPLATE_SEGMENT_TOKEN = "{$分段符}"
    TEMPLATE_IMAGE_PATTERN = re.compile(r"\{\$图片:(\d+)\}")

    @classmethod
    def parse_message_template(cls, text):
        """Parse opt-in templates without changing ordinary reply behavior."""
        source = str(text or "")
        parts = []
        cursor = 0
        for match in cls.TEMPLATE_IMAGE_PATTERN.finditer(source):
            before = source[cursor:match.start()]
            for value in before.split(cls.TEMPLATE_SEGMENT_TOKEN):
                value = value.strip()
                if value:
                    parts.append({"type": "text", "content": value})
            parts.append({"type": "image", "asset_id": int(match.group(1))})
            cursor = match.end()
        for value in source[cursor:].split(cls.TEMPLATE_SEGMENT_TOKEN):
            value = value.strip()
            if value:
                parts.append({"type": "text", "content": value})
        return parts[:8]

    async def send_message_template(self, ws, cid, toid, scope_id, item_id, text, *, sanitize=True):
        """Send a saved first-reply/keyword template in order; ordinary replies never enter here."""
        sent_text = []
        parts = self.parse_message_template(text)
        if not parts:
            raise ValueError("首次回复没有可发送的文字或图片")
        assets = {}
        # Validate the whole template before the first WebSocket write. A stale
        # image placeholder must not leave a partially delivered first reply.
        for part in parts:
            if part["type"] != "image":
                continue
            asset = self.app_store.get_image_asset(part["asset_id"])
            if not asset or str(asset.get("item_id")) != str(item_id) or not asset.get("file_path"):
                raise ValueError(f"首次回复引用的图片 #{part['asset_id']} 不存在或不属于当前商品")
            assets[part["asset_id"]] = asset
        for index, part in enumerate(parts):
            if part["type"] == "text":
                value = self.sanitize_buyer_reply(part["content"]) if sanitize else part["content"]
                await self.send_msg(ws, cid, toid, value, sanitize=False)
                sent_text.append(value)
            else:
                await self.send_image_asset(ws, cid, toid, scope_id, assets[part["asset_id"]])
            if index + 1 < len(parts):
                await asyncio.sleep(0.35)
        return "\n\n".join(sent_text)
    def __init__(self, cookies_str, bot_instance=None, app_store=None, event_callback=None, interactive=True):
        self.xianyu = XianyuApis(interactive=interactive)
        self.base_url = 'wss://wss-goofish.dingtalk.com/'
        self.cookies_str = cookies_str
        self.cookies = trans_cookies(cookies_str)
        self.xianyu.session.cookies.update(self.cookies)  # 直接使用 session.cookies.update
        self.myid = self.cookies['unb']
        self.device_id = generate_device_id(self.myid)
        self.app_store = app_store or AppStore()
        context_path = os.path.join(os.path.dirname(self.app_store.db_path), "chat_history.db")
        self.context_manager = ChatContextManager(db_path=context_path)
        self.bot = bot_instance or XianyuReplyBot()
        self.event_callback = event_callback
        self.loop = None
        self.running = True
        
        # 心跳相关配置
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "15"))  # 心跳间隔，默认15秒
        self.heartbeat_timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "5"))     # 心跳超时，默认5秒
        self.last_heartbeat_time = 0
        self.last_heartbeat_response = 0
        self.heartbeat_task = None
        self.ws = None
        
        # Token刷新相关配置
        self.token_refresh_interval = int(os.getenv("TOKEN_REFRESH_INTERVAL", "3600"))  # Token刷新间隔，默认1小时
        self.token_retry_interval = int(os.getenv("TOKEN_RETRY_INTERVAL", "300"))       # Token重试间隔，默认5分钟
        self.last_token_refresh_time = 0
        self.current_token = None
        self.token_refresh_task = None
        self.connection_restart_flag = False  # 连接重启标志
        
        # 人工接管相关配置
        self.manual_mode_conversations = set()  # 存储处于人工接管模式的会话ID
        self.manual_mode_timeout = int(os.getenv("MANUAL_MODE_TIMEOUT", "3600"))  # 人工接管超时时间，默认1小时
        self.manual_mode_timestamps = {}  # 记录进入人工模式的时间
        self._first_reply_locks = {}
        
        # 消息过期时间配置
        self.message_expire_time = int(os.getenv("MESSAGE_EXPIRE_TIME", "300000"))  # 消息过期时间，默认5分钟
        
        # 人工接管关键词，从环境变量读取
        self.toggle_keywords = os.getenv("TOGGLE_KEYWORDS", "。")
        
        # 模拟人工输入配置
        self.simulate_human_typing = os.getenv("SIMULATE_HUMAN_TYPING", "False").lower() == "true"
        # Merge short consecutive buyer messages before drafting a reply. Newer
        # messages invalidate any older draft that is still being generated.
        # 极速模式：只留极短窗口合并连续消息，避免每条消息固定等待数秒。
        self.message_bundle_delay = max(0.0, float(os.getenv("MESSAGE_BUNDLE_DELAY", "0.15")))
        self.media_bundle_delay = max(
            self.message_bundle_delay, float(os.getenv("MEDIA_BUNDLE_DELAY", "0.8"))
        )
        self._message_generations = {}
        self._message_buffers = {}
        self._recent_buyer_media = {}
        self._recent_buyer_locations = {}
        self._media_notice_times = {}
        self._seen_messages = set()
        self._review_notified_scopes = set()
        self._order_notice_scopes = set()
        self._buyer_routes = {}
        self._order_routes = {}
        self._store_contexts = {}
        self._query_contexts = {}
        self._listing_status_cache = None
        self.listing_status_ttl = max(
            10.0, float(os.getenv("LISTING_STATUS_TTL", "60"))
        )

    def emit_event(self, event_type, **payload):
        if self.event_callback:
            try:
                self.event_callback({"type": event_type, **payload})
            except Exception as exc:
                logger.debug(f"界面事件回调失败: {exc}")

    def scope_key(self, chat_id, item_id):
        """Isolate state by seller account, Xianyu conversation and item."""
        return f"{self.myid}:{chat_id}:{item_id}"

    @staticmethod
    def sanitize_buyer_reply(text):
        """Remove internal vocabulary and unverified coverage claims."""
        value = str(text or "").strip()
        if find_unauthorized_promises(value):
            return DEFAULT_POLICIES["manual_review_notice"]
        value = re.sub(r"(?i)sku", "商品规格", value)
        value = value.replace("知识库", "商品资料").replace("数据库", "资料")
        value = re.sub(
            r"(?:不是|并非|不属于)\s*(?:全国通用|全国门店(?:都|全部)?(?:可以用|可用|能用)|所有门店(?:都|全部)?(?:可以用|可用|能用))",
            "仅限已配置的可用门店使用",
            value,
        )
        return value

    @staticmethod
    def model_reply_grounding_issue(user_message, item_description, context, reply):
        """Reject factual numbers that the model cannot trace to supplied evidence."""
        unit_pattern = re.compile(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(元|张|折|%|小时|天|斤|公斤|千克|克|人)",
            re.I,
        )

        def tokens(value):
            found = set()
            for number, unit in unit_pattern.findall(str(value or "")):
                try:
                    number = f"{float(number):.6f}".rstrip("0").rstrip(".")
                except ValueError:
                    pass
                found.add((number, unit.lower()))
            return found

        context_text = "\n".join(
            str(item.get("content") or "")
            for item in (context or [])
            if isinstance(item, dict)
        )
        allowed = tokens(user_message) | tokens(item_description) | tokens(context_text)
        unsupported = sorted(tokens(reply) - allowed)
        if unsupported:
            detail = "、".join(f"{number}{unit}" for number, unit in unsupported[:5])
            return f"模型草稿包含资料中没有的数字：{detail}"
        return ""

    @staticmethod
    def grounding_fallback_reply(user_message):
        """Answer safely when a model draft contains facts absent from evidence."""
        text = str(user_message or "")
        if re.search(r"多少钱|多钱|价格|售价|价钱|怎么卖|\d+(?:\.\d+)?\s*(?:元|块)?\s*(?:的)?\s*多少", text):
            return (
                "当前商品资料中暂未找到能确认的对应价格。"
                "请告诉我具体商品规格、人数、使用日期或消费金额，我再为您准确查询。"
            )
        if re.search(r"门店|店铺|地址|哪里|哪家|商场|广场", text):
            return "请发送城市和具体店名，我会按当前商品的可用门店资料为您查询。"
        return "当前商品资料暂时无法准确回答这个问题，请补充具体想查询的商品、门店或使用条件。"

    @staticmethod
    def model_reply_operational_issue(reply, order_context=None):
        """Reject model claims about actions or order state the process did not verify."""
        value = str(reply or "")
        order_status = str((order_context or {}).get("status") or "")
        unverified_actions = (
            r"订单号(?:已经|已)?(?:查询|查到|核实)",
            r"(?:已经|已)(?:收到|查到|核实)(?:您的)?(?:退款|售后)申请",
            r"(?:我|我们|这边)(?:已经|已)?(?:帮您|帮你)?(?:处理|提交|登记)(?:退款|售后)",
            r"(?:已经|已)(?:为您|为你)?(?:转接|转交)(?:给)?人工",
        )
        for pattern in unverified_actions:
            if re.search(pattern, value):
                return "模型草稿声称执行了系统未完成的订单或人工操作"
        if re.search(r"订单(?:尚未|还未|没有)付款|订单未付款", value):
            if not any(word in order_status for word in ("待付款", "等待买家付款")):
                return "模型草稿声称了未核验的未付款状态"
        if re.search(r"订单(?:已经|已)付款|确认(?:已经|已)付款", value):
            if not any(word in order_status for word in ("已付款", "待发货", "等待卖家发货", "已发货")):
                return "模型草稿声称了未核验的已付款状态"
        return ""

    @staticmethod
    def operational_fallback_reply(user_message):
        """Acknowledge buyer-provided state without pretending it was queried."""
        text = re.sub(r"\s+", "", str(user_message or ""))
        if re.search(r"(?:已经|已)付款", text):
            return "已了解您反馈订单已经付款。当前无法直接核验订单状态；请说明是要咨询发券、核销还是退款。"
        if re.search(r"(?:已经|已)(?:申请|提交)", text):
            return "已了解您反馈已经提交申请。当前无法直接核验申请状态；如需人工处理，请回复“人工”。"
        return "当前无法直接核验订单或售后状态；请说明具体问题，如需人工处理请回复“人工”。"

    @staticmethod
    def media_marker(text):
        value = str(text or "")
        if re.search(r"(?:\[\s*图片\s*\]|图片消息|买家发送了一张图片)", value):
            return "[图片]"
        if re.search(r"(?:\[\s*语音\s*\]|语音消息|买家发送了一条语音)", value):
            return "[语音]"
        return ""

    @classmethod
    def inbound_media_marker(cls, payload):
        """Recognize inbound image/voice payloads even when no text marker exists."""
        if isinstance(payload, str):
            return cls.media_marker(payload)
        if not isinstance(payload, (dict, list)):
            return ""
        media_keys = {
            "imagecontent": "[图片]", "imagepayload": "[图片]",
            "audiocontent": "[语音]", "audiopayload": "[语音]", "audiourl": "[语音]",
            "voicecontent": "[语音]", "voicepayload": "[语音]", "voiceurl": "[语音]",
            "recordcontent": "[语音]", "recordurl": "[语音]",
        }
        stack = [payload]
        visited = 0
        while stack and visited < 120:
            current = stack.pop()
            visited += 1
            if isinstance(current, list):
                stack.extend(current[:30])
                continue
            if not isinstance(current, dict):
                marker = cls.media_marker(current)
                if marker:
                    return marker
                continue
            for key, value in current.items():
                normalized_key = re.sub(r"[^a-z]", "", str(key).lower())
                if normalized_key in {"contenttype", "messagetype", "msgtype", "mediatype"}:
                    type_text = str(value or "").lower()
                    if re.search(r"image|picture|photo|图片", type_text) or type_text == "2":
                        return "[图片]"
                    if re.search(r"audio|voice|record|语音", type_text) or type_text == "3":
                        return "[语音]"
                if normalized_key in media_keys and value not in (None, "", [], {}):
                    return media_keys[normalized_key]
                marker = cls.media_marker(value)
                if marker:
                    return marker
                if isinstance(value, (dict, list)):
                    stack.append(value)
        return ""

    @staticmethod
    def inbound_location_card(payload):
        """Extract a buyer-shared store name from a platform location card."""
        if not isinstance(payload, (dict, list)):
            return ""
        values = []
        stack = [payload]
        visited = 0
        location_hint = False
        while stack and visited < 160:
            current = stack.pop()
            visited += 1
            if isinstance(current, list):
                stack.extend(current[:40])
                continue
            if not isinstance(current, dict):
                continue
            for key, value in current.items():
                key_norm = re.sub(r"[^a-z]", "", str(key).lower())
                if key_norm in {"contenttype", "messagetype", "msgtype", "cardtype", "type"}:
                    location_hint = location_hint or bool(re.search(
                        r"location|poi|map|place|地址|位置|地图", str(value), re.I
                    ))
                if isinstance(value, (dict, list)):
                    stack.append(value)
                elif isinstance(value, str) and key_norm in {
                    "title", "name", "poiname", "placename", "locationname",
                    "address", "poiaddress", "locationaddress", "remindercontent",
                }:
                    text = value.strip()
                    if text:
                        values.append(text)
                        location_hint = location_hint or bool(re.search(
                            r"店|商场|广场|mall|城|中心|地址|路|街|大道", text, re.I
                        ))
        if not location_hint:
            return ""
        for value in values:
            inner = re.search(r"[（(]([^（）()\n]{2,36}(?:店|商场|广场|MALL|Mall|mall))[）)]", value)
            if inner:
                return inner.group(1).strip()
            match = re.search(r"([^\n，,。]{2,40}(?:店|商场|广场|MALL|Mall|mall))", value)
            if match:
                return match.group(1).strip("()（）[]【】 ")
        return ""

    @staticmethod
    def is_media_dependent_text(text):
        """Detect short follow-ups whose missing object can only be in nearby media."""
        value = re.sub(r"[\s，,。.!！?？~～]+", "", str(text or "")).lower()
        if not value or len(value) > 18:
            return False
        if re.search(r"\d+(?:\.\d+)?\s*(?:元|块|折|%)", value):
            return False
        if re.search(r"(?:省|市|区|县|镇|街道|万达|万象城|万科里|天街|银泰|吾悦)", value):
            return False
        return bool(re.fullmatch(
            r"(?:这个|那个|这里|那里|这家店|图片里|语音里|帮我看下|你看下)?"
            r"(?:可以用(?:吗|么|嘛|不)?|能用(?:吗|么|嘛|不)?|可用(?:吗|么|嘛|不)?|"
            r"多少钱|什么价格|要付多少|付多少钱|怎么买|怎么拍|怎么核销|怎么用|是这个吗)",
            value,
        ))

    def stop(self):
        self.running = False
        if self.loop and self.ws:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)

    def approve_reply(self, audit_id, final_reply, resume_ai=True):
        if not self.loop or not self.ws:
            raise RuntimeError("客服尚未连接，无法发送")
        audit = self.app_store.get_audit(audit_id)
        if not audit or audit["status"] != "pending":
            raise ValueError("该回复已处理或不存在")
        text = (final_reply or "").strip()
        if not text:
            raise ValueError("回复内容不能为空")
        future = asyncio.run_coroutine_threadsafe(self._send_approved(audit, text, resume_ai), self.loop)
        return future

    async def _send_approved(self, audit, final_reply, resume_ai=True):
        final_reply = self.sanitize_buyer_reply(final_reply)
        scope_id = audit.get("scope_id") or self.scope_key(audit["chat_id"], audit["item_id"])
        if audit.get("image_asset_id"):
            await self.send_message_template(
                self.ws, audit["chat_id"], audit["user_id"], scope_id,
                audit["item_id"], final_reply,
            )
        else:
            await self.send_msg(self.ws, audit["chat_id"], audit["user_id"], final_reply)
        self.context_manager.add_message_by_chat(
            scope_id, self.myid, audit["item_id"], "assistant", final_reply
        )
        self.app_store.update_audit(audit["id"], "sent", final_reply)
        self.app_store.record_ai_reply(scope_id)
        product_getter = getattr(self.app_store, "get_v2_product", None)
        product = product_getter(audit["item_id"]) if product_getter else None
        first_reply = str((product or {}).get("first_reply_text") or "").strip()
        if first_reply and first_reply in final_reply:
            self.app_store.mark_first_reply_sent(scope_id)
        if audit.get("image_asset_id"):
            asset = self.app_store.get_image_asset(int(audit["image_asset_id"]))
            if not asset or str(asset.get("item_id")) != str(audit["item_id"]):
                logger.error("待发送套餐图片不存在或与当前商品不匹配")
            else:
                try:
                    await self.send_image_asset(
                        self.ws, audit["chat_id"], audit["user_id"], scope_id, asset
                    )
                except Exception as exc:
                    logger.error(f"人工审核后的套餐图片发送失败: {exc}")
                    self.emit_event(
                        "image_send_error", audit_id=audit["id"],
                        image_asset_id=asset["id"], message=str(exc),
                    )
        if resume_ai:
            self.exit_manual_mode(scope_id)
        logger.info(f"人工审核后发送: {final_reply}")
        self.emit_event("audit_updated", audit_id=audit["id"], status="sent")

    def reject_reply(self, audit_id, final_reply=""):
        audit = self.app_store.get_audit(audit_id)
        if not audit or audit["status"] != "pending":
            raise ValueError("该回复已处理或不存在")
        self.app_store.update_audit(audit_id, "rejected", final_reply or "")
        scope_id = audit.get("scope_id") or self.scope_key(audit["chat_id"], audit["item_id"])
        self.exit_manual_mode(scope_id)
        self.emit_event("audit_updated", audit_id=audit_id, status="rejected")
        return {"id": audit_id, "status": "rejected"}

    async def refresh_token(self):
        """刷新token"""
        try:
            logger.info("开始刷新token...")
            
            # 获取新token（如果Cookie失效，get_token会直接退出程序）
            token_result = self.xianyu.get_token(self.device_id)
            if 'data' in token_result and 'accessToken' in token_result['data']:
                new_token = token_result['data']['accessToken']
                self.current_token = new_token
                self.last_token_refresh_time = time.time()
                logger.info("Token刷新成功")
                return new_token
            else:
                logger.error(f"Token刷新失败: {token_result}")
                return None
                
        except Exception as e:
            logger.error(f"Token刷新异常: {str(e)}")
            return None

    async def token_refresh_loop(self):
        """Token刷新循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要刷新token
                if current_time - self.last_token_refresh_time >= self.token_refresh_interval:
                    logger.info("Token即将过期，准备刷新...")
                    
                    new_token = await self.refresh_token()
                    if new_token:
                        logger.info("Token刷新成功，准备重新建立连接...")
                        # 设置连接重启标志
                        self.connection_restart_flag = True
                        # 关闭当前WebSocket连接，触发重连
                        if self.ws:
                            await self.ws.close()
                        break
                    else:
                        logger.error("Token刷新失败，将在{}分钟后重试".format(self.token_retry_interval // 60))
                        await asyncio.sleep(self.token_retry_interval)  # 使用配置的重试间隔
                        continue
                
                # 每分钟检查一次
                await asyncio.sleep(60)
                
            except Exception as e:
                logger.error(f"Token刷新循环出错: {e}")
                await asyncio.sleep(60)

    async def send_msg(self, ws, cid, toid, text, *, sanitize=True):
        # Normal AI/rule replies always pass through the final safety filter.
        # A product-card first reply has already been prepared separately:
        # automatic drafts are sanitized by prepare_product_first_reply(), while
        # an explicitly saved manual reply must be sent verbatim.  Re-sanitizing
        # it here would turn legitimate purchase-order words such as “改价/代付”
        # into the generic 72-hour manual-review notice.
        if sanitize:
            text = self.sanitize_buyer_reply(text)
        text = {
            "contentType": 1,
            "text": {
                "text": text
            }
        }
        text_base64 = str(base64.b64encode(json.dumps(text).encode('utf-8')), 'utf-8')
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {
                "mid": generate_mid()
            },
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {
                            "type": 1,
                            "data": text_base64
                        }
                    },
                    "redPointPolicy": 0,
                    "extension": {
                        "extJson": "{}"
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    },
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish"
                    ]
                }
            ]
        }
        await ws.send(json.dumps(msg))

    async def _send_and_record_auto_reply(
        self, websocket, chat_id, send_user_id, scope_id, item_id, audit_id, final_reply,
        reply_parts=None,
    ):
        """Persist a reply as sent only after the WebSocket write succeeds."""
        final_reply = self.sanitize_buyer_reply(final_reply)
        try:
            parts = [str(value).strip() for value in (reply_parts or []) if str(value).strip()]
            if parts:
                for index, value in enumerate(parts):
                    await self.send_msg(websocket, chat_id, send_user_id, value)
                    if index + 1 < len(parts):
                        await asyncio.sleep(0.35)
            else:
                await self.send_msg(websocket, chat_id, send_user_id, final_reply)
        except Exception:
            # Keep the draft for diagnosis/retry, but never claim delivery when
            # the transport rejected the message.
            self.app_store.update_audit(audit_id, "failed", final_reply)
            raise
        self.context_manager.add_message_by_chat(
            scope_id, self.myid, item_id, "assistant", final_reply
        )
        self.app_store.update_audit(audit_id, "sent", final_reply)
        self.app_store.record_ai_reply(scope_id)

    async def send_image_msg(self, ws, cid, toid, image_url, width, height):
        payload = {
            "contentType": 2,
            "image": {
                "pics": [{
                    "type": 0,
                    "url": str(image_url),
                    "width": int(width or 0),
                    "height": int(height or 0),
                }]
            },
        }
        image_base64 = str(
            base64.b64encode(json.dumps(payload).encode("utf-8")), "utf-8"
        )
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {"mid": generate_mid()},
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {"type": 2, "data": image_base64},
                    },
                    "redPointPolicy": 0,
                    "extension": {"extJson": "{}"},
                    "ctx": {"appVersion": "1.0", "platform": "web"},
                    "mtags": {},
                    "msgReadStatusSetting": 1,
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish",
                    ]
                },
            ],
        }
        await ws.send(json.dumps(msg))

    async def send_image_asset(self, ws, cid, toid, scope_id, asset):
        """Upload and send one product-bound image without involving the LLM."""
        try:
            upload = await asyncio.to_thread(self.xianyu.upload_media, asset["file_path"])
            image_object = upload["object"]
            width, height = 0, 0
            pix = str(image_object.get("pix") or "")
            if "x" in pix.lower():
                try:
                    width, height = map(int, pix.lower().split("x", 1))
                except (TypeError, ValueError):
                    width, height = 0, 0
            await self.send_image_msg(
                ws, cid, toid, image_object["url"], width, height
            )
            self.app_store.record_image_send(
                asset["id"], asset["item_id"], scope_id, cid, toid,
                "sent", remote_url=image_object["url"],
            )
            logger.info(f"套餐图片已发送: {asset['name']} (商品: {asset['item_id']})")
        except Exception as exc:
            self.app_store.record_image_send(
                asset["id"], asset["item_id"], scope_id, cid, toid,
                "failed", error=str(exc),
            )
            raise

    async def init(self, ws):
        # 如果没有token或者token过期，获取新token
        if not self.current_token or (time.time() - self.last_token_refresh_time) >= self.token_refresh_interval:
            logger.info("获取初始token...")
            await self.refresh_token()
        
        if not self.current_token:
            logger.error("无法获取有效token，初始化失败")
            raise Exception("Token获取失败")
            
        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": self.current_token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        # 等待一段时间，确保连接注册完成
        await asyncio.sleep(1)
        msg = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "5701741704675979 0"}, "body": [
            {"pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync", "topic": "sync", "highPts": 0,
             "pts": int(time.time() * 1000) * 1000, "seq": 0, "timestamp": int(time.time() * 1000)}]}
        await ws.send(json.dumps(msg))
        logger.info('连接注册完成')

    def is_chat_message(self, message):
        """判断是否为用户聊天消息"""
        try:
            return (
                isinstance(message, dict) 
                and "1" in message 
                and isinstance(message["1"], dict)  # 确保是字典类型
                and "10" in message["1"]
                and isinstance(message["1"]["10"], dict)  # 确保是字典类型
                and (
                    "reminderContent" in message["1"]["10"]
                    or bool(self.inbound_media_marker(message["1"]["10"]))
                )
            )
        except Exception:
            return False

    def is_sync_package(self, message_data):
        """判断是否为同步包消息"""
        try:
            return (
                isinstance(message_data, dict)
                and "body" in message_data
                and "syncPushPackage" in message_data["body"]
                and "data" in message_data["body"]["syncPushPackage"]
                and len(message_data["body"]["syncPushPackage"]["data"]) > 0
            )
        except Exception:
            return False

    def is_typing_status(self, message):
        """判断是否为用户正在输入状态消息"""
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], list)
                and len(message["1"]) > 0
                and isinstance(message["1"][0], dict)
                and "1" in message["1"][0]
                and isinstance(message["1"][0]["1"], str)
                and "@goofish" in message["1"][0]["1"]
            )
        except Exception:
            return False

    def is_system_message(self, message):
        """判断是否为系统消息"""
        try:
            return (
                isinstance(message, dict)
                and "3" in message
                and isinstance(message["3"], dict)
                and "needPush" in message["3"]
                and message["3"]["needPush"] == "false"
            )
        except Exception:
            return False
    
    def is_bracket_system_message(self, message):
        """检查是否为带中括号的系统消息"""
        try:
            if not message or not isinstance(message, str):
                return False
            
            clean_message = message.strip()
            # Image and voice markers are buyer messages and must reach the
            # deterministic media reply instead of being discarded as notices.
            if self.media_marker(clean_message):
                return False
            # 检查是否以 [ 开头，以 ] 结尾
            if clean_message.startswith('[') and clean_message.endswith(']'):
                logger.debug(f"检测到系统消息: {clean_message}")
                return True
            return False
        except Exception as e:
            logger.error(f"检查系统消息失败: {e}")
            return False

    @staticmethod
    def is_recall_message(message):
        text = str(message or "").strip()
        return any(phrase in text for phrase in (
            "撤回了一条消息", "撤回了消息", "消息已撤回", "对方撤回",
        ))

    @staticmethod
    def extract_actual_paid_amount(message):
        """Read explicit order paid amount fields when the Xianyu event provides them."""
        keys = {
            "actualpaidamount", "paidamount", "payamount", "realpayamount",
            "orderpayamount", "totalpayamount", "实付金额",
        }

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(key).lower())
                    if normalized in keys and isinstance(value, (int, float, str)):
                        match = re.search(r"\d+(?:\.\d+)?", str(value))
                        if match:
                            return match.group()
                    found = walk(value)
                    if found is not None:
                        return found
            elif isinstance(node, list):
                for value in node:
                    found = walk(value)
                    if found is not None:
                        return found
            return None

        return walk(message)

    def check_toggle_keywords(self, message):
        """检查消息是否包含切换关键词"""
        message_stripped = message.strip()
        return message_stripped in self.toggle_keywords

    def is_manual_mode(self, chat_id):
        """检查特定会话是否处于人工接管模式"""
        state_getter = getattr(self.app_store, "get_conversation_state", None)
        if state_getter and state_getter(chat_id) == "manual":
            self.manual_mode_conversations.add(chat_id)
            return True
        if chat_id not in self.manual_mode_conversations:
            return False
        
        # 检查是否超时
        current_time = time.time()
        if chat_id in self.manual_mode_timestamps:
            if current_time - self.manual_mode_timestamps[chat_id] > self.manual_mode_timeout:
                # 超时，自动退出人工模式
                self.exit_manual_mode(chat_id)
                return False
        
        return True

    def enter_manual_mode(self, chat_id):
        """进入人工接管模式"""
        self.manual_mode_conversations.add(chat_id)
        self.manual_mode_timestamps[chat_id] = time.time()
        pause = getattr(self.app_store, "pause_conversation", None)
        if pause:
            pause(chat_id, "manual")

    @staticmethod
    def requires_persistent_manual_takeover(deterministic, user_message=""):
        """Only order/authorization risks should lock later messages to a human."""
        kind = str((deterministic or {}).get("kind") or "")
        if kind in {
            "purchase_order_price", "refund_quality", "refund_dispute",
            "expiry_quality", "code_operation_review", "code_link_escalation",
            "sensitive_aftersale", "refund_status_review", "delivery_mismatch_review",
        }:
            return True
        return bool(re.search(
            r"赔偿|补偿|投诉|平台介入|强制退款|必须退款|立即退款|马上退款",
            str(user_message or ""),
        ))

    @staticmethod
    def should_silence_disabled_purchase_order_greeting(product, message=""):
        """A disabled welcome message must not become a fake manual-review notice."""
        product = product or {}
        return bool(
            str(product.get("coupon_type") or "") == "purchase_order"
            and not bool(product.get("first_reply_enabled", True))
            and re.fullmatch(
                r"(?:你好|您好|在吗|有人吗|哈喽|嗨|hi|hello|hey|有人不)(?:呀|啊|哦|呢|吗)?[？?。！!]*",
                re.sub(r"\s+", "", str(message or "")), re.I,
            )
        )

    @staticmethod
    def inbound_message_id(message, reminder=None, url_info=""):
        """Return the platform message id used for exact event deduplication."""
        reminder = reminder if isinstance(reminder, dict) else {}
        for key in ("messageId", "message_id", "msgId", "msg_id"):
            value = reminder.get(key)
            if value not in (None, ""):
                return str(value).strip()
        match = re.search(r"(?:messageId|message_id|msgId)=([^&?#]+)", str(url_info or ""), re.I)
        if match:
            return match.group(1).strip()

        wanted = {"messageid", "msgid"}
        stack = [message]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if re.sub(r"[^a-z0-9]", "", str(key).lower()) in wanted and value not in (None, ""):
                        return str(value).strip()
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)
        return ""

    @staticmethod
    def should_send_first_reply(message="", deterministic=None):
        """Recognize a pure greeting for the manual-takeover exception."""
        compact = re.sub(r"[\s，,。.!！?？~～]+", "", str(message or "")).lower()
        return bool(re.fullmatch(
            r"(?:你好|您好|在吗|有人吗|哈喽|嗨|hi|hello|hey|有人不)(?:呀|啊|哦|呢|吗)?",
            compact, re.I,
        ))

    @staticmethod
    def should_attach_first_reply(enabled, first_reply, deterministic_kind=""):
        """Return whether this product has an enabled greeting available."""
        return bool(enabled and str(first_reply or "").strip())

    @staticmethod
    def is_product_offline(product):
        return str((product or {}).get("item_status") or "").strip().lower() in {
            "offline", "off_shelf", "offshelf", "deleted", "下架",
        }

    @staticmethod
    def _listing_card_identity(card):
        """Return ``(item_id, is_onsale)`` from one marketplace list card."""
        data = card.get("cardData") if isinstance(card, dict) else None
        data = data if isinstance(data, dict) else (card if isinstance(card, dict) else {})
        detail = data.get("detailParams") or {}
        item_id = str(detail.get("itemId") or data.get("id") or "").strip()
        raw_status = str(data.get("itemStatus", "")).strip().lower()
        return item_id, raw_status in {"0", "onsale", "on_sale", "selling"}

    async def refresh_current_listing_status(self, item_id, product):
        """Refresh the listing gate before any first reply or business reply.

        The complete seller list is cached briefly per live account.  A missing
        product is considered offline only when pagination explicitly completed;
        API failures preserve the last local state and are logged by the caller.
        """
        product = product or None
        if not product or str(product.get("source_type") or "") != "goofish":
            return product
        now = time.monotonic()
        cache = getattr(self, "_listing_status_cache", None)
        ttl = float(getattr(self, "listing_status_ttl", 60.0))
        if not cache or now - float(cache.get("checked_at") or 0) >= ttl:
            cards = await asyncio.to_thread(self.xianyu.get_all_user_items, self.myid)
            statuses = {}
            for card in cards or []:
                card_item_id, is_onsale = self._listing_card_identity(card)
                if card_item_id:
                    statuses[card_item_id] = is_onsale
            cache = {
                "checked_at": now,
                "statuses": statuses,
                "complete": bool(getattr(self.xianyu, "last_item_list_complete", False)),
            }
            self._listing_status_cache = cache

        statuses = cache.get("statuses") or {}
        remote_state = statuses.get(str(item_id))
        if remote_state is None and not cache.get("complete"):
            return product
        normalized = "onsale" if remote_state else "offline"
        if normalized == "offline" or normalized != str(product.get("item_status") or "").lower():
            setter = getattr(self.app_store, "set_product_listing_status", None)
            updated = setter(item_id, normalized) if setter else None
            if updated:
                return updated
            product = dict(product)
            product["item_status"] = normalized
            if normalized == "offline":
                product["enabled"] = 0
        return product

    def prepare_product_first_reply(self, product):
        """Preserve an explicitly saved welcome; sanitize automatic drafts."""
        product = product or {}
        text = str(product.get("first_reply_text") or "").strip()
        if not text:
            return ""
        if bool(product.get("first_reply_manual")):
            return text
        return self.sanitize_buyer_reply(text)

    async def send_required_first_reply(
        self, websocket, chat_id, send_user_id, scope_id, item_id, product, conversation,
        message="",
    ):
        """Send the product introduction only for a pure first-turn greeting."""
        if (
            self.is_product_offline(product)
            or int((conversation or {}).get("first_reply_sent", 0))
            or not self.should_attach_first_reply(
                bool((product or {}).get("first_reply_enabled", True)),
                (product or {}).get("first_reply_text"),
            )
        ):
            return False

        lock_map = getattr(self, "_first_reply_locks", None)
        if lock_map is None:
            lock_map = self._first_reply_locks = {}
        lock = lock_map.setdefault(scope_id, asyncio.Lock())
        async with lock:
            durable_getter = getattr(self.app_store, "is_first_reply_sent", None)
            if durable_getter and durable_getter(scope_id):
                return False
            if (
                bool((product or {}).get("enabled", 1))
                and not self.should_send_first_reply(message)
            ):
                # A concrete first question must receive its direct answer
                # without a long catalog in front of it. Consume the welcome
                # flag so it cannot appear unexpectedly later in the chat.
                self.app_store.mark_first_reply_sent(scope_id)
                self.emit_event(
                    "product_first_reply_skipped", chat_id=chat_id, item_id=item_id,
                    scope_id=scope_id, message="买家首条消息为具体问题，已直接回答",
                )
                return False
            reply = self.prepare_product_first_reply(product)
            if not reply:
                return False
            await self.send_message_template(
                websocket, chat_id, send_user_id, scope_id, item_id,
                reply, sanitize=False,
            )
            self.context_manager.add_message_by_chat(
                scope_id, self.myid, item_id, "assistant", reply
            )
            self.app_store.mark_first_reply_sent(scope_id)
            self.emit_event(
                "product_first_reply", chat_id=chat_id, item_id=item_id,
                scope_id=scope_id, message="商品首次回复已在问题答案前发送",
            )
            return True

    def exit_manual_mode(self, chat_id):
        """退出人工接管模式"""
        self.manual_mode_conversations.discard(chat_id)
        if chat_id in self.manual_mode_timestamps:
            del self.manual_mode_timestamps[chat_id]
        self._review_notified_scopes.discard(chat_id)
        resume = getattr(self.app_store, "resume_conversation", None)
        if resume:
            resume(chat_id)

    def toggle_manual_mode(self, chat_id):
        """切换人工接管模式"""
        if self.is_manual_mode(chat_id):
            self.exit_manual_mode(chat_id)
            return "auto"
        else:
            self.enter_manual_mode(chat_id)
            return "manual"
    
    def format_price(self, price):
        """
        处理逻辑：标准化价格（分转元）
        """
        try:
            return round(float(price) / 100, 2)
        except (ValueError, TypeError):
            # 遇到 None 或脏数据，默认返回 0
            return 0.0
    
    def build_item_description(self, item_info):
        """构建商品描述"""
        
        # 处理 SKU 列表
        clean_skus = []
        raw_sku_list = item_info.get('skuList', [])
        
        for sku in raw_sku_list:
            # 提取规格文本
            specs = [p['valueText'] for p in sku.get('propertyList', []) if p.get('valueText')]
            spec_text = " ".join(specs) if specs else "默认规格"
            
            clean_skus.append({
                "spec": spec_text,
                "price": self.format_price(sku.get('price', 0)),
                "stock": sku.get('quantity', 0)
            })

        # 获取价格
        valid_prices = [s['price'] for s in clean_skus if s['price'] > 0]
        
        if valid_prices:
            min_price = min(valid_prices)
            max_price = max(valid_prices)
            if min_price == max_price:
                price_display = f"¥{min_price}"
            else:
                price_display = f"¥{min_price} - ¥{max_price}" # 价格区间
        else:
            # 如果没有SKU价格，回退使用商品主价格
            main_price = round(float(item_info.get('soldPrice', 0)), 2)
            price_display = f"¥{main_price}"

        summary = {
            "title": item_info.get('title', ''),
            "desc": item_info.get('desc', ''),
            "price_range": price_display,
            "total_stock": item_info.get('quantity', 0),
            "sku_details": clean_skus
        }

        return json.dumps(summary, ensure_ascii=False)

    async def handle_order_reminder(self, message, websocket):
        """Handle Xianyu order-state notices before ordinary chat routing."""
        reminder = message.get("3") if isinstance(message, dict) else None
        reminder = reminder if isinstance(reminder, dict) else {}
        status = str(reminder.get("redReminder") or "").strip()
        serialized = json.dumps(message, ensure_ascii=False)
        if not status:
            if re.search(r"我已拍下\s*[,，]?\s*待付款|等待买家付款|已拍下[^\n]{0,12}待付款", serialized):
                status = "等待买家付款"
            elif re.search(r"(?:买家已申请|退款申请|退货退款申请|等待卖家处理|售后申请)", serialized):
                status = "退款申请"
        refund_status = any(word in status for word in ("退款", "退货", "售后", "纠纷"))
        ordinary_order_status = any(word in status for word in (
            "等待买家付款", "待付款", "等待卖家发货", "待发货", "已付款",
            "已发货", "确认收货", "交易成功", "交易关闭",
        ))
        if not ordinary_order_status and not refund_status:
            return False

        primary = message.get("1") if isinstance(message, dict) else None
        info = primary.get("10") if isinstance(primary, dict) else {}
        info = info if isinstance(info, dict) else {}

        def find_value(node, keys):
            if isinstance(node, dict):
                for key, value in node.items():
                    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                    if normalized in keys and value not in (None, "", [], {}):
                        return value
                    found = find_value(value, keys)
                    if found not in (None, ""):
                        return found
            elif isinstance(node, list):
                for value in node:
                    found = find_value(value, keys)
                    if found not in (None, ""):
                        return found
            return None

        user_id = ""
        if isinstance(primary, str):
            user_id = primary.split("@", 1)[0]
        user_id = str(
            info.get("senderUserId") or reminder.get("buyerUserId")
            or reminder.get("buyerId")
            or find_value(message, {"senderuserid", "buyeruserid", "buyerid", "userid"})
            or user_id or ""
        ).strip()
        chat_value = primary.get("2") if isinstance(primary, dict) else ""
        chat_id = str(
            chat_value or reminder.get("conversationId")
            or find_value(message, {"conversationid", "chatid", "cid"}) or ""
        ).split("@", 1)[0]
        url_info = str(info.get("reminderUrl") or reminder.get("reminderUrl") or "")
        item_match = re.search(r"(?:itemId|item_id)[=\"':：\\/]+([0-9A-Za-z_-]+)", url_info + serialized)
        item_id = str(
            reminder.get("itemId") or find_value(message, {"itemid", "item_id"})
            or (item_match.group(1) if item_match else "")
        ).strip()
        order_id = str(
            reminder.get("orderId")
            or find_value(message, {"orderid", "bizorderid", "mainorderid"}) or ""
        ).strip()
        remembered = self._buyer_routes.get(user_id) if user_id else None
        if remembered:
            chat_id = chat_id or remembered[0]
            item_id = item_id or remembered[1]
        scope_id = self.scope_key(chat_id, item_id) if chat_id and item_id else ""
        if scope_id:
            order_routes = getattr(self, "_order_routes", None)
            if order_routes is None:
                order_routes = {}
                self._order_routes = order_routes
            route = dict(order_routes.get(scope_id) or {})
            if order_id:
                route["order_id"] = order_id
            if url_info:
                route["order_url"] = url_info
            route["status"] = status
            order_routes[scope_id] = route

        if refund_status:
            product_getter = getattr(self.app_store, "get_v2_product", None)
            product = product_getter(item_id) if product_getter and item_id else None
            recorder = getattr(self.app_store, "upsert_refund_order", None)
            if recorder:
                recorder({
                    "order_id": order_id,
                    "scope_id": scope_id,
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "item_id": item_id,
                    "product_title": (product or {}).get("title", ""),
                    "reason": status,
                    "status": "处理中",
                    "source": "闲鱼订单状态",
                    "order_url": url_info,
                })
            logger.info(f"退款订单状态：{status}")
            self.emit_event("refund_order", message=status, item_id=item_id, order_id=order_id)
            return True
        if status != "等待买家付款":
            logger.info(f"订单状态：{status}")
            return True

        if not (user_id and chat_id and item_id):
            logger.warning("检测到买家拍下，但订单消息缺少会话、买家或商品ID，未发送付款前提示")
            return True
        scope_id = self.scope_key(chat_id, item_id)
        product_getter = getattr(self.app_store, "get_v2_product", None)
        product = product_getter(item_id) if product_getter else None
        if product and product.get("item_status") != "offline" and not bool(product.get("enabled", 1)):
            logger.info(f"商品 {item_id} 已关闭AI客服，跳过付款前自动提示")
            return True
        notice_key = order_id or scope_id
        if notice_key in self._order_notice_scopes:
            return True
        notice_getter = getattr(self.app_store, "order_payment_notice", None)
        notice = notice_getter(item_id) if notice_getter else (
            "温馨提示：本商品卡券必须当天购买、当天使用。"
            "非卡券质量问题的退款需收取5%手续费。付款后会自动发货，介意请勿付款。"
        )
        if not str(notice or "").strip():
            logger.info(f"当前商品已关闭付款前提醒 (商品: {item_id})")
            return True
        await self.send_msg(websocket, chat_id, user_id, notice)
        self._order_notice_scopes.add(notice_key)
        self.context_manager.add_message_by_chat(scope_id, self.myid, item_id, "assistant", notice)
        logger.info(f"买家拍下后付款前提示已发送 (商品: {item_id}, 会话: {chat_id})")
        self.emit_event("order_payment_notice", chat_id=chat_id, item_id=item_id, scope_id=scope_id)
        return True

    async def handle_message(self, message_data, websocket):
        """处理所有类型的消息"""
        try:

            try:
                message = message_data
                ack = {
                    "code": 200,
                    "headers": {
                        "mid": message["headers"]["mid"] if "mid" in message["headers"] else generate_mid(),
                        "sid": message["headers"]["sid"] if "sid" in message["headers"] else '',
                    }
                }
                if 'app-key' in message["headers"]:
                    ack["headers"]["app-key"] = message["headers"]["app-key"]
                if 'ua' in message["headers"]:
                    ack["headers"]["ua"] = message["headers"]["ua"]
                if 'dt' in message["headers"]:
                    ack["headers"]["dt"] = message["headers"]["dt"]
                await websocket.send(json.dumps(ack))
            except Exception as e:
                pass

            # 如果不是同步包消息，直接返回
            if not self.is_sync_package(message_data):
                return

            # 获取并解密数据
            sync_data = message_data["body"]["syncPushPackage"]["data"][0]
            
            # 检查是否有必要的字段
            if "data" not in sync_data:
                logger.debug("同步包中无data字段")
                return

            # 解密数据
            try:
                data = sync_data["data"]
                try:
                    data = base64.b64decode(data).decode("utf-8")
                    data = json.loads(data)
                    # logger.info(f"无需解密 message: {data}")
                    return
                except Exception as e:
                    # logger.info(f'加密数据: {data}')
                    decrypted_data = decrypt(data)
                    message = json.loads(decrypted_data)
            except Exception as e:
                logger.error(f"消息解密失败: {e}")
                return

            if await self.handle_order_reminder(message, websocket):
                return

            # 判断消息类型
            if self.is_typing_status(message):
                logger.debug("用户正在输入")
                return
            elif not self.is_chat_message(message):
                logger.debug("其他非聊天消息")
                logger.debug(f"原始消息: {message}")
                return

            # 处理聊天消息
            create_time = int(message["1"]["5"])
            reminder = message["1"]["10"]
            send_user_name = reminder["reminderTitle"]
            send_user_id = reminder["senderUserId"]
            send_message = str(reminder.get("reminderContent") or "").strip()
            location_card = self.inbound_location_card(reminder)
            payload_marker = self.inbound_media_marker(reminder)
            if payload_marker and not self.media_marker(send_message):
                send_message = (
                    f"{payload_marker}\n{send_message}" if send_message else payload_marker
                )
            
            # 时效性验证（过滤5分钟前消息）
            if (time.time() * 1000 - create_time) > self.message_expire_time:
                logger.debug("过期消息丢弃")
                return
                
            # 获取商品ID和会话ID
            url_info = reminder["reminderUrl"]
            item_id = url_info.split("itemId=")[1].split("&")[0] if "itemId=" in url_info else None
            chat_id = message["1"]["2"].split('@')[0]
            
            if not item_id:
                logger.warning("无法获取商品ID")
                return
            scope_id = self.scope_key(chat_id, item_id)

            if send_user_id != self.myid:
                self._buyer_routes[str(send_user_id)] = (str(chat_id), str(item_id))
                if location_card:
                    locations = getattr(self, "_recent_buyer_locations", None)
                    if locations is None:
                        self._recent_buyer_locations = locations = {}
                    locations[scope_id] = (time.monotonic(), location_card)
                    # A shared POI card supplies context rather than a complete
                    # question. Wait for the buyer's following “可以用吗”.
                    if not re.search(r"(?:可以|能|可)(?:使用|用)|支持吗|适用吗", send_message):
                        logger.info(f"已记录买家发送的门店位置卡：{location_card}")
                        return

            if self.is_recall_message(send_message):
                self._message_generations[scope_id] = self._message_generations.get(scope_id, 0) + 1
                self._message_buffers.pop(scope_id, None)
                self._recent_buyer_media.pop(scope_id, None)
                logger.info(f"检测到消息撤回，已取消会话 {chat_id} 尚未发送的草稿")
                self.emit_event("message_recalled", chat_id=chat_id, item_id=item_id, scope_id=scope_id)
                return

            message_id = self.inbound_message_id(message, reminder, url_info)
            fingerprint = (
                f"message:{chat_id}:{send_user_id}:{message_id}" if message_id
                else f"fallback:{chat_id}:{send_user_id}:{create_time}:{send_message}"
            )
            if fingerprint in self._seen_messages:
                logger.info(f"重复消息已忽略 (messageId: {message_id or '缺失'})")
                self.emit_event(
                    "duplicate_message", chat_id=chat_id, item_id=item_id,
                    scope_id=scope_id, message_id=message_id,
                )
                return
            self._seen_messages.add(fingerprint)
            if len(self._seen_messages) > 3000:
                self._seen_messages.clear()

            if send_user_id != self.myid:
                marker = self.media_marker(send_message)
                now_monotonic = time.monotonic()
                recent_location = getattr(self, "_recent_buyer_locations", {}).get(scope_id)
                if (
                    not location_card and recent_location
                    and now_monotonic - recent_location[0] <= 1800
                    and self.is_media_dependent_text(send_message)
                ):
                    send_message = f"{recent_location[1]}{send_message}"
                if marker:
                    self._recent_buyer_media[scope_id] = (now_monotonic, marker)
                else:
                    recent_media = self._recent_buyer_media.get(scope_id)
                    if recent_media and now_monotonic - recent_media[0] <= 15 and self.is_media_dependent_text(send_message):
                        already_notified = self._media_notice_times.get(scope_id, 0) >= recent_media[0]
                        if already_notified:
                            logger.info(f"会话 {chat_id} 的图片/语音关联短问已由上一条提示覆盖")
                            return
                        buffered = self._message_buffers.get(scope_id, [])
                        if not any(self.media_marker(value) for value in buffered):
                            send_message = recent_media[1] + "\n" + send_message

            # 检查是否为卖家（自己）发送的控制命令
            if send_user_id == self.myid:
                logger.debug("检测到卖家消息，检查是否为控制命令")
                
                # 检查切换命令
                if self.check_toggle_keywords(send_message):
                    mode = self.toggle_manual_mode(scope_id)
                    if mode == "manual":
                        logger.info(f"🔴 已接管会话 {chat_id} (商品: {item_id})")
                    else:
                        self.app_store.reset_conversation(scope_id)
                        self._store_contexts.pop(scope_id, None)
                        self._query_contexts.pop(scope_id, None)
                        logger.info(f"🟢 已恢复会话 {chat_id} 的自动回复 (商品: {item_id})")
                    return
                
                # 记录卖家人工回复
                self.context_manager.add_message_by_chat(scope_id, self.myid, item_id, "assistant", send_message)
                logger.info(f"卖家人工回复 (会话: {chat_id}, 商品: {item_id}): {send_message}")
                return

            generation = self._message_generations.get(scope_id, 0) + 1
            self._message_generations[scope_id] = generation
            self._message_buffers.setdefault(scope_id, []).append(send_message)
            # Also hold a short object-less question briefly. The image/voice
            # event can arrive after its text companion on the WebSocket.
            delay = (
                self.media_bundle_delay
                if self.media_marker(send_message) or self.is_media_dependent_text(send_message)
                else self.message_bundle_delay
            )
            await asyncio.sleep(delay)
            if self._message_generations.get(scope_id) != generation:
                logger.debug(f"会话 {chat_id} 收到更新消息，旧草稿任务已取消")
                return
            send_message = "\n".join(self._message_buffers.pop(scope_id, []))

            product_getter = getattr(self.app_store, "get_v2_product", None)
            current_product = product_getter(item_id) if product_getter else None
            policies = self.app_store.get_policies()
            reset_hours = int(policies.get("conversation_reset_hours", 24))
            conversation = self.app_store.touch_conversation(
                scope_id, self.myid, chat_id, send_user_id, item_id, reset_hours
            )
            if conversation.get("reset"):
                self.exit_manual_mode(scope_id)
                self._store_contexts.pop(scope_id, None)
                self._query_contexts.pop(scope_id, None)
                self._recent_buyer_media.pop(scope_id, None)
                getattr(self, "_recent_buyer_locations", {}).pop(scope_id, None)
                self._media_notice_times.pop(scope_id, None)
                self.emit_event("conversation_reset", chat_id=chat_id, item_id=item_id, scope_id=scope_id)

            # Chat-shaped platform notices are not buyer consultations and must
            # never consume the mandatory first-reply flag.
            if self.is_bracket_system_message(send_message):
                logger.info(f"检测到系统消息：'{send_message}'，跳过自动回复")
                return
            if self.is_system_message(message):
                logger.debug("系统消息，跳过处理")
                return

            # Product state is a hard gate and must be fresh before the welcome.
            # A stale two-day-old local sync must not let an offline listing send
            # its old first reply, keyword rule or model answer.
            if current_product:
                try:
                    current_product = await self.refresh_current_listing_status(
                        item_id, current_product
                    )
                except Exception as exc:
                    logger.warning(f"实时核对商品上下架状态失败，保留本地状态: {exc}")

            product_offline = self.is_product_offline(current_product)
            first_reply_sent_now = False
            if current_product and not product_offline:
                try:
                    first_reply_sent_now = await self.send_required_first_reply(
                        websocket, chat_id, send_user_id, scope_id, item_id,
                        current_product, conversation, send_message,
                    )
                    if first_reply_sent_now:
                        await asyncio.sleep(0.25)
                except Exception as exc:
                    # A question answer must never overtake a required welcome.
                    # Keep the durable flag unset so the next delivery can retry.
                    logger.error(f"商品首次回复发送失败，暂不发送问题答案: {exc}")
                    self.emit_event(
                        "product_first_reply_error", chat_id=chat_id,
                        item_id=item_id, scope_id=scope_id, message=str(exc),
                    )
                    return

            if current_product and not bool(current_product.get("enabled", 1)) and not product_offline:
                self.context_manager.add_message_by_chat(
                    scope_id, send_user_id, item_id, "user", send_message
                )
                if first_reply_sent_now:
                    self.emit_event(
                        "product_first_reply_only", chat_id=chat_id, item_id=item_id,
                        scope_id=scope_id, message="AI客服关闭，仅发送商品首次回复",
                    )
                    logger.info(f"商品 {item_id} 已关闭AI客服，已独立发送首次回复")
                else:
                    logger.info(f"商品 {item_id} 已关闭AI客服，本条消息保持静默且不调用模型")
                self.emit_event(
                    "product_ai_disabled", chat_id=chat_id, item_id=item_id,
                    scope_id=scope_id, message="当前商品已关闭AI客服",
                )
                return
            if not product_offline and self.should_silence_disabled_purchase_order_greeting(
                current_product, send_message
            ):
                logger.info(f"代买单商品已关闭首次回复，纯问候保持静默 (会话: {chat_id})")
                return
            
            logger.info(
                f"用户: {send_user_name} (ID: {send_user_id}), 商品: {item_id}, "
                f"会话: {chat_id}, 消息: {redact_sensitive_text(send_message)}"
            )
            
            
            prompt_setter = getattr(self.bot, "set_global_system_prompt", None)
            if prompt_setter:
                prompt_setter(policies.get("global_system_prompt", ""))
            max_rounds = int(policies.get("max_reply_rounds", 25))
            if not product_offline and int(conversation.get("ai_reply_count", 0)) >= max_rounds:
                self.enter_manual_mode(scope_id)
                self.app_store.pause_conversation(scope_id)
                self.context_manager.add_message_by_chat(scope_id, send_user_id, item_id, "user", send_message)
                notice = str(policies.get("manual_review_notice") or "").strip()
                if not notice:
                    notice = "该事项需要人工核实，已经为您记录并转交人工处理，我们会在72小时内处理。"
                await self.send_msg(websocket, chat_id, send_user_id, notice)
                self.context_manager.add_message_by_chat(
                    scope_id, self.myid, item_id, "assistant", notice
                )
                logger.warning(f"会话 {chat_id} 已达到 {max_rounds} 次AI回复上限，自动停止")
                self.emit_event(
                    "conversation_limit", chat_id=chat_id, item_id=item_id,
                    scope_id=scope_id, count=conversation["ai_reply_count"], limit=max_rounds,
                    message=f"会话达到 {max_rounds} 次AI回复上限，已转人工",
                )
                return

            # 人工接管是持久且静默的：记录买家后续消息供人工查看，禁止
            # 自动问候、重复回执或任何模型回复打断人工处理。
            if not product_offline and self.is_manual_mode(scope_id):
                self.context_manager.add_message_by_chat(scope_id, send_user_id, item_id, "user", send_message)
                self.emit_event(
                    "manual_message_queued", chat_id=chat_id, item_id=item_id,
                    scope_id=scope_id, message="人工接管期间收到买家新消息",
                )
                logger.info(f"🔴 会话 {chat_id} 处于人工接管模式，买家消息已静默留给人工")
                return
            # 极速路径：先跑本地安全、门店、时间和套餐图片规则。
            # 命中后不读取商品页面、不调用模型，直接生成可审核/发送的答复。
            deterministic = None
            image_match = None
            image_asset = None
            actual_paid_amount = self.extract_actual_paid_amount(message)
            predecision = PolicyEngine(policies).evaluate(send_message, "")
            # Product keyword rules are an exclusive local fast path. Resolve
            # them before any semantic/model work, but never above offline.
            image_resolver = getattr(self.app_store, "resolve_image_asset", None)
            if image_resolver and not product_offline:
                image_match = image_resolver(item_id, send_message, scope_id)
            resolver = getattr(self.app_store, "resolve_deterministic", None)
            query_context = dict(conversation.get("query_context") or {})
            query_context.update(self._query_contexts.get(scope_id) or {})
            query_context.update(self._store_contexts.get(scope_id) or {})

            # Resolve high-confidence business questions locally first.  The
            # model is only an intent/slot parser for unresolved or genuinely
            # compound messages; it never writes the final business answer.
            if resolver and not image_match:
                try:
                    deterministic = resolver(
                        item_id, send_message, actual_paid_amount,
                        query_context or None,
                        getattr(self, "_order_routes", {}).get(scope_id) or None,
                    )
                except TypeError:
                    deterministic = resolver(item_id, send_message)

            # For the small uncertain remainder, interpret buyer language into
            # validated structure. Existing local rules still own every fact,
            # decision, formatting rule and buyer-visible reply.
            semantic_checker = getattr(self.bot, "should_analyze_message", None)
            semantic_parser = getattr(self.bot, "analyze_message", None)
            semantic_resolver = getattr(self.app_store, "resolve_semantic_analysis", None)
            semantic_mode_getter = getattr(self.bot, "semantic_router_mode", None)
            semantic_mode = semantic_mode_getter() if semantic_mode_getter else "on"
            semantic_analysis = None
            if (
                not product_offline and not image_match
                and predecision.action != "replace" and semantic_checker
                and semantic_parser and semantic_resolver
                and semantic_checker(send_message, deterministic)
            ):
                try:
                    product_getter = getattr(self.app_store, "get_v2_product", None)
                    product = product_getter(item_id) if product_getter else None
                    sku_getter = getattr(self.app_store, "list_product_skus", None)
                    sku_names = []
                    if sku_getter and product:
                        sku_names = [
                            str(sku.get("sku_name") or "")
                            for sku in sku_getter(item_id, product)
                            if sku.get("sku_name")
                        ]
                    semantic_context = {
                        "product_title": str((product or {}).get("title") or ""),
                        "sku_names": sku_names[:30],
                        "last_store_query": str(query_context.get("query") or ""),
                        "last_store_names": [
                            str(store.get("branch") or store.get("brand") or "")
                            for store in list(query_context.get("matches") or [])[:3]
                        ],
                        "last_selected_sku": str(query_context.get("selected_sku_name") or ""),
                        "pending_store_query": str(query_context.get("pending_store_query") or ""),
                    }
                    semantic_history = self.context_manager.get_context_by_chat(scope_id)
                    semantic_analysis = await asyncio.to_thread(
                        semantic_parser, send_message, semantic_history, semantic_context,
                    )
                    if semantic_mode == "shadow" and semantic_analysis:
                        logger.info("语义路由处于影子模式：已记录意图，不改变当前回复")
                except Exception as exc:
                    logger.warning(f"前置语义识别失败，继续使用原有规则：{exc}")

            # Apply model structure only when the completed local result
            # is eligible. Review/silent/system answers cannot be overridden.
            if (
                semantic_mode == "on" and semantic_analysis
                and not image_match and predecision.action != "replace"
                and semantic_checker and semantic_resolver
                and semantic_checker(send_message, deterministic)
            ):
                try:
                    enhanced = semantic_resolver(
                        item_id, send_message, semantic_analysis, actual_paid_amount,
                        query_context or None,
                        getattr(self, "_order_routes", {}).get(scope_id) or None,
                        deterministic,
                    )
                    if enhanced:
                        deterministic = enhanced
                        logger.info("语义辅助仅完成问题拆分，答案已由本地规则重新核验")
                except Exception as exc:
                    logger.warning(f"语义辅助失败，保留原有命中结果：{exc}")
            if deterministic and deterministic.get("query_context_update"):
                stored_context = dict(conversation.get("query_context") or {})
                for key, value in deterministic["query_context_update"].items():
                    stored_context[key] = value
                self._query_contexts[scope_id] = stored_context
                context_updater = getattr(self.app_store, "update_query_context", None)
                if context_updater:
                    context_updater(scope_id, stored_context)
            else:
                stored_context = dict(conversation.get("query_context") or {})
                context_changed = stored_context.pop("price_filters", None) is not None
                if stored_context.get("intent") == "aftersale":
                    for key in (
                        "intent", "aftersale_stage", "aftersale_prompt", "aftersale_payment_state",
                    ):
                        context_changed = stored_context.pop(key, None) is not None or context_changed
                if context_changed:
                    self._query_contexts[scope_id] = stored_context
                    context_updater = getattr(self.app_store, "update_query_context", None)
                    if context_updater:
                        context_updater(scope_id, stored_context)
            if deterministic and "store_matches" in deterministic:
                store_status = deterministic.get("store_status", "available")
                store_matches = deterministic.get("store_matches", [])
                trusted_qualities = {"exact", "contained", "area", "phonetic"}
                verified_store_context = bool(
                    store_status == "unavailable"
                    or (
                        store_status == "available" and store_matches
                        and all(
                            match.get("match_quality") in trusted_qualities
                            and (
                                match.get("match_quality") != "phonetic"
                                or float(match.get("score") or 0) >= 0.94
                            )
                            for match in store_matches
                        )
                    )
                )
                next_store_context = {
                    "query": deterministic.get("store_query", ""),
                    "matches": store_matches,
                    "status": store_status,
                    "verified": verified_store_context,
                    "store_sku_matrix": deterministic.get("store_sku_matrix", []),
                    "selected_sku_key": deterministic.get("query_context_update", {}).get(
                        "selected_sku_key", query_context.get("selected_sku_key", "")
                    ),
                    "selected_sku_name": deterministic.get("query_context_update", {}).get(
                        "selected_sku_name", query_context.get("selected_sku_name", "")
                    ),
                }
                next_store_context.update(deterministic.get("store_context_update") or {})
                self._store_contexts[scope_id] = next_store_context
            elif not deterministic or deterministic.get("kind") not in {
                "stores", "media", "media_context",
            }:
                self._store_contexts.pop(scope_id, None)
            if deterministic and deterministic.get("kind") == "offline":
                bot_reply = deterministic["reply"]
                logger.info("当前商品已下架，停止商品内容回复")
            elif image_match and image_match.get("status") in {"allow", "review"}:
                matched_asset = image_match["asset"]
                image_asset = matched_asset if matched_asset.get("file_path") else None
                bot_reply = matched_asset.get("reply_text") or "可以的，给您发一下对应的套餐图片。"
                deterministic = {
                    "reply": bot_reply,
                    "source": f"当前商品关键词规则：{matched_asset.get('name', '')}",
                    "decision": image_match["status"],
                    "kind": "keyword_rule",
                }
                logger.info(
                    f"关键词规则命中: {matched_asset.get('name')} ({image_match.get('status')})"
                )
            elif image_match and image_match.get("status") == "cooldown":
                bot_reply = "这张图片刚刚已经发过了，如需我可以继续帮您核对套餐信息。"
                deterministic = {
                    "reply": bot_reply,
                    "source": "套餐图片30分钟防重复规则",
                    "decision": "allow",
                }
            elif predecision.action == "replace":
                bot_reply = predecision.suggested_reply
                logger.info("议价请求命中最高规则，直接使用礼貌婉拒")
            elif deterministic:
                bot_reply = deterministic["reply"]
                logger.info(f"确定性规则命中: {deterministic.get('source', '')}")
            else:
                # 只有固定规则无法回答时，才准备商品知识和最近对话并调用一次模型。
                item_info = self.context_manager.get_item_info(item_id)
                if not item_info:
                    logger.info(f"从API获取商品信息: {item_id}")
                    api_result = self.xianyu.get_item_info(item_id)
                    if 'data' in api_result and 'itemDO' in api_result['data']:
                        item_info = api_result['data']['itemDO']
                        self.context_manager.save_item_info(item_id, item_info)
                    else:
                        logger.warning(f"获取商品信息失败: {api_result}")
                        # 商品接口偶发失败不能吞掉买家消息。仍使用本地知识和
                        # 大模型兜底，确保每条有效消息都有答复。
                        item_info = {}
                else:
                    logger.info(f"从数据库获取商品信息: {item_id}")

                platform_summary = self.build_item_description(item_info)
                item_description = self.app_store.build_merged_knowledge(
                    item_id, platform_summary, send_message
                )
                context = self.context_manager.get_context_by_chat(scope_id)
                bot_reply = await asyncio.to_thread(
                    self.bot.generate_reply,
                    send_message,
                    item_description,
                    context,
                )
                grounding_issue = self.model_reply_grounding_issue(
                    send_message, item_description, context, bot_reply
                )
                if grounding_issue:
                    risky_question = predecision.action == "review"
                    bot_reply = (
                        str(
                            policies.get("manual_review_notice")
                            or DEFAULT_POLICIES["manual_review_notice"]
                        ).strip()
                        if risky_question else self.grounding_fallback_reply(send_message)
                    )
                    deterministic = {
                        "reply": bot_reply,
                        "source": grounding_issue,
                        "decision": "review" if risky_question else "allow",
                        "kind": "model_grounding_guard",
                    }
                    logger.warning(grounding_issue)
                else:
                    operational_issue = self.model_reply_operational_issue(
                        bot_reply, self._order_routes.get(scope_id)
                    )
                    if operational_issue:
                        bot_reply = self.operational_fallback_reply(send_message)
                        deterministic = {
                            "reply": bot_reply,
                            "source": operational_issue,
                            "decision": "allow",
                            "kind": "model_operational_guard",
                        }
                        logger.warning(operational_issue)

            # If a newer buyer message arrived while the model was working, do
            # not audit or send this now-stale reply.
            if self._message_generations.get(scope_id) != generation:
                logger.info(f"会话 {chat_id} 草稿已过期，等待最新消息重新生成")
                return
            if deterministic and deterministic.get("decision") in {"silent", "silent_review"}:
                self.context_manager.add_message_by_chat(
                    scope_id, send_user_id, item_id, "user", send_message
                )
                if deterministic.get("decision") == "silent":
                    logger.info(f"当前商品规则要求静默等待后续可处理问题 (会话: {chat_id})")
                    return
                audit_id = self.app_store.create_audit(
                    scope_id=scope_id, chat_id=chat_id, user_id=send_user_id,
                    user_name=send_user_name, item_id=item_id,
                    user_message=send_message, draft_reply="", final_reply="",
                    action="review", reasons=[deterministic.get("source", "代买单报价由人工回复")],
                    status="pending",
                    order_id=(self._order_routes.get(scope_id) or {}).get("order_id", ""),
                    conversation_url=url_info,
                    order_url=(self._order_routes.get(scope_id) or {}).get("order_url", ""),
                )
                self.enter_manual_mode(scope_id)
                self.emit_event("pending_reply", audit_id=audit_id, chat_id=chat_id, item_id=item_id)
                logger.info(f"代买单报价问题已静默转人工 (会话: {chat_id})")
                return
            # 检查是否需要回复
            if bot_reply == "-":
                bot_reply = "您好，消息已收到。请问您想咨询当前商品的价格、使用规则还是适用门店？"
                logger.info(f"模型返回无需回复标记，已改用必回兜底: {send_user_name}")
            
            # 添加用户消息到上下文
            self.context_manager.add_message_by_chat(scope_id, send_user_id, item_id, "user", send_message)

            # 检查是否为价格意图，如果是则增加议价次数
            if predecision.action == "replace" or (not deterministic and self.bot.last_intent == "price"):
                self.context_manager.increment_bargain_count_by_chat(scope_id)
                bargain_count = self.context_manager.get_bargain_count_by_chat(scope_id)
                logger.info(f"用户 {send_user_name} 对商品 {item_id} 的议价次数: {bargain_count}")
            
            logger.info(f"机器人草稿: {bot_reply}")

            decision = predecision if predecision.action == "replace" else PolicyEngine(policies).evaluate(send_message, bot_reply)
            if deterministic and deterministic.get("decision") in {"review", "clarify"}:
                decision.action = "review"
                decision.reasons.append(f"确定性规则需要人工确认：{deterministic.get('source', '')}")
                decision.suggested_reply = bot_reply
            elif deterministic and predecision.action != "replace":
                # Fixed replies are generated by trusted local code. Do not let
                # a bare word such as “退款/过期/全国” turn a confirmed,
                # informational answer into a false positive review.
                decision.action = "allow"
                decision.reasons = []
                decision.suggested_reply = bot_reply
            reply_mode = policies.get("reply_mode", "review")
            requires_review = reply_mode == "review" or decision.action == "review"
            final_reply = self.sanitize_buyer_reply(decision.suggested_reply)
            audit_id = self.app_store.create_audit(
                scope_id=scope_id,
                chat_id=chat_id,
                user_id=send_user_id,
                user_name=send_user_name,
                item_id=item_id,
                user_message=send_message,
                draft_reply=bot_reply,
                final_reply=final_reply,
                action=decision.action,
                reasons=decision.reasons,
                status="pending" if requires_review else "sending",
                image_asset_id=image_asset.get("id") if image_asset else None,
                image_asset_name=image_asset.get("name") if image_asset else "",
                order_id=(self._order_routes.get(scope_id) or {}).get("order_id", ""),
                conversation_url=url_info,
                order_url=(self._order_routes.get(scope_id) or {}).get("order_url", ""),
            )

            if deterministic and deterministic.get("kind") == "manual_handoff":
                self.enter_manual_mode(scope_id)

            if requires_review:
                persistent_takeover = self.requires_persistent_manual_takeover(
                    deterministic, send_message
                )
                if persistent_takeover:
                    self.enter_manual_mode(scope_id)
                notice = str(policies.get("manual_review_notice") or "").strip()
                if not notice:
                    notice = "该事项需要人工核实，已经为您记录并转交人工处理，我们会在72小时内处理。"
                # 确定性风险规则已经生成了安全的政策说明：先告知买家，
                # 同时保留审核记录；模型草稿触发风险时仍只发统一审核回执。
                buyer_notice = final_reply if (
                    deterministic and deterministic.get("decision") in {"review", "clarify"}
                ) else notice
                buyer_notice = self.sanitize_buyer_reply(buyer_notice)
                if buyer_notice:
                    await self.send_msg(websocket, chat_id, send_user_id, buyer_notice)
                    self._review_notified_scopes.add(scope_id)
                    self.context_manager.add_message_by_chat(
                        scope_id, self.myid, item_id, "assistant", buyer_notice
                    )
                    logger.info(f"人工审核提示已发送 (会话: {chat_id}, 商品: {item_id})")
                logger.warning(
                    f"回复进入人工审核队列 (ID: {audit_id}, "
                    f"持续接管: {'是' if persistent_takeover else '否'}): "
                    f"{decision.reasons or ['当前为审核模式']}"
                )
                self.emit_event("pending_reply", audit_id=audit_id, chat_id=chat_id, item_id=item_id)
                return

            # 自动模式只有通过独立规则审查的草稿才会发送。
            # 模拟人工输入延迟
            if self.simulate_human_typing:
                # 基础延迟 0-1秒 + 每字 0.1-0.3秒
                base_delay = random.uniform(0, 1)
                typing_delay = len(final_reply) * random.uniform(0.1, 0.3)
                total_delay = base_delay + typing_delay
                # 设置最大延迟上限，防止过长回复等待太久
                total_delay = min(total_delay, 10.0)
                
                logger.info(f"模拟人工输入，延迟发送 {total_delay:.2f} 秒...")
                await asyncio.sleep(total_delay)
                
            if image_asset:
                await self.send_message_template(
                    websocket, chat_id, send_user_id, scope_id, item_id, final_reply,
                )
                self.context_manager.add_message_by_chat(
                    scope_id, self.myid, item_id, "assistant", final_reply
                )
                self.app_store.update_audit(audit_id, "sent", final_reply)
                self.app_store.record_ai_reply(scope_id)
            else:
                await self._send_and_record_auto_reply(
                    websocket, chat_id, send_user_id, scope_id, item_id, audit_id, final_reply,
                    ([value for value in final_reply.split(self.TEMPLATE_SEGMENT_TOKEN) if value.strip()]
                     if image_match else (deterministic or {}).get("reply_parts")),
                )
            if deterministic and deterministic.get("kind") == "media":
                recent_media = self._recent_buyer_media.get(scope_id)
                self._media_notice_times[scope_id] = recent_media[0] if recent_media else time.monotonic()
            if image_asset:
                try:
                    await self.send_image_asset(
                        websocket, chat_id, send_user_id, scope_id, image_asset
                    )
                except Exception as exc:
                    logger.error(f"套餐图片发送失败，文字回复已发送: {exc}")
                    self.emit_event(
                        "image_send_error", audit_id=audit_id,
                        image_asset_id=image_asset["id"], message=str(exc),
                    )

        except Exception as e:
            logger.error(f"处理消息时发生错误: {str(e)}")
            logger.debug(f"原始消息: {redact_sensitive_text(message_data)}")
            # 最后一层故障兜底：解析/接口/模型异常都不能静默吞消息。
            try:
                if websocket and chat_id and send_user_id:
                    await self.send_msg(
                        websocket, chat_id, send_user_id,
                        "您好，消息已收到，系统刚刚出现短暂异常，已为您记录，请稍后再试或发送更具体的文字信息。",
                    )
            except Exception as send_exc:
                logger.error(f"故障兜底回复发送失败: {send_exc}")

    async def send_heartbeat(self, ws):
        """发送心跳包并等待响应"""
        try:
            heartbeat_mid = generate_mid()
            heartbeat_msg = {
                "lwp": "/!",
                "headers": {
                    "mid": heartbeat_mid
                }
            }
            await ws.send(json.dumps(heartbeat_msg))
            self.last_heartbeat_time = time.time()
            logger.debug("心跳包已发送")
            return heartbeat_mid
        except Exception as e:
            logger.error(f"发送心跳包失败: {e}")
            raise

    async def heartbeat_loop(self, ws):
        """心跳维护循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要发送心跳
                if current_time - self.last_heartbeat_time >= self.heartbeat_interval:
                    await self.send_heartbeat(ws)
                
                # 检查上次心跳响应时间，如果超时则认为连接已断开
                if (current_time - self.last_heartbeat_response) > (self.heartbeat_interval + self.heartbeat_timeout):
                    logger.warning("心跳响应超时，可能连接已断开")
                    break
                
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"心跳循环出错: {e}")
                break

    async def handle_heartbeat_response(self, message_data):
        """处理心跳响应"""
        try:
            if (
                isinstance(message_data, dict)
                and "headers" in message_data
                and "mid" in message_data["headers"]
                and "code" in message_data
                and message_data["code"] == 200
            ):
                self.last_heartbeat_response = time.time()
                logger.debug("收到心跳响应")
                return True
        except Exception as e:
            logger.error(f"处理心跳响应出错: {e}")
        return False

    async def main(self):
        self.loop = asyncio.get_running_loop()
        self.running = True
        self.emit_event("status", value="starting")
        while self.running:
            try:
                # 重置连接重启标志
                self.connection_restart_flag = False
                
                headers = {
                    "Cookie": self.cookies_str,
                    "Host": "wss-goofish.dingtalk.com",
                    "Connection": "Upgrade",
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    "Origin": "https://www.goofish.com",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }

                async with websockets.connect(self.base_url, extra_headers=headers) as websocket:
                    self.ws = websocket
                    await self.init(websocket)
                    self.emit_event("status", value="connected")
                    
                    # 初始化心跳时间
                    self.last_heartbeat_time = time.time()
                    self.last_heartbeat_response = time.time()
                    
                    # 启动心跳任务
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(websocket))
                    
                    # 启动token刷新任务
                    self.token_refresh_task = asyncio.create_task(self.token_refresh_loop())
                    
                    async for message in websocket:
                        if not self.running:
                            break
                        try:
                            # 检查是否需要重启连接
                            if self.connection_restart_flag:
                                logger.info("检测到连接重启标志，准备重新建立连接...")
                                break
                                
                            message_data = json.loads(message)
                            
                            # 处理心跳响应
                            if await self.handle_heartbeat_response(message_data):
                                continue
                            
                            # 发送通用ACK响应
                            if "headers" in message_data and "mid" in message_data["headers"]:
                                ack = {
                                    "code": 200,
                                    "headers": {
                                        "mid": message_data["headers"]["mid"],
                                        "sid": message_data["headers"].get("sid", "")
                                    }
                                }
                                # 复制其他可能的header字段
                                for key in ["app-key", "ua", "dt"]:
                                    if key in message_data["headers"]:
                                        ack["headers"][key] = message_data["headers"][key]
                                await websocket.send(json.dumps(ack))
                            
                            # 处理其他消息
                            asyncio.create_task(self.handle_message(message_data, websocket))
                                
                        except json.JSONDecodeError:
                            logger.error("消息解析失败")
                        except Exception as e:
                            logger.error(f"处理消息时发生错误: {str(e)}")
                            logger.debug(f"原始消息: {message}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket连接已关闭")
                if self.running:
                    self.emit_event("status", value="reconnecting")
                
            except Exception as e:
                logger.error(f"连接发生错误: {e}")
                self.emit_event("status", value="error", message=str(e))
                
            finally:
                # 清理任务
                if self.heartbeat_task:
                    self.heartbeat_task.cancel()
                    try:
                        await self.heartbeat_task
                    except asyncio.CancelledError:
                        pass
                        
                if self.token_refresh_task:
                    self.token_refresh_task.cancel()
                    try:
                        await self.token_refresh_task
                    except asyncio.CancelledError:
                        pass
                
            # 如果是主动重启，立即重连；否则等待5秒
            if not self.running:
                break
            if self.connection_restart_flag:
                logger.info("主动重启连接，立即重连...")
            else:
                logger.info("等待5秒后重连...")
                await asyncio.sleep(5)
        self.emit_event("status", value="stopped")



def check_and_complete_env():
    """检查并补全关键环境变量"""
    # 定义关键变量及其默认无效值（占位符）
    critical_vars = {
        "API_KEY": "默认使用通义千问,apikey通过百炼模型平台获取",
        "COOKIES_STR": "your_cookies_here"
    }
    
    env_path = ".env"
    updated = False
    
    for key, placeholder in critical_vars.items():
        curr_val = os.getenv(key)
        
        # 如果变量未设置，或者值等于占位符
        if not curr_val or curr_val == placeholder:
            logger.warning(f"配置项 [{key}] 未设置或为默认值，请输入")
            while True:
                val = input(f"请输入 {key}: ").strip()
                if val:
                    # 更新当前环境
                    os.environ[key] = val
                    
                    # 尝试持久化到 .env
                    try:
                        # 如果没有.env文件，先创建
                        if not os.path.exists(env_path):
                            with open(env_path, 'w', encoding='utf-8') as f:
                                pass # Create empty file
                        
                        set_key(env_path, key, val)
                        updated = True
                    except Exception as e:
                        logger.warning(f"无法自动写入.env文件，请手动保存: {e}")
                    break
                else:
                    print(f"{key} 不能为空，请重新输入")
    
    if updated:
        logger.info("新的配置已保存/更新至 .env 文件中")


if __name__ == '__main__':
    # 加载环境变量
    if os.path.exists(".env"):
        load_dotenv()
        logger.info("已加载 .env 配置")
    
    if os.path.exists(".env.example"):
        load_dotenv(".env.example")  # 不会覆盖已存在的变量
        logger.info("已加载 .env.example 默认配置")
    
    # 配置日志级别
    log_level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    logger.remove()  # 移除默认handler
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    logger.info(f"日志级别设置为: {log_level}")
    
    # 交互式检查并补全配置
    check_and_complete_env()
    
    cookies_str = os.getenv("COOKIES_STR")
    bot = XianyuReplyBot()
    xianyuLive = XianyuLive(cookies_str)
    # 常驻进程
    asyncio.run(xianyuLive.main())
