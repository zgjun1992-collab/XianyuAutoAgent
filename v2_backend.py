import argparse
import asyncio
import ctypes
import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from build_info import APP_EDITION, APP_VERSION, BUILD_COMMIT

from openai import OpenAI

from XianyuAgent import XianyuReplyBot
from XianyuApis import XianyuApis
from app_store import PolicyEngine
from main import XianyuLive
from utils.xianyu_utils import trans_cookies
from v2_store import V2Store, extract_store_query


SUMMARY_PROMPT = """你是餐饮电子券商品资料整理员。用户会提供任意格式的原始资料。
只能整理原文明确出现的内容，禁止补充常识、猜测门店、价格、有效期或退款承诺。
整理的目标是调整结构和表达，不是压缩信息。原文每一条独立事实、限制、例外、提醒和咨询要求都必须保留，
不得因为内容看似次要、重复或无法归类而删除；无法归入固定栏目时放入“其他说明”。
生成summary前必须逐句核对原文，确认每条规则都能在summary或结构化字段中找到对应内容。
输入JSON中的sku是当前商品真实在售规格，优先级高于description文案。SKU名称、售价、库存和在售状态必须原样保留；
description只用于补充使用规则。文案价格与SKU冲突时以SKU为准，并把冲突写入risk_fields；文案出现但SKU中不存在的规格不得作为可售规格输出。
输出一个JSON对象，字段必须为：
summary: 适合客服快速阅读的中文分点摘要字符串；在原文有对应内容时，应完整包含商品规格、适用门店范围、
有效期和不可用日期、使用时间、堂食/外带/外卖、预约和等位、优惠互斥、叠加与限用数量、发券核销、
退款、发票及下单前提醒；禁止输出Python对象、JSON片段或字段字典；
risk_fields: 需要人工确认的高风险或矛盾字段数组；
facts: 对象，按原文明确信息尽量完整提取，允许包含商品类型、有效期、适用日期、不可用日期、
使用时间、预约要求、堂食限制、外带限制、外卖限制、包间限制、酒水限制、锅底限制、服务费限制、
优惠同享、叠加规则、不同面额混用、单次或每桌限用数量、退款规则、发码平台、发码方式、领取方式、
核销方式、发票规则、适用人群、人数限制和下单前提醒；原文未说明的内容不得猜测；
products: 仅填写代金券规格数组，每个规格必须独立一项，字段为name、option_type、face_value、sale_price、applicable_time、composition、max_stack。
sale_options: 仅填写套餐、自助餐、人数餐等非代金券商品选项数组，每项字段为name、option_type、sale_price、people_count、applicable_day、meal_period、applicable_time；
stores: 商品文案明确列出的适用门店数组，每项字段为brand、branch、province、city、district、address、phone；
只有原文出现具体门店名称时才可填写，不能把“全国通用”“全国60店”等概括描述推测为门店。
其中face_value是券面额，sale_price是售价，composition是实际发券组成。例如“200元：135.8（发两张100）”必须写成
{"name":"200元代金券","option_type":"代金券","face_value":"200","sale_price":"135.8","composition":"100元券2张"}；
“400元：279.9（300+100）”的composition必须为“300元券1张＋100元券1张”。
套餐、菜品、重量、人数和普通商品售价不得填写为face_value，也不得放入products，应逐项放入sale_options；例如“2.6斤烤鱼套餐售价118元”中的118只是套餐售价，不是118元代金券。
人数、工作日/周末/节假日、早餐/午餐/晚餐等限制条件只有原文明确出现时才可写入对应规格字段，不得从商品名称或常识补全。
但使用时间采用统一业务默认值：原文没有明确“仅限工作日/周末/节假日”、特定日期不可用或午晚市等时段限制时，facts.使用时间必须写“适用门店营业时间内可用”；不得编造具体几点营业。原文存在明确限制时必须完整保留，具体禁用日期优先于该默认值。
不得把多个面额和售价合并到同一个字符串，不得遗漏括号中的发券组成。
叠加规则要与发券组成分开。默认仅支持同面额叠加；只有原文明写“不同面额可叠加”时，
才可以在facts.叠加规则中写不同面额叠加，禁止自行扩展；
“100×4/发4张”是发券组成，不是叠加上限。明确“可叠加N张/最多N张”才是有限上限；
写“可叠加/支持叠加使用/同面额可叠加”但没有张数，以及“无限叠加/不限制使用张数”时，均整理为不限制使用张数；
“不可叠加/每次限用1张”分别按禁止叠加或上限1张整理，不能与每日不限次数、限购数量、单品优惠数量混淆。
time_rules: 数组，每项包含label、day_type(any/weekday/weekend)、start_time(HH:MM)、end_time(HH:MM)、allowed(boolean)、reply、next_hint。
时间规则只能从当前商品原文提取；若同一商品的不同规格分别适用于工作日、周末或餐段，
reply必须写明当前时段对应可用的规格名称/面额和售价，不得推荐其他商品。
原文没有明确时间规则时time_rules必须为空数组。不要输出Markdown。"""

AFTERSALE_POLICY_PROMPT = """你是餐饮电子卡券售后政策整理员。
用户会用自然语言描述发货、使用、退款和过期政策。只能归纳原文明确出现的内容，禁止新增承诺、期限、比例或退款条件。
请输出简洁中文纯文本，固定使用以下栏目且栏目之间不要空行：
【购买与发货】
【非卡券质量问题退款】
【卡券质量问题退款】
【过期与收货】
没有资料的栏目写“未说明”，不要输出Markdown、JSON或解释。"""


class BackendState:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.store = V2Store(os.path.join(self.data_dir, "xianyu-v2.db"))
        self.runtime = {
            "api_key": "",
            "cookie": "",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus",
        }
        self.live = None
        self.worker = None
        self.service_status = "stopped"
        self.service_message = ""
        self.verification_required = False
        self.verification_url = ""
        self.lock = threading.RLock()
        self._service_mutex_handle = None
        self._service_mutex_api = None

    def _acquire_service_mutex(self):
        """Allow only one live customer-service worker per local data directory."""
        if os.name != "nt" or self._service_mutex_handle:
            return
        digest = hashlib.sha256(os.path.normcase(self.data_dir).encode("utf-8")).hexdigest()[:24]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Keep a named kernel object open for the lifetime of the worker.  An
        # event is used instead of a mutex because the worker may stop on a
        # different thread than the HTTP request that started it.
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateEventW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        ctypes.set_last_error(0)
        handle = kernel32.CreateEventW(None, True, False, f"Local\\XianyuCardAI-{digest}")
        if not handle:
            raise OSError(ctypes.get_last_error(), "无法创建客服单实例锁")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            raise ValueError("同一数据目录已有客服实例运行，请先关闭旧程序后再启动")
        self._service_mutex_handle = handle
        self._service_mutex_api = kernel32

    def _release_service_mutex(self):
        handle = self._service_mutex_handle
        kernel32 = self._service_mutex_api
        self._service_mutex_handle = None
        self._service_mutex_api = None
        if handle and kernel32:
            kernel32.CloseHandle(handle)

    def configure(self, payload):
        with self.lock:
            previous_cookie = self.runtime["cookie"]
            for key in ("api_key", "cookie", "base_url", "model"):
                if key in payload and payload[key] is not None:
                    self.runtime[key] = str(payload[key]).strip()
            for env_key, value in (
                ("API_KEY", self.runtime["api_key"]),
                ("COOKIES_STR", self.runtime["cookie"]),
                ("MODEL_BASE_URL", self.runtime["base_url"]),
                ("MODEL_NAME", self.runtime["model"]),
            ):
                if value:
                    os.environ[env_key] = value
            if (
                self.live
                and self.runtime["cookie"]
                and self.runtime["cookie"] != previous_cookie
            ):
                previous = trans_cookies(previous_cookie)
                current = trans_cookies(self.runtime["cookie"])
                auth_keys = ("unb", "_m_h5_tk", "_m_h5_tk_enc", "cookie2")
                auth_changed = any(previous.get(key) != current.get(key) for key in auth_keys)
                verification_cookie_changed = (
                    self.verification_required
                    and self.runtime["cookie"] != previous_cookie
                )
                if auth_changed or verification_cookie_changed:
                    self.live.update_cookie(self.runtime["cookie"])
                    self.service_status = "reconnecting"
                    self.service_message = "已同步验证凭据，正在重新获取消息Token"
        return self.config_status()

    def config_status(self):
        return {
            "api_key_saved": bool(self.runtime["api_key"]),
            "cookie_saved": bool(self.runtime["cookie"]),
            "base_url": self.runtime["base_url"],
            "model": self.runtime["model"],
        }

    def ai_client(self):
        if not self.runtime["api_key"]:
            raise ValueError("请先在AI配置中保存API Key")
        return OpenAI(api_key=self.runtime["api_key"], base_url=self.runtime["base_url"])

    @staticmethod
    def _parse_summary(content: str):
        content = (content or "").strip()
        try:
            structured = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.S)
            if not match:
                raise ValueError("模型没有返回可识别的归纳结果")
            structured = json.loads(match.group(0))
        summary = structured.get("summary") or ""
        if isinstance(summary, list):
            summary = "\n".join(f"• {value}" for value in summary)
        return str(summary), structured

    def _generate_summary(self, source_text: str):
        """Summarize text only. Product and package images are never sent to the model."""
        request = {
            "model": self.runtime["model"],
            "messages": [
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": source_text},
            ],
            "temperature": 0,
            "max_tokens": 3200,
            "timeout": 45,
        }
        try:
            result = self.ai_client().chat.completions.create(
                **request, response_format={"type": "json_object"}
            )
        except Exception as exc:
            # Some OpenAI-compatible gateways do not implement response_format.
            if "response_format" not in str(exc).lower() and "400" not in str(exc):
                raise
            result = self.ai_client().chat.completions.create(**request)
        return self._parse_summary(result.choices[0].message.content or "")

    def summarize_product(self, item_id: str):
        product = self.store.get_v2_product(item_id)
        if not product:
            raise ValueError("商品不存在")
        platform_source = str(product.get("platform_summary") or "").strip()
        manual_source = str(product.get("raw_text") or "").strip() if product.get("manual_edited") else ""
        source_text = "\n".join(value for value in (
            platform_source,
            ("【人工补充资料】\n" + manual_source) if manual_source else "",
        ) if value) or str(product.get("raw_text") or "").strip()
        if not source_text:
            raise ValueError("当前商品没有可归纳的资料")
        summary, structured = self._generate_summary(source_text)
        return self.store.save_ai_summary(item_id, summary, structured)

    def summarize_aftersale_policy(self, text: str) -> str:
        text = str(text or "").strip()
        if not text:
            raise ValueError("请先填写发货与退款政策")
        result = self.ai_client().chat.completions.create(
            model=self.runtime["model"],
            messages=[
                {"role": "system", "content": AFTERSALE_POLICY_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=700,
            timeout=30,
        )
        summary = str(result.choices[0].message.content or "").strip()
        if not summary:
            raise ValueError("模型未返回可用的政策归纳结果")
        return summary

    def preview_store_text(self, text: str):
        preview = self.store.preview_store_text(text)
        fallback = (
            f"共归纳出{preview['province_count']}个省份、{preview['city_count']}个城市、"
            f"{preview['store_count']}家可用门店；导入时只保存原文明确出现的门店。"
        )
        preview["ai_summary"] = fallback
        if not self.runtime["api_key"]:
            return preview
        groups = [
            {"province": group["province"], "city": group["city"], "stores": group["stores"]}
            for group in preview["preview"]
        ]
        try:
            result = self.ai_client().chat.completions.create(
                model=self.runtime["model"],
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是门店资料整理员。根据已经解析的省市门店JSON写一段不超过120字的中文归纳。"
                            "禁止新增、改写或猜测任何门店；只说明覆盖地区、数量和资料缺失情况。"
                        ),
                    },
                    {"role": "user", "content": json.dumps(groups, ensure_ascii=False)},
                ],
                temperature=0,
                max_tokens=180,
                timeout=15,
            )
            ai_summary = str(result.choices[0].message.content or "").strip()
            if ai_summary:
                preview["ai_summary"] = ai_summary
        except Exception:
            # Parsing/importing remains available even if the model is temporarily unavailable.
            pass
        return preview

    @staticmethod
    def _collect_image_urls(value):
        found = []

        def walk(node):
            if isinstance(node, dict):
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)
            elif isinstance(node, str):
                candidate = node.strip()
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                if candidate.startswith("http") and any(
                    marker in candidate.lower() for marker in ("alicdn", ".jpg", ".jpeg", ".png", ".webp")
                ):
                    found.append(candidate)

        walk(value)
        return list(dict.fromkeys(found))

    @staticmethod
    def _collect_sku_records(value):
        """Find priced SKU arrays even when Goofish moves them below a nested key."""
        found = []
        seen = set()

        def looks_like_sku(row):
            if not isinstance(row, dict):
                return False
            has_price = any(
                row.get(key) not in (None, "")
                for key in ("priceInCent", "price", "soldPrice")
            )
            has_identity = bool(
                row.get("propertyList") or row.get("skuId") or row.get("id")
                or row.get("name") or row.get("title") or row.get("features")
            )
            return has_price and has_identity

        def walk(node, parent_key=""):
            if isinstance(node, dict):
                for key, child in node.items():
                    if isinstance(child, list) and "sku" in str(key).lower():
                        for row in child:
                            if looks_like_sku(row):
                                fingerprint = json.dumps(row, ensure_ascii=False, sort_keys=True)
                                if fingerprint not in seen:
                                    seen.add(fingerprint)
                                    found.append(row)
                    walk(child, str(key))
            elif isinstance(node, list):
                for child in node:
                    walk(child, parent_key)

        walk(value)
        return found

    def sync_products(self):
        if not self.runtime["cookie"]:
            raise ValueError("请先在内置闲鱼页面登录")
        if not self.runtime["api_key"]:
            raise ValueError("请先保存百炼API Key，才能根据商品文案生成初始知识")
        api = XianyuApis(interactive=False)
        cookies = trans_cookies(self.runtime["cookie"])
        if not cookies.get("_m_h5_tk"):
            raise ValueError("闲鱼登录状态已失效，请在内置闲鱼页面重新登录后再同步商品")
        api.session.cookies.update(cookies)
        user_id = cookies.get("unb")
        if not user_id:
            raise ValueError("登录信息中缺少闲鱼账号ID，请重新登录后同步")
        cards = api.get_all_user_items(user_id)
        synced = []
        skipped = []
        failed = []
        for card in cards:
            data = card.get("cardData") if isinstance(card, dict) else None
            data = data if isinstance(data, dict) else (card if isinstance(card, dict) else {})
            status = data.get("itemStatus", 0)
            if str(status).lower() not in {"0", "onsale", "on_sale", "selling"}:
                continue
            detail = data.get("detailParams") or {}
            item_id = str(detail.get("itemId") or data.get("id") or "").strip()
            if not item_id:
                continue
            if self.store.is_product_ignored(item_id):
                skipped.append(item_id)
                continue
            try:
                detail_result = api.get_item_info(item_id)
                item_do = ((detail_result or {}).get("data") or {}).get("itemDO") or {}
                title = str(item_do.get("title") or data.get("title") or detail.get("title") or "").strip()
                price = item_do.get("soldPrice") or (data.get("priceInfo") or {}).get("price") or detail.get("soldPrice") or ""
                sku_records = item_do.get("skuList") or self._collect_sku_records(
                    (detail_result or {}).get("data") or detail_result or {}
                )
                platform = {
                    "title": title,
                    "description": item_do.get("desc") or "",
                    "price": str(price),
                    "stock": item_do.get("quantity"),
                    "sku": sku_records,
                }
                platform_summary = json.dumps(platform, ensure_ascii=False)
                images = self._collect_image_urls(item_do)
                thumbnail = str((data.get("picInfo") or {}).get("picUrl") or "").strip()
                if thumbnail.startswith("//"):
                    thumbnail = "https:" + thumbnail
                if thumbnail and thumbnail not in images:
                    images.insert(0, thumbnail)
                old = self.store.get_v2_product(item_id)
                unchanged = bool(
                    old and old.get("platform_summary") == platform_summary
                    and old.get("ai_summary")
                    and old.get("sync_status") != "summary_failed"
                )
                self.store.upsert_synced_product({
                    "item_id": item_id, "title": title, "platform_summary": platform_summary,
                    "thumbnail_url": thumbnail, "image_urls": images, "price": str(price),
                    "item_status": "onsale",
                })
                incomplete = not str(platform.get("description") or "").strip() and not platform.get("sku")
                if unchanged:
                    # Existing products keep their effective store bindings. A normal
                    # sync must never create/bind a second page-derived store list;
                    # store changes are staged and require explicit operator approval.
                    skipped.append(item_id)
                else:
                    if incomplete:
                        self.store.stage_source_update(item_id, "", {}, "")
                        failed.append({"item_id": item_id, "error": "商品详情文案和规格均为空，已保留当前知识"})
                        continue
                    description = str(item_do.get("desc") or "")
                    try:
                        summary, structured = self._generate_summary(platform_summary)
                    except Exception as summary_exc:
                        # Page acquisition and AI condensation are independent.
                        # A model timeout must not discard rules/stores already
                        # present in the complete marketplace payload.
                        fallback = description or platform_summary
                        has_effective_knowledge = bool(
                            old and (str(old.get("raw_text") or "").strip()
                                     or str(old.get("ai_summary") or "").strip())
                        )
                        if has_effective_knowledge:
                            self.store.stage_source_update(item_id, fallback, {}, description)
                        else:
                            self.store.save_synced_summary(item_id, fallback, {})
                            self.store.sync_platform_store_list(
                                item_id, title, description, {}
                            )
                            self.store.mark_summary_failed(item_id)
                        failed.append({
                            "item_id": item_id,
                            "error": f"AI归纳失败，已保留完整商品页面资料：{summary_exc}",
                        })
                        continue
                    if old:
                        self.store.stage_source_update(item_id, summary, structured, description)
                    else:
                        self.store.save_synced_summary(item_id, summary, structured)
                        self.store.sync_platform_store_list(
                            item_id, title, description, structured
                        )
                    synced.append(item_id)
                time.sleep(0.12)
            except Exception as exc:
                failed.append({"item_id": item_id, "error": str(exc)})
        seen = synced + skipped + [entry["item_id"] for entry in failed]
        self.store.mark_unsynced_products(seen)
        self.store.add_event(
            "product_sync",
            f"闲鱼在售商品同步完成：更新 {len(synced)}，未变化 {len(skipped)}，失败 {len(failed)}",
            {"synced": synced, "skipped": skipped, "failed": failed},
        )
        return {"synced": len(synced), "unchanged": len(skipped), "failed": failed, "total": len(seen)}

    def save_image_asset(self, payload):
        item_id = str(payload.get("item_id") or "").strip()
        source_value = str(payload.get("source_path") or "").strip()
        source_path = os.path.abspath(source_value) if source_value else ""
        if not item_id:
            raise ValueError("请先选择商品")
        previous = self.store.get_image_asset(int(payload["id"])) if payload.get("id") else None
        if source_path and not os.path.isfile(source_path):
            raise FileNotFoundError("关键词规则图片不存在")
        if not source_path and not str(payload.get("reply_text") or "").strip():
            raise ValueError("请至少填写一段触发后发送的文字或选择图片")
        extension = os.path.splitext(source_path)[1].lower() if source_path else ""
        if extension and extension not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("图片仅支持 PNG、JPG、JPEG 或 WebP")
        if source_path and os.path.getsize(source_path) > 10 * 1024 * 1024:
            raise ValueError("单张图片不能超过10MB")
        safe_item_id = re.sub(r"[^0-9A-Za-z_-]+", "_", item_id)[:80] or "product"
        asset_dir = os.path.join(self.data_dir, "product-images", safe_item_id)
        os.makedirs(asset_dir, exist_ok=True)
        target_path = os.path.join(asset_dir, f"{uuid.uuid4().hex}{extension}") if source_path else str((previous or {}).get("file_path") or "")
        if source_path and source_path != target_path:
            shutil.copy2(source_path, target_path)
        record = dict(payload)
        record.update({
            "item_id": item_id,
            "file_path": target_path,
            "original_name": os.path.basename(source_path) if source_path else str((previous or {}).get("original_name") or ""),
        })
        try:
            saved = self.store.save_image_asset(record)
            if source_path and previous and previous.get("file_path") != target_path:
                try:
                    os.remove(previous["file_path"])
                except OSError:
                    pass
            return saved
        except Exception:
            try:
                if source_path:
                    os.remove(target_path)
            except OSError:
                pass
            raise

    def delete_image_asset(self, asset_id: int):
        asset = self.store.delete_image_asset(asset_id)
        if not asset:
            raise ValueError("套餐图片不存在")
        try:
            os.remove(asset["file_path"])
        except OSError:
            pass
        return {"id": int(asset_id)}

    def test_ai(self):
        # A configuration probe must fail fast.  The SDK retries transient
        # network failures by default, which previously let this endpoint run
        # longer than Electron's RPC deadline and made the whole UI look dead.
        result = self.ai_client().with_options(max_retries=0, timeout=15).chat.completions.create(
            model=self.runtime["model"],
            messages=[{"role": "user", "content": "只回复：连接成功"}],
            temperature=0,
            max_tokens=12,
        )
        # Reaching this line is the success condition. Do not display arbitrary
        # provider output (or a wrongly decoded provider string) in the UI.
        _ = result.choices[0].message.content
        return {"reply": "连接成功"}

    def test_reply(self, payload):
        item_id = str(payload.get("item_id") or "").strip()
        message = str(payload.get("message") or "").strip()
        store_query = str(payload.get("store_query") or "").strip()
        product = self.store.get_v2_product(item_id)
        if not product:
            raise ValueError("请选择已建立知识库的商品")
        evidence = [
            {"source": "用户原始资料", "priority": 1, "status": "used" if product["raw_text"] else "empty"},
            {"source": "AI归纳摘要", "priority": 2, "status": "used" if product["ai_summary"] else "empty"},
            {"source": "闲鱼商品页面", "priority": 3, "status": "fallback"},
        ]
        safety = PolicyEngine(self.store.get_policies()).evaluate(message, "")
        if safety.action == "replace":
            return {
                "reply": safety.suggested_reply,
                "action": "allow",
                "reason": safety.reasons[0],
                "time": self.store.evaluate_time(item_id, payload.get("at")),
                "evidence": [{"source": "全局最高规则：议价婉拒", "priority": 0, "status": "decisive"}] + evidence,
            }
        image_match = self.store.resolve_image_asset(
            item_id, message, f"test:{item_id}"
        )
        if image_match and image_match.get("status") in {"allow", "review"}:
            asset = image_match["asset"]
            return {
                "reply": asset.get("reply_text") or "可以的，给您发一下对应的套餐图片。",
                "action": image_match["status"],
                "reason": image_match["reason"],
                "image_asset": {"id": asset["id"], "name": asset["name"]},
                "time": self.store.evaluate_time(item_id, payload.get("at")),
                "evidence": [{"source": f"当前商品套餐图片：{asset['name']}", "priority": 0, "status": "decisive"}] + evidence,
            }
        deterministic = self.store.resolve_deterministic(item_id, message)
        if deterministic:
            return {
                "reply": deterministic["reply"],
                "action": deterministic.get("decision", "allow"),
                "reason": deterministic.get("source", "本地固定规则"),
                "time": self.store.evaluate_time(item_id, payload.get("at")),
                "evidence": [{
                    "source": deterministic.get("source", "本地固定规则"),
                    "priority": 0,
                    "status": "decisive",
                }] + evidence,
            }
        time_result = self.store.evaluate_time(item_id, payload.get("at"))
        if time_result["status"] == "blocked":
            rule = time_result["rule"] or {}
            reply = rule.get("reply") or "这个商品当前时段不可使用。"
            if rule.get("next_hint"):
                reply += " " + rule["next_hint"]
            return {
                "reply": reply,
                "action": "block",
                "reason": "命中当前商品的时间限制",
                "time": time_result,
                "evidence": evidence + [{"source": f"时间规则：{rule.get('label', '')}", "priority": 0, "status": "decisive"}],
            }

        store_words = (
            "门店", "店里", "店能", "店可", "可用", "能用", "适用",
            "地址", "位置", "在哪", "电话", "号码", "营业", "开门", "打烊",
        )
        if store_query or any(word in message for word in store_words):
            query = store_query or extract_store_query(message)
            store_result = self.store.search_store(item_id, query)
            if store_result["status"] == "available":
                reply = self.store.format_store_matches(store_result["matches"], query, message)
                action = "allow"
            elif store_result["status"] == "no_store_list":
                reply = "当前商品尚未配置可用门店资料，需要人工核实。"
                action = "review"
            else:
                reply = self.store.format_store_unavailable(query)
                action = "deny"
            return {
                "reply": reply,
                "action": action,
                "reason": "仅查询当前商品绑定的门店表",
                "store": store_result,
                "time": time_result,
                "evidence": evidence + [{"source": "当前商品绑定门店表", "priority": 0, "status": "decisive"}],
            }

        if time_result["status"] == "allowed":
            rule = time_result["rule"] or {}
            reply = rule.get("reply") or f"这张券当前{time_result['meal_period']}时段可用。"
            return {
                "reply": reply,
                "action": "allow",
                "reason": "只推荐买家正在咨询的当前商品",
                "time": time_result,
                "evidence": evidence + [{"source": f"时间规则：{rule.get('label', '')}", "priority": 0, "status": "decisive"}],
            }

        return {
            "reply": "没有命中确定性门店或时间规则；实际运行时会结合当前商品资料生成答复。",
            "action": "review",
            "reason": "测试中心不会发送消息，也不会推荐其他商品",
            "time": time_result,
            "evidence": evidence,
        }

    def on_live_event(self, event):
        event_type = event.get("type", "live")
        if event_type == "status":
            self.service_status = event.get("value", "error")
            self.service_message = event.get("message", "")
            if self.service_status == "connected":
                self.verification_required = False
                self.verification_url = ""
        elif event_type == "verification_required":
            self.service_status = "verification_required"
            self.service_message = event.get("message") or "闲鱼要求安全验证"
            self.verification_required = True
            self.verification_url = str(event.get("url") or "").strip()
        self.store.add_event(event_type, event.get("message") or event_type, event)

    def start_service(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return self.service_state()
            if not self.runtime["api_key"]:
                raise ValueError("请先保存API Key")
            if not self.runtime["cookie"]:
                raise ValueError("请先在内置闲鱼登录，等待Cookie自动同步")
            self._acquire_service_mutex()
            os.environ["API_KEY"] = self.runtime["api_key"]
            os.environ["COOKIES_STR"] = self.runtime["cookie"]
            os.environ["MODEL_BASE_URL"] = self.runtime["base_url"]
            os.environ["MODEL_NAME"] = self.runtime["model"]
            bot = XianyuReplyBot()
            try:
                self.live = XianyuLive(
                    self.runtime["cookie"],
                    bot_instance=bot,
                    app_store=self.store,
                    event_callback=self.on_live_event,
                    interactive=False,
                )
            except Exception:
                self._release_service_mutex()
                raise
            self.service_status = "starting"
            self.service_message = ""
            self.verification_required = False
            self.verification_url = ""

            def run():
                try:
                    asyncio.run(self.live.main())
                except Exception as exc:
                    self.service_status = "error"
                    self.service_message = str(exc)
                    self.store.add_event("service_error", str(exc), {})
                finally:
                    self._release_service_mutex()

            self.worker = threading.Thread(target=run, name="xianyu-v2-live", daemon=True)
            self.worker.start()
        return self.service_state()

    def stop_service(self):
        with self.lock:
            if self.live:
                self.live.stop()
            self.service_status = "stopping"
        return self.service_state()

    def retry_service_auth(self):
        """Retry message-token acquisition after browser verification."""
        with self.lock:
            if not self.live or not self.worker or not self.worker.is_alive():
                raise RuntimeError("客服服务未运行，请先启动客服")
            self.live.current_token = None
            self.live.last_token_refresh_time = 0
            self.live.verification_required = False
            self.live.verification_url = ""
            self.live.connection_restart_flag = True
            if self.live.loop and self.live.ws:
                asyncio.run_coroutine_threadsafe(self.live.ws.close(), self.live.loop)
            self.verification_required = False
            self.verification_url = ""
            self.service_status = "reconnecting"
            self.service_message = "验证已完成，正在重新获取消息Token"
        return self.service_state()

    def service_state(self):
        return {
            "status": self.service_status,
            "message": self.service_message,
            "verification_required": self.verification_required,
            "verification_url": self.verification_url,
        }

    def approve(self, audit_id: int, final_reply: str):
        if not self.live:
            raise RuntimeError("客服服务未连接")
        self.live.approve_reply(audit_id, final_reply, resume_ai=True)
        return {"ok": True}

    def reject(self, audit_id: int, final_reply: str = ""):
        if self.live:
            return self.live.reject_reply(audit_id, final_reply)
        audit = self.store.get_audit(audit_id)
        if not audit or audit["status"] != "pending":
            raise ValueError("该回复已处理或不存在")
        self.store.update_audit(audit_id, "rejected", final_reply)
        return {"id": audit_id, "status": "rejected"}


class ApiHandler(BaseHTTPRequestHandler):
    state: BackendState = None

    def log_message(self, _format, *_args):
        return

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        length = min(int(self.headers.get("Content-Length", "0") or 0), 5_000_000)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _ok(self, data=None):
        self._send(200, {"ok": True, "data": data})

    def _error(self, exc):
        message = str(exc).strip() or "操作失败"
        lower = message.lower()
        if "api key" in lower and any(word in lower for word in ("invalid", "incorrect", "unauthorized")):
            message = "API Key无效，请到“AI与连接”重新保存后测试。"
        elif "image format is illegal" in lower:
            message = "模型无法读取商品图片。本版已改为仅根据商品文案归纳，请重启新版后再试。"
        elif "object could not be cloned" in lower:
            message = "界面数据传递失败，请重试；若仍出现请重启软件。"
        elif len(message) > 420:
            message = message[:420] + "…"
        self._send(400, {"ok": False, "error": message})

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                return self._ok({
                    "status": "ok",
                    "edition": APP_EDITION,
                    "version": APP_VERSION,
                    "build_commit": BUILD_COMMIT,
                    "pid": os.getpid(),
                })
            if path == "/snapshot":
                products = self.state.store.list_v2_products()
                store_lists = self.state.store.list_store_lists()
                refund_orders = self.state.store.list_refund_orders()
                return self._ok({
                    "dashboard": self.state.store.dashboard(products, store_lists, refund_orders),
                    "products": products,
                    "store_lists": store_lists,
                    "reviews": self.state.store.list_audits(limit=200),
                    "refund_orders": refund_orders,
                    "conversations": self.state.store.list_conversations(),
                    "service": self.state.service_state(),
                    "config": self.state.config_status(),
                    "policies": self.state.store.get_policies(),
                })
            if path == "/products":
                return self._ok(self.state.store.list_v2_products())
            if path.startswith("/products/"):
                parts = path.split("/")
                item_id = unquote(parts[2])
                if len(parts) == 4 and parts[3] == "versions":
                    return self._ok(self.state.store.list_versions(item_id))
                return self._ok(self.state.store.get_v2_product(item_id))
            if path == "/store-lists":
                return self._ok(self.state.store.list_store_lists())
            if path.startswith("/store-lists/") and path.endswith("/stores"):
                list_id = int(path.split("/")[2])
                query = parse_qs(urlparse(self.path).query).get("q", [""])[0]
                return self._ok(self.state.store.list_stores(list_id, query))
            if path == "/reviews":
                return self._ok(self.state.store.list_audits(limit=200))
            if path == "/refund-orders":
                return self._ok(self.state.store.list_refund_orders())
            if path == "/events":
                return self._ok(self.state.store.list_events(100))
            if path == "/service/status":
                return self._ok(self.state.service_state())
            if path == "/config/status":
                return self._ok(self.state.config_status())
            return self._send(404, {"ok": False, "error": "接口不存在"})
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            body = self._json_body()
            if path == "/config/runtime":
                return self._ok(self.state.configure(body))
            if path == "/config/test-ai":
                return self._ok(self.state.test_ai())
            if path == "/products":
                product = self.state.store.save_v2_product(
                    body.get("item_id"), body.get("title", ""), body.get("raw_text", ""),
                    body.get("enabled", True), body.get("note", "手工保存"),
                    body.get("first_reply_enabled"), body.get("first_reply_text"),
                    body.get("first_reply_manual"),
                    body.get("coupon_type"), body.get("coupon_type_custom"),
                    body.get("coupon_instructions"),
                    body.get("custom_policy_enabled"), body.get("custom_policy_raw"),
                    body.get("custom_policy_summary"), body.get("order_notice_enabled"),
                )
                return self._ok(product)
            if path == "/products/sync":
                return self._ok(self.state.sync_products())
            if path.startswith("/products/") and path.endswith("/enabled"):
                item_id = unquote(path.split("/")[2])
                return self._ok(self.state.store.set_product_enabled(
                    item_id, bool(body.get("enabled"))
                ))
            if path.startswith("/products/") and path.endswith("/summarize"):
                item_id = unquote(path.split("/")[2])
                return self._ok(self.state.summarize_product(item_id))
            if path.startswith("/products/") and path.endswith("/first-reply/regenerate"):
                item_id = unquote(path.split("/")[2])
                return self._ok(self.state.store.refresh_first_reply(item_id, force=True))
            if path.startswith("/products/") and "/versions/" in path and path.endswith("/restore"):
                parts = path.split("/")
                item_id = unquote(parts[2])
                version_id = int(parts[4])
                return self._ok(self.state.store.restore_version(item_id, version_id))
            if path.startswith("/products/") and path.endswith("/apply-source-update"):
                item_id = unquote(path.split("/")[2])
                return self._ok(self.state.store.apply_source_update(item_id, body.get("sections", [])))
            if path.startswith("/products/") and path.endswith("/images"):
                item_id = unquote(path.split("/")[2])
                body["item_id"] = item_id
                return self._ok(self.state.save_image_asset(body))
            if path.startswith("/products/") and path.endswith("/stores"):
                item_id = unquote(path.split("/")[2])
                self.state.store.set_product_store_lists(item_id, body.get("list_ids", []))
                return self._ok(self.state.store.get_v2_product(item_id))
            if path.startswith("/products/") and path.endswith("/sku-stores"):
                item_id = unquote(path.split("/")[2])
                return self._ok(self.state.store.set_sku_store_rule(
                    item_id, body.get("sku_key", ""), body.get("sku_name", ""),
                    body.get("mode", "inherit"), body.get("list_ids", []),
                    body.get("sale_price", ""),
                ))
            if path == "/store-lists/import":
                return self._ok(self.state.store.import_store_list(
                    body.get("path", ""), body.get("name", ""), body.get("item_ids", []),
                    bool(body.get("replace_item_bindings")),
                ))
            if path == "/store-lists/preview-text":
                return self._ok(self.state.preview_store_text(body.get("text", "")))
            if path == "/store-lists/import-text":
                return self._ok(self.state.store.import_store_text(
                    body.get("text", ""), body.get("name", ""), body.get("item_ids", []),
                    replace_item_bindings=bool(body.get("replace_item_bindings")),
                ))
            if path.startswith("/store-lists/") and path.endswith("/bind"):
                list_id = int(path.split("/")[2])
                self.state.store.bind_store_list(list_id, body.get("item_ids", []))
                return self._ok({"id": list_id})
            if path == "/policies":
                for key, value in body.items():
                    if key in self.state.store.get_policies():
                        self.state.store.set_setting(key, value)
                return self._ok(self.state.store.get_policies())
            if path == "/aftersale-policy/summarize":
                return self._ok({
                    "summary": self.state.summarize_aftersale_policy(body.get("text", ""))
                })
            if path.startswith("/refund-orders/") and path.endswith("/status"):
                refund_id = int(path.split("/")[2])
                return self._ok(self.state.store.update_refund_order_status(
                    refund_id, body.get("status", "")
                ))
            if path == "/test":
                return self._ok(self.state.test_reply(body))
            if path == "/service/start":
                return self._ok(self.state.start_service())
            if path == "/service/stop":
                return self._ok(self.state.stop_service())
            if path == "/service/retry-auth":
                return self._ok(self.state.retry_service_auth())
            if path.startswith("/reviews/") and path.endswith("/approve"):
                audit_id = int(path.split("/")[2])
                return self._ok(self.state.approve(audit_id, body.get("reply", "")))
            if path.startswith("/reviews/") and path.endswith("/reject"):
                audit_id = int(path.split("/")[2])
                return self._ok(self.state.reject(audit_id, body.get("reply", "")))
            if path.startswith("/conversations/") and path.endswith("/reset"):
                scope_id = unquote(path[len("/conversations/") : -len("/reset")].strip("/"))
                self.state.store.reset_conversation(scope_id)
                if self.state.live:
                    self.state.live.exit_manual_mode(scope_id)
                return self._ok({"scope_id": scope_id, "state": "active", "ai_reply_count": 0})
            return self._send(404, {"ok": False, "error": "接口不存在"})
        except Exception as exc:
            traceback.print_exc()
            self._error(exc)

    def do_DELETE(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path.startswith("/images/"):
                return self._ok(self.state.delete_image_asset(int(path.split("/")[2])))
            if path.startswith("/products/"):
                item_id = unquote(path.split("/")[2])
                query = parse_qs(urlparse(self.path).query)
                ignore_on_sync = query.get("ignore_on_sync", ["0"])[0] in {"1", "true", "yes"}
                return self._ok(self.state.store.delete_v2_product(item_id, ignore_on_sync))
            if path.startswith("/store-lists/"):
                return self._ok(self.state.store.delete_store_list(int(path.split("/")[2])))
            return self._send(404, {"ok": False, "error": "接口不存在"})
        except Exception as exc:
            traceback.print_exc()
            self._error(exc)


def _handle_stdio_rpc(line: str, port: int, output_lock: threading.Lock):
    request_id = ""
    connection = None
    try:
        request = json.loads(line)
        request_id = str(request.get("id", ""))
        method = str(request.get("method", "GET")).upper()
        path = str(request.get("path", "/"))
        body = request.get("body")
        encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"} if encoded is not None else {}
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        response_text = response.read().decode("utf-8")
        result = {
            "id": request_id,
            "status": response.status,
            "payload": json.loads(response_text),
        }
    except Exception as exc:
        result = {"id": request_id, "status": 0, "error": str(exc)}
    finally:
        if connection is not None:
            connection.close()
    # Multiple requests may finish together; serialize each complete response
    # line so Electron never receives interleaved JSON.
    with output_lock:
        # Keep the stdio transport ASCII-only. Some customer Windows installs
        # still expose a legacy console code page even with PYTHONUTF8 set;
        # JSON.parse restores escaped Chinese characters on the Electron side.
        print("__XIANYU_RPC__" + json.dumps(result, ensure_ascii=True), flush=True)


def _serve_stdio_rpc(port: int):
    output_lock = threading.Lock()
    for line in sys.stdin:
        threading.Thread(
            target=_handle_stdio_rpc,
            args=(line, port, output_lock),
            name="xianyu-stdio-rpc",
            daemon=True,
        ).start()


def serve(port: int, data_dir: str):
    state = BackendState(data_dir)
    ApiHandler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", port), ApiHandler)
    threading.Thread(target=_serve_stdio_rpc, args=(port,), daemon=True).start()
    print(json.dumps({
        "ready": True,
        "port": port,
        "edition": APP_EDITION,
        "version": APP_VERSION,
        "build_commit": BUILD_COMMIT,
    }, ensure_ascii=True), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--data-dir", default=os.path.join(os.getcwd(), "data", "v2"))
    args = parser.parse_args()
    serve(args.port, args.data_dir)
