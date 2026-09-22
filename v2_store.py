import csv
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app_store import AppStore
from privacy_guard import contains_sensitive_voucher_data

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # Optional in source mode; packaged builds include it.
    Style = None
    lazy_pinyin = None

FIRST_REPLY_TEMPLATE_VERSION = 8
PROMOTION_QR_PURPOSE = "promotion_qr"


COUPON_TYPE_LABELS = {
    "meituan": "美团卡券",
    "douyin": "抖音卡券",
    "merchant_miniapp": "商家小程序券",
    "electronic_code": "普通电子券码",
    "purchase_order": "代买单",
    "mixed": "混合发卡",
    "promotion": "推广模式",
    "other": "其他卡券",
}

COUPON_TYPE_DEFAULT_INSTRUCTIONS = {
    "meituan": "当前商品发放的是美团电子券，付款后发送领取信息，到店出示券码核销。",
    "douyin": "当前商品发放的是抖音电子券，付款后发送领取信息，到店出示券码核销。",
    "merchant_miniapp": "当前商品发放的是商家小程序电子券，付款后发送领取信息，到店出示券码核销。",
    "electronic_code": "付款后发电子券码，门店扫码核销。",
    "purchase_order": "当前商品为代买单，付款后按订单说明发送领取信息，请按领取页面提示到店核销。",
    # Mixed delivery deliberately has no generic fallback. Each SKU must use
    # the channel and redemption facts stored in product knowledge.
    "mixed": "",
    # Promotion mode is a sales route rather than a voucher channel. Buyer-facing
    # wording must come from the operator's knowledge and promotion assets.
    "promotion": "",
    "other": "付款后发送当前商品对应的电子卡券，到店按券码说明核销。",
}

SAME_DAY_NOTICE = (
    "请确认当天到店使用后再购买，卡券必须当天购买、当天使用。"
    "忘记使用或未及时使用导致过期，不退不补。"
)

KNOWN_CITY_NAMES = {
    "北京", "上海", "天津", "重庆", "武汉", "黄石", "十堰", "宜昌", "襄阳", "荆州", "荆门", "孝感", "鄂州",
    "南京", "苏州", "无锡", "常州", "南通", "扬州", "徐州", "南昌", "九江", "赣州", "上饶", "安庆", "合肥",
    "深圳", "广州", "佛山", "东莞", "珠海", "成都", "厦门", "泉州", "福州", "长沙", "郑州", "贵阳", "三亚",
    "大连", "西安", "杭州", "宁波", "温州", "绍兴", "嘉兴", "金华", "湖州", "衢州", "舟山", "台州", "丽水",
    "海口", "青岛", "济南", "昆明",
    "惠州", "焦作", "寿光", "潍坊", "烟台", "威海", "临沂", "淄博", "泰安", "济宁", "菏泽",
    "洛阳", "开封", "新乡", "许昌", "南阳", "信阳", "商丘", "周口", "驻马店", "安阳", "沧州",
    "江门", "肇庆", "中山", "汕头", "湛江", "清远", "韶关", "揭阳", "潮州", "茂名",
    "长春", "吉林", "哈尔滨", "沈阳", "石家庄", "太原", "唐山", "秦皇岛", "邢台", "保定", "廊坊",
    "通辽", "赤峰", "乌鲁木齐", "兰州", "银川", "西宁", "呼和浩特", "南宁", "桂林",
}

KNOWN_PROVINCE_NAMES = {
    "北京", "上海", "天津", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江",
    "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
}

STORE_LANDMARK_WORDS = (
    "门店", "店里", "店能", "店可", "商场", "商圈", "广场", "购物中心", "万达", "万象城",
    "万科里", "天街", "银泰", "吾悦", "大悦城", "来福士", "太古里", "印象城", "奥特莱斯",
    "天虹", "总店", "旗舰店", "分店", "ifs", "mall", "地址", "位置", "在哪", "电话", "号码",
    "营业", "开门", "打烊",
)

GENERIC_STORE_LANDMARKS = (
    "万达", "万象城", "万象汇", "壹方城", "壹方天地", "万科里", "天街",
    "银泰", "吾悦", "大悦城", "来福士", "太古里", "印象城", "海岸城",
    "奥特莱斯", "天虹", "ifs", "mall",
)

# Only aliases confirmed against a canonical branch in the current product's
# bound store table are eligible. This keeps merchant-specific nicknames from
# leaking into other products or inventing an unsupported branch.
STORE_QUERY_ALIASES = {
    "深圳大运中心": "龙岗大运天地店",
    "龙岗大运中心": "龙岗大运天地店",
    "深圳大运天地": "龙岗大运天地店",
}

# County-level city names buyers commonly use while merchant exports keep only
# the parent prefecture in the city column. Values are the canonical city keys
# used by the imported store rows.
KNOWN_SUBCITY_TO_CITY = {
    "乐清": "温州",
}

STORE_RELATION_WORDS = (
    "附近", "旁边", "对面", "楼上", "楼下", "隔壁", "周边",
)

STORE_QUERY_NEUTRAL_WORDS = (
    "请问一下", "麻烦帮忙", "麻烦帮我", "帮忙查一下", "帮我查一下",
    "帮忙看看", "帮我看看", "请问", "麻烦", "帮忙", "帮我", "查一下",
    "查下", "看一下", "看下", "我想问", "想问", "咨询一下", "咨询",
    "那个门店", "这个门店", "那家门店", "这家门店", "那个店", "这个店",
    "那家", "这家", "那边", "这边", "您好", "你好", "当前", "现在", "今天", "一下",
    "本周末", "周末", "工作日", "平日", "节假日", "早餐", "中午", "午餐", "晚上", "晚餐", "晚市",
    "可以使用", "可以用", "能不能用", "可不可以用", "是否可用", "能用",
    "可用", "适用", "支持", "还有没有", "还有吗", "还有么", "还有嘛",
    "有没有", "有吗", "有不", "行不行", "行吗",
    "吃完再买对吧", "吃完再买", "吃了再买对吧", "吃了再买", "用餐后再买",
    "结账前再买", "买单前再买", "对吧", "是吧", "没错吧",
    "的吗", "的么", "的嘛", "的", "吗", "嘛", "么", "呀", "呢", "吧",
)

try:
    CHINA_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows Python installations may not ship the IANA timezone database.
    # China has used UTC+8 without daylight saving since 1991.
    CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def normalize_text(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s\-—_·,，。/\\()（）【】\[\]]+", "", text)
    for suffix in ("餐厅", "门店", "分店", "店", "旗舰店"):
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            text = text[: -len(suffix)]
    return text


def normalize_match_text(value: object) -> str:
    """Normalize user text for entity containment without changing display text."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


MEDIA_MARKER_PATTERN = (
    r"(?:\[\s*图片\s*\]|图片消息|买家发送了一张图片|"
    r"\[\s*语音\s*\]|语音消息|买家发送了一条语音)"
)


def strip_media_markers(value: object) -> str:
    text = re.sub(MEDIA_MARKER_PATTERN, " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip(" ，,。.!！?？~～")


def is_media_dependent_query(value: object) -> bool:
    """True when the missing object can only be understood from media."""
    text = normalize_text(value)
    if not text or len(text) > 18:
        return False
    if re.search(r"\d+(?:\.\d+)?(?:元|块|折|%)", text):
        return False
    if re.search(r"(?:省|市|区|县|镇|街道|万达|万象城|万科里|天街|银泰|吾悦)", text):
        return False
    return bool(re.fullmatch(
        r"(?:这个|那个|这里|那里|这家店|图片里|语音里|帮我看下|你看下)?"
        r"(?:可以用(?:吗|么|嘛|不)?|能用(?:吗|么|嘛|不)?|可用(?:吗|么|嘛|不)?|"
        r"多少钱|什么价格|要付多少|付多少钱|怎么买|怎么拍|怎么核销|怎么用|是这个吗)",
        text,
    ))


STORE_COLUMN_ALIASES = {
    "brand": ("店名", "品牌", "品牌名称", "总店", "商户名称"),
    "branch": ("分店名", "分店名称", "门店", "门店名", "门店名称", "店铺名称", "网点名称"),
    "province": ("省", "省份", "所在省"),
    "city": ("市", "城市", "所在市"),
    "district": ("区县", "区", "县", "行政区", "所在区县"),
    "address": ("地址", "详细地址", "门店地址", "店铺地址", "所在地址"),
    "phone": ("电话", "联系电话", "手机号", "联系方式", "门店电话", "店铺电话"),
    "business_hours": ("营业时间", "显示营业时间", "营业时段", "营业时间段", "开放时间", "服务时间"),
}


def normalize_store_header(value: object) -> str:
    text = str(value or "").strip().lower().replace("\ufeff", "").replace("\u200b", "")
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    return re.sub(r"[\s\-—_·,，。/\\:：()（）【】\[\]]+", "", text)


def classify_store_header(value: object) -> str:
    """Map loose spreadsheet headings to one canonical store field."""
    header = normalize_store_header(value)
    if not header:
        return ""
    candidates = []
    for field, aliases in STORE_COLUMN_ALIASES.items():
        for alias in aliases:
            normalized_alias = normalize_store_header(alias)
            if header == normalized_alias:
                candidates.append((1000 + len(normalized_alias), field))
            elif len(normalized_alias) >= 2 and normalized_alias in header:
                candidates.append((len(normalized_alias), field))
    return max(candidates, default=(0, ""))[1]


class V2Store(AppStore):
    """V2 data layer. It keeps V1 tables compatible with the live reply engine."""

    def __init__(self, db_path: str):
        super().__init__(db_path)
        self._init_v2_db()

    @staticmethod
    def _platform_description(product: Dict) -> str:
        """Return the untouched marketplace description from its stored payload."""
        payload = str((product or {}).get("platform_summary") or "").strip()
        if not payload:
            return ""
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return payload
        if isinstance(parsed, dict):
            return str(parsed.get("description") or "").strip()
        return ""

    @staticmethod
    def _merge_platform_summary(previous: object, incoming: object) -> str:
        """Keep the last complete marketplace payload when a refresh is partial."""
        current_text = str(incoming or "").strip()
        previous_text = str(previous or "").strip()
        if not previous_text:
            return current_text
        try:
            current = json.loads(current_text) if current_text else {}
            old = json.loads(previous_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return current_text or previous_text
        if not isinstance(current, dict) or not isinstance(old, dict):
            return current_text or previous_text
        current_complete = bool(
            str(current.get("description") or "").strip()
            or (isinstance(current.get("sku"), list) and current.get("sku"))
        )
        old_complete = bool(
            str(old.get("description") or "").strip()
            or (isinstance(old.get("sku"), list) and old.get("sku"))
        )
        if current_complete or not old_complete:
            return current_text
        merged = dict(old)
        for key in ("title", "price", "stock"):
            if current.get(key) not in (None, ""):
                merged[key] = current[key]
        return json.dumps(merged, ensure_ascii=False)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_v2_db(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS v2_products (
                    item_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    raw_text TEXT NOT NULL DEFAULT '',
                    ai_summary TEXT NOT NULL DEFAULT '',
                    structured_json TEXT NOT NULL DEFAULT '{}',
                    ai_draft_summary TEXT NOT NULL DEFAULT '',
                    ai_draft_structured_json TEXT NOT NULL DEFAULT '{}',
                    platform_summary TEXT NOT NULL DEFAULT '',
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    image_urls_json TEXT NOT NULL DEFAULT '[]',
                    price TEXT NOT NULL DEFAULT '',
                    item_status TEXT NOT NULL DEFAULT 'onsale',
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    sync_status TEXT NOT NULL DEFAULT 'manual',
                    manual_edited INTEGER NOT NULL DEFAULT 0,
                    last_synced_at TEXT NOT NULL DEFAULT '',
                    first_reply_enabled INTEGER NOT NULL DEFAULT 1,
                    first_reply_text TEXT NOT NULL DEFAULT '',
                    first_reply_manual INTEGER NOT NULL DEFAULT 0,
                    first_reply_generated_at TEXT NOT NULL DEFAULT '',
                    first_reply_template_version INTEGER NOT NULL DEFAULT 0,
                    coupon_type TEXT NOT NULL DEFAULT 'meituan',
                    coupon_type_custom TEXT NOT NULL DEFAULT '',
                    coupon_instructions TEXT NOT NULL DEFAULT '',
                    custom_policy_enabled INTEGER NOT NULL DEFAULT 0,
                    custom_policy_raw TEXT NOT NULL DEFAULT '',
                    custom_policy_summary TEXT NOT NULL DEFAULT '',
                    order_notice_enabled INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    ai_summary TEXT NOT NULL DEFAULT '',
                    structured_json TEXT NOT NULL DEFAULT '{}',
                    note TEXT NOT NULL DEFAULT '',
                    first_reply_text TEXT NOT NULL DEFAULT '',
                    first_reply_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_versions_item
                    ON knowledge_versions(item_id, id DESC);
                CREATE TABLE IF NOT EXISTS store_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    source_file TEXT NOT NULL DEFAULT '',
                    store_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_id INTEGER NOT NULL,
                    brand TEXT NOT NULL DEFAULT '',
                    branch TEXT NOT NULL DEFAULT '',
                    province TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT '',
                    district TEXT NOT NULL DEFAULT '',
                    address TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    business_hours TEXT NOT NULL DEFAULT '',
                    normalized TEXT NOT NULL,
                    FOREIGN KEY(list_id) REFERENCES store_lists(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_stores_list ON stores(list_id);
                CREATE INDEX IF NOT EXISTS idx_stores_norm ON stores(normalized);
                CREATE TABLE IF NOT EXISTS product_store_lists (
                    item_id TEXT NOT NULL,
                    list_id INTEGER NOT NULL,
                    PRIMARY KEY(item_id, list_id)
                );
                CREATE TABLE IF NOT EXISTS product_sku_store_rules (
                    item_id TEXT NOT NULL,
                    sku_key TEXT NOT NULL,
                    sku_name TEXT NOT NULL DEFAULT '',
                    sale_price_override TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'inherit',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(item_id, sku_key)
                );
                CREATE TABLE IF NOT EXISTS product_sku_store_lists (
                    item_id TEXT NOT NULL,
                    sku_key TEXT NOT NULL,
                    list_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(item_id, sku_key, list_id),
                    FOREIGN KEY(list_id) REFERENCES store_lists(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sku_store_list
                    ON product_sku_store_lists(list_id, item_id, sku_key);
                CREATE TABLE IF NOT EXISTS time_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    day_type TEXT NOT NULL DEFAULT 'any',
                    start_time TEXT NOT NULL DEFAULT '00:00',
                    end_time TEXT NOT NULL DEFAULT '23:59',
                    allowed INTEGER NOT NULL DEFAULT 1,
                    reply TEXT NOT NULL DEFAULT '',
                    next_hint TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_time_item ON time_rules(item_id, enabled);
                CREATE TABLE IF NOT EXISTS product_image_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    purpose TEXT NOT NULL DEFAULT '',
                    trigger_words_json TEXT NOT NULL DEFAULT '[]',
                    reply_text TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL,
                    original_name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_image_assets_item
                    ON product_image_assets(item_id, enabled, id DESC);
                CREATE TABLE IF NOT EXISTS image_send_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    remote_url TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_image_send_cooldown
                    ON image_send_log(asset_id, scope_id, status, created_at DESC);
                CREATE TABLE IF NOT EXISTS v2_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refund_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL DEFAULT '',
                    scope_id TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    user_name TEXT NOT NULL DEFAULT '',
                    item_id TEXT NOT NULL DEFAULT '',
                    product_title TEXT NOT NULL DEFAULT '',
                    paid_amount TEXT NOT NULL DEFAULT '',
                    suggested_amount TEXT NOT NULL DEFAULT '',
                    refund_type TEXT NOT NULL DEFAULT '待人工判断',
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '待核实',
                    source TEXT NOT NULL DEFAULT '买家消息',
                    order_url TEXT NOT NULL DEFAULT '',
                    applied_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_refund_orders_status
                    ON refund_orders(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_refund_orders_scope
                    ON refund_orders(scope_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS ignored_products (
                    item_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    ignored_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(v2_products)").fetchall()}
            migrations = {
                "thumbnail_url": "TEXT NOT NULL DEFAULT ''",
                "image_urls_json": "TEXT NOT NULL DEFAULT '[]'",
                "price": "TEXT NOT NULL DEFAULT ''",
                "item_status": "TEXT NOT NULL DEFAULT 'onsale'",
                "source_type": "TEXT NOT NULL DEFAULT 'manual'",
                "sync_status": "TEXT NOT NULL DEFAULT 'manual'",
                "manual_edited": "INTEGER NOT NULL DEFAULT 0",
                "last_synced_at": "TEXT NOT NULL DEFAULT ''",
                "first_reply_enabled": "INTEGER NOT NULL DEFAULT 1",
                "first_reply_text": "TEXT NOT NULL DEFAULT ''",
                "first_reply_manual": "INTEGER NOT NULL DEFAULT 0",
                "first_reply_generated_at": "TEXT NOT NULL DEFAULT ''",
                "first_reply_template_version": "INTEGER NOT NULL DEFAULT 0",
                "source_update_json": "TEXT NOT NULL DEFAULT '{}'",
                "ai_draft_summary": "TEXT NOT NULL DEFAULT ''",
                "ai_draft_structured_json": "TEXT NOT NULL DEFAULT '{}'",
                "coupon_type": "TEXT NOT NULL DEFAULT ''",
                "coupon_type_custom": "TEXT NOT NULL DEFAULT ''",
                "coupon_instructions": "TEXT NOT NULL DEFAULT ''",
                "custom_policy_enabled": "INTEGER NOT NULL DEFAULT 0",
                "custom_policy_raw": "TEXT NOT NULL DEFAULT ''",
                "custom_policy_summary": "TEXT NOT NULL DEFAULT ''",
                "order_notice_enabled": "INTEGER NOT NULL DEFAULT 1",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE v2_products ADD COLUMN {name} {definition}")
            version_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(knowledge_versions)").fetchall()
            }
            if "first_reply_text" not in version_columns:
                conn.execute(
                    "ALTER TABLE knowledge_versions ADD COLUMN first_reply_text TEXT NOT NULL DEFAULT ''"
                )
            if "first_reply_enabled" not in version_columns:
                conn.execute(
                    "ALTER TABLE knowledge_versions ADD COLUMN first_reply_enabled INTEGER NOT NULL DEFAULT 1"
                )
            # Product platform defaults to Meituan unless it was explicitly configured.
            conn.execute(
                "UPDATE v2_products SET coupon_type='meituan' WHERE TRIM(COALESCE(coupon_type,''))=''"
            )
            store_columns = {row[1] for row in conn.execute("PRAGMA table_info(stores)").fetchall()}
            if "business_hours" not in store_columns:
                conn.execute(
                    "ALTER TABLE stores ADD COLUMN business_hours TEXT NOT NULL DEFAULT ''"
                )
            sku_rule_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(product_sku_store_rules)").fetchall()
            }
            if "sale_price_override" not in sku_rule_columns:
                conn.execute(
                    "ALTER TABLE product_sku_store_rules "
                    "ADD COLUMN sale_price_override TEXT NOT NULL DEFAULT ''"
                )
            refund_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(refund_orders)").fetchall()
            }
            if "order_url" not in refund_columns:
                conn.execute(
                    "ALTER TABLE refund_orders ADD COLUMN order_url TEXT NOT NULL DEFAULT ''"
                )
        # Existing installations receive an initial editable first reply on upgrade.
        with self._connect() as conn:
            pending_first_replies = [
                row["item_id"] for row in conn.execute(
                    """SELECT item_id FROM v2_products
                       WHERE first_reply_manual=0 AND first_reply_template_version<?
                         AND (TRIM(raw_text)<>'' OR TRIM(ai_summary)<>'')"""
                    , (FIRST_REPLY_TEMPLATE_VERSION,)
                ).fetchall()
            ]
        for item_id in pending_first_replies:
            self.refresh_first_reply(item_id, force=False)

    def save_v2_product(
        self,
        item_id: str,
        title: str,
        raw_text: str,
        enabled: bool = True,
        note: str = "手工保存",
        first_reply_enabled: Optional[bool] = None,
        first_reply_text: Optional[str] = None,
        first_reply_manual: Optional[bool] = None,
        coupon_type: Optional[str] = None,
        coupon_type_custom: Optional[str] = None,
        coupon_instructions: Optional[str] = None,
        custom_policy_enabled: Optional[bool] = None,
        custom_policy_raw: Optional[str] = None,
        custom_policy_summary: Optional[str] = None,
        order_notice_enabled: Optional[bool] = None,
    ) -> Dict:
        item_id = str(item_id or "").strip()
        if not item_id:
            raise ValueError("商品ID不能为空")
        title = str(title or "").strip()
        raw_text = str(raw_text or "").strip()
        now = self._now()
        with self._connect() as conn:
            old = conn.execute(
                "SELECT * FROM v2_products WHERE item_id=?", (item_id,)
            ).fetchone()
            conn.execute(
                """INSERT INTO v2_products(
                    item_id,title,raw_text,ai_summary,structured_json,platform_summary,
                    enabled,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET title=excluded.title,
                    raw_text=excluded.raw_text,enabled=excluded.enabled,
                    ai_draft_summary='',ai_draft_structured_json='{}',
                    manual_edited=1,sync_status='manual_edited',updated_at=excluded.updated_at""",
                (
                    item_id,
                    title,
                    raw_text,
                    old["ai_summary"] if old else "",
                    old["structured_json"] if old else "{}",
                    old["platform_summary"] if old else "",
                    int(enabled),
                    old["created_at"] if old else now,
                    now,
                ),
            )
            conn.execute(
                """UPDATE v2_products SET manual_edited=1,source_type=CASE
                   WHEN source_type='goofish' THEN source_type ELSE 'manual' END,
                   sync_status='manual_edited' WHERE item_id=?""",
                (item_id,),
            )
            conn.execute(
                """UPDATE v2_products SET coupon_type='meituan'
                   WHERE item_id=? AND TRIM(COALESCE(coupon_type,''))=''""",
                (item_id,),
            )
            if coupon_type is not None or coupon_type_custom is not None or coupon_instructions is not None:
                current_coupon = conn.execute(
                    "SELECT coupon_type,coupon_type_custom,coupon_instructions FROM v2_products WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                selected_type = (
                    str(coupon_type or "").strip()
                    if coupon_type is not None else current_coupon["coupon_type"]
                )
                if selected_type and selected_type not in COUPON_TYPE_LABELS:
                    raise ValueError("卡券类型无效，请重新选择")
                selected_custom = (
                    str(coupon_type_custom or "").strip()
                    if coupon_type_custom is not None else current_coupon["coupon_type_custom"]
                )
                selected_instructions = (
                    str(coupon_instructions or "").strip()
                    if coupon_instructions is not None else current_coupon["coupon_instructions"]
                )
                if selected_type == "other" and not selected_custom:
                    raise ValueError("选择“其他卡券”时请填写卡券名称")
                conn.execute(
                    """UPDATE v2_products SET coupon_type=?,coupon_type_custom=?,
                       coupon_instructions=?,updated_at=? WHERE item_id=?""",
                    (selected_type, selected_custom, selected_instructions, now, item_id),
                )
            if any(value is not None for value in (
                custom_policy_enabled, custom_policy_raw, custom_policy_summary,
                order_notice_enabled,
            )):
                current_policy = conn.execute(
                    """SELECT custom_policy_enabled,custom_policy_raw,
                       custom_policy_summary,order_notice_enabled
                       FROM v2_products WHERE item_id=?""",
                    (item_id,),
                ).fetchone()
                conn.execute(
                    """UPDATE v2_products SET custom_policy_enabled=?,custom_policy_raw=?,
                       custom_policy_summary=?,order_notice_enabled=?,updated_at=? WHERE item_id=?""",
                    (
                        int(bool(custom_policy_enabled)) if custom_policy_enabled is not None else int(current_policy["custom_policy_enabled"]),
                        str(custom_policy_raw or "").strip() if custom_policy_raw is not None else current_policy["custom_policy_raw"],
                        str(custom_policy_summary or "").strip() if custom_policy_summary is not None else current_policy["custom_policy_summary"],
                        int(bool(order_notice_enabled)) if order_notice_enabled is not None else int(current_policy["order_notice_enabled"]),
                        now,
                        item_id,
                    ),
                )
            if first_reply_enabled is not None or first_reply_text is not None:
                current_first = conn.execute(
                    "SELECT first_reply_enabled,first_reply_text,first_reply_manual FROM v2_products WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                conn.execute(
                    """UPDATE v2_products SET first_reply_enabled=?,first_reply_text=?,
                       first_reply_manual=?,first_reply_generated_at=?,first_reply_template_version=?,
                       updated_at=? WHERE item_id=?""",
                    (
                        int(bool(first_reply_enabled)) if first_reply_enabled is not None else int(current_first["first_reply_enabled"]),
                        str(first_reply_text).strip() if first_reply_text is not None else current_first["first_reply_text"],
                        int(bool(first_reply_manual)) if first_reply_manual is not None else int(current_first["first_reply_manual"]),
                        now,
                        FIRST_REPLY_TEMPLATE_VERSION,
                        now,
                        item_id,
                    ),
                )
            conn.execute(
                """INSERT INTO knowledge_versions(
                    item_id,raw_text,ai_summary,structured_json,note,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    item_id,
                    raw_text,
                    old["ai_summary"] if old else "",
                    old["structured_json"] if old else "{}",
                    note,
                    now,
                ),
            )
        # Keep the existing live engine compatible. Raw user text remains authoritative.
        self.save_product(item_id, title, raw_text, enabled)
        saved = self.get_v2_product(item_id)
        # Keep automatically generated first replies in sync with the latest
        # authoritative knowledge and manually selected coupon platform.
        if saved and not saved.get("first_reply_manual"):
            saved = self.refresh_first_reply(item_id, force=False)
        return saved

    def upsert_synced_product(self, item: Dict) -> Dict:
        """Update platform facts without overwriting manually edited knowledge."""
        item_id = str(item.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("同步商品缺少ID")
        now = self._now()
        title = str(item.get("title") or "").strip()
        platform_summary = str(item.get("platform_summary") or "").strip()
        image_urls = [str(value).strip() for value in item.get("image_urls", []) if str(value).strip()]
        with self._connect() as conn:
            old = conn.execute("SELECT * FROM v2_products WHERE item_id=?", (item_id,)).fetchone()
            platform_summary = self._merge_platform_summary(
                old["platform_summary"] if old else "", platform_summary,
            )
            conn.execute(
                """INSERT INTO v2_products(
                    item_id,title,raw_text,ai_summary,structured_json,platform_summary,
                    thumbnail_url,image_urls_json,price,item_status,source_type,sync_status,
                    manual_edited,last_synced_at,enabled,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET title=excluded.title,
                    platform_summary=excluded.platform_summary,
                    thumbnail_url=excluded.thumbnail_url,image_urls_json=excluded.image_urls_json,
                    price=excluded.price,item_status=excluded.item_status,source_type='goofish',
                    sync_status=CASE WHEN v2_products.manual_edited=1 THEN 'source_updated' ELSE 'synced' END,
                    last_synced_at=excluded.last_synced_at,enabled=1,updated_at=excluded.updated_at""",
                (
                    item_id, title, "", "", "{}", platform_summary,
                    str(item.get("thumbnail_url") or ""),
                    json.dumps(image_urls, ensure_ascii=False), str(item.get("price") or ""),
                    str(item.get("item_status") or "onsale"), "goofish", "synced", 0,
                    now, 1, old["created_at"] if old else now, now,
                ),
            )
            conn.execute(
                """UPDATE v2_products SET coupon_type='meituan'
                   WHERE item_id=? AND TRIM(COALESCE(coupon_type,''))=''""",
                (item_id,),
            )
        return self.get_v2_product(item_id)

    def stage_source_update(self, item_id: str, summary: str, structured: Dict, description: str = "") -> Dict:
        """Store a source refresh without changing effective knowledge, stores or first reply."""
        payload = {
            "summary": str(summary or "").strip(),
            "structured": structured or {},
            "description": str(description or ""),
            "staged_at": self._now(),
        }
        with self._connect() as conn:
            conn.execute(
                "UPDATE v2_products SET source_update_json=?,sync_status='source_updated',updated_at=? WHERE item_id=?",
                (json.dumps(payload, ensure_ascii=False), self._now(), item_id),
            )
        return self.get_v2_product(item_id)

    def apply_source_update(self, item_id: str, sections: List[str]) -> Dict:
        """Apply only the source sections explicitly approved by the operator."""
        product = self.get_v2_product(item_id)
        if not product:
            raise ValueError("商品不存在")
        pending = product.get("source_update") or {}
        if not pending:
            raise ValueError("当前商品没有待确认的来源更新")
        sections = {str(value) for value in (sections or [])}
        if "knowledge" in sections:
            summary = str(pending.get("summary") or "").strip()
            structured = pending.get("structured") or {}
            if summary:
                # Rebuild from the complete page payload.  The AI summary is an
                # index, not the source of truth: rules omitted by the model
                # (for example "除酒水外全场通用") must still survive approval.
                normalized_product = dict(product)
                source_description = (
                    self._platform_description(product)
                    or str(pending.get("description") or "").strip()
                )
                normalized_product.update({
                    "raw_text": "\n".join(
                        value for value in (summary, source_description) if value
                    ),
                    "structured": structured,
                })
                summary = self.build_knowledge_summary(normalized_product) or summary
                now = self._now()
                with self._connect() as conn:
                    conn.execute(
                        """UPDATE v2_products SET raw_text=?,ai_summary=?,structured_json=?,
                           manual_edited=0,updated_at=? WHERE item_id=?""",
                        (summary, summary, json.dumps(structured, ensure_ascii=False), now, item_id),
                    )
                    conn.execute(
                        """INSERT INTO knowledge_versions(item_id,raw_text,ai_summary,structured_json,note,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (item_id, summary, summary, json.dumps(structured, ensure_ascii=False),
                         "手动允许闲鱼来源更新知识", now),
                    )
                self.save_product(item_id, product.get("title", ""), summary, True)
        if "stores" in sections:
            self.sync_platform_store_list(
                item_id, product.get("title", ""), str(pending.get("description") or ""),
                pending.get("structured") or {},
            )
        if "first_reply" in sections:
            # Explicit approval is the only path allowed to overwrite a manual first reply.
            with self._connect() as conn:
                conn.execute("UPDATE v2_products SET first_reply_manual=0 WHERE item_id=?", (item_id,))
            self.refresh_first_reply(item_id, force=True)
        resolved = set(pending.get("resolved_sections") or []) | sections
        remaining = {"knowledge", "stores", "first_reply"} - resolved
        with self._connect() as conn:
            if not remaining:
                conn.execute(
                    "UPDATE v2_products SET source_update_json='{}',sync_status='ready',updated_at=? WHERE item_id=?",
                    (self._now(), item_id),
                )
            else:
                pending["resolved_sections"] = sorted(resolved)
                conn.execute(
                    "UPDATE v2_products SET source_update_json=?,updated_at=? WHERE item_id=?",
                    (json.dumps(pending, ensure_ascii=False), self._now(), item_id),
                )
        self.add_event("source_update_applied", f"已允许更新商品 {item_id}：{','.join(sorted(sections))}", {})
        return self.get_v2_product(item_id)

    def is_product_ignored(self, item_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM ignored_products WHERE item_id=?", (str(item_id),)
            ).fetchone()
        return bool(row)

    def delete_v2_product(self, item_id: str, ignore_on_sync: bool = False) -> Dict:
        """Delete local product data. Historical events/refunds remain for aftersales."""
        item_id = str(item_id or "").strip()
        product = self.get_v2_product(item_id)
        if not product:
            raise ValueError("商品不存在")
        title = str(product.get("title") or item_id)
        with self._connect() as conn:
            if ignore_on_sync:
                conn.execute(
                    "INSERT OR REPLACE INTO ignored_products(item_id,title,ignored_at) VALUES(?,?,?)",
                    (item_id, title, self._now()),
                )
            conn.execute("DELETE FROM product_store_lists WHERE item_id=?", (item_id,))
            conn.execute("DELETE FROM product_sku_store_lists WHERE item_id=?", (item_id,))
            conn.execute("DELETE FROM product_sku_store_rules WHERE item_id=?", (item_id,))
            conn.execute("DELETE FROM time_rules WHERE item_id=?", (item_id,))
            conn.execute("DELETE FROM product_image_assets WHERE item_id=?", (item_id,))
            conn.execute("DELETE FROM knowledge_versions WHERE item_id=?", (item_id,))
            conn.execute("DELETE FROM local_products WHERE item_id=?", (item_id,))
            conn.execute("DELETE FROM v2_products WHERE item_id=?", (item_id,))
        self.add_event(
            "product_deleted", f"删除本地商品：{title}",
            {"item_id": item_id, "ignore_on_sync": bool(ignore_on_sync)},
        )
        return {"item_id": item_id, "title": title, "ignored": bool(ignore_on_sync)}

    def save_synced_summary(self, item_id: str, summary: str, structured: Dict) -> Dict:
        current = self.get_v2_product(item_id)
        if not current:
            raise ValueError("商品不存在")
        now = self._now()
        summary = str(summary or "").strip()
        normalized_product = dict(current)
        # ``platform_summary`` contains the complete title/description/SKU JSON.
        # Feeding the condensed AI summary back into the completeness pass made
        # any clause omitted by the model impossible to recover.
        source_description = self._platform_description(current)
        normalized_product.update({
            # Keep source prose and AI output in separate fields. Re-feeding an
            # AI summary into raw_text caused later summaries to summarize a
            # summary and made provenance/conflict checks impossible.
            "raw_text": source_description,
            "structured": structured or {},
        })
        summary = self.build_knowledge_summary(normalized_product) or summary
        structured_text = json.dumps(structured or {}, ensure_ascii=False)
        effective = current["raw_text"] if current.get("manual_edited") else source_description
        sync_status = "source_updated" if current.get("manual_edited") else "ready"
        with self._connect() as conn:
            conn.execute(
                """UPDATE v2_products SET raw_text=?,ai_summary=?,structured_json=?,
                   sync_status=?,updated_at=? WHERE item_id=?""",
                (effective, summary, structured_text, sync_status, now, item_id),
            )
            conn.execute(
                """INSERT INTO knowledge_versions(
                    item_id,raw_text,ai_summary,structured_json,note,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (item_id, effective, summary, structured_text, "闲鱼同步并生成初始知识", now),
            )
        self.save_product(item_id, current["title"], effective, True)
        if isinstance(structured, dict) and isinstance(structured.get("time_rules"), list):
            self.replace_time_rules(item_id, structured["time_rules"])
        self.refresh_first_reply(item_id, force=False)
        return self.get_v2_product(item_id)

    def mark_summary_failed(self, item_id: str) -> Dict:
        """Expose an AI-summary failure while retaining complete page facts."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE v2_products SET sync_status='summary_failed',updated_at=? WHERE item_id=?",
                (self._now(), str(item_id)),
            )
        return self.get_v2_product(item_id)

    def mark_unsynced_products(self, seen_item_ids: List[str]):
        seen = {str(value).strip() for value in seen_item_ids if str(value).strip()}
        with self._connect() as conn:
            rows = conn.execute("SELECT item_id FROM v2_products WHERE source_type='goofish'").fetchall()
            for row in rows:
                if row["item_id"] not in seen:
                    conn.execute(
                        """UPDATE v2_products SET item_status='offline',enabled=0,
                           sync_status='offline',updated_at=? WHERE item_id=?""",
                        (self._now(), row["item_id"]),
                    )

    def set_product_listing_status(self, item_id: str, status: str) -> Optional[Dict]:
        """Persist a freshly verified marketplace listing state.

        This narrow status write is used by the live reply gate.  It never
        touches knowledge, store bindings, first replies or SKU rules.
        """
        item_id = str(item_id or "").strip()
        normalized = str(status or "").strip().lower()
        if not item_id or normalized not in {"onsale", "offline"}:
            raise ValueError("商品状态必须是 onsale 或 offline")
        with self._connect() as conn:
            if normalized == "offline":
                cur = conn.execute(
                    """UPDATE v2_products SET item_status='offline',enabled=0,
                       sync_status='offline',last_synced_at=?,updated_at=? WHERE item_id=?""",
                    (self._now(), self._now(), item_id),
                )
            else:
                cur = conn.execute(
                    """UPDATE v2_products SET item_status='onsale',
                       enabled=CASE WHEN sync_status='offline' THEN 1 ELSE enabled END,
                       sync_status=CASE WHEN manual_edited=1 THEN 'source_updated' ELSE 'synced' END,
                       last_synced_at=?,updated_at=? WHERE item_id=?""",
                    (self._now(), self._now(), item_id),
                )
            if not cur.rowcount:
                return None
        return self.get_v2_product(item_id)

    def save_ai_summary(self, item_id: str, summary: str, structured: Dict) -> Dict:
        current = self.get_v2_product(item_id)
        if not current:
            raise ValueError("商品不存在")
        now = self._now()
        normalized_product = dict(current)
        normalized_product.update({"structured": structured or {}})
        summary = self.build_knowledge_summary(normalized_product) or str(summary or "").strip()
        structured_text = json.dumps(structured or {}, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """UPDATE v2_products SET ai_summary=?,structured_json=?,updated_at=?
                   WHERE item_id=?""",
                (summary.strip(), structured_text, now, item_id),
            )
            conn.execute(
                """INSERT INTO knowledge_versions(
                    item_id,raw_text,ai_summary,structured_json,note,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (item_id, current["raw_text"], summary.strip(), structured_text, "AI重新归纳", now),
            )
        if isinstance(structured, dict) and isinstance(structured.get("time_rules"), list):
            self.replace_time_rules(item_id, structured["time_rules"])
        self.refresh_first_reply(item_id, force=False)
        return self.get_v2_product(item_id)

    def save_ai_draft(self, item_id: str, summary: str, structured: Dict) -> Dict:
        """Save a review-only AI draft without changing live customer knowledge."""
        current = self.get_v2_product(item_id)
        if not current:
            raise ValueError("商品不存在")
        summary = str(summary or "").strip()
        if not summary:
            raise ValueError("模型未返回可用的归纳结果")
        structured_text = json.dumps(structured or {}, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """UPDATE v2_products SET ai_draft_summary=?,ai_draft_structured_json=?,
                   updated_at=? WHERE item_id=?""",
                (summary, structured_text, self._now(), item_id),
            )
        self.add_event(
            "ai_knowledge_draft",
            f"生成商品 {item_id} 的AI知识草稿（尚未生效）",
            {"sku_profile_count": len((structured or {}).get("sku_profiles") or [])},
        )
        return self.get_v2_product(item_id)

    def adopt_ai_draft(self, item_id: str, summary: str) -> Dict:
        """Atomically promote the reviewed text and its matching structure."""
        current = self.get_v2_product(item_id)
        if not current:
            raise ValueError("商品不存在")
        reviewed = str(summary or "").strip()
        if not reviewed:
            raise ValueError("当前没有可采纳的归纳知识")
        generated = str(current.get("ai_draft_summary") or "").strip()
        if not generated:
            raise ValueError("当前没有已生成的AI草稿，请先重新归纳")
        draft_structured = current.get("ai_draft_structured") or {}
        # If the operator edited the visible draft, hidden model fields are no
        # longer guaranteed to match the text. Discard them instead of silently
        # activating a deleted restriction. Canonical SKUs remain available from
        # the marketplace payload independently.
        edited = reviewed != generated
        if edited:
            adopted_structured = {}
        elif draft_structured:
            adopted_structured = draft_structured
        else:
            adopted_structured = current.get("structured") or {}
        now = self._now()
        structured_text = json.dumps(adopted_structured, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """UPDATE v2_products SET raw_text=?,ai_summary=?,structured_json=?,
                   ai_draft_summary=?,ai_draft_structured_json=?,manual_edited=1,
                   sync_status='manual_edited',updated_at=? WHERE item_id=?""",
                (reviewed, reviewed, structured_text, reviewed, structured_text, now, item_id),
            )
            conn.execute(
                """INSERT INTO knowledge_versions(
                    item_id,raw_text,ai_summary,structured_json,note,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    item_id, reviewed, reviewed, structured_text,
                    "采纳人工编辑的AI草稿" if edited else "采纳AI整理草稿", now,
                ),
            )
        self.replace_time_rules(
            item_id,
            adopted_structured.get("time_rules")
            if isinstance(adopted_structured, dict)
            and isinstance(adopted_structured.get("time_rules"), list)
            else [],
        )
        self.save_product(item_id, current.get("title", ""), reviewed, bool(current.get("enabled", True)))
        self.refresh_first_reply(item_id, force=False)
        self.add_event(
            "ai_knowledge_adopted",
            f"采纳商品 {item_id} 的AI知识草稿",
            {"edited": edited, "sku_profile_count": len(adopted_structured.get("sku_profiles") or []) if isinstance(adopted_structured, dict) else 0},
        )
        return self.get_v2_product(item_id)

    @staticmethod
    def _first_fact(facts: Dict, aliases: Iterable[str]):
        if not isinstance(facts, dict):
            return ""
        for alias in aliases:
            value = facts.get(alias)
            if isinstance(value, list):
                if any(isinstance(item, (dict, list)) for item in value):
                    continue
                value = "、".join(str(item).strip() for item in value if str(item).strip())
            elif isinstance(value, dict):
                if any(isinstance(child, (dict, list)) for child in value.values()):
                    continue
                value = "；".join(
                    f"{key}：{child}" for key, child in value.items() if str(child).strip()
                )
            value = str(value or "").strip()
            if value:
                return value
        return ""

    @classmethod
    def _fact_value_text(cls, value: object) -> str:
        """Render every structured fact value instead of dropping nested data."""
        if isinstance(value, list):
            return "、".join(
                text for text in (cls._fact_value_text(item) for item in value) if text
            )
        if isinstance(value, dict):
            return "；".join(
                f"{key}：{text}"
                for key, child in value.items()
                if (text := cls._fact_value_text(child))
            )
        return str(value or "").strip()

    @classmethod
    def _fact_section_lines(cls, facts: Dict, aliases: Iterable[str]):
        lines = []
        used = set()
        seen = set()
        if not isinstance(facts, dict):
            return lines, used
        for alias in aliases:
            if alias not in facts:
                continue
            used.add(alias)
            value = cls._fact_value_text(facts.get(alias))
            key = normalize_match_text(value)
            if not value or not key or key in seen:
                continue
            seen.add(key)
            lines.append(f"{alias}：{value.rstrip('。；; ')}。")
        return lines, used

    @classmethod
    def _uncovered_source_rules(cls, raw_text: str, rendered_text: str) -> List[str]:
        """Keep authoritative source clauses that structured extraction missed."""
        source = str(raw_text or "").strip()
        if not source:
            return []
        try:
            payload = json.loads(source)
            if isinstance(payload, dict) and str(payload.get("description") or "").strip():
                source = str(payload["description"]).strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

        rendered_key = normalize_match_text(rendered_text)
        found = []
        seen = set()
        critical_markers = (
            "适用", "不可", "不能", "不支持", "禁止", "仅限", "无需", "预约", "等位",
            "优惠", "发票", "咨询", "退款", "过期", "核销", "营业", "节假日", "中秋",
            "国庆", "春节", "元旦", "劳动节", "门店", "全场", "堂食", "外带", "外卖",
            "包间", "酒水", "锅底", "服务费", "人群", "人数", "拍前", "下单前",
        )
        stack_context_markers = (
            "适用", "仅限", "无需", "预约", "等位", "优惠", "发票", "咨询", "退款",
            "过期", "核销", "营业", "节假日", "中秋", "国庆", "春节", "元旦", "劳动节",
            "门店", "全场", "堂食", "外带", "外卖", "包间", "酒水", "锅底", "服务费",
            "人群", "人数", "拍前", "下单前",
        )
        for piece in re.split(r"(?<=[。！？!?；;])|[\r\n]+", source):
            line = piece.strip()
            line = re.sub(r"^(?:[①②③④⑤⑥⑦⑧⑨⑩]|\d+[.、)）])\s*", "", line)
            line = re.sub(r"^(?:⚠️?|[•·*-])\s*", "", line)
            line = re.sub(r"^【[^】]+】\s*", "", line).strip()
            if not line or re.fullmatch(r"[-—_=\s]+", line):
                continue
            line_key = normalize_match_text(line)
            if not line_key or line_key in seen or line_key in rendered_key:
                continue

            combination = cls._parse_purchase_combination_line(line)
            if combination and "【组合购买方案】" in rendered_text:
                comparison_tokens = [
                    combination.get("target_face_value"),
                    combination.get("sale_price"),
                ]
                if all(
                    normalize_match_text(token) in rendered_key
                    for token in comparison_tokens if token
                ):
                    continue

            # Product rows and pure stacking rows are already rendered from the
            # authoritative SKU records. Keep them only when they also carry an
            # independent restriction such as a store, date or dine-in rule.
            is_product_row = bool(re.search(
                r"(?:代金券|套餐|单人餐|双人餐|多人餐|自助餐).{0,30}(?:售价|价格|￥|¥|\d+(?:\.\d+)?\s*元)",
                line,
            ))
            has_independent_rule = any(marker in line for marker in critical_markers)
            if is_product_row and not has_independent_rule:
                continue
            is_stack_only = bool(re.search(r"叠加|最多(?:使用|可用)?\s*\d+\s*张", line))
            if is_stack_only and not any(marker in line for marker in stack_context_markers):
                continue

            seen.add(line_key)
            found.append(line.rstrip("。；; ") + "。")
        return found

    @classmethod
    def _parse_purchase_combination_line(cls, value: object) -> Dict:
        """Parse a prose-only purchase plan without promoting it to a sellable SKU."""
        line = str(value or "").strip().replace("两", "2")
        line = re.sub(r"^(?:[①②③④⑤⑥⑦⑧⑨⑩]|\d+[.、)）])\s*", "", line)
        match = re.search(
            r"(?<!\d)(?P<target>\d+(?:\.\d+)?)\s*元?\s*"
            r"[（(]\s*(?P<composition>[^（）()\n]{1,40})\s*[）)]\s*"
            r"[：:]\s*(?:售价|价格|支付|实付)?\s*[¥￥]?\s*"
            r"(?P<price>\d+(?:\.\d+)?)\s*元?",
            line,
        )
        if not match:
            return {}
        target = cls._format_number(match.group("target"))
        composition = cls._normalize_composition(match.group("composition"), "")
        components = []
        for amount, count in re.findall(
            r"(\d+(?:\.\d+)?)元券(\d+)张", composition,
        ):
            components.append({
                "face_value": cls._format_number(amount),
                "count": int(count),
            })
        if not components:
            return {}
        try:
            component_total = sum(
                Decimal(item["face_value"]) * item["count"] for item in components
            )
            if component_total != Decimal(target):
                return {}
        except (InvalidOperation, TypeError, ValueError):
            return {}
        return {
            "target_face_value": target,
            "composition": composition,
            "sale_price": cls._format_number(match.group("price")),
            "components": components,
            "source_evidence": line.rstrip("。；; "),
        }

    @classmethod
    def _purchase_combination_summary(cls, product: Dict, options: List[Dict]) -> str:
        """Render validated multi-SKU purchase plans and expose price conflicts."""
        structured = product.get("structured") or {}
        records = []
        if isinstance(structured, dict):
            supplied = structured.get("purchase_combinations")
            if isinstance(supplied, list):
                for row in supplied:
                    if not isinstance(row, dict):
                        continue
                    target = cls._format_number(
                        row.get("target_face_value") or row.get("target_amount")
                        or row.get("face_value") or ""
                    )
                    composition = cls._normalize_composition(
                        row.get("composition") or "", ""
                    )
                    price = cls._format_number(row.get("sale_price") or row.get("price") or "")
                    synthetic = cls._parse_purchase_combination_line(
                        f"{target}（{composition}）：{price}"
                    )
                    if synthetic:
                        synthetic["source_evidence"] = str(
                            row.get("source_evidence") or synthetic["source_evidence"]
                        ).strip()
                        records.append(synthetic)
        source = str(product.get("raw_text") or "").replace("\\n", "\n")
        for piece in re.split(r"[\r\n；;]+", source):
            parsed = cls._parse_purchase_combination_line(piece)
            if parsed:
                records.append(parsed)

        option_by_face = {}
        canonical_targets = set()
        for option in options:
            if str(option.get("availability") or "available") != "available":
                continue
            face = cls._format_number(option.get("face_value") or "")
            if not face:
                continue
            canonical_targets.add(face)
            if face not in option_by_face:
                option_by_face[face] = option
            elif cls._format_number(option_by_face[face].get("sale_price") or "") != cls._format_number(
                option.get("sale_price") or ""
            ):
                option_by_face[face] = None

        rendered = []
        seen = set()
        for record in records:
            key = (
                record.get("target_face_value"), record.get("composition"),
                record.get("sale_price"),
            )
            if key in seen:
                continue
            seen.add(key)
            target = record.get("target_face_value") or ""
            # A real SKU with this face value is already shown in 商品规格 and must
            # not be duplicated as a prose-derived combination.
            if target in canonical_targets:
                continue
            component_rows = []
            calculated = Decimal("0")
            calculable = True
            for component in record.get("components") or []:
                face = component.get("face_value") or ""
                count = int(component.get("count") or 0)
                option = option_by_face.get(face)
                if not option or count <= 0:
                    calculable = False
                    break
                price = cls._format_number(option.get("sale_price") or "")
                if not price:
                    calculable = False
                    break
                calculated += Decimal(price) * count
                option_name = str(option.get("name") or f"{face}元代金券").strip()
                component_rows.append(f"{option_name}×{count}")
            if not calculable or not component_rows:
                continue
            calculated_text = cls._format_number(calculated)
            source_price = cls._format_number(record.get("sale_price") or "")
            plan = "＋".join(component_rows)
            if source_price and source_price != calculated_text:
                rendered.append(
                    f"{target}元方案：购买{plan}，按当前SKU售价应付{calculated_text}元；"
                    f"原文写{source_price}元，价格冲突，需人工确认。"
                )
            else:
                rendered.append(
                    f"{target}元方案：购买{plan}，共支付{calculated_text}元，可抵扣{target}元。"
                )
        return "\n".join(rendered)

    @staticmethod
    def _group_uncovered_source_rules(lines: List[str]):
        """Move recognizable safety rules into stable sections before raw fallback."""
        groups = {
            "【不可用日期】": [],
            "【适用范围】": [],
            "【使用规则】": [],
            "【发券与核销】": [],
            "【退款与发票】": [],
            "【提醒】": [],
        }
        remaining = []
        for line in lines:
            if re.search(r"(?:不可用|不能用|不适用|除外|禁用)", line) and re.search(
                r"\d{1,4}年|\d{1,2}月|\d{1,2}日|节|假日|中秋|国庆|春节|元旦|劳动节",
                line,
            ):
                groups["【不可用日期】"].append(line)
            elif re.search(r"退款|退货|退单|发票", line):
                groups["【退款与发票】"].append(line)
            elif re.search(r"发券|发码|领取|核销|券码|二维码", line):
                groups["【发券与核销】"].append(line)
            elif re.search(r"拍前|下单前|购买前|请咨询|联系客服", line):
                groups["【提醒】"].append(line)
            elif re.search(r"仅适用于|仅可用于|适用范围|适用于(?:餐品|菜品|酒水|门店|地区)", line):
                groups["【适用范围】"].append(line)
            elif re.search(
                r"堂食|外带|外卖|预约|等位|包间|最低消费|找零|兑现金|"
                r"服务费|每桌|每单|限用|使用次数|优惠同享|不可同享|酒水|锅底",
                line,
            ):
                groups["【使用规则】"].append(line)
            else:
                remaining.append(line)
        return groups, remaining

    @staticmethod
    def _format_number(value: object) -> str:
        text = str(value or "").strip().replace("￥", "").replace("¥", "")
        text = re.sub(r"\s*元\s*$", "", text)
        try:
            number = float(text)
            return f"{number:.2f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return text

    @classmethod
    def _normalize_stack_limit(cls, value: object) -> str:
        """Return a numeric per-SKU limit, ignoring descriptive stack rules."""
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            number = Decimal(text)
            return cls._format_number(number) if number > 0 else ""
        except InvalidOperation:
            pass
        match = re.search(
            r"(?:最多|上限|每次最多|可叠加|可使用|最多使用)[^\d]{0,6}(\d+)\s*张|"
            r"(\d+)\s*张(?:封顶|以内|上限)?",
            text,
        )
        return str(int(match.group(1) or match.group(2))) if match else ""

    @staticmethod
    def _pick(record: Dict, aliases: Iterable[str]):
        for alias in aliases:
            if alias in record and record[alias] not in (None, "", [], {}):
                return record[alias]
        return ""

    @classmethod
    def _normalize_composition(cls, value: object, face_value: str = "") -> str:
        if isinstance(value, list):
            parts = [cls._normalize_composition(item) for item in value]
            return "＋".join(part for part in parts if part)
        if isinstance(value, dict):
            parts = []
            for amount, count in value.items():
                amount_text = cls._format_number(amount)
                count_match = re.search(r"\d+", str(count))
                if amount_text and count_match:
                    parts.append(f"{amount_text}元券{count_match.group()}张")
            return "＋".join(parts)
        text = str(value or "").strip()
        if not text:
            return f"{face_value}元券1张" if face_value else ""
        normalized = text.replace("两", "2").replace("×", "x").replace("*", "x")
        pairs = []
        for amount, count in re.findall(r"(\d+(?:\.\d+)?)\s*元?(?:券)?\s*[xX]\s*(\d+)\s*张?", normalized):
            pairs.append(f"{cls._format_number(amount)}元券{int(count)}张")
        if not pairs:
            match = re.search(r"(?:发|给|包含|构成)(?:的是)?\s*(\d+)\s*张\s*(\d+(?:\.\d+)?)\s*元?", normalized)
            if match:
                pairs.append(f"{cls._format_number(match.group(2))}元券{int(match.group(1))}张")
        if not pairs:
            # Listings also commonly write “发125元券2张”. Keep the order
            # exactly as expressed instead of falling back to face-value x1.
            match = re.search(
                r"(?:发|给|包含|构成)(?:的是)?\s*(\d+(?:\.\d+)?)\s*元?(?:代金券|券)?\s*(\d+)\s*张",
                normalized,
            )
            if match:
                pairs.append(f"{cls._format_number(match.group(1))}元券{int(match.group(2))}张")
        if not pairs:
            amounts = re.findall(r"\d+(?:\.\d+)?", normalized)
            if "+" in normalized and len(amounts) >= 2:
                pairs = [f"{cls._format_number(amount)}元券1张" for amount in amounts]
        if pairs:
            return "＋".join(pairs)
        return text.strip("（）() ")

    @classmethod
    def _composition_from_option_name(cls, value: object) -> str:
        """Extract a real coupon bundle such as ``100x2`` from an SKU name.

        Marketplace SKU names are more authoritative than prose or an AI
        fallback such as ``200元券1张``.  In particular, ``200（100x2）`` is a
        200-yuan bundle made of two 100-yuan coupons, not one 200-yuan coupon.
        """
        text = str(value or "").replace("两", "2")
        pairs = re.findall(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?\s*[xX×*]\s*(\d+)\s*张?",
            text,
        )
        if not pairs:
            pairs = re.findall(
                r"(?:发|给|含|包含)?\s*(\d+(?:\.\d+)?)\s*元?(?:代金券|券)\s*(\d+)\s*张",
                text,
            )
        if not pairs:
            reversed_pairs = re.findall(
                r"(?:发|给|含|包含)?\s*(\d+)\s*张\s*(\d+(?:\.\d+)?)\s*元?(?:代金券|券)?",
                text,
            )
            pairs = [(amount, count) for count, amount in reversed_pairs]
        return "＋".join(
            f"{cls._format_number(amount)}元券{int(count)}张"
            for amount, count in pairs if int(count) > 0
        )

    @classmethod
    def _option_availability(cls, record: Dict) -> Dict:
        """Normalize explicit SKU sale/stock state without inventing stock.

        A currently listed option is usable unless its source explicitly marks
        it disabled, off-shelf or sold out. This preserves manually entered
        legacy products while ensuring a zero-stock/disabled SKU is never used
        by a deterministic reply.
        """
        status = str(cls._pick(record, (
            "库存状态", "销售状态", "上架状态", "sku_status", "sale_status", "status",
        )) or "").strip().lower()
        enabled = cls._pick(record, (
            "启用", "是否启用", "enabled", "is_enabled", "available", "is_available",
        ))
        stock = cls._pick(record, (
            "库存", "可售库存", "剩余库存", "stock", "quantity", "inventory",
        ))
        unavailable = bool(re.search(
            r"售罄|无货|缺货|下架|停售|禁用|不可售|sold\s*out|off(?:line|_shelf)|"
            r"disabled|inactive|deleted",
            status,
            re.I,
        ))
        if isinstance(enabled, bool):
            unavailable = unavailable or not enabled
        elif enabled is not None and str(enabled).strip().lower() in {
            "0", "false", "no", "off", "否", "禁用", "下架",
        }:
            unavailable = True
        stock_value = ""
        if stock not in (None, ""):
            try:
                stock_number = Decimal(str(stock).strip())
                stock_value = cls._format_number(stock_number)
                unavailable = unavailable or stock_number <= 0
            except InvalidOperation:
                stock_value = str(stock).strip()
        return {
            "availability": "unavailable" if unavailable else "available",
            "stock": stock_value,
            "availability_explicit": bool(status or enabled is not None or stock not in (None, "")),
        }

    @classmethod
    def _platform_sku_source_id(
        cls, record: Dict, name: str, face_value: str, platforms: List[str],
    ) -> str:
        explicit = str(cls._pick(
            record, ("skuId", "sku_id", "optionId", "option_id", "id"),
        ) or "").strip()
        if explicit:
            return explicit
        if platforms and face_value:
            fingerprint = normalize_text(name).lower()
            suffix = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
            return f"platform:{platforms[0]}:{face_value}:{suffix}"
        return ""

    @classmethod
    def _normalize_product_option(cls, record: Dict) -> Optional[Dict]:
        face_value = cls._format_number(cls._pick(record, (
            "面额", "券面额", "代金券面额", "face_value", "value", "denomination",
        )))
        name = str(cls._pick(record, (
            "商品名", "商品名称", "规格", "规格名称", "券名称", "name", "title",
        )) or "").strip()
        if not face_value:
            face_match = re.search(r"(\d+(?:\.\d+)?)\s*元", name)
            face_value = cls._format_number(face_match.group(1)) if face_match else ""
        price = cls._format_number(cls._pick(record, (
            "售价", "销售价", "价格", "实售价", "sale_price", "price",
        )))
        applicable_time = str(cls._pick(record, (
            "适用时间", "使用时间", "可用时间", "时段", "applicable_time",
        )) or "").strip()
        composition_value = cls._pick(record, (
            "发券组成", "券码组成", "构成", "发放组成", "composition", "delivery_composition",
        ))
        option_type = str(cls._pick(record, (
            "规格类型", "商品类型", "选项类型", "option_type", "kind", "type", "category",
        )) or "").strip()
        evidence = " ".join(
            str(value or "") for value in (name, option_type, composition_value)
        )
        # Numeric prices, weights and package prices are frequently emitted by a
        # summarizer as face_value.  They are not voucher denominations unless
        # the same structured record explicitly identifies a coupon.
        voucher_evidence = bool(re.search(
            r"代金券|抵扣券|现金券|\d+(?:\.\d+)?\s*元券|"
            r"(?:^|[^\u4e00-\u9fff])券(?:$|[^\u4e00-\u9fff])|"
            r"\b(?:voucher|coupon)\b",
            evidence, re.I,
        ))
        invalid_name = bool(re.search(
            r"[=￥¥%]|地址|重量|斤|公斤|千克|克",
            name,
        ))
        if not voucher_evidence or invalid_name:
            return None
        name_composition = cls._composition_from_option_name(name)
        composition = name_composition or cls._normalize_composition(
            composition_value, face_value,
        )
        max_stack = cls._normalize_stack_limit(cls._pick(record, (
            "最多叠加", "叠加上限", "最多使用张数", "最多使用", "max_stack", "max_count",
        )))
        platforms = [value for value in ("美团", "抖音", "小程序") if value in name]
        source_id = cls._platform_sku_source_id(
            record, name, face_value, platforms,
        )
        if not name and face_value:
            name = f"{face_value}元代金券"
        if not name and not price:
            return None
        return {
            "name": name,
            "face_value": face_value,
            "sale_price": price,
            "applicable_time": applicable_time,
            "composition": composition,
            "max_stack": max_stack,
            "sku_id": source_id,
            **cls._option_availability(record),
        }

    @classmethod
    def _platform_product_options(cls, product: Dict) -> List[Dict]:
        """Recover real sellable voucher options from the Goofish SKU payload."""
        payload = str((product or {}).get("platform_summary") or "").strip()
        if not payload:
            return []
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        records = parsed.get("sku") if isinstance(parsed, dict) else None
        if not isinstance(records, list):
            return []
        product_evidence = " ".join((
            str((product or {}).get("title") or ""),
            str(parsed.get("title") or ""), str(parsed.get("description") or ""),
        ))
        if not re.search(r"代金券|抵扣券|现金券|餐饮券", product_evidence):
            return []

        output = []
        for record in records:
            if not isinstance(record, dict):
                continue
            property_rows = record.get("propertyList") or []
            name = next((
                str(row.get("actualValueText") or row.get("valueText") or "").strip()
                for row in property_rows if isinstance(row, dict)
                and str(row.get("actualValueText") or row.get("valueText") or "").strip()
            ), "")
            if not name:
                name = str(cls._pick(record, (
                    "skuName", "sku_name", "name", "title", "skuText", "specName",
                )) or "").strip()
            if not name:
                idle_pairs = str((record.get("features") or {}).get("idlePvPairs") or "")
                name = idle_pairs.rsplit("#", 1)[-1].strip() if "#" in idle_pairs else idle_pairs.strip()
            if not name or re.search(r"勿拍|不要拍|防下架|补差|运费|测试", name):
                continue
            face_match = re.search(
                r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?(?=\s*(?:代金券|券|[xX×*（(]|$))",
                name,
            )
            if not face_match:
                continue
            face = cls._format_number(face_match.group(1))
            cents = record.get("priceInCent")
            if cents in (None, ""):
                cents = record.get("price")
            try:
                price = cls._format_number(Decimal(str(cents)) / Decimal("100"))
            except (InvalidOperation, TypeError, ValueError):
                price = ""
            if not price:
                direct_price = record.get("soldPrice")
                try:
                    price = cls._format_number(Decimal(str(direct_price)))
                except (InvalidOperation, TypeError, ValueError):
                    price = ""
            if not price:
                continue
            composition = (
                cls._composition_from_option_name(name)
                or cls._normalize_composition("", face)
            )
            stack_match = re.search(
                r"(?:最多)?(?:可)?叠加\s*(\d+)\s*张", name,
            )
            max_stack = (
                stack_match.group(1) if stack_match else ""
            )
            platforms = [value for value in ("美团", "抖音", "小程序") if value in name]
            output.append({
                "name": name, "face_value": face, "sale_price": price,
                "applicable_time": "", "composition": composition,
                "max_stack": max_stack, "_platform_sku": True,
                "sku_id": cls._platform_sku_source_id(
                    record, name, face, platforms,
                ),
                **cls._option_availability(record),
            })
        return output

    @classmethod
    def _platform_configuration_options(cls, product: Dict) -> List[Dict]:
        """Expose the marketplace's real SKU rows for per-SKU store settings.

        This path deliberately does not depend on AI summaries or manually
        normalized knowledge.  A synced product already contains the source SKU
        payload in ``platform_summary`` and the configuration UI should use it
        directly, including buffet/package SKUs that are not vouchers.
        """
        payload = str((product or {}).get("platform_summary") or "").strip()
        if not payload:
            return []
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        records = parsed.get("sku") if isinstance(parsed, dict) else None
        if not isinstance(records, list):
            return []

        product_evidence = " ".join((
            str((product or {}).get("title") or ""),
            str(parsed.get("title") or ""), str(parsed.get("description") or ""),
        ))
        output = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            property_values = []
            for row in record.get("propertyList") or []:
                if not isinstance(row, dict):
                    continue
                value = str(
                    row.get("actualValueText") or row.get("valueText")
                    or row.get("propertyValue") or row.get("value") or ""
                ).strip()
                if value and value not in property_values:
                    property_values.append(value)
            name = " / ".join(property_values).strip()
            if not name:
                name = str(cls._pick(record, (
                    "skuName", "sku_name", "name", "title", "skuText", "specName",
                )) or "").strip()
            if not name:
                idle_pairs = str((record.get("features") or {}).get("idlePvPairs") or "")
                name = idle_pairs.rsplit("#", 1)[-1].strip() if "#" in idle_pairs else idle_pairs.strip()
            if not name or re.search(r"勿拍|不要拍|防下架|补差|运费|测试", name):
                continue

            price = ""
            cents = record.get("priceInCent")
            if cents in (None, ""):
                cents = record.get("price")
            if cents not in (None, ""):
                try:
                    price = cls._format_number(Decimal(str(cents)) / Decimal("100"))
                except (InvalidOperation, TypeError, ValueError):
                    price = ""
            if not price:
                try:
                    price = cls._format_number(Decimal(str(record.get("soldPrice"))))
                except (InvalidOperation, TypeError, ValueError):
                    price = ""

            evidence = f"{product_evidence} {name}"
            voucher = bool(re.search(
                r"代金券|抵扣券|现金券|餐饮券|(?:美团|抖音|小程序)\s*\d+(?:\.\d+)?",
                evidence,
            ))
            face = ""
            if voucher:
                face_match = re.search(
                    r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?"
                    r"(?=\s*(?:代金券|抵扣券|现金券|券|[xX×*（(]|$))",
                    name,
                )
                if not face_match:
                    face_match = re.search(r"(?:美团|抖音|小程序)\s*(\d+(?:\.\d+)?)", name)
                face = cls._format_number(face_match.group(1)) if face_match else ""

            composition = (
                cls._composition_from_option_name(name)
                or cls._normalize_composition("", face)
            ) if face else ""
            maximum = cls._normalize_stack_limit(cls._pick(record, (
                "最多叠加", "叠加上限", "最多使用张数", "最多使用", "max_stack", "max_count",
            )))
            if not maximum:
                stack_match = re.search(
                    r"(?:最多)?(?:可)?(?:叠加|使用)\s*(\d+)\s*张", name,
                )
                maximum = stack_match.group(1) if stack_match else ""
            platforms = [value for value in ("美团", "抖音", "小程序") if value in name]
            source_id = cls._platform_sku_source_id(
                record, name, face, platforms,
            )
            if not source_id:
                fingerprint = f"{index}|{name}|{price}"
                source_id = "platform-row:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:20]
            applicable_time = name
            output.append({
                "name": name, "face_value": face, "sale_price": price,
                "applicable_time": applicable_time, "composition": composition,
                "max_stack": maximum, "sku_id": source_id,
                "_platform_sku": True,
                "option_type": "voucher" if voucher and face else "package",
                "people_counts": cls._people_counts(name),
                "day_types": cls._option_day_types_from_fields(name, applicable_time),
                "meal_periods": cls._option_meal_periods_from_fields(name, applicable_time),
                "audience_types": cls._audience_types(name),
                **cls._option_availability(record),
            })
        # A prose rule may define a limit outside the SKU name. Accept it only
        # when the text explicitly says "叠加/使用 N 张" and identifies this
        # platform + denomination (or the denomination is unique). This keeps
        # "50x4" as package composition instead of turning it into a limit.
        rule_text = "\n".join((
            str((product or {}).get("raw_text") or ""),
            str(parsed.get("description") or ""),
        ))
        face_counts = {}
        for option in output:
            face = str(option.get("face_value") or "")
            if face:
                face_counts[face] = face_counts.get(face, 0) + 1
        clauses = re.split(r"[。；;\n]+", rule_text)
        for option in output:
            if option.get("max_stack"):
                continue
            face = str(option.get("face_value") or "")
            if not face:
                continue
            platforms = [
                value for value in ("美团", "抖音", "小程序")
                if value in str(option.get("name") or "")
            ]
            for clause in clauses:
                limit_match = re.search(
                    r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*"
                    r"(\d+)\s*张",
                    clause,
                )
                if not limit_match:
                    continue
                if not re.search(rf"(?<!\d){re.escape(face)}(?:\.0+)?\s*元?", clause):
                    continue
                if platforms and not any(platform in clause for platform in platforms):
                    continue
                if not platforms and face_counts.get(face, 0) > 1:
                    continue
                option["max_stack"] = limit_match.group(1)
                break
        return output

    @classmethod
    def _raw_product_options(cls, raw_text: str, title: str = "") -> List[Dict]:
        text = str(raw_text or "").replace("\\n", "\n")
        output = []
        voucher_context = bool(re.search(
            r"\d+(?:\.\d+)?\s*元?\s*(?:代金券|抵扣券|现金券|券)|"
            r"(?:美团|抖音|小程序)\s*\d+(?:\.\d+)?",
            "\n".join((str(title or ""), text)),
        ))
        pattern = re.compile(
            r"(?:^|[\n；;])[ \t]*(?:[①②③④⑤⑥⑦⑧⑨⑩]|\d{1,2}、[ \t]*|\d{1,2}\.[ \t]+)?[ \t]*"
            r"(?P<platform>美团|抖音|小程序)?[ \t]*"
            r"(?P<face>\d+(?:\.\d+)?)[ \t]*元?[ \t]*(?:代金券|券)?"
            r"(?P<label_tail>[ \t]*[（(][^（）()\n]{1,30}[）)])?"
            r"[ \t]*[：:\"“”']+[ \t]*(?:售价|价格)?[ \t]*[¥￥]?[ \t]*(?P<price>\d+(?:\.\d+)?)[ \t]*元?"
            r"(?P<tail>[^\n；;]{0,100})",
            re.M,
        )
        for match in pattern.finditer(text):
            matched_text = match.group(0)
            if not voucher_context and not re.search(
                r"代金券|抵扣券|现金券|\d+(?:\.\d+)?\s*元券|"
                r"(?:美团|抖音|小程序)\s*\d+(?:\.\d+)?", matched_text
            ):
                continue
            face = cls._format_number(match.group("face"))
            raw_label_tail = (match.group("label_tail") or "").strip()
            label_tail = raw_label_tail if re.search(
                r"(?:叠加|使用|限用|最多)[^）)]*\d+\s*张", raw_label_tail,
            ) else ""
            tail = (raw_label_tail + " " + (match.group("tail") or "")).strip()
            time_match = re.search(
                r"(工作日|节假日|周末|全周(?:通用)?|下午茶|午餐|晚餐|午市|晚市)", tail
            )
            composition_source = ""
            composition_patterns = (
                r"发(?:的是)?\s*(?:两|\d+)\s*张\s*\d+(?:\.\d+)?\s*元?",
                r"发(?:的是)?\s*\d+(?:\.\d+)?\s*元?(?:代金券|券)?\s*(?:两|\d+)\s*张",
                r"\d+(?:\.\d+)?\s*元?(?:券)?\s*[xX×*]\s*\d+",
                r"\d+(?:\.\d+)?\s*\+\s*\d+(?:\.\d+)?",
            )
            for composition_pattern in composition_patterns:
                composition_match = re.search(composition_pattern, tail)
                if composition_match:
                    composition_source = composition_match.group()
                    break
            limit_match = re.search(
                r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张",
                tail,
            )
            output.append({
                "name": f"{match.group('platform') or ''}{face}元代金券{label_tail}",
                "face_value": face,
                "sale_price": cls._format_number(match.group("price")),
                "applicable_time": time_match.group(1) if time_match else "",
                "composition": cls._normalize_composition(composition_source, face),
                "max_stack": limit_match.group(1) if limit_match else "",
            })
        # Some listings put the paid price before the face value, for example
        # “66.8元购100元代金券（工作日可用）”.  This is authoritative
        # knowledge and must not be replaced by the listing-card teaser price.
        reverse_pattern = re.compile(
            r"(?:^|[\n；;，,])[ \t]*(?:[①②③④⑤⑥⑦⑧⑨⑩]|\d{1,2}、[ \t]*|\d{1,2}\.[ \t]+)?[ \t]*"
            r"(?P<price>\d+(?:\.\d+)?)[ \t]*元[ \t]*(?:购|买|得)[ \t]*"
            r"(?P<face>\d+(?:\.\d+)?)[ \t]*元?[ \t]*(?:代金券|券)?"
            r"(?P<tail>[^\n；;]{0,100})",
            re.M,
        )
        for match in reverse_pattern.finditer(text):
            if not voucher_context and not re.search(
                r"代金券|抵扣券|现金券|\d+(?:\.\d+)?\s*元券", match.group(0)
            ):
                continue
            face = cls._format_number(match.group("face"))
            price = cls._format_number(match.group("price"))
            tail = match.group("tail") or ""
            time_match = re.search(
                r"(工作日(?:可用)?|节假日(?:可用)?|周末(?:可用)?|全周(?:通用)?|"
                r"下午茶|午餐|晚餐|午市|晚市)",
                tail,
            )
            option = {
                "name": f"{face}元代金券",
                "face_value": face,
                "sale_price": price,
                "applicable_time": time_match.group(1) if time_match else "",
                "composition": cls._normalize_composition("", face),
                "max_stack": "",
            }
            key = (option["face_value"], option["applicable_time"], option["sale_price"])
            existing = {
                (item.get("face_value", ""), item.get("applicable_time", ""), item.get("sale_price", ""))
                for item in output
            }
            if key not in existing:
                output.append(option)
        return output

    @classmethod
    def _raw_sku_stack_limits(cls, raw_text: str) -> Dict[str, int]:
        """Extract denomination-specific stack limits from authoritative prose."""
        limits = {}
        pattern = re.compile(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*元?\s*(?:代金券|抵扣券|现金券|券)"
            r"[^。；;\n，,、]{0,24}?"
            r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|仅限(?:使用)?|限用|限)\s*"
            r"([一二两三四五六七八九十两\d]+)\s*张"
        )
        for match in pattern.finditer(str(raw_text or "")):
            face = cls._format_number(match.group(1))
            count = cls._chinese_count(match.group(2))
            if face and count and count > 0:
                limits[face] = count
        return limits

    @classmethod
    def extract_product_options(cls, product: Dict) -> List[Dict]:
        raw_options = cls._raw_product_options(
            product.get("raw_text") or "", product.get("title") or ""
        )
        structured = product.get("structured") or {}
        facts = structured.get("facts") if isinstance(structured, dict) else {}
        facts = facts if isinstance(facts, dict) else {}
        candidates = []
        for container in (structured, facts):
            if not isinstance(container, dict):
                continue
            for key in (
                "sku_profiles", "products", "product_options", "skus", "sku", "商品规格", "商品列表",
                "规格", "价格", "售价", "代金券规格",
            ):
                value = container.get(key)
                if isinstance(value, list):
                    candidates.extend(item for item in value if isinstance(item, dict))
                elif isinstance(value, dict):
                    candidates.extend(item for item in value.values() if isinstance(item, dict))
        structured_options = [
            option for option in (cls._normalize_product_option(record) for record in candidates)
            if option
        ]
        platform_options = cls._platform_product_options(product)
        # Real marketplace SKUs are always the authority for sellable identity
        # and price, including after a manual prose edit. Text and AI may enrich
        # a matching SKU's usage rules but may not create another sellable SKU.
        if platform_options:
            output = list(platform_options)
            supplements = structured_options + raw_options
            for option in supplements:
                name_key = normalize_text(option.get("name") or "").lower()
                exact = next((
                    current for current in output
                    if name_key and normalize_text(current.get("name") or "").lower() == name_key
                ), None)
                if exact is None:
                    face = cls._format_number(option.get("face_value") or "")
                    matches = [
                        current for current in output
                        if face and cls._format_number(current.get("face_value") or "") == face
                    ]
                    exact = matches[0] if len(matches) == 1 else None
                if exact is None:
                    continue
                for field in ("applicable_time", "composition", "max_stack"):
                    if not exact.get(field) and option.get(field):
                        exact[field] = option[field]
                if option.get("availability_explicit"):
                    exact.update({
                        "availability": option.get("availability") or "available",
                        "stock": option.get("stock") or "",
                        "availability_explicit": True,
                    })
            sku_authoritative = True
            supplements = []
        else:
            # Manual-only products have no platform SKU payload; keep the
            # established manual/structured fallback behavior.
            if not raw_options:
                structured_options.extend(platform_options)
            sku_authoritative = bool(structured_options) and not bool(product.get("manual_edited"))
            output = list(structured_options if sku_authoritative else raw_options)
            supplements = raw_options if sku_authoritative else structured_options
        seen = {
            (item.get("face_value", ""), item.get("applicable_time", ""), item.get("sale_price", ""))
            for item in output
        }
        for option in supplements:
            key = (option.get("face_value", ""), option.get("applicable_time", ""), option.get("sale_price", ""))
            if key in seen and option.get("availability_explicit"):
                # Exact prose/SKU matches still need the structured inventory.
                # Deduplication must not discard a zero-stock update.
                for existing in output:
                    existing_key = (existing.get("face_value", ""), existing.get("applicable_time", ""), existing.get("sale_price", ""))
                    if existing_key == key:
                        existing.update({
                            "availability": option["availability"],
                            "stock": option.get("stock", ""),
                            "availability_explicit": True,
                        })
            if key not in seen:
                same_sku = next((
                    item for item in output
                    if item.get("face_value", "") == option.get("face_value", "")
                    and item.get("sale_price", "") == option.get("sale_price", "")
                    and (
                        not item.get("applicable_time")
                        or not option.get("applicable_time")
                        or item.get("applicable_time") == option.get("applicable_time")
                        or set(cls._option_day_types(item)) == set(cls._option_day_types(option))
                    )
                ), None)
                if same_sku:
                    # Explicit structured stock/sale state outranks prose that
                    # has no inventory field, even on a manually edited item.
                    if option.get("availability_explicit"):
                        same_sku["availability"] = option.get("availability") or "available"
                        same_sku["stock"] = option.get("stock") or ""
                        same_sku["availability_explicit"] = True
                    for field in ("applicable_time", "max_stack"):
                        if not same_sku.get(field) and option.get(field):
                            same_sku[field] = option[field]
                    incoming_composition = str(option.get("composition") or "")
                    current_composition = str(same_sku.get("composition") or "")
                    default_composition = (
                        f"{same_sku.get('face_value')}元券1张"
                        if same_sku.get("face_value") else ""
                    )
                    if incoming_composition and (
                        not current_composition
                        or current_composition == default_composition
                    ):
                        same_sku["composition"] = incoming_composition
                else:
                    output.append(option)
                    seen.add(key)
        # When a real marketplace SKU payload exists, it is the authority for
        # that SKU's stacking limit. This deliberately clears stale or AI-made
        # limits such as max_stack=4 inferred only from the name "抖音50x4".
        platform_sources = cls._platform_configuration_options(product)
        for option in output:
            face = cls._format_number(option.get("face_value") or "")
            if not face:
                continue
            option_name = str(option.get("name") or "")
            exact = next((
                source for source in platform_sources
                if normalize_text(source.get("name") or "").lower()
                == normalize_text(option_name).lower()
            ), None)
            if exact is None:
                option_platforms = {
                    value for value in ("美团", "抖音", "小程序") if value in option_name
                }
                candidates = [
                    source for source in platform_sources
                    if cls._format_number(source.get("face_value") or "") == face
                    and (
                        not option_platforms
                        or option_platforms.intersection({
                            value for value in ("美团", "抖音", "小程序")
                            if value in str(source.get("name") or "")
                        })
                    )
                ]
                exact = candidates[0] if len(candidates) == 1 else None
            if exact is not None:
                option["max_stack"] = str(exact.get("max_stack") or "")
                option["_platform_sku"] = True
        # The original knowledge is authoritative for usage limits. AI summaries
        # sometimes collapse different denominations into one global limit.
        raw_stack_limits = cls._raw_sku_stack_limits(product.get("raw_text") or "")
        for option in output:
            face = cls._format_number(option.get("face_value") or "")
            named_platform_sku = any(
                platform in str(option.get("name") or "")
                for platform in ("抖音", "美团", "小程序")
            )
            if face in raw_stack_limits and not (
                option.get("_platform_sku")
                or (named_platform_sku and option.get("max_stack"))
            ):
                option["max_stack"] = str(raw_stack_limits[face])
        valid = []
        for option in output:
            if str(option.get("availability") or "available") != "available":
                continue
            try:
                face = Decimal(str(option.get("face_value") or "0"))
                price = Decimal(str(option.get("sale_price") or "0"))
            except InvalidOperation:
                continue
            # A real sellable coupon SKU must have both a positive face value and price.
            # This blocks accidental constructions such as “11元代金券，售价0元”.
            if face > 0 and price > 0:
                valid.append(option)
        # Different ingestion sources can describe the same business SKU with
        # different prose. Collapse by buyer-visible facts before any reply.
        deduplicated = []
        business_keys = set()
        for option in valid:
            key = (
                cls._format_number(option.get("face_value")),
                cls._format_number(option.get("sale_price")),
                normalize_text(option.get("composition") or ""),
                tuple(sorted(cls._option_day_types(option))),
                tuple(sorted(cls._meal_periods(
                    " ".join(str(option.get(field) or "") for field in ("name", "applicable_time"))
                ))),
            )
            if key in business_keys:
                continue
            business_keys.add(key)
            deduplicated.append(option)
        valid = deduplicated
        # Some Goofish listings expose quantity selectors as separate SKUs
        # (100券、100券×2、100券×3). They are one sellable denomination, not
        # three products. Keep the unit SKU, but never treat the package
        # quantity as a stacking limit; only explicit rule text may set one.
        collapsed = []
        for option in sorted(
            valid,
            key=lambda value: (
                Decimal(str(value.get("face_value") or "0")),
                Decimal(str(value.get("sale_price") or "0")),
            ),
        ):
            face = str(option.get("face_value") or "")
            same_face = [item for item in collapsed if str(item.get("face_value") or "") == face]
            quantity_match = re.search(
                r"(?:[×xX*]|发)\s*([2-9]\d*)\s*张?",
                " ".join(str(option.get(key) or "") for key in ("name", "composition")),
            )
            if same_face and quantity_match:
                base = same_face[0]
                quantity = int(quantity_match.group(1))
                try:
                    base_price = Decimal(str(base.get("sale_price") or "0"))
                    current_price = Decimal(str(option.get("sale_price") or "0"))
                except InvalidOperation:
                    base_price = current_price = Decimal("0")
                if base_price > 0 and abs(current_price - base_price * quantity) <= Decimal("0.2"):
                    continue
            collapsed.append(option)
        for option in collapsed:
            option.pop("_platform_sku", None)
        return collapsed

    @staticmethod
    def _chinese_count(value: object) -> Optional[int]:
        text = str(value or "").strip()
        if text.isdigit():
            return int(text)
        aliases = {
            "单": 1, "一": 1, "二": 2, "两": 2, "双": 2, "俩": 2,
            "三": 3, "仨": 3, "四": 4, "五": 5, "六": 6,
            "七": 7, "八": 8, "九": 9, "十": 10,
        }
        if text in aliases:
            return aliases[text]
        digits = {
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
        }
        tens = re.fullmatch(r"([一二两三四五六七八九])?十([一二两三四五六七八九])?", text)
        if tens:
            return (digits.get(tens.group(1), 1) * 10) + digits.get(tens.group(2), 0)
        # Amount questions are often written as “两百怎么拍” or “一千怎么凑”.
        # People-count callers already cap this shared parser at 20.
        units = {"十": 10, "百": 100, "千": 1000}
        if re.fullmatch(r"[零一二两三四五六七八九十百千]+", text):
            total = 0
            current = 0
            for char in text:
                if char == "零":
                    continue
                if char in digits:
                    current = digits[char]
                    continue
                unit = units[char]
                total += (current or 1) * unit
                current = 0
            total += current
            return total or None
        return None

    @staticmethod
    def _cn_count_label(value: int) -> str:
        labels = {
            1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七",
            8: "八", 9: "九", 10: "十",
        }
        return labels.get(int(value), str(value))

    @classmethod
    def _count_pattern(cls, value: int) -> str:
        aliases = {
            1: ("1", "一", "单"), 2: ("2", "二", "两", "双", "俩"),
            3: ("3", "三", "仨"),
        }
        values = aliases.get(int(value), (str(value), cls._cn_count_label(value)))
        return "(?:" + "|".join(re.escape(item) for item in values) + ")"

    @classmethod
    def _purchase_quantity_slot(cls, value: object) -> tuple[Optional[int], str]:
        """Extract a purchase count without mistaking people or money for quantity."""
        text = str(value or "").strip()
        number = r"[一二两三四五六七八九十百单双俩仨\d]+"
        unit = r"张|份|条|只|套|个(?!人)"
        patterns = (
            rf"(?P<count>{number})\s*(?P<unit>{unit})[^。！？\n]{{0,16}}"
            r"(?:多少钱|多钱|几多钱|多少(?:钱|元|块)?|什么价|啥价|价格|怎么卖|"
            r"怎么收|怎么买|怎么拍|一共|总共|合计|要付|"
            r"还有吗|还有么|还有嘛|还有没有|有货吗|有没有|有吗|能用吗|可以用吗)",
            rf"(?:买|要|来|拿|拍|购)\s*(?P<count>{number})\s*(?P<unit>{unit})",
            rf"(?P<count>{number})\s*(?P<unit>{unit})\s*(?:的)?\s*(?:呢|可以吗|有吗)[？?。！!]*$",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            quantity = cls._chinese_count(match.group("count"))
            if quantity and 0 < quantity <= 99:
                return quantity, match.group("unit")[0]
        multiplied = re.search(
            r"(?<!\d)(\d{1,5})\s*[xX×*]\s*(\d{1,5})(?!\d)"
            r"[^。！？\n]{0,8}(?:多少钱|多钱|多少|啥价)",
            text,
        )
        if multiplied:
            left, right = (int(multiplied.group(1)), int(multiplied.group(2)))
            if 0 < left <= 20 < right:
                return left, "张"
            if 0 < right <= 20 < left:
                return right, "张"
        if re.search(r"(?:一整条|整条|单条|一条|单鱼)[^。！？\n]{0,8}(?:鱼|烤鱼|怎么卖|多少钱|多钱)", text):
            return 1, "条"
        return None, ""

    @classmethod
    def _quantity_denomination(cls, value: object) -> str:
        """Return the denomination surrounding a count such as 100的两张."""
        text = str(value or "")
        number = r"[一二两三四五六七八九十百单双俩仨\d]+"
        patterns = (
            rf"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?(?:代金券|优惠券|券)?\s*(?:的|来|买|要|拿|拍)?\s*{number}\s*张",
            rf"{number}\s*张\s*(?:的|面额为|面额)?\s*(\d+(?:\.\d+)?)\s*(?:元)?(?:代金券|优惠券|券)?",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return cls._format_number(match.group(1))
        multiplied = re.search(r"(?<!\d)(\d{1,5})\s*[xX×*]\s*(\d{1,5})(?!\d)", text)
        if multiplied:
            left, right = (int(multiplied.group(1)), int(multiplied.group(2)))
            if 0 < left <= 20 < right:
                return cls._format_number(right)
            if 0 < right <= 20 < left:
                return cls._format_number(left)
        return ""

    @staticmethod
    def _is_consumption_amount_query(value: object) -> bool:
        text = str(value or "")
        return bool(re.search(
            r"(?:消费|吃了|吃到|吃|账单|结账|买单|应付|实付|到店消费|一共消费|总共消费)"
            r"[^。！？\n]{0,8}\d+(?:\.\d+)?|"
            r"\d+(?:\.\d+)?\s*(?:元|块)?[^。！？\n]{0,8}(?:消费|账单|怎么买|怎么凑|如何凑)",
            text,
        ))

    @classmethod
    def _people_counts(cls, value: object) -> List[int]:
        text = str(value or "")
        number = r"[一二两三四五六七八九十单双俩仨\d]+"
        combined = re.search(rf"({number})\s*(?:个)?(?:大|成人)\s*({number})\s*(?:个)?(?:小|儿童|小孩)", text)
        if combined:
            adults = cls._chinese_count(combined.group(1))
            children = cls._chinese_count(combined.group(2))
            total = (adults or 0) + (children or 0)
            return [total] if 0 < total <= 20 else []
        if "一家三口" in text:
            return [3]
        range_match = re.search(
            r"([一二两三四五六七八九十\d]+)\s*(?:-|—|~|～|至|到)\s*"
            r"([一二两三四五六七八九十\d]+)\s*(?:人|位)",
            text,
        )
        if range_match:
            start = cls._chinese_count(range_match.group(1))
            end = cls._chinese_count(range_match.group(2))
            if start and end and 0 < start <= end <= 20:
                return list(range(start, end + 1))
        match = re.search(
            r"([一二两三四五六七八九十单双俩仨\d]+)\s*(?:个|口)?(?:人|位)",
            text,
        )
        if not match:
            match = re.search(r"(?<![一二两三四五六七八九十])([俩仨])(?![一二两三四五六七八九十])", text)
        count = cls._chinese_count(match.group(1)) if match else None
        return [count] if count and 0 < count <= 20 else []

    @staticmethod
    def _day_types(value: object) -> List[str]:
        text = str(value or "")
        result = []
        if re.search(r"全周|全时段|周一\s*(?:至|到|[-—~～])\s*周日|每天|每日", text):
            result.append("any")
        if re.search(
            r"工作日|平日|周中|周一至周五|周一到周五|"
            r"(?:星期|礼拜|周)[一二三四五](?![六日天])",
            text,
        ):
            result.append("weekday")
        if re.search(r"周末|周六|周日|星期[六日天]|礼拜[六日天]|双休(?:日)?", text):
            result.append("weekend")
        if re.search(r"节假日|法定假日|法定节假日", text):
            result.append("holiday")
        return list(dict.fromkeys(result))

    @staticmethod
    def _option_applicability_text(value: object) -> str:
        """Remove opening/closing schedules that are not SKU applicability limits."""
        parts = re.split(r"([。；;\n]+)", str(value or ""))
        kept = []
        schedule_words = re.compile(r"最后加餐|闭店|打烊|开门|关门|停止营业|营业至")
        explicit_limit = re.compile(
            r"仅限|只限|只能|专享|不可用|不能用|不适用|"
            r"(?:工作日|平日|周末|节假日|法定假日)[^。；;\n]{0,8}(?:可用|使用|适用|通用)"
        )
        for part in parts:
            if schedule_words.search(part) and not explicit_limit.search(part):
                continue
            kept.append(part)
        return "".join(kept)

    @classmethod
    def _option_day_types_from_fields(cls, name: object, applicable_time: object) -> List[str]:
        """SKU-name labels constrain dates; business-hour prose does not."""
        return list(dict.fromkeys(
            cls._day_types(name)
            + cls._day_types(cls._option_applicability_text(applicable_time))
        ))

    @classmethod
    def _option_meal_periods_from_fields(cls, name: object, applicable_time: object) -> List[str]:
        """Keep meal labels on the SKU while ignoring last-order schedules."""
        return list(dict.fromkeys(
            cls._meal_periods(name)
            + cls._meal_periods(cls._option_applicability_text(applicable_time))
        ))

    @classmethod
    def _audience_counts(cls, value: object) -> Dict[str, int]:
        """Return independently typed diner counts without conflating tickets."""
        text = str(value or "")
        number = r"[一二两三四五六七八九十单双俩仨\d]+"
        result: Dict[str, int] = {}
        combined = re.search(
            rf"({number})\s*(?:个)?(?:大|成人)\s*({number})\s*(?:个)?(?:小|儿童|小孩|小朋友)",
            text,
        )
        if combined:
            result["adult"] = cls._chinese_count(combined.group(1)) or 0
            result["child"] = cls._chinese_count(combined.group(2)) or 0
        first_identity = re.search(
            r"成人|大人|儿童|小孩|孩子|小朋友|宝宝|娃|学生|老人|老年人|长者|女士|女生|女性|女宾",
            text,
        )
        first_count = re.search(number, text)
        identity_first = bool(
            first_identity and first_count and first_identity.start() < first_count.start()
        )
        patterns = {
            "adult": rf"({number})\s*(?:个|名|位)?(?:成人|大人|大(?!学))",
            "child": rf"({number})\s*(?:个|名|位)?(?:儿童|小孩|孩子|小朋友|宝宝|娃|小(?!时|午))",
            "student": rf"({number})\s*(?:个|名|位)?学生",
            "senior": rf"({number})\s*(?:个|名|位)?(?:老人|老年人|长者|老)",
            "female": rf"({number})\s*(?:个|名|位)?(?:女士|女生|女性|女宾)",
        }
        if not identity_first:
            for kind, pattern in patterns.items():
                match = re.search(pattern, text)
                if match:
                    count = cls._chinese_count(match.group(1))
                    if count:
                        result[kind] = count
        # Identity-first colloquial forms: “成人2个老人1个孩子1个” and
        # “成人x2老人×1儿童x1”.
        reverse_patterns = {
            "adult": rf"(?:成人|大人|大(?!学))\s*(?:[xX×*]\s*)?({number})\s*(?:个|名|位)?",
            "child": rf"(?:儿童|小孩|孩子|小朋友|宝宝|娃)\s*(?:[xX×*]\s*)?({number})\s*(?:个|名|位)?",
            "student": rf"学生\s*(?:[xX×*]\s*)?({number})\s*(?:个|名|位)?",
            "senior": rf"(?:老人|老年人|长者)\s*(?:[xX×*]\s*)?({number})\s*(?:个|名|位)?",
            "female": rf"(?:女士|女生|女性|女宾)\s*(?:[xX×*]\s*)?({number})\s*(?:个|名|位)?",
        }
        for kind, pattern in reverse_patterns.items():
            if kind in result and not identity_first:
                continue
            match = re.search(pattern, text)
            if match:
                count = cls._chinese_count(match.group(1))
                if count:
                    result[kind] = count
        if "一家三口" in text and not result:
            # The composition is ambiguous when identity-specific fares exist.
            result["party"] = 3
        return {key: value for key, value in result.items() if 0 < value <= 20}

    @staticmethod
    def _audience_types(value: object) -> List[str]:
        text = str(value or "")
        result = []
        if re.search(r"儿童|小孩|孩子|小朋友|宝宝|娃|儿童票|儿童餐|\d+\s*小(?!时|午)", text):
            result.append("child")
        if re.search(r"学生|学生票", text):
            result.append("student")
        if re.search(r"老人|老年|长者|老人票|\d+\s*老", text):
            result.append("senior")
        if re.search(r"女士|女生|女性|女宾|女士票", text):
            result.append("female")
        if re.search(r"成人|大人|成人票|\d+\s*大(?!学)", text):
            result.append("adult")
        return list(dict.fromkeys(result))

    @staticmethod
    def _option_supports_audience(option: Dict, audience: str) -> bool:
        """An unlabeled SKU is an adult fare, never a special-identity fare."""
        explicit = set(option.get("audience_types") or [])
        if audience == "adult":
            return not explicit.intersection({"child", "student", "senior", "female"})
        return audience in explicit

    @classmethod
    def _effective_audience_types(cls, option: Dict) -> List[str]:
        explicit = list(option.get("audience_types") or [])
        return explicit or ["adult"]

    @classmethod
    def _requested_package_tier_options(
        cls, options: List[Dict], message: str,
    ) -> tuple[List[Dict], bool]:
        """Scope a buffet query to its named tier without hard-coding one menu."""
        available = list(options or [])
        if not available:
            return [], False
        raw = str(message or "")

        # Preserve the established business alias: buyers call the M8-9 SKU
        # “高阶和牛”, even when the marketplace SKU omits that marketing label.
        if re.search(r"M\s*8\s*[-—~～至到/]?\s*M?\s*9|M8-?9|高阶和牛", raw, re.I):
            matched = [
                option for option in available
                if re.search(
                    r"M\s*8\s*[-—~～至到/]?\s*M?\s*9|M8-?9",
                    str(option.get("name") or ""), re.I,
                )
            ]
            if matched:
                return matched, True

        canonical = re.sub(
            r"m\s*8\s*(?:[-—~～至到/]\s*)?m?\s*9", " m89 ", raw.lower(), flags=re.I,
        )
        canonical = normalize_text(canonical).lower()
        # Remove structural words. What remains is a brand/tier/menu marker,
        # such as “轻享”“尊享”“经典”“梭子蟹” or “m89”.
        structural = (
            r"请问|您好|你好|我要|我想|想要|帮我|给我|现在|这个|那个|这款|那款|"
            r"怎么拍|如何拍|怎么选|如何选|怎么买|如何买|怎么下单|多少钱|多钱|"
            r"什么价|价格|售价|收费|有吗|有没有|能拍吗|可以拍吗|"
            r"今天|明天|后天|今晚|明晚|工作日|平日|周末|节假日|法定假日|"
            r"早餐|早市|早上|上午|中午|午餐|午市|下午茶|晚餐|晚市|晚上|夜宵|"
            r"成人|大人|儿童|小孩|孩子|小朋友|学生|老人|老年人|长者|女士|女生|女宾|"
            r"单人|双人|三人|四人|五人|六人|七人|八人|九人|十人|"
            r"[一二两三四五六七八九十单双俩仨\d]+(?:个|口)?(?:人|位)|"
            r"自助餐|自助|套餐|团购|商品|规格|票种|票|券|一份|一张|份|张|呢|吗|嘛|么"
        )
        canonical = re.sub(structural, " ", canonical, flags=re.I)
        tokens = [
            token for token in re.findall(r"m\d+|[a-z]{2,}\d*|[\u4e00-\u9fff]{2,}", canonical)
            if token
        ]
        if not tokens:
            return available, False

        option_names = [
            re.sub(
                r"m\s*8\s*(?:[-—~～至到/]\s*)?m?\s*9", "m89",
                normalize_text(option.get("name") or "").lower(), flags=re.I,
            )
            for option in available
        ]
        recognized = [token for token in tokens if any(token in name for name in option_names)]
        if not recognized:
            return available, False
        discriminating = [
            token for token in recognized
            if sum(token in name for name in option_names) < len(option_names)
        ]
        required = discriminating or recognized
        matched = [
            option for option, name in zip(available, option_names)
            if all(token in name for token in required)
        ]
        return (matched or available), True

    @staticmethod
    def _height_cm(value: object) -> Optional[Decimal]:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(米|m|厘米|cm)", str(value or ""), re.I)
        if not match:
            return None
        number = Decimal(match.group(1))
        return number * 100 if match.group(2).lower() in {"米", "m"} else number

    @staticmethod
    def _age_years(value: object) -> Optional[Decimal]:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:周?岁|年龄)", str(value or ""))
        return Decimal(match.group(1)) if match else None

    @classmethod
    def _option_person_constraint_match(
        cls, option: Dict, *, height_cm: Optional[Decimal] = None,
        age_years: Optional[Decimal] = None,
    ) -> Optional[bool]:
        """Return whether one option's explicit height/age tier matches."""
        evidence = " ".join(str(option.get(key) or "") for key in ("name", "applicable_time"))
        value, unit_pattern = (height_cm, r"米|m|厘米|cm") if height_cm is not None else (
            age_years, r"周?岁",
        )
        if value is None:
            return None

        def normalized(number: str, unit: str) -> Decimal:
            parsed = Decimal(number)
            if height_cm is not None and str(unit).lower() in {"米", "m"}:
                return parsed * 100
            return parsed

        ranged = re.search(
            rf"(\d+(?:\.\d+)?)\s*({unit_pattern})?\s*(?:-|—|~|～|至|到)\s*"
            rf"(\d+(?:\.\d+)?)\s*({unit_pattern})",
            evidence,
            re.I,
        )
        if ranged:
            unit1 = ranged.group(2) or ranged.group(4)
            low = normalized(ranged.group(1), unit1)
            high = normalized(ranged.group(3), ranged.group(4))
            return low <= value <= high
        bounded = re.search(
            rf"(\d+(?:\.\d+)?)\s*({unit_pattern})\s*"
            r"(以下|以内|及以下|不超过|以上|及以上|超过)",
            evidence,
            re.I,
        )
        if bounded:
            boundary = normalized(bounded.group(1), bounded.group(2))
            return value <= boundary if bounded.group(3) in {"以下", "以内", "及以下", "不超过"} else value >= boundary
        return None

    @staticmethod
    def _meal_periods(value: object) -> List[str]:
        text = str(value or "")
        result = []
        if re.search(r"全天|全时段", text):
            result.append("any")
        if re.search(r"早餐|早市|早上|上午", text):
            result.append("breakfast")
        if re.search(r"午餐|午市|中午|午间|午饭|中餐", text):
            result.append("lunch")
        if re.search(r"今晚|明晚|晚餐|晚市|晚上|夜间|夜宵|晚饭", text):
            result.append("dinner")
        if re.search(r"下午茶|茶歇|午后茶", text):
            result.append("afternoon_tea")
        return list(dict.fromkeys(result))

    @classmethod
    def _normalize_sale_option(cls, record: Dict) -> Optional[Dict]:
        voucher = cls._normalize_product_option(record)
        if voucher:
            return {
                **voucher,
                "option_type": "voucher",
                "people_counts": cls._people_counts(voucher.get("name")),
                "day_types": cls._option_day_types_from_fields(
                    voucher.get("name"), voucher.get("applicable_time"),
                ),
                "meal_periods": cls._option_meal_periods_from_fields(
                    voucher.get("name"), voucher.get("applicable_time"),
                ),
            }
        name = str(cls._pick(record, (
            "商品名", "商品名称", "规格", "规格名称", "套餐名", "套餐名称", "name", "title",
        )) or "").strip()
        option_type = str(cls._pick(record, (
            "规格类型", "商品类型", "选项类型", "option_type", "kind", "type", "category",
        )) or "").strip()
        price = cls._format_number(cls._pick(record, (
            "售价", "销售价", "价格", "实售价", "sale_price", "price",
        )))
        applicable_time = str(cls._pick(record, (
            "适用时间", "使用时间", "可用时间", "时段", "日期类型", "applicable_time",
        )) or "").strip()
        people_value = cls._pick(record, ("人数", "适用人数", "people_count", "people"))
        day_value = cls._pick(record, ("适用日期", "日期", "applicable_day", "day_type"))
        meal_value = cls._pick(record, ("餐段", "用餐时段", "meal_period"))
        evidence = " ".join(
            str(value or "") for value in
            (name, option_type, applicable_time, people_value, day_value, meal_value)
        )
        if not re.search(
            r"套餐|人餐|自助|单人|双人|[一二两三四五六七八九十\d]+人|"
            r"儿童|小孩|学生|老人|老年|长者|女士|女生|成人票|"
            r"菜品|餐品|烤鱼|单鱼|整条|单条|\d+(?:\.\d+)?\s*斤",
            evidence,
        ):
            return None
        if re.search(r"代金券|抵扣券|现金券", evidence) or not name or not price:
            return None
        try:
            if Decimal(price) <= 0:
                return None
        except InvalidOperation:
            return None
        return {
            "name": name,
            "face_value": "",
            "sale_price": price,
            "applicable_time": applicable_time,
            "composition": "",
            "max_stack": "",
            "option_type": "package",
            "people_counts": cls._people_counts(f"{name} {people_value}"),
            "day_types": cls._option_day_types_from_fields(
                name, f"{applicable_time} {day_value}",
            ),
            "meal_periods": cls._option_meal_periods_from_fields(
                name, f"{applicable_time} {meal_value}",
            ),
            "audience_types": cls._audience_types(evidence),
            **cls._option_availability(record),
        }

    @classmethod
    def extract_sale_options(cls, product: Dict) -> List[Dict]:
        """Return verifiable coupon and package choices for semantic filtering."""
        output = []
        for option in cls.extract_product_options(product):
            output.append({
                **option,
                "option_type": "voucher",
                "people_counts": cls._people_counts(option.get("name")),
                "day_types": cls._option_day_types_from_fields(
                    option.get("name"), option.get("applicable_time"),
                ),
                "meal_periods": cls._option_meal_periods_from_fields(
                    option.get("name"), option.get("applicable_time"),
                ),
                "audience_types": cls._audience_types(" ".join(
                    str(option.get(key) or "") for key in ("name", "applicable_time")
                )),
            })

        structured = product.get("structured") or {}
        facts = structured.get("facts") if isinstance(structured, dict) else {}
        candidates = []
        for container in (structured, facts):
            if not isinstance(container, dict):
                continue
            for key in (
                "sku_profiles", "sale_options", "packages", "package_options", "套餐规格", "套餐列表",
                "products", "product_options", "skus", "sku", "商品规格", "商品列表", "规格",
            ):
                value = container.get(key)
                if isinstance(value, list):
                    candidates.extend(item for item in value if isinstance(item, dict))
                elif isinstance(value, dict):
                    candidates.extend(item for item in value.values() if isinstance(item, dict))
        output.extend(
            option for option in (cls._normalize_sale_option(record) for record in candidates)
            if option and option.get("option_type") == "package"
        )

        raw_text = str(product.get("raw_text") or "").replace("\\n", "\n")
        has_structured_package = any(item.get("option_type") == "package" for item in output)
        for line in ([] if has_structured_package else re.split(r"[\r\n；;]+", raw_text)):
            if not re.search(
                r"套餐|人餐|自助|单人|双人|[一二两三四五六七八九十\d]+人|"
                r"儿童|小孩|学生|老人|老年|长者|女士|女生|成人|大人|"
                r"菜品|餐品|烤鱼|单鱼|整条|单条|\d+(?:\.\d+)?\s*斤",
                line,
            ):
                continue
            match = re.search(
                r"^\s*(?:[①②③④⑤⑥⑦⑧⑨⑩]|\d{1,2}、\s*|\d{1,2}\.\s+|\d?⃣️?)?\s*"
                r"(?P<name>[^：:=\n]{1,60}?)\s*[：:=]\s*(?:售价|价格)?\s*[¥￥]?"
                r"(?P<price>\d+(?:\.\d+)?)\s*元?",
                line,
            )
            if not match:
                continue
            name = match.group("name").strip(" ，,、")
            price = cls._format_number(match.group("price"))
            evidence = f"{name} {line}"
            output.append({
                "name": name,
                "face_value": "",
                "sale_price": price,
                "applicable_time": line,
                "composition": "",
                "max_stack": "",
                "option_type": "package",
                "people_counts": cls._people_counts(evidence),
                "day_types": cls._option_day_types_from_fields(name, line),
                "meal_periods": cls._option_meal_periods_from_fields(name, line),
                "audience_types": cls._audience_types(evidence),
            })

        unique = []
        positions = {}
        for option in output:
            if not cls._is_sellable_option(option):
                continue
            key = (
                option.get("option_type"), normalize_text(option.get("name")),
                cls._option_total_value(option), normalize_text(option.get("composition")),
                tuple(option.get("day_types") or []),
                tuple(option.get("meal_periods") or []),
            )
            if key in positions:
                index = positions[key]
                current = unique[index]
                # A limit sentence can resemble a two-yuan SKU after loose
                # normalization. Prefer the option backed by explicit SKU
                # availability instead of choosing the lowest parsed number.
                if option.get("availability_explicit") and not current.get("availability_explicit"):
                    unique[index] = option
                continue
            positions[key] = len(unique)
            unique.append(option)
        return unique

    @staticmethod
    def _is_sellable_option(option: Dict) -> bool:
        return str(option.get("availability") or "available") == "available"

    @classmethod
    def _requested_price_amount(cls, value: object) -> str:
        text = str(value or "").strip()
        patterns = (
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:代金券|优惠券|券|套餐)?\s*(?:的)?\s*"
            r"(?:多少钱|多钱|几多钱|多少|什么价|啥价|价格(?:多少|呢)?|价钱|怎么卖|"
            r"怎么收|怎么买|怎么拍|要付多少|实付多少|实际多少钱)",
            r"(?:消费|吃了|吃到|账单|结账|买单|一共|总共)\s*(\d+(?:\.\d+)?)\s*(?:元|块)?",
            r"多少\s*代\s*(\d+(?:\.\d+)?)",
            r"(?<!\d)(\d+(?:\.\d+)?)\s*代\s*多少",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return cls._format_number(match.group(1))
        return ""

    @classmethod
    def _conditional_query_slots(cls, message: str) -> Dict:
        text = str(message or "").strip()
        # In correction phrases, the last positive clause is authoritative.
        for marker in ("而是", "改成", "要查", "我要"):
            if marker in text:
                text = text.rsplit(marker, 1)[-1]
        if re.search(r"不是|不要|别查|不查", text):
            correction = re.search(r"(?:而是|改成|[，,；;]\s*(?:是|要))\s*(.+)$", text)
            if correction:
                text = correction.group(1)
        people = cls._people_counts(text)
        day_types = cls._day_types(text)
        meal_periods = cls._meal_periods(text)
        if re.search(r"今天|今日|今晚|今早|今晨|今中午", text):
            day_types = ["weekend" if datetime.now(CHINA_TZ).weekday() >= 5 else "weekday"]
        elif re.search(r"明天|明日|明早|明晨|明中午|明午|明晚", text):
            tomorrow = datetime.now(CHINA_TZ) + timedelta(days=1)
            day_types = ["weekend" if tomorrow.weekday() >= 5 else "weekday"]
        target_date = cls._query_date(text)
        if target_date:
            day_types = ["weekend" if target_date.weekday() >= 5 else "weekday"]
        purchase_quantity, purchase_unit = cls._purchase_quantity_slot(text)
        target_amount = cls._requested_price_amount(text)
        quantity_denomination = cls._quantity_denomination(text)
        if quantity_denomination:
            target_amount = quantity_denomination
        return {
            "people_count": people[0] if len(people) == 1 else None,
            "people_counts": people,
            "audience_counts": cls._audience_counts(text),
            "audience_types": cls._audience_types(text),
            "day_type": day_types[-1] if day_types else "",
            "day_types": day_types,
            "meal_period": meal_periods[-1] if meal_periods else "",
            "meal_periods": meal_periods,
            "date_label": (
                "今天" if re.search(r"今天|今日|今晚|今早|今晨|今中午", text)
                else "明天" if re.search(r"明天|明日|明早|明晨|明中午|明午|明晚", text)
                else f"{target_date.month}月{target_date.day}日" if target_date else ""
            ),
            "target_amount": target_amount,
            "purchase_quantity": purchase_quantity,
            "purchase_unit": purchase_unit,
            "amount_kind": (
                "consumption" if target_amount and cls._is_consumption_amount_query(text)
                else "option" if target_amount else ""
            ),
        }

    @staticmethod
    def _conditional_subject(slots: Dict) -> str:
        labels = []
        if slots.get("date_label"):
            labels.append(str(slots["date_label"]))
        elif slots.get("day_type"):
            labels.append({
                "weekday": "工作日", "weekend": "周末", "holiday": "节假日", "any": "全周",
            }.get(slots["day_type"], slots["day_type"]))
        if slots.get("meal_period"):
            labels.append({
                "breakfast": "早餐", "lunch": "中午", "dinner": "晚餐",
                "afternoon_tea": "下午茶", "any": "全天",
            }.get(slots["meal_period"], slots["meal_period"]))
        if slots.get("people_count"):
            labels.append(f"{slots['people_count']}人")
        if slots.get("target_amount"):
            labels.append(f"{slots['target_amount']}元")
        if slots.get("purchase_quantity"):
            labels.append(f"{slots['purchase_quantity']}{slots.get('purchase_unit') or '份'}")
        return "".join(labels) or "所问条件"

    @staticmethod
    def _option_matches_time(option: Dict, day_type: str = "", meal_period: str = "",
                             holiday_covers_weekend: bool = False) -> bool:
        """Empty option metadata means unrestricted; explicit metadata constrains it."""
        days = option.get("day_types") or []
        meals = option.get("meal_periods") or []
        if day_type and days:
            accepted_days = {day_type, "any"}
            if day_type == "weekend" and holiday_covers_weekend:
                accepted_days.add("holiday")
            if not accepted_days.intersection(days):
                return False
        if meal_period and meals and not {meal_period, "any"}.intersection(meals):
            return False
        return True

    @staticmethod
    def _current_day_type() -> str:
        return "weekend" if datetime.now(CHINA_TZ).weekday() >= 5 else "weekday"

    @classmethod
    def _requested_day_type(cls, message: str, *, default_today: bool = False) -> str:
        """Resolve an explicit day/date first; optionally fall back to today."""
        explicit = cls._conditional_query_slots(message).get("day_type")
        if explicit:
            return explicit
        target = cls._query_date(message)
        if target:
            return "weekend" if target.weekday() >= 5 else "weekday"
        return cls._current_day_type() if default_today else ""

    @classmethod
    def _day_reply_prefix(cls, message: str, day_type: str) -> str:
        label = "周末" if day_type == "weekend" else "工作日"
        text = str(message or "")
        if re.search(r"今天|今日", text) or not cls._requested_day_type(text):
            return f"今天是{label}，"
        return f"按您指定的日期（{label}），"

    @classmethod
    def _option_day_types(cls, option: Dict) -> List[str]:
        values = list(option.get("day_types") or [])
        if values:
            return values
        return cls._option_day_types_from_fields(
            option.get("name"), option.get("applicable_time"),
        )

    @classmethod
    def _has_explicit_day_options(cls, options: List[Dict]) -> bool:
        return any(cls._option_day_types(option) for option in options)

    @classmethod
    def _filter_options_for_day(cls, options: List[Dict], day_type: str) -> List[Dict]:
        """Keep the applicable real options and prefer a day-specific tier."""
        normalized = [
            {**option, "day_types": cls._option_day_types(option)} for option in options
        ]
        has_weekend = any("weekend" in option["day_types"] for option in normalized)
        has_holiday = any("holiday" in option["day_types"] for option in normalized)
        holiday_covers_weekend = day_type == "weekend" and has_holiday and not has_weekend
        compatible = [
            option for option in normalized
            if cls._option_matches_time(
                option, day_type=day_type,
                holiday_covers_weekend=holiday_covers_weekend,
            )
        ]
        specific = [
            option for option in compatible
            if day_type in option["day_types"]
            or (holiday_covers_weekend and "holiday" in option["day_types"])
        ]
        return specific or compatible

    @classmethod
    def _query_date(cls, value: object) -> Optional[datetime]:
        text = str(value or "")
        now = datetime.now(CHINA_TZ)
        if re.search(r"今天|今日|今晚|今早|今晨|今中午", text):
            return now
        if re.search(r"明天|明日|明早|明晨|明中午|明午|明晚", text):
            return now + timedelta(days=1)
        if "后天" in text:
            return now + timedelta(days=2)
        match = re.search(
            r"(?<!\d)(?:(\d{4})[年./-])?(\d{1,2})[月./-](\d{1,2})(?:日|号)?"
            r"(?!\s*(?:公斤|千克|斤|克|kg|KG|元|块|折|人|位|张|份|套|桌))(?!\d)",
            text,
        )
        if not match:
            return None
        year = int(match.group(1) or now.year)
        try:
            return datetime(year, int(match.group(2)), int(match.group(3)), tzinfo=CHINA_TZ)
        except ValueError:
            return None

    @classmethod
    def _date_is_explicitly_unavailable(cls, knowledge: str, target: datetime) -> bool:
        month, day, year = target.month, target.day, target.year
        normalized = str(knowledge or "").replace("号", "日")
        unavailable_tail = (
            r"(?:[^。；\n]{0,20}(?:不可用|不能用|不适用)|"
            r"[^。；\n]{0,20}外[^。；\n]{0,20}(?:可用|能用|使用))"
        )
        range_patterns = (
            r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日?\s*(?:至|到|[-—~～])\s*"
            r"(?:(\d{1,2})月)?(\d{1,2})日?" + unavailable_tail,
            r"(?:(\d{4})[./-])?(\d{1,2})[./-](\d{1,2})\s*(?:至|到|[-—~～])\s*"
            r"(?:(\d{1,2})[./-])?(\d{1,2})" + unavailable_tail,
        )
        for pattern in range_patterns:
            for match in re.finditer(pattern, normalized):
                range_year = int(match.group(1) or year)
                start_month = int(match.group(2))
                start_day = int(match.group(3))
                end_month = int(match.group(4) or start_month)
                end_day = int(match.group(5))
                try:
                    start = datetime(range_year, start_month, start_day, tzinfo=CHINA_TZ)
                    end = datetime(range_year, end_month, end_day, tzinfo=CHINA_TZ)
                except ValueError:
                    continue
                if start <= target <= end:
                    return True
        direct = re.search(
            rf"(?:{year}年)?0?{month}月0?{day}日?"
            rf"(?:[^。；\n]{{0,20}}(?:不可用|不能用|不适用)|"
            rf"[^。；\n]{{0,20}}外[^。；\n]{{0,20}}(?:可用|能用|使用))",
            normalized,
        )
        return bool(direct)

    @classmethod
    def _product_rule_knowledge(cls, product: Dict) -> str:
        """Collect rule evidence from raw text, summaries, structured facts and SKU fields."""
        values = [
            str(product.get("raw_text") or ""),
            str(product.get("ai_summary") or ""),
            # Keep the original marketplace payload available as a read-only
            # fallback when an older AI summary omitted a page rule.
            cls._platform_description(product),
        ]

        def collect(node):
            if isinstance(node, dict):
                for value in node.values():
                    collect(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    collect(value)
            elif isinstance(node, str) and node.strip():
                values.append(node.strip())

        collect(product.get("structured") or {})
        output = []
        seen = set()
        for value in values:
            key = normalize_match_text(value)
            if value.strip() and key not in seen:
                seen.add(key)
                output.append(value.strip())
        return "\n".join(output)

    def date_availability_reply(self, product: Dict, message: str) -> Optional[Dict]:
        text = str(message or "")
        use_intent = bool(re.search(r"(?:可以|能|可)(?:使用|用)|能不能用|是否可用|用得了", text))
        target = self._query_date(text)
        holiday_name = next((name for name in ("中秋", "国庆", "春节", "元旦", "劳动节") if name in text), "")
        if not use_intent or (not target and not holiday_name):
            return None
        # A concrete meal period is more specific than a date-only question.
        # Let the condition resolver evaluate both dimensions together.
        if any(period != "any" for period in self._meal_periods(text)):
            return None
        knowledge = self._product_rule_knowledge(product)
        holiday_unavailable = False
        if holiday_name:
            for clause in re.split(r"[。；\n]+", knowledge):
                if holiday_name not in clause:
                    continue
                after_name = clause.split(holiday_name, 1)[1]
                explicitly_allowed_first = re.search(r"^(?:[^，,、]{0,16})(?:可用|能用|可以使用)", after_name)
                if not explicitly_allowed_first and (
                    re.search(r"不可用|不能用|不适用", clause)
                    or re.search(rf"除[^，,。；\n]{{0,80}}{holiday_name}[^。；\n]{{0,80}}外[^。；\n]{{0,20}}(?:可用|能用|使用)", clause)
                ):
                    holiday_unavailable = True
                    break
            if re.search(r"(?:法定)?节假日[^。；\n]{0,12}(?:不可用|不能用|不适用)", knowledge):
                holiday_unavailable = True
        if holiday_unavailable:
            return {"reply": f"{holiday_name}在商品标注的不可用日期范围内，不能使用哦。",
                    "source": "当前商品明确不可用节日", "decision": "allow", "kind": "date_use"}
        if target and self._date_is_explicitly_unavailable(knowledge, target):
            label = f"{target.month}月{target.day}日"
            return {"reply": f"{label}在商品标注的不可用日期范围内，当天不能使用哦。",
                    "source": "当前商品明确不可用日期", "decision": "allow", "kind": "date_use"}
        if target:
            day_type = "weekend" if target.weekday() >= 5 else "weekday"
            options = [item for item in self.extract_sale_options(product) if item.get("sale_price")]
            has_weekend = any("weekend" in (item.get("day_types") or []) for item in options)
            has_holiday = any("holiday" in (item.get("day_types") or []) for item in options)
            holiday_covers = day_type == "weekend" and has_holiday and not has_weekend
            explicit = [item for item in options if item.get("day_types")]
            compatible = [item for item in options if self._option_matches_time(
                item, day_type=day_type, holiday_covers_weekend=holiday_covers
            )]
            if explicit and not compatible:
                return {"reply": f"{target.month}月{target.day}日没有可用的商品选项哦。",
                        "source": "当前商品日期适用范围", "decision": "allow", "kind": "date_use"}
            prefix = (
                "明天" if re.search(r"明天|明日|明早|明晨|明中午|明午|明晚", text)
                else f"{target.month}月{target.day}日"
            )
            if explicit and compatible:
                brand = self.extract_brand(product)
                choices = "\n".join(
                    self.format_product_option(option, brand, show_time=True)
                    for option in compatible
                )
                return {"reply": (
                            f"{prefix}可以使用，请选择以下当日适用的在售规格：\n"
                            f"{choices}\n请在使用当天购买、当天使用。"
                        ),
                        "source": "北京时间、当前商品日期规则与真实SKU列表",
                        "decision": "allow", "kind": "date_use",
                        "query_context_update": {
                            "pending_day_type": day_type,
                            "pending_day_label": prefix,
                        }}
            return {"reply": f"{prefix}可以使用哦，请在使用当天购买、当天使用。",
                    "source": "北京时间与当前商品日期规则", "decision": "allow", "kind": "date_use"}
        holiday_options = [
            item for item in self.extract_sale_options(product)
            if item.get("sale_price") and item.get("day_types")
        ]
        compatible_holiday = [
            item for item in holiday_options
            if self._option_matches_time(item, day_type="holiday")
        ]
        if holiday_options and not compatible_holiday:
            return {"reply": f"{holiday_name}没有可用的商品规格哦。",
                    "source": "当前商品SKU节日适用范围", "decision": "allow", "kind": "date_use"}
        if compatible_holiday:
            brand = self.extract_brand(product)
            choices = "\n".join(
                self.format_product_option(option, brand, show_time=True)
                for option in compatible_holiday
            )
            return {"reply": (
                        f"{holiday_name}期间可以使用，请选择以下节假日适用的在售规格：\n"
                        f"{choices}\n请在使用当天购买、当天使用。"
                    ),
                    "source": "当前商品SKU节日适用范围", "decision": "allow", "kind": "date_use"}
        return {"reply": f"{holiday_name}期间可以使用哦，请在使用当天购买、当天使用。",
                "source": "当前商品节日适用范围", "decision": "allow", "kind": "date_use"}

    def day_availability_reply(self, product: Dict, message: str) -> Optional[Dict]:
        """Resolve explicit weekday/weekend/holiday usage questions only."""
        text = str(message or "")
        use_intent = bool(re.search(
            r"(?:能不能|可不可以|是否|能|可以|可|不能|不可以|不可).{0,3}(?:使用|用|核销)|"
            r"(?:使用|用|核销).{0,3}(?:吗|嘛|么|不|不了)|"
            r"(?:通用|可用).{0,4}(?:还有|其他|别的)|(?:还有|其他|别的).{0,4}(?:通用|可用)", text,
        ))
        if not use_intent:
            return None
        if re.search(r"周末|周六|周日|星期[六日天]|礼拜[六日天]|双休(?:日)?", text):
            day_type, label = "weekend", "周末"
        elif re.search(r"工作日|平日|周中|周一至周五|周一到周五", text):
            day_type, label = "weekday", "工作日"
        elif re.search(r"节假日|法定假日|法定节假日", text):
            day_type, label = "holiday", "节假日"
        else:
            return None

        options = self.extract_sale_options(product)
        requested_amount = self._requested_price_amount(text)
        if not requested_amount:
            face_match = re.search(
                r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:的)?\s*(?:代金券|优惠券|券)",
                text,
            )
            if face_match:
                requested_amount = self._format_number(face_match.group(1))
        amount_scoped = False
        if requested_amount:
            amount_options = [
                option for option in options
                if self._format_number(option.get("face_value")) == requested_amount
                or bool(re.search(
                    rf"(?<![\d.]){re.escape(requested_amount)}(?:\.0+)?\s*(?:元|块|面额|选项)",
                    str(option.get("name") or ""),
                ))
            ]
            if not amount_options:
                return {
                    "reply": f"当前商品没有{requested_amount}元这一已确认的在售规格。",
                    "source": "当前商品真实SKU列表", "decision": "allow",
                    "kind": "day_use",
                }
            options = amount_options
            amount_scoped = True

        named = self.match_message_skus(str(product.get("item_id") or ""), text, product)
        named_label = self._sku_public_label(named[0]) if len(named) == 1 else ""
        explicit_named = bool(
            named_label and normalize_text(named_label) in normalize_text(text)
        )
        if explicit_named:
            named_options = [
                option for option in options
                if self.sku_key_for_option(option) == named[0]["sku_key"]
            ]
            if named_options:
                options = named_options

        # A denial belonging to one SKU must never disable every other SKU.
        # Once the buyer names a face value/specification, inspect only that
        # option's own name/time fields. Product-wide prose remains the fallback
        # for genuinely generic questions such as “周末能用吗”.
        scoped = amount_scoped or explicit_named
        day_signatures = {
            tuple(sorted(self._option_day_types(option))) for option in options
            if self._option_day_types(option)
        }
        has_distinct_day_skus = len(day_signatures) > 1
        knowledge = (
            "\n".join(
                " ".join(str(option.get(key) or "") for key in ("name", "applicable_time"))
                for option in options
            )
            if scoped else
            "\n".join((
                str(product.get("raw_text") or ""), str(product.get("ai_summary") or ""),
            ))
        )
        explicit_denial = {
            "weekend": (
                r"(?:周末|周六|周日|星期[六日天]|礼拜[六日天])[^。；\n]{0,12}(?:不可用|不能用|不适用)|"
                r"(?:仅限|只限|只能)[^。；\n]{0,8}(?:工作日|周一至周五)"
            ),
            "weekday": (
                r"(?:工作日|平日|周一至周五)[^。；\n]{0,12}(?:不可用|不能用|不适用)|"
                r"(?:仅限|只限|只能)[^。；\n]{0,8}(?:周末|周六|周日)"
            ),
            "holiday": (
                r"(?:节假日|法定假日|法定节假日)[^。；\n]{0,12}(?:不可用|不能用|不适用)"
            ),
        }[day_type]
        if re.search(explicit_denial, knowledge) and not (
            not scoped and has_distinct_day_skus
        ):
            return {
                "reply": f"不可以，当前商品规则明确标注{label}不可用。",
                "source": f"当前商品明确的{label}限制", "decision": "allow",
                "kind": "day_use",
            }

        # The source commonly writes “平日节假日通用”.  In merchant usage,
        # the holiday/weekend option covers non-working days; absence of a
        # literal “周末” token must not turn that statement into a denial.
        has_weekend = any("weekend" in (option.get("day_types") or []) for option in options)
        has_holiday = any("holiday" in (option.get("day_types") or []) for option in options)
        holiday_covers_weekend = day_type == "weekend" and has_holiday and not has_weekend
        compatible = [option for option in options if self._option_matches_time(
            option, day_type, holiday_covers_weekend=holiday_covers_weekend
        )]
        if options and not compatible:
            return {
                "reply": f"不可以，当前商品没有适用于{label}的规格。",
                "source": f"当前商品规格的{label}适用范围", "decision": "allow",
                "kind": "day_use",
            }
        if (
            not scoped and has_distinct_day_skus and compatible
            and not re.search(r"还有|其他|别的|另外|换一", text)
        ):
            brand = self.extract_brand(product)
            return {
                "reply": f"可以，{label}请选择以下适用规格：\n" + "\n".join(
                    self.format_product_option(option, brand, show_time=True)
                    for option in compatible
                ),
                "source": f"当前商品真实SKU的{label}适用范围",
                "decision": "allow", "kind": "day_use",
            }
        expansion = bool(re.search(r"还有|其他|别的|另外|换一", text))
        if expansion and compatible:
            brand = self.extract_brand(product)
            return {
                "reply": f"有的，当前商品{label}可用的规格如下：\n" + "\n".join(
                    self.format_product_option(option, brand, show_time=True)
                    for option in compatible
                ),
                "source": f"当前商品真实SKU的{label}适用范围",
                "decision": "allow", "kind": "sku_availability",
                "query_context_update": {"last_sku_catalog": True},
            }
        if explicit_named:
            subject = self._sku_public_label(named[0])
        elif requested_amount:
            subject = f"{requested_amount}元代金券"
        else:
            subject = "该券"
        return {
            "reply": f"可以，{subject}{label}在适用门店营业时间内可以使用；商品明确标注的特殊不可用日期除外。",
            "source": "无特别限制时默认适用门店营业时间内可用",
            "decision": "allow", "kind": "day_use",
        }

    def audience_price_reply(
        self, product: Dict, message: str, query_context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        text = str(message or "")
        previous = dict((query_context or {}).get("price_filters") or {})
        direct_counts = self._audience_counts(text)
        counts = dict(direct_counts)
        explicit_types = self._audience_types(text)
        knowledge = "\n".join((str(product.get("raw_text") or ""), str(product.get("ai_summary") or "")))
        identity_product = bool(re.search(
            r"自助|成人|儿童|小孩|学生|老人|老年|女士|女宾", knowledge
        ))
        if not identity_product:
            return None
        slots = self._conditional_query_slots(text)
        if not slots.get("day_type") and previous.get("day_type"):
            slots["day_type"] = previous["day_type"]
        if not slots.get("meal_period") and previous.get("meal_period"):
            slots["meal_period"] = previous["meal_period"]
        if not slots.get("date_label") and previous.get("date_label"):
            slots["date_label"] = previous["date_label"]
        awaiting = str(previous.get("awaiting") or "")
        if not counts and awaiting in {"meal_period", "day_type"}:
            counts = {
                str(kind): int(count)
                for kind, count in dict(previous.get("audience_counts") or {}).items()
                if str(count).isdigit() and 0 < int(count) <= 20
            }
        price_intent = bool(re.search(r"多少钱|多钱|价格|售价|怎么卖|几块|几元|收费|票价", text))
        availability_intent = bool(re.search(r"有吗|有没有|有货|能买|能拍|可以吗", text))
        plan_intent = bool(re.search(
            r"怎么买|如何买|怎么拍|如何拍|怎么选|买哪种|拍哪种|"
            r"买几张|拍几张|需要几张|怎么下单|购买方案|给个方案|推荐",
            text,
        ))
        if (
            not counts and len(explicit_types) == 1
            and (price_intent or availability_intent or plan_intent)
        ):
            counts = {explicit_types[0]: 1}
        options = [
            item for item in self.extract_sale_options(product)
            if item.get("sale_price") and self._is_sellable_option(item)
        ]
        options, _tier_scoped = self._requested_package_tier_options(options, text)
        explicit_option_audiences = {
            audience
            for option in options
            for audience in (option.get("audience_types") or [])
        }
        option_audiences = {
            audience
            for option in options
            for audience in self._effective_audience_types(option)
        }
        has_unlabelled_adult_option = any(
            not (option.get("audience_types") or []) for option in options
        )
        party_counts = self._people_counts(text)
        party_count = party_counts[0] if len(party_counts) == 1 else None

        # A bare party count such as “三人” means three adults by business rule.
        # Only explicit child/senior/student wording switches to identity fares.
        # This default also applies when the catalogue contains those fares.
        if (
            not counts and party_count and "adult" in explicit_option_audiences
            and not has_unlabelled_adult_option
            and (price_intent or availability_intent or plan_intent)
        ):
            counts = {"adult": party_count}

        # “老人和小孩怎么收费” asks for the fare table, not for an assumed
        # one-senior/one-child order. Quote every requested real SKU and wait
        # for counts before calculating a total.
        if not counts and explicit_types and price_intent:
            quoted = []
            for option in options:
                audiences = set(option.get("audience_types") or [])
                if audiences and audiences.intersection(explicit_types):
                    quoted.append(self._format_conditional_option(option, slots))
            if quoted:
                return {
                    "reply": "；".join(dict.fromkeys(quoted)) + "。如需计算合计，请告诉我各有几位。",
                    "source": "自助餐当前在售身份票SKU",
                    "decision": "allow", "kind": "audience_price",
                    "query_context_update": {"price_filters": {
                        **{key: slots.get(key) for key in ("day_type", "meal_period", "date_label") if slots.get(key)},
                        "intent": "price", "awaiting": "audience_mix",
                        "updated_at": datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
                    }},
                }

        def make_audience_context(next_slot: str = "") -> Dict:
            return {"price_filters": {
                **{key: slots.get(key) for key in (
                    "day_type", "meal_period", "date_label", "people_count",
                ) if slots.get(key) not in (None, "")},
                "audience_counts": counts,
                "intent": "price",
                "awaiting": next_slot,
                "updated_at": datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
            }}

        if (
            not counts and party_count and len(option_audiences) >= 2
            and (price_intent or availability_intent or plan_intent)
        ):
            if has_unlabelled_adult_option and not explicit_types:
                return None
            slots["people_count"] = party_count
            identity_labels = {
                "adult": "成人", "senior": "老人", "child": "儿童",
                "student": "学生", "female": "女士",
            }
            choices = "、".join(
                identity_labels[kind]
                for kind in ("adult", "senior", "child", "student", "female")
                if kind in option_audiences
            )
            return {
                "reply": f"{party_count}位用餐需要区分票种，请问分别有几位{choices}？",
                "source": "自助餐不同身份使用不同票价",
                "decision": "allow", "kind": "audience_price",
                "query_context_update": make_audience_context("audience_mix"),
            }
        if not counts:
            return None
        if previous.get("people_count") and direct_counts:
            expected = int(previous["people_count"])
            actual = sum(direct_counts.values())
            if expected != actual:
                return {
                    "reply": f"刚才说的是{expected}位，但这次人数合计为{actual}位，请重新确认成人、老人和儿童各几位。",
                    "source": "自助餐分票人数与总人数不一致",
                    "decision": "allow", "kind": "audience_price",
                    "query_context_update": make_audience_context("audience_mix"),
                }
        explicit_mix = len(counts) >= 2 or bool(re.search(
            r"\d+\s*(?:大|成人).*\d+\s*(?:小|儿童|小孩)|"
            r"[一二两三四五六七八九十]+\s*(?:大|成人).*"
            r"[一二两三四五六七八九十]+\s*(?:小|儿童|小孩)|一家三口",
            text,
        ))
        if not (price_intent or availability_intent or plan_intent or explicit_mix or awaiting):
            return None
        if set(counts) == {"party"}:
            return {
                "reply": "请问一家三口是几位成人、几位儿童？",
                "source": "不同身份票价需分别确认",
                "decision": "allow", "kind": "audience_price",
                "query_context_update": {"price_filters": {
                    "awaiting": "audience_mix", "intent": "price" if price_intent else "availability",
                    "updated_at": datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
                }},
            }
        if not slots.get("day_type") and self._has_explicit_day_options(options):
            slots["day_type"] = self._requested_day_type(text, default_today=True)
        relevant_meals = {
            meal
            for option in options
            if any(
                self._option_supports_audience(option, kind)
                for kind in counts
            )
            for meal in (option.get("meal_periods") or [])
            if meal != "any"
        }
        if not slots.get("meal_period") and len(relevant_meals) > 1:
            meal_labels = {
                "breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐",
                "afternoon_tea": "下午茶",
            }
            choices = "、".join(
                meal_labels[meal]
                for meal in ("breakfast", "lunch", "afternoon_tea", "dinner")
                if meal in relevant_meals
            )
            return {
                "reply": f"不同用餐时段价格不同，请问选择{choices}中的哪一个时段？",
                "source": "自助餐存在多个时段票价",
                "decision": "allow", "kind": "audience_price",
                "query_context_update": make_audience_context("meal_period"),
            }
        has_weekend = any("weekend" in (item.get("day_types") or []) for item in options)
        has_holiday = any("holiday" in (item.get("day_types") or []) for item in options)
        holiday_covers = slots.get("day_type") == "weekend" and has_holiday and not has_weekend
        options = [item for item in options if self._option_matches_time(
            item, slots.get("day_type", ""), slots.get("meal_period", ""), holiday_covers
        )]
        labels = {"adult": "成人", "child": "儿童", "student": "学生", "senior": "老人", "female": "女士"}
        lines, missing = [], []
        total = Decimal("0")
        covered_kinds = set()

        # Prefer one exact existing package (for example “2大1小套餐”) over
        # adding unrelated individual ticket prices together.
        if "adult" in counts and "child" in counts:
            adults, children = counts["adult"], counts["child"]
            exact_pattern = re.compile(
                rf"{self._count_pattern(adults)}\s*(?:大|成人).*"
                rf"{self._count_pattern(children)}\s*(?:小|儿童|小孩)"
            )
            exact_packages = [
                option for option in options
                if option.get("option_type") == "package"
                and exact_pattern.search(str(option.get("name") or ""))
            ]
            if len(exact_packages) == 1:
                option = exact_packages[0]
                package_price = Decimal(str(option["sale_price"]))
                total += package_price
                lines.append(
                    f"{adults}位成人、{children}位儿童需要购买1份{option['name']}，"
                    f"共{self._format_number(package_price)}元"
                )
                covered_kinds.update({"adult", "child"})
        for kind, count in counts.items():
            if kind in covered_kinds:
                continue
            candidates = []
            for option in options:
                if not self._option_supports_audience(option, kind):
                    continue
                people = option.get("people_counts") or []
                exact = count in people
                single = 1 in people or not people
                if exact or single:
                    score = (
                        (2 if exact else 1)
                        + (2 if slots.get("meal_period") in (option.get("meal_periods") or []) else 0)
                        + (1 if slots.get("day_type") in (option.get("day_types") or []) else 0)
                    )
                    candidates.append((score, option, 1 if exact else count))
            if not candidates:
                missing.append(kind)
                continue
            if kind in {"child", "student", "senior"} and len(candidates) > 1:
                prices = {str(row[1].get("sale_price") or "") for row in candidates}
                if len(prices) > 1:
                    height_cm = self._height_cm(text) if kind == "child" else None
                    age_years = self._age_years(text)
                    if (
                        kind == "child"
                        and re.search(r"身高|\d+(?:\.\d+)?\s*(?:米|m|厘米|cm)", knowledge, re.I)
                        and height_cm is None
                    ):
                        return {"reply": "儿童票价格需要根据身高确认，请问儿童身高是多少？",
                                "source": "儿童票按身高分档", "decision": "allow", "kind": "audience_price"}
                    if re.search(r"年龄|周岁|\d+\s*岁", knowledge) and age_years is None:
                        return {"reply": f"{labels[kind]}票价格需要根据年龄确认，请问使用人的年龄是多少？",
                                "source": "身份票按年龄分档", "decision": "allow", "kind": "audience_price"}
                    constrained = [
                        row for row in candidates
                        if self._option_person_constraint_match(
                            row[1], height_cm=height_cm, age_years=age_years,
                        ) is True
                    ]
                    if len(constrained) == 1:
                        candidates = constrained
                    else:
                        condition = (
                            f"身高{self._format_number(height_cm)}厘米" if height_cm is not None
                            else f"{self._format_number(age_years)}岁" if age_years is not None
                            else "所述条件"
                        )
                        return {
                            "reply": (
                                f"当前有多个{labels[kind]}票价档，但无法把{condition}唯一对应到一个有货SKU。"
                                "请告诉我商品页面显示的完整规格名称，或到店咨询。"
                            ),
                            "source": "身份条件无法唯一对应当前有货SKU",
                            "decision": "allow", "kind": "audience_price",
                        }
            _, option, quantity = max(candidates, key=lambda row: (row[0], -Decimal(str(row[1]["sale_price"]))))
            subtotal = Decimal(str(option["sale_price"])) * quantity
            total += subtotal
            # Individual identity fares are bought as tickets even when the
            # upstream parser classified a named buffet ticket as a package.
            unit = "张"
            if quantity == 1:
                lines.append(
                    f"{count}位{labels[kind]}需要购买1{unit}{option['name']}，"
                    f"共{self._format_number(subtotal)}元"
                )
            else:
                lines.append(
                    f"{count}位{labels[kind]}需要购买{quantity}{unit}{option['name']}，"
                    f"单价{self._format_number(option['sale_price'])}元，共{self._format_number(subtotal)}元"
                )
            if kind != "adult":
                restriction = next((
                    part.strip(" 。；") for part in re.split(r"[\r\n。；]+", knowledge)
                    if any(word in part for word in {
                        "child": ("儿童", "小孩", "身高"),
                        "student": ("学生", "学生证"),
                        "senior": ("老人", "老年", "周岁"),
                        "female": ("女士", "女生", "女性"),
                    }.get(kind, ())) and re.search(r"身高|年龄|周岁|证|陪同|限购|限制", part)
                ), "")
                if restriction and restriction not in lines[-1]:
                    lines[-1] += f"（{restriction}）"
        missing_labels = {"child": "儿童票", "student": "学生票", "senior": "老人票", "female": "女士票", "adult": "成人票"}
        if lines and not missing:
            reply = "，".join(lines)
            if len(lines) > 1:
                reply += f"，合计{self._format_number(total)}元"
            condition_labels = []
            if slots.get("date_label"):
                condition_labels.append(str(slots["date_label"]))
            elif slots.get("day_type"):
                condition_labels.append({
                    "weekday": "工作日", "weekend": "周末", "holiday": "节假日",
                }.get(slots["day_type"], str(slots["day_type"])))
            if slots.get("meal_period"):
                condition_labels.append({
                    "breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐",
                    "afternoon_tea": "下午茶",
                }.get(slots["meal_period"], str(slots["meal_period"])))
            if condition_labels:
                reply = "".join(condition_labels) + "购买建议：" + reply
            return {
                "reply": reply + "。", "source": "当前日期、时段及不同身份真实票价",
                "decision": "allow", "kind": "audience_price",
                "query_context_update": make_audience_context(),
            }
        if lines and missing:
            names = "、".join(missing_labels[item] for item in missing)
            return {
                "reply": "，".join(lines) + "。\n\n" + (
                    f"当前商品暂时没有“{names}”这一规格，"
                    "无法通过本商品购买，相关价格可以到店咨询。"
                ),
                "source": "当前商品缺少对应的有货身份票种",
                "decision": "deny", "kind": "audience_price",
            }
        names = "、".join(missing_labels[item] for item in missing)
        return {
            "reply": (
                f"您好，本店目前没有“{names}”这一有货规格，"
                "暂时无法通过当前商品购买，您可以到店咨询。"
            ),
            "source": "当前商品缺少对应的有货身份票种",
            "decision": "deny", "kind": "audience_price",
        }

    @classmethod
    def _format_conditional_option(cls, option: Dict, slots: Dict) -> str:
        name = str(option.get("name") or "当前商品").strip()
        prefixes = []
        day = slots.get("day_type")
        meal = slots.get("meal_period")
        if slots.get("date_label"):
            prefixes.append(f"{slots['date_label']}可用的")
        elif day and not cls._day_types(name):
            prefixes.append({
                "weekday": "工作日可用的", "weekend": "周末可用的", "holiday": "节假日可用的",
            }.get(day, ""))
        if meal and not cls._meal_periods(name):
            prefixes.append({
                "breakfast": "早餐可用的", "lunch": "中午可用的", "dinner": "晚餐可用的",
                "afternoon_tea": "下午茶可用的",
            }.get(meal, ""))
        details = [f"售价{option['sale_price']}元"]
        if option.get("composition"):
            details.append(f"发{option['composition']}")
        return f"{''.join(prefixes)}{name}{'，'.join(details)}。"

    def conditional_sale_reply(
        self, product: Dict, message: str, query_context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Resolve amount, quantity, people, day and meal slots from real choices."""
        text = str(message or "").strip()
        price_intent = bool(re.search(
            r"多少钱|多钱|几多钱|什么价格|价格多少|价钱|售价|什么价|啥价|怎么卖|"
            r"怎么收|怎么买|怎么拍|怎么凑|几块(?:钱)?|几元|多少米|收费|票价|"
            r"一共多少|合计多少|要付多少|应付多少|多少一个|多少一份|代多少|"
            r"\d+(?:\.\d+)?\s*(?:元|块)?\s*(?:的)?\s*多少",
            text,
        ))
        availability_intent = bool(re.search(
            r"有吗|有没有|有无|有么|有嘛|有哪些|有什么|还有吗|有货吗|"
            r"能拍吗|可以拍吗|能买到吗|能不能买|卖不卖|"
            r"能不能(?:使用|用)|可不可以(?:使用|用)|是否可用|"
            r"可以(?:使用|用)|能(?:使用|用)|可用|"
            r"(?:中午|午餐|午市|晚上|晚餐|晚市|下午茶).{0,4}(?:就餐|吃|用餐)",
            text,
        ))
        previous = dict((query_context or {}).get("price_filters") or {})
        previous_time = str(previous.get("updated_at") or "")
        if previous_time:
            try:
                parsed_time = datetime.fromisoformat(previous_time)
                if parsed_time.tzinfo is None:
                    parsed_time = parsed_time.replace(tzinfo=CHINA_TZ)
                if (datetime.now(CHINA_TZ) - parsed_time).total_seconds() > 600:
                    previous = {}
            except (TypeError, ValueError):
                previous = {}

        direct_slots = self._conditional_query_slots(text)
        if not direct_slots.get("day_type"):
            direct_slots["day_type"] = self._requested_day_type(text)
        # Buyers often send only a compact condition bundle, for example
        # “三人明天中午”. A person count plus an explicit date/day/meal period
        # is a complete local price request even when “多少钱/能用吗” is omitted.
        condition_package_options = [
            option for option in self.extract_sale_options(product)
            if option.get("option_type") == "package" and option.get("sale_price")
        ]
        _, package_tier_intent = self._requested_package_tier_options(
            condition_package_options, text,
        )
        condition_people_counts = {
            int(count)
            for option in condition_package_options
            for count in (option.get("people_counts") or [])
            if count
        }
        condition_only_package_query = bool(
            direct_slots.get("meal_period")
            and (direct_slots.get("day_type") or direct_slots.get("date_label"))
            and len(condition_people_counts) > 1
        )
        implicit_condition_price = bool(
            (
                direct_slots.get("people_count")
                and (
                    direct_slots.get("day_type") or direct_slots.get("meal_period")
                    or package_tier_intent
                )
            )
            or condition_only_package_query
        )
        if (
            availability_intent
            and any(word.lower() in text.lower() for word in STORE_LANDMARK_WORDS)
            and re.search(
                r"(?:单人|双人|三人|四人|五人|六人|\d+\s*人)"
                r"[^，,。；;]{0,12}(?:套餐|券|自助)",
                text,
            )
            and not direct_slots.get("day_type")
            and not direct_slots.get("meal_period")
        ):
            # This is a named option + store applicability question.  The SKU
            # aware store resolver must decide it; answering only “the option
            # exists” would silently drop the store half of the question.
            return None
        awaiting = str(previous.get("awaiting") or "")
        pending_filled = False
        bare_value = re.fullmatch(
            r"\s*(?:我们|我这边|一共|总共|就)?\s*"
            r"([一二两三四五六七八九十单双俩仨\d]+)\s*"
            r"(?:个)?(?:人|位|张|份|条|只|套)?\s*(?:呢)?[？?。！!]*\s*",
            text,
        )
        if awaiting and bare_value:
            raw_value = bare_value.group(1)
            if awaiting == "people_count":
                count = self._chinese_count(raw_value)
                if count and 0 < count <= 20:
                    direct_slots["people_count"] = count
                    pending_filled = True
            elif awaiting == "purchase_quantity":
                count = self._chinese_count(raw_value)
                if count and 0 < count <= 99:
                    direct_slots["purchase_quantity"] = count
                    direct_slots["purchase_unit"] = str(previous.get("purchase_unit") or "份")
                    pending_filled = True
            elif awaiting == "target_amount" and raw_value.isdigit():
                direct_slots["target_amount"] = self._format_number(raw_value)
                direct_slots["amount_kind"] = "option"
                pending_filled = True

        slot_keys = (
            "people_count", "day_type", "meal_period", "target_amount",
            "purchase_quantity", "purchase_unit", "amount_kind", "date_label",
        )
        has_direct_slot = any(direct_slots.get(key) not in (None, "") for key in slot_keys)
        referential = bool(re.search(r"(?:呢|那|这个|这种|这款)[？?。！!]*$", text))
        context_followup = bool(previous and (has_direct_slot or pending_filled) and len(normalize_text(text)) <= 16)
        tier_followup = bool(previous and package_tier_intent and len(normalize_text(text)) <= 16)
        if not (
            price_intent or availability_intent or implicit_condition_price or pending_filled
            or bool(direct_slots.get("purchase_quantity"))
            or ((referential or context_followup or tier_followup) and previous)
        ) or not (has_direct_slot or previous):
            return None

        only_new_amount = bool(
            previous and direct_slots.get("target_amount")
            and not any(direct_slots.get(key) for key in (
                "people_count", "day_type", "meal_period", "purchase_quantity",
            ))
            and len(normalize_text(text)) <= 12
        )
        inherited = (
            ((referential or context_followup or tier_followup) and not (price_intent or availability_intent))
            or only_new_amount or pending_filled
        )
        slots = {key: previous.get(key) for key in slot_keys} if inherited else {}
        slots.update({key: value for key, value in direct_slots.items() if value not in (None, "")})
        # “3张多少钱/3张呢” replaces the previous amount target.  Keeping a
        # preceding “300多少钱” here used to combine both turns and multiply an
        # already bundled 100x2 SKU three more times.
        if direct_slots.get("purchase_quantity") and not direct_slots.get("target_amount"):
            slots.pop("target_amount", None)
            slots.pop("amount_kind", None)
        # A newly stated audience such as “晚市双人” replaces a previous
        # purchase quantity.  Otherwise “3张” from the last question can leak
        # into the new two-person option and multiply the price incorrectly.
        explicit_audience = bool(re.search(
            r"单人|双人|三人|四人|五人|六人|\d+\s*(?:人|位)|"
            r"[一二两三四五六七八九十]+\s*(?:人|位)", text,
        ))
        explicit_purchase_quantity = bool(re.search(
            r"(?:买|要|拍|来|需要)?\s*(?:\d+|[一二两三四五六七八九十]+)\s*(?:张|份|套|条|只)",
            text,
        ))
        if explicit_audience and not explicit_purchase_quantity:
            slots.pop("purchase_quantity", None)
            slots.pop("purchase_unit", None)
        if not any(slots.get(key) not in (None, "") for key in slot_keys):
            return None
        target_date = self._query_date(text)
        if target_date and self._date_is_explicitly_unavailable(
            self._product_rule_knowledge(product), target_date,
        ):
            label = str(slots.get("date_label") or f"{target_date.month}月{target_date.day}日")
            return {
                "reply": f"{label}在商品标注的不可用日期范围内，当天不能使用哦。",
                "source": "当前商品明确不可用日期",
                "decision": "deny", "kind": "date_use",
            }
        options = [option for option in self.extract_sale_options(product) if option.get("sale_price")]
        requested_audiences = set(direct_slots.get("audience_types") or [])
        if not requested_audiences:
            # Child/senior/student fares must never become the default adult
            # answer merely because they are the only option left after date filtering.
            options = [
                option for option in options
                if self._option_supports_audience(option, "adult")
            ]
        else:
            options = [
                option for option in options
                if any(self._option_supports_audience(option, kind) for kind in requested_audiences)
            ]

        # A named menu tier is a hard SKU filter. Generic extraction covers
        # 轻享/尊享/经典/etc.; the helper also keeps the M8-9 alias compatible.
        options, _tier_scoped = self._requested_package_tier_options(options, text)
        if not slots.get("day_type") and self._has_explicit_day_options(options):
            # A buyer asking the current price/availability without naming a
            # date means today. Never fall back to the first or cheapest tier.
            slots["day_type"] = self._requested_day_type(text, default_today=True)
        has_weekend_options = any("weekend" in (option.get("day_types") or []) for option in options)
        has_holiday_options = any("holiday" in (option.get("day_types") or []) for option in options)
        holiday_covers_weekend = bool(
            slots.get("day_type") == "weekend" and has_holiday_options and not has_weekend_options
        )
        effective_intent = (
            "price" if price_intent
            else "availability" if availability_intent
            else "price" if implicit_condition_price
            else str(previous.get("intent") or "price")
        )

        def make_context(next_slot: str = "") -> Dict:
            return {"price_filters": {
                **{key: slots.get(key) for key in slot_keys if slots.get(key) not in (None, "")},
                "intent": effective_intent,
                "awaiting": next_slot,
                "updated_at": datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
            }}

        context_update = make_context()
        matched = []
        for option in options:
            people = option.get("people_counts") or []
            if slots.get("people_count") and slots["people_count"] not in people:
                continue
            if not self._option_matches_time(
                option, slots.get("day_type", ""), slots.get("meal_period", ""),
                holiday_covers_weekend,
            ):
                continue
            matched.append(option)

        if (
            effective_intent == "availability"
            and matched
            and (slots.get("date_label") or slots.get("day_type") or slots.get("meal_period"))
            and any(
                (not (option.get("day_types") or []) or "any" in (option.get("day_types") or []))
                and (not (option.get("meal_periods") or []) or "any" in (option.get("meal_periods") or []))
                for option in matched
            )
        ):
            return {
                "reply": "门店营业时段可用就可以了。",
                "source": "当前商品未配置额外日期或餐段限制",
                "decision": "allow", "kind": "time",
                "query_context_update": context_update,
            }

        # A condition-specific price overrides a broader “all week/all day”
        # option. Keep the general option only when no specific one exists.
        if slots.get("day_type"):
            specific = [
                option for option in matched
                if slots["day_type"] in (option.get("day_types") or [])
                or (holiday_covers_weekend and "holiday" in (option.get("day_types") or []))
            ]
            if specific:
                matched = specific
        if slots.get("meal_period"):
            specific = [
                option for option in matched
                if slots["meal_period"] in (option.get("meal_periods") or [])
            ]
            if specific:
                matched = specific

        target_amount = slots.get("target_amount")
        purchase_quantity = slots.get("purchase_quantity")
        purchase_unit = str(slots.get("purchase_unit") or "份")
        voucher_matches = [
            option for option in matched
            if option.get("option_type") == "voucher" and option.get("face_value")
        ]
        package_matches = [option for option in matched if option.get("option_type") == "package"]

        # Prefer an exact people-count package. If none exists, combine compatible
        # one-/multi-person packages from the same product family. This covers
        # natural buffet questions such as “明晚三人多少钱” with one double ticket
        # plus one single ticket, while keeping unrelated buffet tiers isolated.
        # Identity fares and explicit one-item purchase limits are excluded.
        people_count = slots.get("people_count")
        if (
            people_count and int(people_count) > 1 and not matched
            and effective_intent == "price"
        ):
            time_compatible = [
                option for option in options
                if self._option_matches_time(
                    option, slots.get("day_type", ""), slots.get("meal_period", ""),
                    holiday_covers_weekend,
                )
            ]
            package_options = []
            for option in time_compatible:
                evidence = " ".join(str(option.get(key) or "") for key in ("name", "applicable_time"))
                people_values = sorted(set(option.get("people_counts") or []))
                if (
                    option.get("option_type") == "package"
                    and len(people_values) == 1
                    and 0 < people_values[0] <= int(people_count)
                    and not (option.get("audience_types") or [])
                    and not re.search(r"(?:每单|每人|限购|仅限购买|最多购买)\s*1\s*(?:份|张|套|个)", evidence)
                ):
                    package_options.append(option)

            def package_family(option: Dict) -> str:
                name = normalize_text(option.get("name") or "")
                return re.sub(
                    r"全周(?:通用)?|每天|每日|工作日|平日|周末|节假日|法定假日|"
                    r"早餐|早市|午餐|午市|晚餐|晚市|下午茶|全天|全时段|"
                    r"单人|双人|三人|四人|五人|六人|七人|八人|九人|十人|\d+人",
                    "", name,
                ) or name

            plans = []
            for family in dict.fromkeys(package_family(item) for item in package_options):
                family_options = [
                    item for item in package_options if package_family(item) == family
                ]
                by_capacity = {}
                for option in family_options:
                    capacity = int((option.get("people_counts") or [0])[0])
                    by_capacity.setdefault(capacity, []).append(option)
                choices = []
                for capacity, rows in by_capacity.items():
                    preferred = rows
                    if slots.get("day_type"):
                        specific = [
                            item for item in preferred
                            if slots["day_type"] in (item.get("day_types") or [])
                            or (holiday_covers_weekend and "holiday" in (item.get("day_types") or []))
                        ]
                        if specific:
                            preferred = specific
                    if slots.get("meal_period"):
                        specific = [
                            item for item in preferred
                            if slots["meal_period"] in (item.get("meal_periods") or [])
                        ]
                        if specific:
                            preferred = specific
                    option = min(preferred, key=lambda item: Decimal(str(item.get("sale_price"))))
                    choices.append((capacity, option))

                best = {0: (Decimal("0"), 0, [])}
                for covered in range(1, int(people_count) + 1):
                    candidates = []
                    for capacity, option in choices:
                        previous_plan = best.get(covered - capacity)
                        if not previous_plan:
                            continue
                        candidates.append((
                            previous_plan[0] + Decimal(str(option.get("sale_price"))),
                            previous_plan[1] + 1,
                            previous_plan[2] + [(capacity, option)],
                        ))
                    if candidates:
                        best[covered] = min(candidates, key=lambda item: (item[0], item[1]))
                if int(people_count) in best:
                    plans.append(best[int(people_count)])

            if plans:
                total, item_count, selected = min(plans, key=lambda item: (item[0], item[1]))
                grouped = []
                for capacity, option in selected:
                    key = self.sku_key_for_option(option)
                    existing = next((row for row in grouped if row[0] == key), None)
                    if existing:
                        existing[3] += 1
                    else:
                        grouped.append([key, option, capacity, 1])
                if len(grouped) == 1 and grouped[0][2] == 1:
                    _, option, _, quantity = grouped[0]
                    name = str(option.get("name") or "单人商品").strip()
                    reply = (
                        f"{people_count}人需要购买{quantity}份{name}，"
                        f"单价{self._format_number(option.get('sale_price'))}元，"
                        f"共{self._format_number(total)}元。"
                    )
                    selected_context = {
                        "selected_sku_key": self.sku_key_for_option(option),
                        "selected_sku_name": name,
                    }
                else:
                    parts = []
                    for _, option, capacity, quantity in grouped:
                        subtotal = Decimal(str(option.get("sale_price"))) * quantity
                        name = str(option.get("name") or "当前商品").strip()
                        if quantity == 1:
                            parts.append(
                                f"购买1份{name}（适用{capacity}人），{self._format_number(subtotal)}元"
                            )
                        else:
                            parts.append(
                                f"购买{quantity}份{name}（每份适用{capacity}人），"
                                f"单价{self._format_number(option.get('sale_price'))}元，"
                                f"小计{self._format_number(subtotal)}元"
                            )
                    subject = self._conditional_subject({
                        "date_label": slots.get("date_label", ""),
                        "day_type": slots.get("day_type", ""),
                        "meal_period": slots.get("meal_period", ""),
                        "people_count": people_count,
                    })
                    reply = f"{subject}购买建议：" + "；".join(parts) + f"，合计{self._format_number(total)}元。"
                    selected_context = {}
                return {
                    "reply": reply,
                    "source": "当前日期和餐段可用的真实套餐组合",
                    "decision": "allow", "kind": "price",
                    "query_context_update": {**context_update, **selected_context},
                }

        # “一条鱼/单条/整条” is an ordinary package description, not a literal
        # option name. Narrow to fish packages when possible.
        if re.search(r"一整条|整条|单条|一条鱼|单鱼|烤鱼", text):
            fish_matches = [
                option for option in package_matches
                if re.search(r"鱼|烤鱼|回鱼", str(option.get("name") or ""))
            ]
            if fish_matches:
                package_matches = fish_matches

        if purchase_quantity:
            quantity = int(purchase_quantity)
            candidates = voucher_matches if purchase_unit == "张" and voucher_matches else package_matches
            if not candidates:
                candidates = voucher_matches or package_matches
            if candidates and package_matches and candidates is package_matches:
                # “两张” is a purchase count, not a two-person SKU request.  If
                # the listing title identifies the advertised ticket size, keep
                # that real option and multiply it instead of asking the buyer to
                # choose between unrelated single/double/triple packages.
                title_people = self._people_counts(product.get("title") or "")
                if len(title_people) == 1:
                    titled = [
                        option for option in candidates
                        if title_people[0] in (option.get("people_counts") or [])
                    ]
                    if len(titled) == 1:
                        candidates = titled
            if voucher_matches and candidates is voucher_matches:
                faces = sorted({
                    self._format_number(amount)
                    for option in candidates
                    for amount, _ in self._option_coupon_pairs(option)
                    if amount > 0
                }, key=lambda value: Decimal(value))
                if target_amount:
                    exact = [
                        option for option in candidates
                        if (
                            self._format_number(target_amount) == self._format_number(
                                option.get("face_value") or ""
                            )
                            or self._format_number(target_amount) in {
                                self._format_number(amount)
                                for amount, _ in self._option_coupon_pairs(option)
                            }
                        )
                    ]
                    if not exact:
                        catalog = "、".join(f"{face}元" for face in faces)
                        return {
                            "reply": (
                                f"当前商品没有{self._format_number(target_amount)}元代金券。"
                                + (f"现有面额为{catalog}，请从中选择。" if catalog else "")
                            ),
                            "source": "当前商品真实代金券与购买数量",
                            "decision": "deny", "kind": "price",
                            "query_context_update": make_context("target_amount"),
                        }
                    candidates = exact
                elif len(faces) > 1:
                    return {
                        "reply": f"您想购买哪种面额？当前可选：{'、'.join(face + '元' for face in faces)}。",
                        "source": "当前商品真实代金券与购买数量",
                        "decision": "allow", "kind": "price",
                        "query_context_update": make_context("target_amount"),
                    }
                if not slots.get("day_type"):
                    prices = {self._format_number(item.get("sale_price")) for item in candidates}
                    dated = any(item.get("day_types") for item in candidates)
                    if dated and len(prices) > 1:
                        return {
                            "reply": "这个面额在不同日期价格不同，请问是工作日、周末还是节假日使用？",
                            "source": "同一代金券存在不同日期价格",
                            "decision": "allow", "kind": "price",
                            "query_context_update": make_context("day_type"),
                        }
                # An explicit “2张200券” names the 200-yuan sellable SKU.  If
                # that SKU itself contains two 100-yuan coupons, “2张” means
                # two SKU packages.  A bare “3张多少钱”, by contrast, means
                # three actually delivered coupons and uses the planner below.
                package_face_matches = []
                if target_amount:
                    for option in candidates:
                        pairs = self._option_coupon_pairs(option)
                        unit_faces = {
                            self._format_number(amount) for amount, _ in pairs if amount > 0
                        }
                        if (
                            self._format_number(option.get("face_value") or "")
                            == self._format_number(target_amount)
                            and self._format_number(target_amount) not in unit_faces
                        ):
                            package_face_matches.append(option)
                if package_face_matches:
                    option = min(
                        package_face_matches,
                        key=lambda item: Decimal(str(item.get("sale_price") or "0")),
                    )
                    price = Decimal(str(option.get("sale_price") or "0")) * quantity
                    delivered_per_unit = sum(
                        count for _, count in self._option_coupon_pairs(option)
                    ) or 1
                    delivered_total = delivered_per_unit * quantity
                    option_value = self._option_total_value(option)
                    contents = self._purchase_contents_label(option, quantity)
                    try:
                        maximum = int(Decimal(str(option.get("max_stack") or "0")))
                    except (InvalidOperation, ValueError):
                        maximum = 0
                    reply = f"购买{contents}共{self._format_number(price)}元。"
                    if maximum and delivered_total > maximum:
                        reply += (
                            f"每份该规格可抵扣{option_value}元；"
                            f"当前同面额代金券每次最多使用{maximum}张，"
                            f"本次购买所得的{delivered_total}张不能在同一次消费中全部使用。"
                        )
                    else:
                        reply += f"共可抵扣{self._format_number(Decimal(option_value) * quantity)}元。"
                    return {
                        "reply": reply, "source": "当前商品真实SKU及其发券组成",
                        "decision": "allow", "kind": "price",
                        "query_context_update": {
                            **context_update,
                            "selected_sku_key": self.sku_key_for_option(option),
                            "selected_sku_name": str(option.get("name") or "商品规格").strip(),
                        },
                    }
                plan = self._coupon_quantity_purchase_plan(
                    product, candidates, quantity,
                    requested_face=str(target_amount or ""),
                )
                if plan.get("ambiguous_faces"):
                    labels = "、".join(f"{face}元" for face in plan["ambiguous_faces"])
                    return {
                        "reply": f"您想购买哪种面额？当前可选：{labels}。",
                        "source": "当前商品真实代金券与购买数量",
                        "decision": "allow", "kind": "price",
                        "query_context_update": make_context("target_amount"),
                    }
                if not plan.get("reply"):
                    bundle_labels = []
                    for option in candidates:
                        delivered = sum(count for _, count in self._option_coupon_pairs(option))
                        name = str(option.get("name") or "当前规格").strip()
                        bundle_labels.append(f"{name}每份发{delivered}张")
                    return {
                        "reply": (
                            f"当前在售规格无法刚好发{quantity}张。"
                            + ("可选规格为：" + "；".join(dict.fromkeys(bundle_labels)) + "。" if bundle_labels else "")
                        ),
                        "source": "当前商品真实SKU发券组成",
                        "decision": "deny", "kind": "price",
                        "query_context_update": context_update,
                    }
                reply = str(plan["reply"])
                selected = list(plan.get("selected") or [])
                selected_context = {}
                if len(selected) == 1:
                    option = selected[0][0]
                    selected_context = {
                        "selected_sku_key": self.sku_key_for_option(option),
                        "selected_sku_name": str(option.get("name") or "商品规格").strip(),
                    }
                return {
                    "reply": reply, "source": "当前商品真实代金券与购买数量",
                    "decision": "allow", "kind": "price",
                    "query_context_update": {**context_update, **selected_context},
                }

            if candidates:
                people_values = sorted({
                    count for item in candidates for count in (item.get("people_counts") or [])
                })
                if len(candidates) > 1 and not slots.get("people_count") and len(people_values) > 1:
                    return {
                        "reply": "可选套餐有多个，请问您要单人券、双人券还是其他人数的券？",
                        "source": "当前商品真实套餐与购买数量",
                        "decision": "allow", "kind": "price",
                        "query_context_update": make_context("people_count"),
                    }
                if len(candidates) > 1:
                    return {
                        "reply": "可选商品有多个，请告诉我想购买的套餐名称或用餐时段。",
                        "source": "当前商品真实套餐与购买数量",
                        "decision": "allow", "kind": "price",
                        "query_context_update": context_update,
                    }
                option = candidates[0]
                total = Decimal(str(option.get("sale_price"))) * quantity
                name = str(option.get("name") or "当前套餐").strip()
                if quantity == 1:
                    reply = f"{name}售价{self._format_number(total)}元。"
                else:
                    reply = f"购买{quantity}{purchase_unit}{name}共{self._format_number(total)}元。"
                exclusion = re.search(r"不含[^。；\n）)]{1,30}", str(product.get("raw_text") or ""))
                if exclusion and exclusion.group(0) not in reply:
                    reply = reply.rstrip("。") + f"，{exclusion.group(0)}。"
                return {
                    "reply": reply, "source": "当前商品真实套餐与购买数量",
                    "decision": "allow", "kind": "price",
                    "query_context_update": context_update,
                }

        if target_amount and voucher_matches:
            exact_matches = [
                option for option in voucher_matches
                if self._option_total_value(option) == self._format_number(target_amount)
            ]
            calculation_options = exact_matches or voucher_matches
            if not slots.get("day_type"):
                price_by_face = {}
                for option in calculation_options:
                    if not option.get("day_types"):
                        continue
                    price_by_face.setdefault(
                        self._format_number(option.get("face_value")), set()
                    ).add(self._format_number(option.get("sale_price")))
                if any(len(prices) > 1 for prices in price_by_face.values()):
                    return {
                        "reply": "这个金额在不同日期适用的价格不同，请问是工作日、周末还是节假日使用？",
                        "source": "同一代金券存在不同日期价格",
                        "decision": "allow",
                        "kind": "price",
                        "query_context_update": make_context("day_type"),
                    }
            if exact_matches and slots.get("amount_kind") != "consumption":
                brand = self.extract_brand(product)
                return {
                    "reply": "\n".join(
                        self.format_product_option(option, brand) for option in exact_matches
                    ),
                    "source": "当前日期条件下的真实代金券价格",
                    "decision": "allow", "kind": "price",
                    "query_context_update": context_update,
                }
            reply = self.consumption_plan_reply(
                product, target_amount, missing_denomination=not bool(exact_matches),
                available_options=calculation_options,
            )
            if reply:
                condition = self._conditional_subject({
                    "day_type": slots.get("day_type", ""),
                    "meal_period": slots.get("meal_period", ""),
                    "people_count": slots.get("people_count"),
                })
                if condition != "所问条件":
                    reply = f"{condition}使用时，{reply}"
                return {
                    "reply": reply,
                    "source": "当前日期条件下的真实代金券与使用张数",
                    "decision": "allow",
                    "kind": "price",
                    "query_context_update": context_update,
                }

        if target_amount and package_matches and not voucher_matches:
            target = self._format_number(target_amount)
            exact_packages = [
                option for option in package_matches
                if re.search(
                    rf"(?<![\d.]){re.escape(target)}(?:\.0+)?\s*(?:元|人|位|份|套)",
                    str(option.get("name") or ""),
                )
            ]
            if exact_packages:
                if len(exact_packages) == 1:
                    reply = self._format_conditional_option(exact_packages[0], slots)
                else:
                    reply = "符合条件的商品如下：\n" + "\n".join(
                        self._format_conditional_option(option, slots) for option in exact_packages
                    )
            elif slots.get("amount_kind") == "consumption":
                reply = (
                    f"当前商品按套餐售卖，暂时无法按{target}元消费金额自动组合。"
                    "请告诉我想要的套餐名称、用餐人数或用餐时段。"
                )
            else:
                reply = (
                    f"当前商品没有{target}元这一商品选项。"
                    f"您是想问{target}元消费怎么买，还是想咨询现有套餐？"
                )
            return {
                "reply": reply, "source": "当前商品真实套餐选项",
                "decision": "allow" if exact_packages else "deny", "kind": "price",
                "query_context_update": context_update,
            }

        all_people_values = sorted({
            count for item in package_matches for count in (item.get("people_counts") or [])
        })
        if not slots.get("people_count") and len(all_people_values) > 1:
            period = self._conditional_subject({
                "day_type": slots.get("day_type", ""), "meal_period": slots.get("meal_period", ""),
            })
            return {
                "reply": f"{period}可选的商品有多个，请问您是几个人用餐？",
                "source": "当前商品真实选项的条件匹配",
                "decision": "allow",
                "kind": "price" if effective_intent == "price" else "sku_availability",
                "query_context_update": make_context("people_count"),
            }

        subject = self._conditional_subject(slots)
        if not matched:
            relevant_options = options
            complete = True
            if slots.get("people_count"):
                complete = complete and bool(relevant_options) and all(item.get("people_counts") for item in relevant_options)
            if slots.get("day_type"):
                complete = complete and bool(relevant_options) and all(item.get("day_types") for item in relevant_options)
            if slots.get("meal_period"):
                complete = complete and bool(relevant_options) and all(item.get("meal_periods") for item in relevant_options)
            if effective_intent == "availability":
                # The original sentence may also contain a branch and words
                # such as “还有吗”.  It is not a SKU name.  Report only the
                # structured conditions that failed to match.
                named_option_question = bool(re.search(
                    r"套餐|自助|代金券|优惠券|抵扣券|套餐券", text,
                )) and not bool(re.search(
                    r"门店|总店|旗舰店|分店|[\u4e00-\u9fffA-Za-z0-9]{2,24}店", text,
                ))
                if named_option_question:
                    named_subject = re.sub(
                        r"^(?:请问)?(?:有没有|有无)\s*|"
                        r"\s*(?:还有没有|还有吗|还有么|还有嘛|有吗|有没有|有么|有嘛)[？?]?$",
                        "", text,
                    ).strip(" ，,。.!！?？~～")
                    reply = f"当前商品没有“{named_subject or subject}”这一规格。"
                else:
                    reply = f"当前商品没有符合“{subject}”的商品选项。"
            elif complete:
                reply = f"当前商品没有符合“{subject}”的商品选项。"
            elif effective_intent == "availability":
                reply = f"当前商品资料暂未明确“{subject}”是否有对应商品或是否适用，暂时无法准确确认。"
            else:
                reply = f"当前商品资料暂未明确“{subject}”对应的商品和价格，暂时无法准确报价。"
            return {
                "reply": reply,
                "source": "当前商品真实选项的条件匹配",
                "decision": "deny" if complete else "allow",
                "kind": "price" if effective_intent == "price" else "sku_availability",
                "query_context_update": context_update,
            }

        people_values = sorted({count for item in matched for count in (item.get("people_counts") or [])})
        day_values = {day for item in matched for day in (item.get("day_types") or []) if day != "any"}
        if not slots.get("people_count") and len(people_values) > 1:
            period = self._conditional_subject({
                "day_type": slots.get("day_type", ""), "meal_period": slots.get("meal_period", ""),
            })
            reply = f"{period}可选的商品有多个，请问您是几个人用餐？"
        elif slots.get("people_count") and not slots.get("day_type") and len(day_values) > 1:
            reply = f"{slots['people_count']}人可选的商品在不同日期价格不同，请问是工作日、周末还是节假日使用？"
            context_update = make_context("day_type")
        elif len(matched) == 1:
            reply = self._format_conditional_option(matched[0], slots)
            context_update = {
                **context_update,
                "selected_sku_key": self.sku_key_for_option(matched[0]),
                "selected_sku_name": str(matched[0].get("name") or "商品规格").strip(),
            }
        else:
            reply = "符合条件的商品如下：\n" + "\n".join(
                self._format_conditional_option(option, slots) for option in matched
            )
        return {
            "reply": reply,
            "source": "当前商品真实选项的条件匹配",
            "decision": "allow",
            "kind": "price" if effective_intent == "price" else "sku_availability",
            "query_context_update": context_update,
        }

    @classmethod
    def extract_brand(cls, product: Dict) -> str:
        facts = (product.get("structured") or {}).get("facts") or {}
        brand = cls._first_fact(facts, ("品牌", "品牌名称", "商家", "餐厅", "brand"))
        if brand:
            return re.sub(r"(?:品牌)$", "", brand).strip()
        title = re.sub(
            r"【[^】]*】|\[[^\]]*\]", "", str(product.get("title") or "")
        ).strip()
        title = re.sub(
            r"^(?:自动发货|全国|现货|秒发)[\s·|丨+\-]*", "", title, flags=re.I
        ).strip()
        # Marketplace titles often repeat the merchant before and after “+”,
        # e.g. “鱼酷烤鱼2-3人套餐+鱼酷 活鱼烤鱼”.  The repeated Chinese prefix is
        # a much safer brand boundary than consuming “烤鱼2-3人” into the brand.
        title_parts = [part.strip() for part in re.split(r"[+丨|]", title) if part.strip()]
        if len(title_parts) >= 2:
            first = title_parts[0]
            for part in title_parts[1:]:
                common = []
                for left, right in zip(first, part):
                    if left != right or not ("\u4e00" <= left <= "\u9fff"):
                        break
                    common.append(left)
                repeated = "".join(common).strip()
                if 2 <= len(repeated) <= 8:
                    return repeated
        match = re.match(
            r"(.{2,24}?)(?:\d+(?:[\/、]\d+)+|\d+(?:\.\d+)?(?:代|元)|代金券|电子券|团购券|套餐)",
            title,
        )
        brand = match.group(1).strip(" ·|丨+-") if match else ""
        brand = re.sub(r"(?:西餐厅|餐厅|品牌)$", "", brand).strip()
        return brand

    @classmethod
    def format_product_option(cls, option: Dict, brand: str = "", show_time: bool = False) -> str:
        name = option.get("name") or (
            f"{option.get('face_value')}元代金券" if option.get("face_value") else "商品规格"
        )
        if brand and normalize_text(brand) not in normalize_text(name):
            name = f"{brand}{name}"
        details = []
        if show_time:
            day_types = cls._option_day_types(option)
            labels = [
                label for key, label in (
                    ("weekday", "工作日可用"), ("weekend", "周末可用"),
                    ("holiday", "法定节假日可用"), ("any", "每日可用"),
                ) if key in day_types
            ]
            time_label = "/".join(labels) or str(option.get("applicable_time") or "").strip()
            if time_label:
                name = f"{name}（{time_label}）"
        if option.get("sale_price"):
            details.append(f"售价{option['sale_price']}元")
        if option.get("composition"):
            details.append(f"发{option['composition']}")
        return f"{name}：{'，'.join(details)}" if details else name

    @classmethod
    def coupon_type_display(cls, product: Dict) -> str:
        coupon_type = str(product.get("coupon_type") or "").strip()
        if coupon_type == "other":
            return str(product.get("coupon_type_custom") or "其他卡券").strip()
        return COUPON_TYPE_LABELS.get(coupon_type, "")

    @staticmethod
    def promotion_purchase_intent(message: str) -> bool:
        """Recognize common ways a buyer asks where/how/how much to buy."""
        text = str(message or "").strip()
        compact = re.sub(r"[\s，,。.!！?？~～]+", "", text).lower()
        if not compact:
            return False
        if compact in {
            "多少钱", "多钱", "多少", "价格", "价格呢", "什么价", "怎么卖",
            "怎么买", "如何购买", "哪里买", "在哪买", "怎么下单", "如何下单",
            "购买链接", "下单链接", "链接", "二维码", "购买入口", "下单入口",
            "微信怎么买", "怎么付款", "如何付款", "付款方式", "我要买", "想买",
            "能拍吗", "可以拍吗", "现在能买吗", "现在能拍吗", "能下单吗", "可以下单吗",
            "有货吗", "还有吗", "还有货吗", "拍哪个", "买哪个", "选哪个",
        }:
            return True
        return bool(re.search(
            r"(?:多少钱|多钱|什么价|价格多少|售价|几块钱|几元)|"
            r"(?:怎么|如何|哪里|在哪|从哪)(?:买|购买|拍|下单|付款)|"
            r"(?:我要|我想|想|想要|准备|现在|可以|能|能不能|能否)(?:买|购买|拍|下单)|"
            r"(?:买|购买|下单|付款)(?:入口|地址|链接|二维码|方式)|"
            r"(?:推广|购买|下单|微信)(?:链接|二维码|入口)|"
            r"(?:发|给|看|扫|打开|点)(?:一下|下)?(?:链接|二维码)|"
            r"(?:发|给)我(?:个|一下)?(?:链接|二维码)|"
            r"(?:链接|二维码).{0,5}(?:发我|给我|看看|看下)|"
            r"(?:链接|二维码)(?:呢|在哪|在哪里|有吗|怎么扫|扫不出来|失效|打不开|点不开)|"
            r"(?:扫哪|扫哪里|扫哪个|去哪扫|哪里扫|在哪扫|用微信扫)|"
            r"(?:拍|买|选)(?:哪一个|哪个|哪款|什么规格)|"
            r"(?:闲鱼|这里|页面)(?:能|可以|直接)?(?:买|拍|下单|付款)|"
            r"(?:预算|消费|账单|抵扣|要抵|想抵)\s*\d+(?:\.\d+)?|"
            r"(?:要|来)\s*[一二两三四五六七八九十\d]+\s*(?:张|份|个)|"
            r"\d+(?:\.\d+)?\s*(?:元|块)?(?:的)?(?:多少钱|多钱|什么价|怎么买|怎么拍)",
            text,
            re.I,
        ))

    @staticmethod
    def promotion_links(product: Dict) -> List[str]:
        """Extract operator-supplied promotion links from authoritative raw knowledge."""
        source = str(product.get("raw_text") or "")
        link_pattern = re.compile(
            r"(?:https?://|weixin://|#小程序://|www\.)[^\s<>\"'，。；;、]+",
            re.I,
        )
        all_links = list(dict.fromkeys(match.rstrip(")）]】") for match in link_pattern.findall(source)))
        if not all_links:
            return []
        preferred = []
        for line in source.splitlines():
            if not re.search(r"推广|购买|下单|微信|链接|入口|扫码|二维码", line):
                continue
            preferred.extend(match.rstrip(")）]】") for match in link_pattern.findall(line))
        preferred = list(dict.fromkeys(preferred))
        return preferred or (all_links if len(all_links) == 1 else [])

    @classmethod
    def _mixed_coupon_delivery_rows(cls, product: Dict, message: str = "") -> List[str]:
        """Render SKU-level delivery facts without inventing one global channel."""
        structured = product.get("structured") or {}
        structured = structured if isinstance(structured, dict) else {}
        common = structured.get("common_rules") or {}
        common = common if isinstance(common, dict) else {}
        profiles = structured.get("sku_profiles") or []
        profiles = [dict(row) for row in profiles if isinstance(row, dict)]
        if not profiles:
            profiles = [
                {
                    "sku_key": cls.sku_key_for_option(option),
                    "sku_name": option.get("name") or "商品规格",
                    "face_value": option.get("face_value") or "",
                }
                for option in cls.extract_product_options(product)
            ]

        query = normalize_text(message).lower()
        exact_profiles = []
        if query:
            exact_profiles = [
                row for row in profiles
                if len(normalize_text(row.get("sku_name") or row.get("name") or "")) >= 2
                and normalize_text(row.get("sku_name") or row.get("name") or "").lower() in query
            ]
        requested_amounts = {
            cls._format_number(value)
            for value in re.findall(
                r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:元|块|面额|代金券|优惠券|券)",
                str(message or ""),
            )
            if cls._format_number(value)
        }
        selected = exact_profiles
        if not selected and requested_amounts:
            for row in profiles:
                face = cls._format_number(row.get("face_value") or "")
                name = str(row.get("sku_name") or row.get("name") or "")
                name_amounts = {
                    cls._format_number(value)
                    for value in re.findall(
                        r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:元|块|面额|代金券|优惠券|券)",
                        name,
                    )
                }
                if face in requested_amounts or requested_amounts & name_amounts:
                    selected.append(row)
        if not selected and not requested_amounts:
            platform_names = (
                "商家小程序", "美团", "抖音", "快手", "大众点评", "支付宝",
                "微信", "云闪付", "京东", "淘宝", "饿了么", "小程序",
            )
            mentioned_platforms = [
                value for value in platform_names
                if value in str(message or "")
                and not (value == "小程序" and "商家小程序" in str(message or ""))
            ]
            # “美团还是抖音” asks for a comparison, while “抖音券怎么用”
            # selects the matching SKU channel.
            if len(set(mentioned_platforms)) == 1:
                platform = mentioned_platforms[0]
                selected = [
                    row for row in profiles
                    if platform in " ".join(
                        str(row.get(key) or "")
                        for key in ("sku_name", "name", "delivery_platform", "delivery_method")
                    )
                ]
        targets = selected or ([] if requested_amounts else profiles)

        labels = (
            ("delivery_platform", "发券平台"),
            ("delivery_method", "发券方式"),
            ("claim_method", "领取方式"),
            ("redeem_method", "核销方式"),
        )
        rows = []
        for profile in targets:
            merged = dict(common)
            merged.update({
                key: value for key, value in profile.items()
                if value not in (None, "", [], {})
            })
            name = str(profile.get("sku_name") or profile.get("name") or "商品规格").strip()
            details = []
            seen = set()
            for key, label in labels:
                text = cls._fact_value_text(merged.get(key)).rstrip("。；; ")
                if text and text not in seen:
                    seen.add(text)
                    details.append(f"{label}：{text}")
            if not any(item.startswith("发券平台：") for item in details):
                platforms = []
                for platform in (
                    "商家小程序", "美团", "抖音", "快手", "大众点评", "支付宝",
                    "微信", "云闪付", "京东", "淘宝", "饿了么", "小程序",
                ):
                    if (
                        platform in name and platform not in platforms
                        and not (platform == "小程序" and "商家小程序" in name)
                    ):
                        platforms.append(platform)
                if platforms:
                    details.insert(0, "发券平台：" + "、".join(platforms))
            if details:
                rows.append(f"{name}：" + "；".join(details) + "。")

        if rows:
            return rows

        if requested_amounts:
            return []

        facts = structured.get("facts") or {}
        facts = facts if isinstance(facts, dict) else {}
        fallback = []
        fact_keys = (
            (("发码平台", "发券平台", "delivery_platform"), "发券平台"),
            (("发码方式", "发券方式", "delivery_method"), "发券方式"),
            (("领取方式", "claim_method"), "领取方式"),
            (("核销方式", "redeem_method"), "核销方式"),
        )
        for keys, label in fact_keys:
            text = cls._first_fact(facts, keys).rstrip("。；; ")
            if text:
                fallback.append(f"{label}：{text}")
        return ["商品知识：" + "；".join(fallback) + "。"] if fallback else []

    @classmethod
    def coupon_usage_instructions(cls, product: Dict, message: str = "") -> str:
        custom = str(product.get("coupon_instructions") or "").strip()
        if custom:
            return custom
        if str(product.get("coupon_type") or "").strip() in {"mixed", "promotion"}:
            return "\n".join(cls._mixed_coupon_delivery_rows(product, message))
        return COUPON_TYPE_DEFAULT_INSTRUCTIONS.get(
            str(product.get("coupon_type") or "").strip(),
            "付款后发送电子券码，到店扫码核销。",
        )

    @classmethod
    def purchase_order_public_instructions(cls, product: Dict) -> str:
        """Remove internal quotation clauses from buyer-facing purchase-order steps."""
        custom = str(product.get("coupon_instructions") or "").strip()
        if not custom:
            return COUPON_TYPE_DEFAULT_INSTRUCTIONS["purchase_order"]
        blocked = (
            "报价", "价格", "售价", "价钱", "折扣", "优惠", "改价", "实付", "应付",
            "几折", "小刀", "乘以", "×", "*", "%", "比例", "费率",
        )
        clauses = [
            value.strip() for value in re.split(r"[，,。；;\r\n]+", custom) if value.strip()
        ]
        safe = [
            value for value in clauses
            if not any(word in value for word in blocked)
            and not re.search(r"\d+(?:\.\d+)?\s*(?:元|折)", value)
        ]
        return "，".join(safe).rstrip("，") + "。" if safe else COUPON_TYPE_DEFAULT_INSTRUCTIONS["purchase_order"]

    def effective_aftersale_policy(self, item_id: str = "") -> str:
        product = self.get_v2_product(item_id) if item_id else None
        if product and product.get("custom_policy_enabled"):
            custom = str(
                product.get("custom_policy_summary")
                or product.get("custom_policy_raw") or ""
            ).strip()
            if custom:
                return custom
        policies = self.get_policies()
        return str(
            policies.get("aftersale_policy_summary")
            or policies.get("aftersale_policy_raw") or ""
        ).strip()

    def price_reply(self, product: Dict, message: str = "") -> str:
        brand = self.extract_brand(product)
        options = self.extract_product_options(product)
        priced = [option for option in options if option.get("sale_price")]
        implicit_day = ""
        if str(message or "").strip() and self._has_explicit_day_options(priced):
            implicit_day = self._requested_day_type(message, default_today=True)
            priced = self._filter_options_for_day(priced, implicit_day)
            if not priced:
                prefix = self._day_reply_prefix(message, implicit_day)
                return f"{prefix}当前商品没有该日期适用的在售规格。"
        today_prefix = self._day_reply_prefix(message, implicit_day) if implicit_day else ""
        request_text = re.sub(
            r"(?:(?:\d{4})[年./-])?\d{1,2}[月./-]\d{1,2}(?:日|号)?", "", str(message or "")
        )
        requested = []
        for pattern in (
            r"(\d+(?:\.\d+)?)\s*元\s*(?:代金券|券)",
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?\s*的?\s*(?:代金券|优惠券|券)",
            r"(?:面额|券面)\s*(\d+(?:\.\d+)?)",
            r"多少\s*代\s*(\d+(?:\.\d+)?)",
        ):
            requested.extend(re.findall(pattern, request_text))
        if re.search(r"多少钱|价格|卖多少|怎么卖|多少元", request_text):
            requested.extend(re.findall(
                r"(?<!\d)(\d+(?:\.\d+)?)",
                request_text,
            ))
        requested = list(dict.fromkeys(self._format_number(value) for value in requested))
        if requested:
            matched = [
                option for option in priced
                if self._option_total_value(option) in requested
            ]
            if not matched:
                target = max(Decimal(value) for value in requested)
                plan = self.consumption_plan_reply(
                    product, target, missing_denomination=True,
                    available_options=priced if implicit_day else None,
                )
                if plan:
                    return plan
                label = "、".join(f"{value}元代金券" for value in requested)
                return f"当前商品没有{label}。"
            lines = [self.format_product_option(option, brand) for option in matched]
            return today_prefix + "\n".join(lines)
        if priced:
            lines = [self.format_product_option(option, brand) for option in priced]
            return today_prefix + "当前价格如下：\n" + "\n".join(lines)
        package_options = [
            option for option in self.extract_sale_options(product)
            if option.get("option_type") == "package" and option.get("sale_price")
        ]
        if package_options:
            package_prefix = ""
            if self._has_explicit_day_options(package_options):
                day_type = self._requested_day_type(message, default_today=True)
                package_options = self._filter_options_for_day(package_options, day_type)
                if not package_options:
                    prefix = self._day_reply_prefix(message, day_type)
                    return f"{prefix}当前商品没有该日期适用的在售套餐。"
                package_prefix = self._day_reply_prefix(message, day_type)
            return package_prefix + "当前价格如下：\n" + "\n".join(
                self._format_conditional_option(option, {}) for option in package_options
            )
        facts = (product.get("structured") or {}).get("facts") or {}
        price = self._first_fact(facts, ("价格", "售价", "销售价格", "price"))
        if price:
            product_name = self._first_fact(
                facts, ("商品名", "商品名称", "券名称", "规格", "sku")
            ) or str(product.get("title") or "当前商品")
            if brand and normalize_text(brand) not in normalize_text(product_name):
                product_name = brand + product_name
            price_text = price if re.search(r"元|以.*为准", price) else f"{price}元"
            return f"{product_name}当前售价{price_text}。"
        return "当前商品资料中暂未记录可以确认的实际售价，暂时无法准确报价。"

    def voucher_value_confirmation_reply(self, product: Dict, message: str) -> str:
        """Answer compact paid-price/face-value confirmations from real SKUs."""
        text = str(message or "")
        paid_face = None
        match = re.search(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?\s*"
            r"(?:(?:拍下|下单|购买)(?:后)?(?:就|可|可以)?\s*)?(?:直接\s*)?"
            r"(?:可?抵(?:用|扣)?|代)\s*"
            r"(\d+(?:\.\d+)?)\s*(?:元|块)?",
            text,
        )
        if match:
            paid_face = (match.group(1), match.group(2))
        if not paid_face:
            # Paid price first: 108是200的券吗 / 108买200 / 108→200 / 108能抵200.
            relation = re.search(
                r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?\s*"
                r"(?:是|可以买|可买|买|购|得|发|换|/|→|->|能抵|可抵|抵)\s*"
                r"(\d+(?:\.\d+)?)\s*(?:元|块)?(?:的)?(?:代金券|优惠券|券)?",
                text,
            )
            if relation:
                paid_face = (relation.group(1), relation.group(2))
        if not paid_face:
            # Face value first: 200的券是108 / 200卖108 / 300券拍下来205.
            reverse = re.search(
                r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?(?:的)?(?:代金券|优惠券|券)?"
                r"[^\d。！？]{0,14}(?:是|卖|售价|价格|拍下来(?:是)?|下单(?:是)?)\s*"
                r"(\d+(?:\.\d+)?)\s*(?:元|块)?",
                text,
            )
            if reverse:
                paid_face = (reverse.group(2), reverse.group(1))
        if not paid_face:
            lookup = re.search(
                r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:的券)?\s*"
                r"(?:是多少抵|能抵多少|抵多少|代多少)",
                text,
            )
            if not lookup:
                return ""
            requested = self._format_number(lookup.group(1))
            options = self.extract_product_options(product)
            day_prefix = ""
            if self._has_explicit_day_options(options):
                day_type = self._requested_day_type(message, default_today=True)
                options = self._filter_options_for_day(options, day_type)
                day_prefix = self._day_reply_prefix(message, day_type)
                if not options:
                    return f"{day_prefix}当前商品没有该日期适用的在售代金券。"
            by_face = next((
                option for option in options
                if self._option_total_value(option) == requested
            ), None)
            if by_face:
                return day_prefix + (
                    f"{requested}元代金券当前售价"
                    f"{self._format_number(by_face.get('sale_price'))}元，"
                    f"共可抵扣{requested}元。"
                )
            by_price = next((
                option for option in options
                if self._format_number(option.get("sale_price")) == requested
            ), None)
            if by_price:
                face = self._option_total_value(by_price)
                return day_prefix + f"当前售价{requested}元对应{face}元代金券，共可抵扣{face}元。"
            faces = sorted({
                self._option_total_value(option) for option in options
                if self._option_total_value(option)
            }, key=Decimal)
            suffix = f"当前已确认面额为{'、'.join(value + '元' for value in faces)}。" if faces else ""
            return day_prefix + f"当前商品没有{requested}元这一已确认的代金券规格。{suffix}"
        paid = self._format_number(paid_face[0])
        face = self._format_number(paid_face[1])
        options = self.extract_product_options(product)
        day_prefix = ""
        if self._has_explicit_day_options(options):
            day_type = self._requested_day_type(message, default_today=True)
            options = self._filter_options_for_day(options, day_type)
            day_prefix = self._day_reply_prefix(message, day_type)
            if not options:
                return f"{day_prefix}当前商品没有该日期适用的在售代金券。"
        exact = next((
            option for option in options
            if self._format_number(option.get("sale_price")) == paid
            and self._option_total_value(option) == face
        ), None)
        if exact:
            return day_prefix + f"是的，{self._atomic_voucher_option_text(exact)}。"

        # A listed bundle may be represented in the active manual facts as one
        # atomic coupon plus an explicit stack cap (for example 54元×2 ->
        # 200元 face value). Derive only exact arithmetic within that cap.
        for option in options:
            try:
                unit_paid = Decimal(str(option.get("sale_price") or "0"))
                unit_face = Decimal(str(self._option_total_value(option) or "0"))
                asked_paid = Decimal(paid)
                asked_face = Decimal(face)
            except InvalidOperation:
                continue
            if unit_paid <= 0 or unit_face <= 0 or asked_face % unit_face:
                continue
            quantity = int(asked_face / unit_face)
            if quantity <= 1 or quantity > self._option_stack_limit(product, option):
                continue
            if unit_paid * quantity != asked_paid:
                continue
            contents = self._purchase_contents_label(option, quantity)
            option_name = str(option.get("name") or f"{self._format_number(unit_face)}元代金券")
            return day_prefix + (
                f"是的，{option_name}购买{quantity}张共支付{paid}元，购买后发放{contents}，"
                f"共可抵扣{face}元。"
            )

        same_face = next((
            option for option in options
            if self._option_total_value(option) == face
        ), None)
        if same_face:
            actual_price = self._format_number(same_face.get("sale_price"))
            contents = self._purchase_contents_label(same_face)
            return day_prefix + (
                f"不是，{str(same_face.get('name') or face + '元代金券')}当前售价{actual_price}元，"
                f"购买后发放{contents}。"
            )

        same_price = next((
            option for option in options
            if self._format_number(option.get("sale_price")) == paid
        ), None)
        if same_price:
            actual_face = self._option_total_value(same_price)
            contents = self._purchase_contents_label(same_price)
            return day_prefix + (
                f"当前售价{paid}元对应{actual_face}元代金券，"
                f"购买后发放{contents}。"
            )
        return ""

    def discount_reply(self, item_id: str, product: Dict, message: str) -> str:
        text = str(message or "")
        if not re.search(
            r"多少折|几折|折扣(?:多少|是几|呢|吗)?|\d+(?:\.\d+)?\s*折(?:吗|么|嘛|不|呢)?",
            text,
        ):
            return ""
        options = self.extract_product_options(product)
        day_type = self._requested_day_type(
            text, default_today=self._has_explicit_day_options(options),
        )
        if day_type:
            options = self._filter_options_for_day(options, day_type)

        store_query = extract_store_query(
            text, product=product, product_brand=self.extract_brand(product),
        )
        if is_meaningful_store_query(store_query) and self.is_explicit_store_query(
            text, store_query,
        ):
            supported = self.reverse_store_sku_matches(item_id, store_query, product)
            supported_keys = {str(sku.get("sku_key") or "") for sku in supported}
            if supported_keys:
                options = [
                    option for option in options
                    if self.sku_key_for_option(option) in supported_keys
                ]
            elif self._multi_sku_store_scope(item_id, product).get("stores_differ"):
                return ""

        lines = []
        requested_match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*折", text)
        requested = Decimal(requested_match.group(1)) if requested_match else None
        for option in options:
            try:
                price = Decimal(str(option.get("sale_price") or "0"))
                face = Decimal(str(option.get("face_value") or "0"))
            except InvalidOperation:
                continue
            if price <= 0 or face <= 0:
                continue
            discount = (price / face * Decimal("10")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            line = (
                f"{self._format_number(face)}元代金券售价{self._format_number(price)}元，"
                f"约{self._format_number(discount)}折"
            )
            if requested is not None:
                line = ("是的，" if abs(discount - requested) <= Decimal("0.01") else "不是，") + line
            lines.append(line)
        prefix = self._day_reply_prefix(text, day_type) if day_type else ""
        return prefix + "；".join(lines) + "。" if lines else ""

    def purchase_entry_reply(self, product: Dict, message: str) -> str:
        """Guide purchase from the current listing using only sellable SKUs."""
        compact = re.sub(r"[\s，,。.!！?？~～]+", "", str(message or ""))
        if compact not in {
            "哪里买", "在哪里买", "在哪买", "哪里购买", "在哪购买",
            "购买入口在哪", "购买入口在哪里", "从哪里下单", "在哪下单",
            "怎么下单", "如何下单", "购买流程", "怎么买", "如何购买", "拍哪个",
        }:
            return ""
        if str(product.get("item_status") or "").lower() in {
            "offline", "off_shelf", "offshelf", "deleted", "下架",
        }:
            return "当前商品目前已下架，暂时无法购买。"
        if compact in {"怎么下单", "如何下单", "购买流程", "怎么买", "如何购买", "拍哪个"}:
            return (
                "直接在当前商品页面选择需要的规格并完成付款即可。"
                "付款后会按商品说明发送领券信息，到店出示券码核销。"
            )
        options = self.extract_product_options(product)
        if options and self._has_explicit_day_options(options):
            day_type = self._requested_day_type(message, default_today=True)
            options = self._filter_options_for_day(options, day_type)
            if not options:
                return "当前商品暂时没有符合今天使用条件的可购买规格。"
            brand = self.extract_brand(product)
            choices = "、".join(
                self.format_product_option(option, brand, show_time=True)
                for option in options
            )
            return f"直接在当前商品页面下单即可。{self._day_reply_prefix(message, day_type)}请选择{choices}。"
        if not options and re.search(r"代金券|优惠券|券", " ".join((
            str(product.get("title") or ""), str(product.get("raw_text") or ""),
        ))):
            return "当前商品暂时没有可购买的在售规格。"
        return (
            "直接在当前商品页面选择需要的规格并完成付款即可。"
            "付款后会按商品说明发送领券信息，到店出示券码核销。"
        )

    @classmethod
    def purchase_timing_reply(cls, product: Dict, message: str) -> str:
        """Answer colloquial questions about buying after dining but before checkout."""
        text = str(message or "").strip()
        if not re.search(
            r"(?:吃完|吃了|用餐后|消费后|结账前|买单前)"
            r"[^。！？\n]{0,10}(?:再|才)?(?:买|拍|购买|下单)",
            text,
        ):
            return ""
        knowledge = "\n".join((
            str(product.get("raw_text") or ""),
            str(product.get("ai_summary") or ""),
        ))
        same_day = bool(re.search(
            r"当天[^。；\n]{0,12}(?:购买|使用)|(?:购买|使用)[^。；\n]{0,12}当天",
            knowledge,
        ))
        prefix = "可以在用餐结束后、结账前购买，并在收到券码后交给门店核销。"
        if same_day:
            prefix += "该券需要当天购买、当天使用。"
        return prefix

    def direct_coupon_purchase_reply(self, product: Dict, message: str) -> str:
        """Confirm a specifically named denomination without expanding the catalog."""
        text = str(message or "").strip()
        if not re.search(
            r"直接(?:拍|买)|(?:可以|能|可不可以|能不能)(?:直接)?(?:拍|买|购买)",
            text,
        ):
            return ""
        value_match = re.search(
            r"(?:代|面额)\s*(\d+(?:\.\d+)?)|"
            r"(\d+(?:\.\d+)?)\s*(?:元)?(?:代金券|券)",
            text,
        )
        if not value_match:
            return ""
        requested = self._format_number(next(
            value for value in value_match.groups() if value is not None
        ))
        candidates = [
            item for item in self.extract_product_options(product)
            if self._format_number(item.get("face_value")) == requested
        ]
        implicit_day = ""
        if self._has_explicit_day_options(candidates):
            implicit_day = self._requested_day_type(text, default_today=True)
            candidates = self._filter_options_for_day(candidates, implicit_day)
            if not candidates:
                prefix = self._day_reply_prefix(text, implicit_day)
                return f"{prefix}当前没有该日期适用的{requested}元代金券。"
        option = candidates[0] if candidates else None
        if not option:
            plan = self.consumption_plan_reply(
                product, Decimal(requested), missing_denomination=True,
            )
            return plan or f"当前商品没有{requested}元代金券，请选择商品页面已有的规格。"
        prefix = self._day_reply_prefix(text, implicit_day) if implicit_day else ""
        return f"{prefix}可以直接拍下，{self.format_product_option(option, self.extract_brand(product))}。"

    def voucher_sku_lookup_reply(self, product: Dict, message: str) -> Optional[Dict]:
        """Answer explicit denomination/option stock questions atomically.

        The sellable SKU is matched before any denomination combination.  Its
        delivery composition is then rendered from the same record, preventing
        the old error where a real 200-yuan option was replaced by two unrelated
        100-yuan SKU calculations.
        """
        text = str(message or "").strip()
        if not re.search(r"有吗|有没有|有无|有么|有嘛|有货|卖不卖|选项|规格", text):
            return None
        amount = self._requested_price_amount(text)
        if not amount:
            match = re.search(
                r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:代金券|优惠券|券|选项|规格)",
                text,
            )
            amount = self._format_number(match.group(1)) if match else ""
        if not amount:
            return None
        options = [
            option for option in self.extract_product_options(product)
            if option.get("sale_price") and self._is_sellable_option(option)
        ]
        if not options:
            # A package-only product must stay on the existing package path;
            # numeric package prices are never reclassified as coupon faces.
            voucher_source = "\n".join((
                str(product.get("title") or ""), str(product.get("raw_text") or ""),
                json.dumps(product.get("structured") or {}, ensure_ascii=False),
            ))
            if not re.search(r"代金券|抵扣券|现金券", voucher_source):
                return None
            return {
                "reply": (
                    f"当前商品没有{amount}元代金券。"
                    f"您好，本店目前没有“{amount}元”这一有货规格，"
                    "暂时无法通过当前商品购买，您可以到店咨询。"
                ),
                "source": "当前商品不存在对应的有货代金券SKU",
                "decision": "deny", "kind": "sku_availability",
            }
        day_type = self._requested_day_type(text)
        if not day_type and self._has_explicit_day_options(options):
            day_type = self._requested_day_type(text, default_today=True)
        if day_type:
            options = self._filter_options_for_day(options, day_type)

        strict_single = bool(re.search(
            rf"(?:一|1)\s*张[^。！？\n]{{0,8}}{re.escape(amount)}|"
            rf"{re.escape(amount)}\s*(?:元)?(?:代金券|券)?[^。！？\n]{{0,8}}(?:一|1)\s*张",
            text,
        ))
        exact = []
        for option in options:
            total = self._option_total_value(option)
            if total != amount:
                continue
            if strict_single and not self._option_delivers_single_face(option, amount):
                continue
            exact.append(option)
        if not exact:
            subject = f"一张{amount}元代金券" if strict_single else f"{amount}元"
            alternative = next((
                option for option in options if self._option_total_value(option) == amount
            ), None)
            reply = (
                f"当前商品没有{amount}元代金券。"
                f"您好，本店目前没有“{subject}”这一有货规格，"
                "暂时无法通过当前商品购买，您可以到店咨询。"
            )
            if strict_single and alternative:
                reply += " 当前有货的是" + self._atomic_voucher_option_text(alternative) + "。"
            return {
                "reply": reply, "source": "当前商品现有有货SKU",
                "decision": "deny", "kind": "sku_availability",
            }
        day_prefix = self._day_reply_prefix(text, day_type) if day_type else ""
        return {
            "reply": day_prefix + "有的。" + "\n".join(
                self._atomic_voucher_option_text(option) + "。" for option in exact
            ),
            "source": "当前商品现有有货SKU及同一SKU发券组成",
            "decision": "allow", "kind": "sku_availability",
        }

    def named_sku_price_reply(self, product: Dict, message: str) -> str:
        """Resolve named ``xxx多少钱`` queries against SKU names first."""
        match = re.fullmatch(
            r"(?:请问)?(.+?)\s*(?:多少钱|多钱|的多少|什么价格|价格多少|卖多少|怎么卖|几块钱|几元)[？?]?",
            str(message or "").strip(),
        )
        if not match:
            return ""
        subject = match.group(1).strip(" ，,。.!！?？~～")
        if normalize_text(subject).lower() in {"今天", "现在", "当前", "这个", "它"}:
            return ""
        # Numeric denomination questions are handled by price_reply, including
        # its closest-lower coupon recommendation.
        if re.fullmatch(
            r"\d+(?:\.\d+)?\s*(?:元)?\s*(?:的)?\s*(?:代金券|优惠券|券)?",
            subject,
        ):
            return self.price_reply(product, message)

        options = [
            option for option in self.extract_product_options(product)
            if option.get("sale_price")
        ]
        subject_key = normalize_text(subject).lower()
        matched = []
        for option in options:
            name_key = normalize_text(option.get("name") or "").lower()
            if subject_key and (subject_key in name_key or name_key in subject_key):
                matched.append(option)
        brand = self.extract_brand(product)
        if matched:
            prefix = ""
            if self._has_explicit_day_options(matched):
                day_type = self._requested_day_type(message, default_today=True)
                matched = self._filter_options_for_day(matched, day_type)
                if not matched:
                    prefix = self._day_reply_prefix(message, day_type)
                    return f"{prefix}当前没有该日期适用的“{subject}”规格。"
                prefix = self._day_reply_prefix(message, day_type)
            return prefix + "\n".join(self.format_product_option(option, brand) for option in matched)
        raw_text = str(product.get("raw_text") or "").replace("\\n", "\n")
        raw_match = re.search(
            rf"(?m)^\s*([^\n：:]{{0,30}}{re.escape(subject)}[^\n：:]{{0,30}})\s*[：:]\s*"
            rf"(?:售价|价格)?\s*[¥￥]?\s*(\d+(?:\.\d+)?)\s*元",
            raw_text,
        )
        if raw_match:
            name = raw_match.group(1).strip()
            price = self._format_number(raw_match.group(2))
            return f"{name}：售价{price}元。"
        if options:
            return (
                f"当前商品没有“{subject}”这一规格，您可以选择以下已有规格：\n"
                + "\n".join(self.format_product_option(option, brand) for option in options)
            )
        return f"当前商品没有“{subject}”这一规格。"

    def coupon_catalog_reply(self, product: Dict, message: str = "") -> str:
        """List only real coupon SKUs; package products must not invent coupons."""
        brand = self.extract_brand(product)
        options = [
            option for option in self.extract_product_options(product)
            if option.get("face_value") and option.get("sale_price")
        ]
        if not options:
            title = str(product.get("title") or "当前商品").strip()
            price = self._format_number(product.get("price") or "")
            suffix = f"，当前售价{price}元" if price else ""
            return f"当前商品没有代金券选项，售卖的是{title}{suffix}。"
        signatures = {tuple(sorted(self._option_day_types(option))) for option in options}
        show_time = len(options) > 1 and len(signatures) > 1
        prefix = "当前可选代金券如下：\n"
        if show_time:
            day_type = self._requested_day_type(message, default_today=True)
            options = self._filter_options_for_day(options, day_type)
            prefix = self._day_reply_prefix(message, day_type) + "可选代金券如下：\n"
        return prefix + "\n".join(
            self.format_product_option(option, brand, show_time=show_time) for option in options
        )

    @classmethod
    def _non_coupon_sale_description(cls, product: Dict) -> str:
        """Describe a real package/item without converting its price to face value."""
        title = str(product.get("title") or "当前商品").strip()
        raw_text = str(product.get("raw_text") or "").replace("\\n", "\n")
        candidates = []
        for line in re.split(r"[\r\n；;]+", raw_text):
            if not re.search(r"套餐|人餐|自助|单品|烤鱼|菜品|餐品", line):
                continue
            match = re.search(
                r"^\s*([^：:\n]{1,50}?)\s*[：:]\s*(?:售价|价格)?\s*[¥￥]?(\d+(?:\.\d+)?)\s*元",
                line,
            )
            if match:
                candidates.append((match.group(1).strip(), cls._format_number(match.group(2))))
        if candidates:
            name, price = candidates[0]
            return f"{name}，售价{price}元"
        price = cls._format_number(product.get("price") or "")
        return f"{title}，售价{price}元" if price else title

    def sku_availability_reply(self, product: Dict, message: str) -> str:
        """Answer ``xxx有吗`` strictly from real SKUs, never from LLM guesses."""
        text = str(message or "").strip()
        match = re.fullmatch(
            r"(?:请问)?(?:有没有|有无|有没有卖|卖不卖)\s*(.+?)(?:呢|啊|呀|吗)?[？?]?|"
            r"(?:请问)?(?:能不能|可不可以|可以不可以|能否)(?:买|拍|购买)\s*(.+?)[？?]?|"
            r"(?:请问)?(.+?)\s*(?:有吗|有没有|有么|有嘛|还有吗|有货吗|"
            r"能拍吗|可以拍吗|能买到吗|能不能买)[？?]?|"
            r"(?:请问)?有\s*(.+?)\s*(?:吗|么|嘛|不)[？?]?",
            text,
        )
        if not match:
            return ""
        subject = next((value for value in match.groups() if value), "").strip(" ，,。.!！?？~～")
        subject = re.sub(r"^还(?:有)?|还$", "", subject).strip()
        subject_key = normalize_text(subject).lower()
        if not subject_key or subject_key in {"还", "这个", "那个", "它", "这款", "当前", "现在"}:
            return ""

        options = self.extract_product_options(product)
        # A bare category question means “which coupon SKUs are available”.
        if subject_key in {"代金券", "券", "券型", "面额", "优惠券"}:
            return self.coupon_catalog_reply(product, message)

        requested_values = list(dict.fromkeys(re.findall(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?(?:代金券|券)?",
            subject,
        )))
        subject_without_numbers = normalize_text(re.sub(r"\d+(?:\.\d+)?\s*元?", "", subject)).lower()
        matched = []
        for option in options:
            option_name = normalize_text(option.get("name") or "").lower()
            face = self._format_number(option.get("face_value") or "")
            value_match = bool(requested_values) and face in {
                self._format_number(value) for value in requested_values
            }
            name_match = bool(subject_without_numbers) and (
                subject_without_numbers in option_name or option_name in subject_key
            )
            # Once the buyer names a denomination, matching that denomination
            # is mandatory; generic words such as “代金券” must not turn a
            # 300元 query into a hit on a 100元 SKU.
            if value_match or (not requested_values and name_match):
                matched.append(option)

        if matched:
            implicit_day = ""
            if self._has_explicit_day_options(matched):
                implicit_day = self._requested_day_type(text, default_today=True)
                matched = self._filter_options_for_day(matched, implicit_day)
                if not matched:
                    requested = "、".join(
                        f"{self._format_number(value)}元" for value in requested_values
                    ) or subject
                    prefix = self._day_reply_prefix(text, implicit_day)
                    return f"{prefix}当前没有该日期适用的{requested}商品规格。"
            brand = self.extract_brand(product)
            prefix = self._day_reply_prefix(text, implicit_day) if implicit_day else ""
            return prefix + "有的，当前商品包含：\n" + "\n".join(
                self.format_product_option(option, brand) for option in matched
            )
        if not requested_values:
            raw_text = str(product.get("raw_text") or "").replace("\\n", "\n")
            for line in re.split(r"[\r\n；;]+", raw_text):
                line_key = normalize_text(line).lower()
                if not subject_key or subject_key not in line_key:
                    continue
                price_match = re.search(
                    r"(?:售价|价格)\s*[¥￥]?\s*(\d+(?:\.\d+)?)\s*元", line
                )
                name = re.split(r"[：:]", line, maxsplit=1)[0].strip(" ，,、")
                if name:
                    suffix = (
                        f"，售价{self._format_number(price_match.group(1))}元"
                        if price_match else ""
                    )
                    return f"有的，当前商品包含{name}{suffix}。"
        if requested_values:
            target = max(Decimal(self._format_number(value)) for value in requested_values)
            plan = self.consumption_plan_reply(
                product, target, missing_denomination=True
            )
            if plan:
                return plan
        # Only an explicit product-option noun is strong enough to conclude
        # that a named option is missing.  A phrase such as “广东万达有吗” or
        # “西丽益田假日店有吗” must continue to the store matcher.
        if re.search(r"(?:套餐|代金券|优惠券|团购券|电子券|餐券|人餐|自助)", subject):
            return f"当前商品没有“{subject}”这一规格，请选择商品页面已有的商品规格。"
        return ""

    @staticmethod
    def _safe_flavor_value(value: object) -> str:
        text = str(value or "").strip(" ，,、；;：:")
        text = re.sub(r"^(?:可选|包含|包括|有)\s*", "", text).strip()
        if not text or len(text) > 60:
            return ""
        if re.search(
            r"\d|[=￥¥%{}\[\]（）()]|售价|价格|金额|门店|地址|城市|省份|"
            r"北京|上海|广州|深圳|全国|可选\s*=|商品信息|使用规则",
            text,
        ):
            return ""
        values = [
            item.strip(" ，,、；;：:")
            for item in re.split(r"[、，,；;/|]+", text)
            if item.strip(" ，,、；;：:")
        ]
        if not values or len(values) > 10:
            return ""
        if any(
            len(item) > 12
            or not re.search(r"[A-Za-z\u4e00-\u9fff]", item)
            or item in {"可选", "任选", "未说明", "未知", "暂无"}
            for item in values
        ):
            return ""
        return "、".join(dict.fromkeys(values))

    @classmethod
    def product_attribute_reply(cls, product: Dict, message: str) -> Optional[Dict]:
        """Answer explicit product-attribute questions before any store search."""
        message = str(message or "").strip()
        compact = normalize_text(message)
        if not compact:
            return None
        # A phrase such as “万达店有多大” still names a location. Do not let a
        # generic attribute keyword steal an explicit store question.
        if any(word in compact for word in (
            "门店", "商场", "商圈", "购物中心", "万达", "万象城", "万科里",
            "天街", "银泰", "吾悦", "大悦城", "来福士", "太古里",
        )):
            return None

        raw = str(product.get("raw_text") or "").replace("\\n", "\n")
        ai = str(product.get("ai_summary") or "").replace("\\n", "\n")
        facts = (product.get("structured") or {}).get("facts") or {}
        fact_text = "\n".join(
            f"{key}：{value}"
            for key, value in facts.items()
            if not isinstance(value, (dict, list)) and str(value).strip()
        ) if isinstance(facts, dict) else ""
        sources = [value for value in (raw, ai, fact_text) if value.strip()]

        weight_question = bool(re.search(
            r"(?:鱼|商品|一条|这个|套餐)?(?:有)?多重|重量|几斤|多少斤|多少公斤|多少克|一条多大",
            message,
        ))
        if weight_question:
            for source in sources:
                match = re.search(
                    r"(?:单条(?:活)?鱼|一条(?:活)?鱼|活鱼|鱼|重量|净重)?"
                    r"[^。；\n]{0,18}?(\d+(?:\.\d+)?)\s*(斤|公斤|千克|kg|KG|克)",
                    source,
                )
                if not match:
                    continue
                weight = f"{cls._format_number(match.group(1))}{match.group(2)}"
                prefix = "单条鱼约" if re.search(r"单条|一条|活鱼|烤鱼", source) else "当前商品约"
                reply = f"{prefix}{weight}"
                nearby = source[max(0, match.start() - 30):match.end() + 60]
                missing = []
                if "不含米饭" in nearby:
                    missing.append("米饭")
                if "不含米饭配菜" in nearby or "不含配菜" in nearby:
                    missing.append("配菜")
                if missing:
                    reply += f"，不含{'和'.join(dict.fromkeys(missing))}"
                return {
                    "reply": reply + "。", "source": "当前商品资料中的重量",
                    "decision": "allow", "kind": "product_attribute",
                }
            return {
                "reply": "当前商品资料暂未说明具体重量，暂时无法准确确认。",
                "source": "商品重量资料缺失", "decision": "allow",
                "kind": "product_attribute",
            }

        flavor_question = bool(re.search(
            r"口味|味道|几种味|什么味|哪些味|辣不辣|辣吗|有辣的|能选味",
            message,
        ))
        if flavor_question:
            for source in sources:
                if re.search(
                    r"(?:口味|味道)[^。；\n]{0,8}(?:固定|不可选|不能选|无法选择)|"
                    r"(?:固定|统一|默认)[^。；\n]{0,8}(?:口味|味道)",
                    source,
                ):
                    return {
                        "reply": "当前商品为固定口味，暂不支持选择口味哦。",
                        "source": "当前商品资料中的口味规则", "decision": "allow",
                        "kind": "product_attribute",
                    }
                match = re.search(
                    r"(?:可选)?(?:口味|味道)\s*(?:有|包括|为)?\s*[：:]?\s*"
                    r"([^。；\n]{1,80})",
                    source,
                )
                if not match:
                    continue
                value = re.split(
                    r"(?:使用|价格|售价|仅限|不可|不能|有效期|门店)", match.group(1), maxsplit=1
                )[0].strip(" ，,、；;：:")
                value = cls._safe_flavor_value(value)
                if value:
                    return {
                        "reply": f"当前可选口味有：{value}。",
                        "source": "当前商品资料中的口味", "decision": "allow",
                        "kind": "product_attribute",
                    }
            # Some merchant descriptions list one flavor per line without a
            # “口味：” heading, e.g. “贵州凯里酸汤牛肉烤鱼（不辣）”.  Extract only
            # explicit dish + spice pairs so prices, regions and broken AI
            # summaries cannot be mistaken for flavor names.
            listed_flavors = []
            spice_pattern = r"不辣|微辣|中辣|重辣|特辣|香辣|麻辣"
            dish_pattern = r"[A-Za-z\u4e00-\u9fff·]{2,30}?(?:烤鱼|酸汤|香锅|锅底)"
            combined_source = "\n".join(sources)
            for pattern in (
                rf"(?P<dish>{dish_pattern})\s*[（(]\s*(?P<spice>{spice_pattern})\s*[）)]",
                rf"[（(]\s*(?P<spice>{spice_pattern})\s*[）)]\s*(?P<dish>{dish_pattern})",
            ):
                for match in re.finditer(pattern, combined_source):
                    label = f"{match.group('dish')}（{match.group('spice')}）"
                    if label not in listed_flavors:
                        listed_flavors.append(label)
            if listed_flavors:
                return {
                    "reply": f"当前商品资料列出的口味有：{'、'.join(listed_flavors[:10])}。",
                    "source": "当前商品逐项列出的口味", "decision": "allow",
                    "kind": "product_attribute",
                }
            return {
                "reply": "当前商品资料暂未说明具体口味，暂时无法准确确认。",
                "source": "商品口味资料缺失", "decision": "allow",
                "kind": "product_attribute",
            }

        people_question = bool(re.search(
            r"几人(?:吃|用|餐)?|几个人|适合几人|适合几个人|够\s*[一二两三四五六七八九十\d]+\s*人|"
            r"[一二两三四五六七八九十\d]+\s*个人?够吗",
            message,
        ))
        if people_question:
            source = "\n".join((str(product.get("title") or ""), *sources))
            match = re.search(r"(\d+)\s*[-~～至到—]\s*(\d+)\s*人|(?<!\d)(\d+)\s*人(?:餐|使用|份)", source)
            if match:
                label = (
                    f"{match.group(1)}-{match.group(2)}人"
                    if match.group(1) else f"{match.group(3)}人"
                )
                return {
                    "reply": f"当前套餐适合{label}使用。",
                    "source": "当前商品资料中的适用人数", "decision": "allow",
                    "kind": "product_attribute",
                }
            return {
                "reply": "当前商品资料暂未说明适用人数，暂时无法准确确认。",
                "source": "适用人数资料缺失", "decision": "allow",
                "kind": "product_attribute",
            }

        contents_question = bool(re.search(
            r"包含什么|包括什么|套餐内容|有什么菜|有米饭|含米饭|带米饭|有配菜|含配菜|带配菜",
            message,
        ))
        if contents_question:
            source = "\n".join(sources)
            asks_rice = "米饭" in message
            asks_side = "配菜" in message
            if asks_rice or asks_side:
                absent = []
                if asks_rice and re.search(r"不含[^。；\n]{0,8}米饭|不带[^。；\n]{0,8}米饭", source):
                    absent.append("米饭")
                if asks_side and re.search(r"不含[^。；\n]{0,8}配菜|不带[^。；\n]{0,8}配菜", source):
                    absent.append("配菜")
                # “不含米饭配菜” commonly omits a separator between the two.
                if re.search(r"不含米饭配菜", source):
                    absent.extend(["米饭", "配菜"])
                if absent:
                    return {
                        "reply": f"当前套餐不含{'和'.join(dict.fromkeys(absent))}。",
                        "source": "当前商品资料中的套餐内容", "decision": "allow",
                        "kind": "product_attribute",
                    }
            for value in sources:
                match = re.search(
                    r"(?:套餐内容|包含|包括)\s*[：:]?\s*([^。；\n]{1,100})", value
                )
                if match:
                    return {
                        "reply": f"当前套餐包含：{match.group(1).strip(' ，,、')}。",
                        "source": "当前商品资料中的套餐内容", "decision": "allow",
                        "kind": "product_attribute",
                    }
            return {
                "reply": "当前商品资料暂未说明具体套餐内容，暂时无法准确确认。",
                "source": "套餐内容资料缺失", "decision": "allow",
                "kind": "product_attribute",
            }
        return None

    @staticmethod
    def _coupon_scope_reply(product: Dict) -> str:
        knowledge = "\n".join((
            str(product.get("raw_text") or ""),
            str(product.get("ai_summary") or ""),
        ))
        sentences = [
            part.strip(" \t\r\n；;。")
            for part in re.split(r"[\r\n]+|(?<=[。；;])", knowledge)
            if part.strip(" \t\r\n；;。")
        ]
        scope_words = (
            "全场通用", "全场可用", "酒水", "饮料", "特价菜", "套餐", "堂食",
            "不参与抵扣", "不可用", "不能用", "适用范围",
        )
        selected = []
        for sentence in sentences:
            if any(word in sentence for word in scope_words) and sentence not in selected:
                selected.append(sentence)
            if len(selected) >= 2:
                break
        return "；".join(selected)

    @classmethod
    def _option_coupon_pairs(cls, option: Dict) -> List[tuple[Decimal, int]]:
        pairs = [
            (Decimal(amount), int(count))
            for amount, count in re.findall(
                r"(\d+(?:\.\d+)?)元券(\d+)张",
                str(option.get("composition") or ""),
            )
            if int(count) > 0
        ]
        if pairs:
            return pairs
        try:
            face = Decimal(str(option.get("face_value") or "0"))
        except InvalidOperation:
            face = Decimal("0")
        return [(face, 1)] if face > 0 else []

    @classmethod
    def _purchase_contents_label(cls, option: Dict, quantity: int = 1) -> str:
        """Render the coupons actually delivered, not an internal option name."""
        pairs = cls._option_coupon_pairs(option)
        if pairs:
            face = cls._format_number(option.get("face_value") or "")
            if (
                quantity == 1
                and len(pairs) == 1
                and cls._format_number(pairs[0][0]) == face
                and pairs[0][1] == 1
            ):
                return f"{face}元代金券"
            parts = [
                f"{count * quantity}张{cls._format_number(amount)}元代金券"
                for amount, count in pairs
            ]
            return "、".join(parts)
        face = cls._format_number(option.get("face_value") or "")
        if not face:
            return "当前商品"
        return f"{face}元代金券" if quantity == 1 else f"{quantity}张{face}元代金券"

    @classmethod
    def _option_total_value(cls, option: Dict) -> str:
        pairs = cls._option_coupon_pairs(option)
        if pairs:
            total = sum(amount * count for amount, count in pairs)
            return cls._format_number(total)
        return cls._format_number(option.get("face_value") or "")

    @classmethod
    def _option_delivers_single_face(cls, option: Dict, amount: str) -> bool:
        pairs = cls._option_coupon_pairs(option)
        if pairs:
            return (
                len(pairs) == 1 and pairs[0][1] == 1
                and cls._format_number(pairs[0][0]) == cls._format_number(amount)
            )
        return cls._format_number(option.get("face_value") or "") == cls._format_number(amount)

    @classmethod
    def _atomic_voucher_option_text(cls, option: Dict) -> str:
        name = str(option.get("name") or f"{cls._option_total_value(option)}元选项").strip()
        price = cls._format_number(option.get("sale_price") or "")
        total = cls._option_total_value(option)
        contents = cls._purchase_contents_label(option)
        text = f"{name}售价{price}元，购买后发放{contents}"
        if total:
            text += f"，共可抵扣{total}元"
        delivered = sum(count for _, count in cls._option_coupon_pairs(option))
        try:
            stack = int(Decimal(str(option.get("max_stack") or "0")))
        except (InvalidOperation, ValueError):
            stack = 0
        if delivered > 1 and stack >= delivered:
            text += f"，该规格支持{delivered}张券叠加使用"
        return text

    @classmethod
    def _option_stack_limit(cls, product: Dict, option: Dict) -> int:
        """Return a conservative sellable quantity under the stated coupon limit."""
        try:
            maximum = int(Decimal(str(option.get("max_stack") or "0")))
        except (InvalidOperation, ValueError):
            maximum = 0
        if not maximum:
            knowledge = "\n".join((
                str(product.get("raw_text") or ""),
                str(product.get("ai_summary") or ""),
            ))
            if cls._supports_unlimited_stacking(knowledge):
                return 10000
            limit = re.search(
                r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张",
                knowledge,
            )
            # Same-face stacking is the only safe default when no mixed-face
            # rule exists. Two identical coupons covers the explicit business
            # case “only 100 available, buyer asks for 200”; anything beyond
            # this conservative fallback is called out as unconfirmed below.
            maximum = int(limit.group(1)) if limit else 2
        delivered = sum(count for _, count in cls._option_coupon_pairs(option)) or 1
        return max(1, maximum // delivered)

    def _coupon_quantity_purchase_plan(
        self, product: Dict, options: List[Dict], desired_count: int,
        requested_face: str = "",
    ) -> Dict:
        """Price an exact number of delivered coupons, not an SKU multiplier."""
        groups: Dict[str, List[tuple[Dict, int, Decimal]]] = {}
        for option in options:
            pairs = self._option_coupon_pairs(option)
            unit_faces = {self._format_number(amount) for amount, _ in pairs if amount > 0}
            if len(unit_faces) != 1:
                continue
            unit_face = next(iter(unit_faces))
            if requested_face and unit_face != self._format_number(requested_face):
                continue
            try:
                price = Decimal(str(option.get("sale_price") or "0"))
            except InvalidOperation:
                continue
            delivered = sum(count for _, count in pairs)
            if price > 0 and delivered > 0:
                groups.setdefault(unit_face, []).append((option, delivered, price))
        if not groups:
            return {}
        if not requested_face and len(groups) > 1:
            return {"ambiguous_faces": sorted(groups, key=Decimal)}

        best = None
        for unit_face, rows in groups.items():
            # dp[count] = (price, package_count, quantities_per_sku)
            zero_counts = tuple(0 for _ in rows)
            dp: List[Optional[tuple[Decimal, int, tuple[int, ...]]]] = [None] * (desired_count + 1)
            dp[0] = (Decimal("0"), 0, zero_counts)
            for count in range(1, desired_count + 1):
                for index, (_, delivered, price) in enumerate(rows):
                    if delivered > count or dp[count - delivered] is None:
                        continue
                    previous = dp[count - delivered]
                    quantities = list(previous[2])
                    quantities[index] += 1
                    candidate = (previous[0] + price, previous[1] + 1, tuple(quantities))
                    if dp[count] is None or candidate[:2] < dp[count][:2]:
                        dp[count] = candidate
            if dp[desired_count] is None:
                continue
            total_price, package_count, quantities = dp[desired_count]
            candidate = (total_price, package_count, unit_face, rows, quantities)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            return {
                "unavailable_count": desired_count,
                "available_bundles": {
                    face: sorted({delivered for _, delivered, _ in rows})
                    for face, rows in groups.items()
                },
            }

        total_price, _, unit_face, rows, quantities = best
        selected = [
            (rows[index][0], quantity)
            for index, quantity in enumerate(quantities) if quantity
        ]
        total_value = Decimal(unit_face) * desired_count
        contents = f"{desired_count}张{unit_face}元代金券"
        if len(selected) == 1:
            option, package_quantity = selected[0]
            name = str(option.get("name") or option.get("sku_name") or "当前规格").strip()
            if package_quantity == 1:
                reply = (
                    f"{name}：售价{self._format_number(total_price)}元，"
                    f"发{contents}，共可抵扣{self._format_number(total_value)}元。"
                )
            else:
                reply = (
                    f"购买{package_quantity}份{name}，共支付{self._format_number(total_price)}元，"
                    f"发{contents}，共可抵扣{self._format_number(total_value)}元。"
                )
        else:
            details = "＋".join(
                f"{str(option.get('name') or option.get('sku_name') or '当前规格').strip()}×{quantity}"
                for option, quantity in selected
            )
            reply = (
                f"购买方案：{details}，共支付{self._format_number(total_price)}元，"
                f"发{contents}，共可抵扣{self._format_number(total_value)}元。"
            )

        limits = []
        for option, _ in selected:
            try:
                limit = int(Decimal(str(option.get("max_stack") or "0")))
            except (InvalidOperation, ValueError):
                limit = 0
            if limit > 0:
                limits.append(limit)
        knowledge = "\n".join((
            str(product.get("raw_text") or ""), str(product.get("ai_summary") or ""),
        ))
        if limits:
            limit = min(limits)
            if desired_count > limit:
                if len(selected) == 1:
                    option, _ = selected[0]
                    pairs = self._option_coupon_pairs(option)
                    if len(pairs) == 1 and pairs[0][1] == 1:
                        reply = (
                            f"购买{contents}共{self._format_number(total_price)}元。"
                            f"每张可抵扣{unit_face}元；当前同面额代金券每次最多使用{limit}张，"
                            f"{desired_count}张不能在同一次消费中全部使用。"
                        )
                    else:
                        reply = reply.replace(
                            f"，共可抵扣{self._format_number(total_value)}元。", "。",
                        ) + (
                            f"该面额每次最多使用{limit}张，"
                            "本次购买的券不能在同一次消费中全部使用。"
                        )
                else:
                    reply = reply.replace(
                        f"，共可抵扣{self._format_number(total_value)}元。", "。",
                    ) + (
                        f"该面额每次最多使用{limit}张，"
                        "本次购买的券不能在同一次消费中全部使用。"
                    )
            else:
                reply += f"该面额每次最多使用{limit}张。"
        elif self._supports_unlimited_stacking(knowledge):
            reply += "该面额支持无限叠加。"
        return {
            "reply": reply,
            "selected": selected,
            "unit_face": unit_face,
            "total_price": total_price,
        }

    def amount_inquiry_plan_reply(
        self, product: Dict, message: str,
        available_options: Optional[List[Dict]] = None,
    ) -> Optional[Dict]:
        """Plan an amount inquiry using one in-stock denomination and explicit stacking."""
        text = str(message or "").strip()
        contextual_amount = bool(re.match(
            r"\s*(?:我(?:这边)?|这边|一共|总共)\s*\d", text,
        ))
        match = re.fullmatch(
            r"\s*(?:(?:我(?:这边)?|这边|一共|总共)\s*)?"
            r"(\d+(?:\.\d+)?)\s*(?:元|块)?\s*"
            r"(?:(?:的)?(?:呢|嘛|么|呀|啊))?\s*[?？。!！]?\s*",
            text,
        )
        bare = bool(match)
        discount_total = False
        if not match:
            discount_match = re.fullmatch(
                r"\s*(?:消费|吃|用餐)?\s*(\d+(?:\.\d+)?)\s*(?:元|块)?\s*"
                r"(?:优惠完|优惠后|打折后)[^。！？]{0,6}(?:多少|多少钱)\s*[?？。!！]?\s*",
                text,
            )
            if discount_match:
                match = discount_match
                discount_total = True
        if not match and re.search(r"有吗|有没有|有无|有么|有嘛|有货|没有|还有|有.*[吗么嘛？?]", message):
            if re.search(r"单张|一张|1\s*张|\d\s*(?:人|位|份|折|路|号|点)", message):
                return None
            amounts = re.findall(r"(?<!\d)\d+(?:\.\d+)?", message)
            if len(amounts) == 1:
                match = re.search(r"(\d+(?:\.\d+)?)", message)
        if not match:
            return None
        knowledge = "\n".join(str(product.get(key) or "") for key in ("title", "raw_text", "ai_summary"))
        options = (
            list(available_options)
            if available_options is not None else self.extract_product_options(product)
        )
        if not options and not re.search(r"代金券|抵扣券|现金券", knowledge):
            return None
        target = Decimal(match.group(1))
        if target <= 0:
            return None
        day = self._requested_day_type(message, default_today=True)
        options = self._filter_options_for_day(options, day)
        if bare and not contextual_amount:
            exact_sale_prices = [
                option for option in self.extract_sale_options(product)
                if option.get("option_type") == "voucher"
                and option.get("sale_price")
                and int(Decimal(str(option.get("sale_price")))) == int(target)
            ]
            exact_sale_prices = self._filter_options_for_day(exact_sale_prices, day)
            if exact_sale_prices:
                return {
                    "reply": "\n".join(
                        self._atomic_voucher_option_text(option) + "。"
                        for option in exact_sale_prices
                    ),
                    "kind": "sku_price", "decision": "allow",
                    "source": "当前SKU售价按整数部分匹配",
                }
        if discount_total:
            plan = self.consumption_plan_reply(
                product, target, available_options=options, include_final_cost=True,
            )
            if plan:
                return {
                    "reply": plan, "kind": "consumption_plan", "decision": "allow",
                    "source": "优惠后金额问法按当前SKU生成不超额凑单方案",
                }
        if not bare and any(self._option_total_value(option) == self._format_number(target) for option in options):
            return None  # Keep the existing atomic answer for an actual matching SKU.
        candidates = []
        rejected = {"inventory": False, "stack": False, "value_limit": False, "composition": False}
        global_limit = re.search(r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张", knowledge)
        value_limit = re.search(r"(?:一桌|每桌|单桌|每次)[^\n。；]{0,12}?(?:最多|上限)[^\n。；\d]{0,5}(\d+(?:\.\d+)?)\s*元", knowledge)
        mixed_face_forbidden = bool(re.search(
            r"(?:不同面额|跨面额|混合)[^。；\n]{0,12}"
            r"(?:不可|不能|不支持|不得)[^。；\n]{0,6}叠加|"
            r"(?:不可|不能|不支持|不得)[^。；\n]{0,12}"
            r"(?:不同面额|跨面额|混合)[^。；\n]{0,6}叠加",
            knowledge,
        ))
        unlimited_stacking = self._supports_unlimited_stacking(knowledge)
        same_face_stacking = unlimited_stacking or bool(re.search(r"(?:支持|允许|可|仅)[^。；\n]{0,12}同面额[^。；\n]{0,12}叠加", knowledge))
        forbidden = bool(re.search(
            r"不可叠加|不能叠加|不支持叠加|不得叠加", knowledge
        )) and not mixed_face_forbidden and not same_face_stacking
        for option in options:
            try:
                face = Decimal(self._option_total_value(option))
                price = Decimal(str(option.get("sale_price") or "0"))
                stock_text = str(option.get("stock") or "").strip()
                stock = Decimal(stock_text) if stock_text else None
                if face <= 0 or price <= 0 or target % face:
                    rejected["composition"] = True
                    continue
                quantity = int(target / face)
                delivered = sum(int(count) for _, count in re.findall(
                    r"(\d+(?:\.\d+)?)元券(\d+)张", str(option.get("composition") or "")
                )) or 1
                default_limit = quantity * delivered if same_face_stacking else delivered
                limit = int(Decimal(str(option.get("max_stack") or (global_limit.group(1) if global_limit else default_limit))))
                # A missing legacy stock field means that no explicit sell-out
                # state was supplied.  It must not be converted into numeric
                # zero: explicit zero remains unavailable, while an otherwise
                # active SKU can still be planned under its stacking limit.
                if stock is not None and (stock <= 0 or quantity > stock):
                    rejected["inventory"] = True
                    continue
                if quantity * delivered > limit:
                    rejected["stack"] = True
                    continue
                if forbidden and quantity * delivered > 1:
                    rejected["stack"] = True
                    continue
                if value_limit and target > Decimal(value_limit.group(1)):
                    rejected["value_limit"] = True
                    continue
                candidates.append((price * quantity, quantity, option))
            except (InvalidOperation, ValueError):
                continue
        amount = self._format_number(target)
        if not candidates:
            exact_unpriced = [
                option for option in options
                if self._option_total_value(option) == amount
                and self._is_sellable_option(option)
                and not str(option.get("sale_price") or "").strip()
            ]
            if len(exact_unpriced) == 1:
                option = exact_unpriced[0]
                name = str(
                    option.get("name") or option.get("sku_name") or f"{amount}元代金券"
                ).strip()
                return {
                    "reply": f"当前商品包含{name}，但该规格售价尚未同步，请先在后台补充售价后再报价。",
                    "kind": "sku_price", "decision": "allow",
                    "source": "当前真实SKU存在但售价待同步",
                    "query_context_update": {
                        "selected_sku_key": str(
                            option.get("sku_key") or self.sku_key_for_option(option)
                        ),
                        "selected_sku_name": name,
                    },
                }
            if not bare:
                return None
            catalog_totals = set()
            for option in options:
                try:
                    option_total = Decimal(str(self._option_total_value(option) or "0"))
                except (InvalidOperation, ValueError):
                    continue
                if option_total > 0:
                    catalog_totals.add(option_total)
            has_lower_catalog_choice = (
                len(catalog_totals) > 1
                and any(option_total < target for option_total in catalog_totals)
            )
            # An exact amount may exceed one SKU's stacking limit while a
            # smaller, fully valid SKU still exists.  Fall back to the closest
            # non-overpaying plan instead of rejecting the whole request.
            if (
                options
                and not any(rejected[key] for key in ("inventory", "value_limit"))
                and (not rejected["stack"] or has_lower_catalog_choice)
            ):
                plan = self.consumption_plan_reply(
                    product, target, available_options=options, include_final_cost=False,
                )
                if plan:
                    return {
                        "reply": plan, "kind": "consumption_plan", "decision": "allow",
                        "source": "裸数字未匹配SKU售价，按消费金额生成不超额凑单方案",
                    }
            if rejected["inventory"]:
                reason = f"当前可售库存不足，无法组成抵扣{amount}元的方案。"
            elif rejected["stack"]:
                reason = f"当前代金券的单次使用张数限制，无法组成抵扣{amount}元的方案。"
            elif rejected["value_limit"]:
                reason = f"当前单桌或单次抵扣上限不足{amount}元，无法按该金额抵扣。"
            else:
                reason = f"当前在售面额无法准确组成抵扣{amount}元的方案。"
            return {
                "reply": reason,
                "kind": "redemption_plan", "decision": "deny",
                "source": "当前SKU库存、适用日期、面额与叠加限制",
            }
        candidates.sort(key=lambda row: (
            row[0], row[1], str(row[2].get("name") or row[2].get("sku_name") or "")
        ))
        plans = []
        seen_plans = set()
        for total, quantity, option in candidates:
            name = str(option.get("name") or option.get("sku_name") or "代金券").strip()
            key = (normalize_text(name), quantity, self._format_number(total))
            if key in seen_plans:
                continue
            seen_plans.add(key)
            purchase = name if quantity == 1 else f"{name}×{quantity}"
            plans.append(
                f"{len(plans) + 1}. {purchase}（共支付{self._format_number(total)}元，"
                f"可抵扣{amount}元）"
            )
        if len(plans) > 1:
            return {
                "reply": f"需要抵扣{amount}元，可选组合方案：\n" + "\n".join(plans)
                + "\n请按所选平台和对应规格拍下，不同平台、不同面额不要混用。",
                "kind": "redemption_plan", "decision": "allow",
                "source": "当前门店可用SKU售价、面额与各自叠加上限",
            }
        total, quantity, option = min(candidates, key=lambda row: (row[1] != 1, row[0], row[1]))
        contents = self._purchase_contents_label(option, quantity)
        return {
            "reply": f"需要抵扣{amount}元的话，可以购买{contents}，共支付{self._format_number(total)}元，可抵扣{amount}元。",
            "kind": "redemption_plan", "decision": "allow",
            "source": "当前可售且日期适用的同一SKU及明确叠加限制",
            "query_context_update": {
                "selected_sku_key": str(option.get("sku_key") or self.sku_key_for_option(option)),
                "selected_sku_name": str(
                    option.get("name") or option.get("sku_name") or "商品规格"
                ),
            },
        }

    def consumption_plan_reply(
        self, product: Dict, target_value: object, missing_denomination: bool = False,
        available_options: Optional[List[Dict]] = None, include_final_cost: bool = False,
    ) -> str:
        """Recommend the closest real coupon plan not exceeding consumption."""
        try:
            target = Decimal(str(target_value))
        except InvalidOperation:
            return ""
        if target <= 0:
            return ""
        options = (
            list(available_options)
            if available_options is not None else self.extract_product_options(product)
        )
        exact = []
        for option in options:
            if not self._is_sellable_option(option):
                continue
            try:
                option_value = Decimal(str(self._option_total_value(option) or "0"))
                price = Decimal(str(option.get("sale_price") or "0"))
            except InvalidOperation:
                continue
            if option_value == target and price > 0:
                exact.append((price, option))
        if exact:
            price, option = min(exact, key=lambda value: value[0])
            option_value = Decimal(str(self._option_total_value(option) or "0"))
            contents = self._purchase_contents_label(option, 1)
            reply = (
                f"{self._format_number(target)}元消费的话，可以购买"
                f"{contents}，售价{self._format_number(price)}元，"
                f"可抵扣{self._format_number(option_value)}元。"
            )
            if include_final_cost:
                savings = target - price
                reply += (
                    f"合计实际支出{self._format_number(price)}元，"
                    f"共优惠{self._format_number(savings)}元。"
                )
            return reply

        candidates = []
        for option in options:
            try:
                option_value = Decimal(str(self._option_total_value(option) or "0"))
                price = Decimal(str(option.get("sale_price") or "0"))
            except (InvalidOperation, ValueError):
                continue
            maximum = self._option_stack_limit(product, option)
            if option_value <= 0 or price <= 0 or maximum <= 0:
                continue
            quantity = min(int(target // option_value), maximum)
            if quantity > 0:
                delivered_count = sum(count for _, count in self._option_coupon_pairs(option)) or 1
                candidates.append((
                    option_value * quantity, price * quantity, delivered_count * quantity,
                    quantity, option_value, price, maximum, option,
                ))
        if not candidates:
            if missing_denomination:
                if options:
                    return (
                        f"当前商品没有{self._format_number(target)}元代金券，"
                        "现有代金券中也没有不高于该金额的可选规格。"
                    )
                return (
                    f"当前商品没有{self._format_number(target)}元代金券。"
                    f"当前售卖的是{self._non_coupon_sale_description(product)}，"
                    "不属于代金券商品。"
                )
            return "当前商品没有不超过该消费金额的可用代金券。"
        covered, total_price, coupon_count, quantity, option_value, price, maximum, option = min(
            candidates, key=lambda row: (-row[0], row[1], row[2])
        )
        remainder = target - covered
        contents = self._purchase_contents_label(option, quantity)
        paragraphs = []
        if missing_denomination:
            paragraphs.append(f"当前商品没有{self._format_number(target)}元代金券。")
        price_phrase = (
            f"价格{self._format_number(total_price)}元"
            if coupon_count == 1 else f"共支付{self._format_number(total_price)}元"
        )
        paragraphs.append(
            f"{self._format_number(target)}元消费的话，可以购买{contents}，"
            f"{price_phrase}，可抵扣{self._format_number(covered)}元。"
        )
        if remainder > 0:
            paragraphs.append(f"剩余{self._format_number(remainder)}元到店自行支付。")
            explicit_limit = bool(str(option.get("max_stack") or "").strip())
            if quantity >= maximum:
                if explicit_limit:
                    paragraphs.append(f"当前同面额代金券每次最多使用{coupon_count}张。")
                else:
                    paragraphs.append("当前资料仅能确认以上使用方式，更多张能否同时使用暂未明确。")
        if include_final_cost:
            actual_total = total_price + remainder
            savings = target - actual_total
            paragraphs.append(
                f"合计实际支出{self._format_number(actual_total)}元，"
                f"共优惠{self._format_number(savings)}元。"
            )
        return "\n\n".join(paragraphs)

    def _calculated_denomination_reply(self, product: Dict, requested_value: object) -> str:
        """Calculate a requested denomination from a real same-face SKU.

        Exact SKUs always win in ``price_reply``. This fallback is only used
        when an integer number of one authoritative denomination can satisfy
        the request without exceeding its explicit stacking limit.
        """
        try:
            requested = Decimal(str(requested_value))
        except InvalidOperation:
            return ""
        if requested <= 0:
            return ""
        candidates = []
        for option in self.extract_product_options(product):
            try:
                face = Decimal(str(option.get("face_value") or "0"))
                price = Decimal(str(option.get("sale_price") or "0"))
            except InvalidOperation:
                continue
            if face <= 0 or price <= 0 or requested % face != 0:
                continue
            quantity = int(requested / face)
            if quantity <= 0:
                continue
            try:
                max_stack = int(Decimal(str(option.get("max_stack") or "0")))
            except (InvalidOperation, ValueError):
                max_stack = 0
            if not max_stack:
                knowledge = "\n".join((
                    str(product.get("raw_text") or ""),
                    str(product.get("ai_summary") or ""),
                ))
                limit = re.search(
                    r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张",
                    knowledge,
                )
                max_stack = int(limit.group(1)) if limit else 2
            if quantity <= max_stack:
                candidates.append((quantity, face, price, max_stack))
        if not candidates:
            return ""
        quantity, face, price, _ = min(candidates, key=lambda value: value[0])
        total = price * quantity
        return (
            f"{self._format_number(requested)}元需要购买{quantity}张"
            f"{self._format_number(face)}元代金券，共{self._format_number(total)}元，"
            f"最多可抵扣{self._format_number(requested)}元。"
        )

    def quantity_price_reply(self, product: Dict, message: str) -> str:
        """Calculate ordinary item quantity from authoritative unit-price text."""
        match = re.search(r"([一二两三四五六七八九十\d]+)\s*(条|份|只|张)\s*(?:一共|总共)?(?:多少(?:钱|元)|多少钱)", message)
        if not match:
            return ""
        chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        quantity = int(match.group(1)) if match.group(1).isdigit() else chinese.get(match.group(1), 0)
        if quantity <= 0:
            return ""
        unit = match.group(2)
        knowledge = str(product.get("raw_text") or "")
        unit_match = re.search(
            rf"一{re.escape(unit)}[^。；\n]{{0,12}}?(\d+(?:\.\d+)?)\s*(?:斤|公斤|克)[^。；\n]{{0,12}}?[：:]?\s*(\d+(?:\.\d+)?)\s*元",
            knowledge,
        )
        if not unit_match:
            return ""
        weight, unit_price = unit_match.groups()
        total = Decimal(unit_price) * quantity
        return f"{quantity}{unit}共{self._format_number(total)}元，每{unit}约{weight}斤。"

    def benefit_combination_reply(self, product: Dict, message: str) -> str:
        """Handle coupon/package/platform combinations before generic stacking."""
        text = str(message or "").strip()
        package_words = r"套餐|团购|单品|菜品|餐品|店内套餐"
        package_coupon_question = bool(
            re.search(package_words, text)
            and re.search(r"代金券|抵扣券|现金券|优惠券|券", text)
            and re.search(r"可以|能|可用|使用|抵扣|叠加|一起|同时|混用", text)
        )
        combine_intent = bool(re.search(
            r"一起用|同时用|一块用|一并使用|搭配使用|同时核销|一起抵扣|"
            r"叠加|叠券|叠优惠|混用|合并用|组合用|累计使用|还能用|还能叠|"
            r"能叠|可叠|再用|再叠|再减",
            text,
        )) or package_coupon_question
        if not combine_intent:
            return ""
        # “和朋友一起用” talks about people, not combining benefits.
        if re.search(r"(?:和|跟|同)\s*(?:朋友|家人|同事|对象|孩子|爸妈)\s*一起", text):
            return ""

        # Platform names are enough evidence when they appear with a combining
        # verb.  Requiring a trailing “券” caused “可以叠加抖音吗” to fall into
        # the same-denomination rule and produce an unrelated answer.
        platform_names = [name for name in ("抖音", "美团", "小程序") if name in text]
        if platform_names:
            # A platform plus a real denomination may identify this product's
            # own SKU, not an external coupon-combination question.
            requested_faces = {
                self._format_number(value)
                for value in re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?", text)
            }
            current_platform_sku = len(platform_names) == 1 and bool(requested_faces) and any(
                platform_names[0] in str(option.get("name") or "")
                and self._format_number(option.get("face_value") or "") in requested_faces
                for option in self.extract_product_options(product)
            )
            if not current_platform_sku:
                labels = "、".join(f"{name}券" for name in platform_names)
                return (
                    f"当前仅能确认本商品单独使用，不能与{labels}或其他优惠券混用，"
                    "也无法保证能和其他优惠一起使用哦。"
                )

        buyer_owned_external = bool(re.search(
            r"(?:我|手里|之前|已经|还有|另有|另外|店里送的|朋友给的)"
            r"[^。！？\n]{0,14}(?:有|买|领|拿|送)?[^。！？\n]{0,8}"
            r"(?:券|红包|满减|积分|会员|团购|折扣|优惠)",
            text,
        ))
        if buyer_owned_external:
            return (
                "当前仅能确认本商品单独使用，无法保证可以和您已有的其他券或优惠一起使用，"
                "请勿混用哦。"
            )

        options = self.extract_product_options(product)
        if re.search(package_words, text) and (options or re.search(r"代金券|券", text)):
            knowledge = self._product_rule_knowledge(product)
            explicitly_allowed = bool(re.search(
                r"(?:代金券|券)[^。；\n]{0,12}(?:可以|可|支持)[^。；\n]{0,8}(?:套餐|团购)|"
                r"(?:套餐|团购)[^。；\n]{0,12}(?:可以|可|支持)[^。；\n]{0,8}(?:代金券|券)",
                knowledge,
            )) and not bool(re.search(
                r"(?:代金券|券)[^。；\n]{0,12}(?:不可|不能|不支持)[^。；\n]{0,8}(?:套餐|团购)|"
                r"(?:套餐|团购)[^。；\n]{0,12}(?:不可|不能|不支持)[^。；\n]{0,8}(?:代金券|券)",
                knowledge,
            ))
            if explicitly_allowed:
                return "当前商品资料明确说明代金券可以和套餐一起使用哦。"
            return "代金券和套餐不能一起使用哦。"

        other_benefit = bool(re.search(
            r"(?:其他|别的|另外|已有|店里|店内|商家|会员|生日|平台|银行卡|信用卡)"
            r"[^。！？\n]{0,8}(?:优惠券|券|红包|满减|积分|折扣|优惠|团购)|"
            r"(?:会员券|生日券|店内券|银行卡优惠|信用卡优惠|红包|满减|积分|团购)"
            r"[^。！？\n]{0,8}(?:一起|同时|叠加|混用|还能|再用|再减)",
            text,
        ))
        if other_benefit:
            return (
                "当前仅能确认本商品单独使用，无法保证可以和其他券或优惠一起使用，"
                "请勿混用哦。"
            )
        return ""

    def coupon_quantity_limit_reply(self, product: Dict, message: str) -> str:
        """Explain delivered coupon units and the per-use cap without ambiguity."""
        text = str(message or "")
        if not re.search(
            r"一次(?:可以|能)?用(?:几|多少)张|(?:可以|能)用(?:几|多少)张|"
            r"(?:每次|一桌)?(?:最多)(?:可以|能)?用(?:几|多少)张|"
            r"每次用(?:几|多少)张|一桌用(?:几|多少)张|"
            r"一次(?:几|多少)张|最多(?:叠加|叠|用)?(?:几|多少)张|"
            r"(?:限|限制)(?:用)?(?:几|多少)张",
            text,
        ):
            return ""

        values = list(dict.fromkeys(re.findall(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?(?:代金券|券)", text
        )))
        if not values:
            bare_value = re.search(
                r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:元)?"
                r"(?=[^。！？]{0,12}(?:可以|能)用(?:几|多少)张)", text,
            )
            if bare_value:
                values = [bare_value.group(1)]
        options = self.extract_product_options(product)
        requested_platforms = [name for name in ("抖音", "美团", "小程序") if name in text]
        requested_values = {self._format_number(value) for value in values}
        candidates = []
        for option in options:
            face = self._format_number(option.get("face_value"))
            if requested_values and face not in requested_values:
                continue
            if requested_platforms and not any(
                platform in str(option.get("name") or "") for platform in requested_platforms
            ):
                continue
            candidates.append(option)
        if len(candidates) > 1 and requested_values and len({
            self._format_number(option.get("face_value") or "") for option in candidates
        }) == 1:
            limited = []
            for option in candidates:
                try:
                    limit = int(Decimal(str(option.get("max_stack") or "0")))
                except (InvalidOperation, ValueError):
                    limit = 0
                if limit > 0:
                    limited.append((str(option.get("name") or "当前规格"), limit))
            if len(limited) > 1:
                details = "；".join(
                    f"{name}每次最多使用{limit}张" for name, limit in limited
                )
                return f"{details}。请确认需要哪一种规格。"
        selected = candidates[0] if len(candidates) == 1 else None
        if not selected and len(options) == 1:
            selected = options[0]
        if not selected:
            return ""

        composition = str(selected.get("composition") or "")
        pairs = [
            (Decimal(amount), int(count))
            for amount, count in re.findall(r"(\d+(?:\.\d+)?)元券(\d+)张", composition)
        ]
        if not pairs:
            try:
                pairs = [(Decimal(str(selected.get("face_value") or "0")), 1)]
            except InvalidOperation:
                return ""
        unit_values = {amount for amount, _ in pairs if amount > 0}
        if len(unit_values) != 1:
            return ""
        unit_value = next(iter(unit_values))
        knowledge = "\n".join((
            str(product.get("raw_text") or ""),
            str(product.get("ai_summary") or ""),
            self._first_fact(
                ((product.get("structured") or {}).get("facts") or {}),
                ("使用规则", "使用条件", "核销规则", "限制", "usage", "conditions"),
            ),
        ))
        unit_label = re.escape(self._format_number(unit_value))
        unit_count_cap = re.search(
            rf"{unit_label}\s*元(?:代金)?券[^。；\n]{{0,12}}?"
            r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张",
            knowledge,
        )
        amount_cap = re.search(
            r"(?:一桌|每桌|单桌|每次|单次)[^。；\n]{0,12}?"
            r"(?:最多)?(?:可)?(?:代|抵(?:用|扣)?)\s*(\d+(?:\.\d+)?)\s*元",
            knowledge,
        )
        maximum = 0
        total_cap = Decimal("0")
        selected_face = self._format_number(selected.get("face_value") or "")
        selected_name = str(selected.get("name") or "")
        same_face_variants = [
            option for option in options
            if self._format_number(option.get("face_value") or "") == selected_face
        ]
        platform_specific_variant = (
            len(same_face_variants) > 1
            and any(platform in selected_name for platform in ("抖音", "美团", "小程序"))
        )
        if platform_specific_variant:
            try:
                maximum = int(Decimal(str(selected.get("max_stack") or "0")))
            except (InvalidOperation, ValueError):
                maximum = 0
        elif amount_cap and unit_value > 0:
            total_cap = Decimal(amount_cap.group(1))
            maximum = int(total_cap // unit_value)
        elif unit_count_cap:
            # A denomination-specific usage rule outranks a structured SKU field.
            # Summaries can mistake “每单限购1份” for “每次限用1张”, while one
            # sellable 200元规格 may actually deliver two stackable 100元 coupons.
            maximum = int(unit_count_cap.group(1))
        if maximum <= 0:
            try:
                maximum = int(Decimal(str(selected.get("max_stack") or "0")))
            except (InvalidOperation, ValueError):
                maximum = 0
        if maximum <= 0:
            count_cap = re.search(
                rf"{unit_label}\s*元(?:代金)?券[^。；\n]{{0,12}}?"
                r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张",
                knowledge,
            ) or re.search(
                r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张",
                knowledge,
            )
            maximum = int(count_cap.group(1)) if count_cap else 0
        if maximum <= 0:
            return ""
        if total_cap <= 0:
            total_cap = unit_value * maximum

        delivered = self._purchase_contents_label(selected)
        delivered_count = sum(count for _, count in pairs)
        option_total = self._option_total_value(selected)
        option_name = str(
            selected.get("name") or f"{self._format_number(option_total)}元规格"
        ).strip()
        option_price = self._format_number(selected.get("sale_price") or "")
        mixed = (
            "支持不同面额代金券一起使用"
            if re.search(
                r"(?:支持|可以|可)[^。；\n]{0,10}不同面额[^。；\n]{0,8}叠加|"
                r"不同面额[^。；\n]{0,10}(?:支持|可以|可)[^。；\n]{0,8}叠加|混合叠加",
                knowledge,
            )
            else "不同面额的券不能混用"
        )
        intro = option_name
        if option_price:
            intro += f"售价{option_price}元"
        intro += f"，购买后发放{delivered}。"
        if delivered_count > 1 and delivered_count <= maximum and option_total:
            usage = (
                f"这{delivered_count}张可以同一次使用，"
                f"合计抵扣{self._format_number(option_total)}元；"
            )
            cap = (
                f"每次最多使用{maximum}张"
                f"{self._format_number(unit_value)}元券"
            )
        else:
            usage = ""
            cap = (
                f"每次最多使用{maximum}张"
                f"{self._format_number(unit_value)}元券，共可抵扣"
                f"{self._format_number(total_cap)}元"
            )
        return (
            f"{intro}{usage}{cap}；{mixed}。"
        )

    def stacking_reply(self, product: Dict, message: str) -> str:
        if not re.search(
            r"一起用|同时用|叠加|混用|合并用|一次(?:可以|能)?用|"
            r"同时核销|可以用几张|能用几张|最多(?:可以|能)?用(?:几|多少)张|"
            r"一次(?:几|多少)张|最多(?:叠加|叠|用)?(?:几|多少)张|"
            r"(?:限|限制)(?:用)?(?:几|多少)张|"
            r"(?:可以|能)用[一二两三四五六七八九十\d]+张",
            message,
        ):
            return ""
        if not re.search(r"代金券|优惠券|券|面额|\d+(?:\.\d+)?|[一二两三四五六七八九十]\s*张|几张|多少张|叠加|混用", message):
            return ""
        quantity_reply = self.coupon_quantity_limit_reply(product, message)
        if quantity_reply:
            return quantity_reply
        knowledge = str(product.get("raw_text") or "")
        facts = (product.get("structured") or {}).get("facts") or {}
        stacking_fact = self._first_fact(
            facts,
            ("叠加规则", "代金券叠加", "使用张数", "最多使用", "stacking", "stack_rule"),
        )
        stack_evidence = f"{knowledge}\n{stacking_fact}"
        values = list(dict.fromkeys(re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*元", message)))
        if not values:
            values = list(dict.fromkeys(re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\s*张)", message)))
        different = len(values) >= 2 and len(set(values)) >= 2
        options = self.extract_product_options(product)
        requested_platforms = [name for name in ("抖音", "美团", "小程序") if name in message]
        if len(values) == 1:
            denomination = self._format_number(values[0])
            matching = [
                option for option in options
                if self._format_number(option.get("face_value") or "") == denomination
                and (not requested_platforms or any(
                    platform in str(option.get("name") or "")
                    for platform in requested_platforms
                ))
            ]
            limited = []
            for option in matching:
                try:
                    maximum = int(Decimal(str(option.get("max_stack") or "0")))
                except (InvalidOperation, ValueError):
                    maximum = 0
                if maximum > 0:
                    limited.append((option, maximum))
            if len(limited) > 1:
                details = "；".join(
                    f"{str(option.get('name') or '当前规格')}每次最多使用{maximum}张"
                    for option, maximum in limited
                )
                return f"{details}。请确认需要哪一种规格。"
            if len(limited) == 1:
                selected, maximum = limited[0]
                name = str(selected.get("name") or f"{denomination}元代金券")
                quantity_match = re.search(r"([一二两三四五六七八九十\d]+)\s*张", message)
                if quantity_match:
                    chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                    quantity = (int(quantity_match.group(1)) if quantity_match.group(1).isdigit()
                                else chinese.get(quantity_match.group(1), 0))
                    if quantity > maximum:
                        return f"{name}每次最多使用{maximum}张，超出的金额请在门店另行支付。"
                    total = Decimal(denomination) * quantity
                    return (
                        f"可以，{quantity}张{name}合计可抵扣{self._format_number(total)}元；"
                        f"每次最多使用{maximum}张。"
                    )
                return f"{name}可以叠加，每次最多使用{maximum}张；不同规格不能混用。"
        sku_limits = self._sku_stack_limits(options)
        denies_mixed = bool(re.search(
            r"不同面额[^。；\n]{0,12}(?:不可|不能|不支持|禁止)[^。；\n]{0,8}(?:叠加|混用|一起)|"
            r"(?:不可|不能|不支持|禁止)[^。；\n]{0,8}不同面额",
            knowledge,
        ))
        allows_mixed = not denies_mixed and bool(re.search(
            r"(?:支持|可以|可)[^。；\n]{0,10}不同面额[^。；\n]{0,8}叠加|不同面额[^。；\n]{0,10}(?:可以|可|支持)[^。；\n]{0,8}叠加|混合叠加",
            knowledge,
        ))
        unlimited_stack = self._supports_unlimited_stacking(stack_evidence)
        max_match = re.search(r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张", knowledge)
        max_text = f"，每次最多使用{max_match.group(1)}张" if max_match else ""
        if different and not allows_mixed:
            mentioned_limits = [
                f"{value}元代金券最多使用{sku_limits[value]}张"
                for value in values if value in sku_limits
            ]
            detail = "；" + "；".join(mentioned_limits) if mentioned_limits else max_text
            return f"{values[0]}元和{values[1]}元属于不同面额，不能一起使用；当前仅支持同面额代金券叠加{detail}。"
        if allows_mixed:
            if unlimited_stack and not max_match:
                return "可以，同面额及不同面额代金券均可叠加使用，不限制使用张数。"
            return f"支持不同面额代金券一起叠加使用{max_text}。"
        denomination = values[0] if values else ""
        if not denomination and len(set(sku_limits.values())) > 1:
            details = "；".join(
                f"{face}元代金券最多使用{count}张"
                for face, count in sku_limits.items()
            )
            return f"不同面额的叠加上限不同：{details}；不同面额不能混用。"
        maximum = sku_limits.get(denomination)
        if maximum or max_match:
            maximum = maximum or int(max_match.group(1))
            quantity_match = re.search(r"([一二两三四五六七八九十\d]+)\s*张", message)
            if quantity_match:
                chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                quantity = (int(quantity_match.group(1)) if quantity_match.group(1).isdigit()
                            else chinese.get(quantity_match.group(1), 0))
                if quantity > maximum:
                    return f"同面额代金券每次最多使用{maximum}张，超出的金额请在门店另行支付。"
                if denomination:
                    total = Decimal(denomination) * quantity
                    return (
                        f"可以，{quantity}张{self._format_number(denomination)}元代金券"
                        f"合计可抵扣{self._format_number(total)}元；每次最多使用{maximum}张。"
                    )
                return f"可以，每次最多使用{maximum}张同一规格代金券。"
            return f"{denomination + '元代金券' if denomination else '同面额代金券'}可以叠加，每次最多使用{maximum}张；不同面额不能混用。"
        if unlimited_stack:
            target = f"{self._format_number(denomination)}元代金券" if denomination else "同面额代金券"
            return f"可以，{target}支持叠加使用，不限制使用张数；不同面额不能混用。"
        return "当前商品资料暂未明确说明每次可以使用几张，暂时无法准确确认叠加数量。"

    @classmethod
    def _raw_use_time_text(cls, raw_text: str) -> str:
        """Return an explicit use-time section before any AI-generated default."""
        match = re.search(
            r"【使用时间】\s*(.*?)(?=\n\s*【|\Z)", str(raw_text or ""), re.S,
        )
        if not match:
            return ""
        text = "；".join(
            line.strip() for line in match.group(1).splitlines() if line.strip()
        ).strip(" \t\r\n。；;")
        wrapped = re.fullmatch(r"[（(]\s*(.*?)\s*[）)]", text)
        if wrapped:
            text = wrapped.group(1).strip()
        if re.search(
            r"工作日|平日|周末|节假日|法定假日|周[一二三四五六日天]|"
            r"早餐|午餐|晚餐|午市|晚市|下午茶|\d{1,2}[:：]\d{2}",
            text,
        ):
            return text
        return ""

    @classmethod
    def _use_time_text(cls, product: Dict) -> str:
        facts = (product.get("structured") or {}).get("facts") or {}
        raw_text = str(product.get("raw_text") or "")
        raw_use_time = cls._raw_use_time_text(raw_text)
        if raw_use_time:
            return raw_use_time
        use_time = cls._first_fact(
            facts, ("使用时间", "可用时间", "营业时间", "使用日期", "time")
        )
        unavailable = cls._first_fact(
            facts,
            ("不可用日期", "禁用日期", "不适用日期", "不可使用日期", "blackout_dates"),
        )
        combined = raw_text + "\n" + use_time + "\n" + unavailable
        for clause in re.split(r"[。；;\r\n]+", raw_text):
            clause = re.sub(
                r"^(?:[①②③④⑤⑥⑦⑧⑨⑩]|\d+[.、)）])\s*", "", clause.strip()
            )
            if re.search(r"^除.+外[，,]?.*(?:可用|使用)", clause):
                return clause.rstrip("。；; ").replace("营业时间可用", "适用门店营业时间内可用")
        has_unavailable = bool(re.search(
            r"(?:\d{4}年)?\d{1,2}月\d{1,2}日?.{0,24}(?:不可用|不能用|不适用)|"
            r"(?:中秋|国庆|春节|元旦|劳动节|节假日).{0,20}(?:不可用|不能用|不适用)",
            combined,
        ))
        cleaned = use_time.rstrip("。；; ")
        if unavailable and normalize_match_text(unavailable) not in normalize_match_text(cleaned):
            if cleaned and not re.search(r"(?:其余|其他|除.+外).{0,12}(?:营业时间|可用)", cleaned):
                return (
                    unavailable.rstrip("。；; ")
                    + "；除上述明确不可用日期外，适用门店营业时间内可用"
                )
            if not cleaned:
                return (
                    unavailable.rstrip("。；; ")
                    + "；除上述明确不可用日期外，适用门店营业时间内可用"
                )
        if cleaned:
            if has_unavailable and not re.search(r"(?:其余|其他|除.+外).{0,12}(?:营业时间|可用)", cleaned):
                clauses = [part.strip() for part in re.split(r"[；;。\n]+", cleaned) if part.strip()]
                restrictions = [part for part in clauses if re.search(r"不可用|不能用|不适用", part)]
                if restrictions:
                    restrictions = [re.sub(
                        r"^(?:适用门店)?营业时间内?可用[，,：:]*", "", part
                    ).strip() for part in restrictions]
                    detail = "；".join(part for part in restrictions if part)
                    return detail + "；除上述明确不可用日期外，适用门店营业时间内可用"
                return cleaned + "；除上述明确不可用日期外，适用门店营业时间内可用"
            return cleaned.replace("营业时间可用", "适用门店营业时间内可用")
        # Product policy: absent an explicit weekday, holiday, date or meal
        # restriction, the coupon follows each applicable store's own hours.
        return "适用门店营业时间内可用"

    @classmethod
    def _use_rule_text(cls, product: Dict, concise: bool = False) -> str:
        facts = (product.get("structured") or {}).get("facts") or {}
        use_rule = cls._first_fact(
            facts,
            ("使用规则", "使用条件", "核销规则", "限制", "usage", "conditions"),
        )
        items = [
            value.strip(" \t。；;，,、")
            for value in re.split(r"[\n。；;，,、]+", use_rule)
            if value.strip(" \t。；;，,、")
        ]
        if not concise:
            return "；".join(items)
        preferred = []
        markers = ("堂食", "包间", "打包", "优惠", "预约", "等位", "找零")
        for item in items:
            if any(marker in item for marker in markers):
                preferred.append(item)
            if len(preferred) >= 3:
                break
        return "；".join(preferred or items[:3])

    @classmethod
    def _sku_stack_limits(cls, options: List[Dict]) -> Dict[str, int]:
        limits = {}
        for option in options:
            face = cls._format_number(option.get("face_value") or "")
            match = re.match(r"\d+", str(option.get("max_stack") or "").strip())
            if not face or not match:
                continue
            count = int(match.group())
            if count > 0:
                limits[face] = max(limits.get(face, 0), count)
        return limits

    @staticmethod
    def _supports_unlimited_stacking(text: str) -> bool:
        """Recognize explicit unlimited or positive no-number stack rules."""
        evidence = str(text or "")
        clauses = [part.strip() for part in re.split(r"[。；;\n]+", evidence) if part.strip()]
        unlimited = re.compile(
            r"无限叠加|不限(?:制)?(?:使用|叠加)?(?:数量|张数)|"
            r"不限制(?:使用|叠加)?(?:数量|张数)|(?:使用|叠加)(?:数量|张数)不限(?:制)?"
        )
        bare_positive = re.compile(
            r"(?:支持|允许|可以|可|仅支持)[^。；\n]{0,12}(?:同面额)?[^。；\n]{0,8}叠加(?:使用)?"
            r"(?!\s*\d+\s*(?:张|个))|同面额[^。；\n]{0,8}(?:可以|可|支持)叠加(?:使用)?"
            r"(?!\s*\d+\s*(?:张|个))"
        )
        for clause in clauses:
            if re.search(
                r"不可(?:无限)?叠加|不能(?:无限)?叠加|不支持(?:无限)?叠加|"
                r"禁止(?:无限)?叠加|并非无限|不是无限",
                clause,
            ):
                continue
            # “可叠加2个单品优惠” is a benefit rule, not coupon quantity.
            if re.search(r"叠加\s*\d+\s*个(?:单品|店内|其他)?优惠", clause):
                continue
            if unlimited.search(clause) or bare_positive.search(clause):
                return True
        return False

    @classmethod
    def _stack_rule(cls, facts: Dict, raw_text: str, options: List[Dict]) -> str:
        explicit = cls._first_fact(
            facts,
            ("叠加规则", "代金券叠加", "使用张数", "最多使用", "stacking", "stack_rule"),
        )
        combined = f"{explicit}\n{raw_text}"
        match = re.search(
            r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张",
            combined,
        )
        if not match:
            match = next((
                re.match(r"\d+", str(option.get("max_stack") or ""))
                for option in options if str(option.get("max_stack") or "").strip()
            ), None)
        allows_mixed = any(re.search(pattern, raw_text) for pattern in (
            r"不同面额[^。；\n]{0,12}(?:支持|可以|可)[^。；\n]{0,6}叠加",
            r"(?:支持|可以|可)[^。；\n]{0,8}不同面额[^。；\n]{0,6}叠加",
            r"(?:混合|跨面额)叠加",
        ))
        denies_mixed = bool(re.search(
            r"(?:不同面额[^。；\n]{0,12}(?:不能|不可|不支持|禁止)[^。；\n]{0,8}叠加|"
            r"(?:不能|不可|不支持|禁止)[^。；\n]{0,12}不同面额[^。；\n]{0,8}叠加)",
            raw_text,
        ))
        if denies_mixed:
            allows_mixed = False
        sku_limits = cls._sku_stack_limits(options)
        limited_options = []
        for option in options:
            face = cls._format_number(option.get("face_value") or "")
            match_limit = re.match(r"\d+", str(option.get("max_stack") or "").strip())
            if face and match_limit and int(match_limit.group()) > 0:
                limited_options.append((
                    str(option.get("name") or f"{face}元代金券"),
                    face,
                    int(match_limit.group()),
                ))
        duplicate_faces = len({face for _, face, _ in limited_options}) < len(limited_options)
        if duplicate_faces and len({count for _, _, count in limited_options}) > 1:
            mode = "支持不同规格代金券叠加" if allows_mixed else "仅支持同一规格代金券叠加"
            details = "；".join(
                f"{name}最多使用{count}张" for name, _, count in limited_options
            )
            return f"{mode}。{details}。"
        if len(set(sku_limits.values())) > 1:
            mode = "支持不同面额代金券叠加" if allows_mixed else "仅支持同面额代金券叠加"
            details = "；".join(
                f"{face}元代金券最多使用{count}张"
                for face, count in sku_limits.items()
            )
            return f"{mode}。{details}。"
        if match:
            count = match.group(1) if match.lastindex else match.group()
            if allows_mixed:
                return f"支持同面额或不同面额代金券叠加，每次最多使用{count}张。"
            return f"仅支持同面额代金券叠加，每次最多使用{count}张。"
        if allows_mixed:
            if cls._supports_unlimited_stacking(combined):
                return "支持同面额及不同面额代金券互相叠加，不限制使用张数。"
            return "支持同面额及不同面额代金券互相叠加。"
        if cls._supports_unlimited_stacking(combined):
            return "仅支持同面额代金券叠加，不限制使用张数。"
        # Never inherit an AI-expanded mixed-denomination claim unless the
        # authoritative user/page text says so explicitly.
        return "仅支持同面额代金券叠加。" if options else ""

    @classmethod
    def _sku_profile_summary(cls, structured: Dict) -> str:
        """Render one self-contained archive per SKU, inheriting common rules by default."""
        profiles = structured.get("sku_profiles") if isinstance(structured, dict) else []
        if not isinstance(profiles, list):
            return ""
        common = structured.get("common_rules") if isinstance(structured, dict) else {}
        common = common if isinstance(common, dict) else {}
        labels = {
            "brand": "品牌", "product_name": "商品名称", "validity": "有效期",
            "available_dates": "可用日期", "unavailable_dates": "不可用日期",
            "applicable_stores": "适用门店", "applicable_regions": "适用地区",
            "applicable_day": "适用日期", "meal_period": "餐段", "use_hours": "使用时段",
            "applicable_time": "适用时间", "audience": "适用人群",
            "height_age_rule": "身高/年龄", "people_count": "人数",
            "stack_rule": "叠加规则", "max_stack": "最多使用张数",
            "mixed_denomination": "不同面额混用", "benefit_combination": "优惠同享",
            "reservation": "预约", "waiting": "等位", "dine_in": "堂食",
            "takeout": "外带", "delivery": "外卖", "usage_scope": "适用范围",
            "excluded_items": "不适用项目", "extra_fees": "额外收费",
            "delivery_platform": "发券平台", "delivery_method": "发券方式",
            "claim_method": "领取方式", "redeem_method": "核销方式",
            "refund_rule": "退款规则", "invoice_rule": "发票规则",
            "purchase_limit": "购买限制", "usage_limit": "使用限制", "notes": "其他说明",
            "package_contents": "套餐内容", "minimum_spend": "最低消费",
            "change_cash_rule": "找零/兑现金", "per_table_limit": "每桌限用",
            "per_order_limit": "每单限用", "daily_limit": "每日限用",
            "usage_frequency": "使用次数", "advance_booking": "提前预约",
            "holiday_policy": "节假日规则", "branch_price_difference": "门店差价",
            "substitution_rule": "替换规则", "expiry_rule": "过期规则",
        }
        ignored = {
            "sku_key", "sku_name", "name", "option_type", "sale_price", "face_value",
            "composition", "stock", "sellable", "source_evidence", "inherited_common_fields",
        }
        blocks = []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            name = str(profile.get("sku_name") or profile.get("name") or "商品规格").strip()
            core = []
            if str(profile.get("sale_price") or "").strip():
                core.append(f"售价{cls._format_number(profile.get('sale_price'))}元")
            if str(profile.get("face_value") or "").strip():
                core.append(f"面额{cls._format_number(profile.get('face_value'))}元")
            if str(profile.get("composition") or "").strip():
                core.append(f"发券{profile.get('composition')}")
            if str(profile.get("stock") or "").strip():
                core.append(f"库存{profile.get('stock')}")
            if profile.get("sellable") is False:
                core.append("当前不可售")
            merged = dict(common)
            merged.update({
                key: value for key, value in profile.items()
                if value not in (None, "", [], {})
            })
            details = []
            for key, label in labels.items():
                if key in ignored:
                    continue
                value = merged.get(key)
                text = cls._fact_value_text(value)
                if text:
                    details.append(f"{label}：{text.rstrip('。；; ')}")
            for key, value in merged.items():
                if key in ignored or key in labels:
                    continue
                text = cls._fact_value_text(value)
                if text:
                    details.append(f"{key}：{text.rstrip('。；; ')}")
            body = [f"〔{name}〕"]
            if core:
                body.append("；".join(core))
            body.extend(details)
            blocks.append("\n".join(body))
        return "\n\n".join(blocks)

    def build_knowledge_summary(self, product: Dict) -> str:
        facts = (product.get("structured") or {}).get("facts") or {}
        options = self.extract_product_options(product)
        lines = [self.format_product_option(option) for option in options]
        sections = []
        if lines:
            sections.append("【商品规格】\n" + "\n".join(lines))
        combination_summary = self._purchase_combination_summary(product, options)
        if combination_summary:
            sections.append("【组合购买方案】\n" + combination_summary)
        profile_summary = self._sku_profile_summary(product.get("structured") or {})
        if profile_summary:
            sections.append("【逐SKU规则档案】\n" + profile_summary)
        stack_rule = self._stack_rule(facts, str(product.get("raw_text") or ""), options)
        if stack_rule:
            sections.append("【叠加规则】\n" + stack_rule)
        # Preserve a free-form AI summary when it could not be split into the
        # known sections; the business-hours default supplements it and must
        # never replace unrelated product knowledge.
        if not sections:
            original = str(product.get("raw_text") or "").strip()
            if original:
                sections.append(original)
        use_time = self._use_time_text(product)
        if use_time:
            sections.append("【使用时间】\n" + use_time.rstrip("。；; ") + "。")
        validity = self._first_fact(facts, ("有效期", "券有效期", "validity", "valid_until"))
        if validity:
            sections.append("【有效期】\n" + validity.rstrip("。；; ") + "。")

        handled = {
            "使用时间", "可用时间", "营业时间", "使用日期", "time",
            "不可用日期", "禁用日期", "不适用日期", "不可使用日期", "blackout_dates",
            "有效期", "券有效期", "validity", "valid_until",
            "叠加规则", "代金券叠加", "使用张数", "最多使用", "stacking", "stack_rule",
        }
        fact_groups = (
            ("【适用范围】", (
                "适用门店范围", "适用门店", "门店范围", "适用范围", "适用人群", "人数限制",
            )),
            ("【使用规则】", (
                "使用规则", "使用条件", "核销规则", "限制", "usage", "conditions",
                "预约要求", "堂食限制", "外带限制", "外卖限制", "包间限制", "酒水限制",
                "锅底限制", "服务费限制", "优惠同享", "不同面额混用", "单次或每桌限用数量",
            )),
            ("【发券与核销】", ("发码平台", "发码方式", "领取方式", "核销方式")),
            ("【退款与发票】", ("退款规则", "发票规则")),
            ("【提醒】", ("下单前提醒",)),
        )
        for title, aliases in fact_groups:
            group_lines, used = self._fact_section_lines(facts, aliases)
            handled.update(used)
            if group_lines:
                sections.append(title + "\n" + "\n".join(group_lines))

        remaining_lines = []
        if isinstance(facts, dict):
            for key, value in facts.items():
                if key in handled:
                    continue
                text = self._fact_value_text(value)
                if text:
                    remaining_lines.append(f"{key}：{text.rstrip('。；; ')}。")
        if remaining_lines:
            sections.append("【其他信息】\n" + "\n".join(remaining_lines))

        source_rules = self._uncovered_source_rules(
            str(product.get("raw_text") or ""), "\n".join(sections)
        )
        grouped_rules, source_rules = self._group_uncovered_source_rules(source_rules)
        for title, rule_lines in grouped_rules.items():
            if rule_lines:
                sections.append(title + "\n" + "\n".join(rule_lines))
        if source_rules:
            sections.append("【原文规则保留】\n" + "\n".join(source_rules))
        return "\n\n".join(sections)

    def build_first_reply_text(self, product: Dict) -> str:
        """Build a short editable reply only from authoritative product knowledge."""
        facts = (product.get("structured") or {}).get("facts") or {}
        raw_text = str(product.get("raw_text") or "")
        title = re.sub(r"【[^】]*】|\[[^\]]*\]", "", str(product.get("title") or "")).strip()
        title = re.sub(r"^(?:自动发货|全国|现货|秒发)[\s·|丨+-]*", "", title, flags=re.I).strip()

        brand = self.extract_brand(product)
        coupon_type = str(product.get("coupon_type") or "").strip()
        if coupon_type == "promotion":
            subject = title or brand or "当前商品"
            sections = [
                f"您好，您咨询的是{subject}。",
                "本商品仅通过推广渠道购买，请不要在闲鱼页面下单或付款。",
            ]
            links = self.promotion_links(product)
            qr_asset = self.promotion_qr_asset(str(product.get("item_id") or ""))
            purchase_lines = []
            if links:
                purchase_lines.append("请使用微信打开以下推广链接购买：")
                purchase_lines.extend(links)
            if qr_asset:
                purchase_lines.append("也可以使用微信扫描下方二维码，进入推广页面选择商品后付款。")
                purchase_lines.append(f"{{$图片:{qr_asset['id']}}}")
            if not purchase_lines:
                purchase_lines.append("当前推广链接和二维码尚未配置，请暂时不要下单，等待卖家补充购买入口。")
            sections.append("【购买方式】\n" + "\n".join(purchase_lines))
            return "\n".join(sections)
        coupon_phrase = {
            "meituan": "美团电子券",
            "douyin": "抖音电子券",
            "merchant_miniapp": "商家小程序电子券",
            "electronic_code": "电子券码",
            "other": self.coupon_type_display(product) or "电子券码",
        }.get(coupon_type, "电子券码")
        if coupon_type == "mixed":
            greeting = f"您好，以下是{brand + '品牌' if brand else ''}当前商品的规格和使用信息。"
        else:
            greeting = (
                f"您好，当前商品为{brand}品牌{coupon_phrase}。"
                if brand else f"您好，当前商品为{coupon_phrase}。"
            )

        options = self.extract_product_options(product)
        product_lines = [self.format_product_option(option, brand) for option in options]
        if not product_lines:
            product_name = self._first_fact(facts, ("商品名", "商品名称", "券名称", "规格", "sku")) or title
            price = self._first_fact(facts, ("价格", "售价", "price", "销售价格"))
            max_count = self._first_fact(
                facts,
                ("最多使用张数", "每桌最多使用", "限用数量", "最多使用", "max_per_table"),
            )
            if not max_count:
                limit_match = re.search(r"(?:每桌)?(?:最多|限)(?:使用)?\s*(\d+)\s*张", raw_text)
                max_count = limit_match.group(1) if limit_match else ""
            if product_name:
                if brand and normalize_text(brand) not in normalize_text(product_name):
                    product_name = brand + product_name
                if price:
                    price = price if re.search(r"元|以.*为准", price) else f"{price}元"
                    product_lines.append(f"{product_name}：售价{price}")
                else:
                    product_lines.append(product_name)

        use_time = self._use_time_text(product)

        stack_rule = self._stack_rule(facts, raw_text, options)

        use_rule = self._use_rule_text(product, concise=True)

        sections = [greeting]
        if product_lines:
            sections.append("【商品信息】\n" + "\n".join(product_lines))
        rule_parts = []
        if stack_rule:
            rule_parts.append(stack_rule.rstrip("。；; ") + "。")
        if use_time:
            rule_parts.append(use_time.rstrip("。；; ") + "。")
        if use_rule:
            rule_parts.append(use_rule.rstrip("。；; ") + "。")
        if rule_parts:
            sections.append("【使用规则】\n" + "\n".join(rule_parts))
        delivery_text = self.coupon_usage_instructions(product)
        if delivery_text:
            sections.append("【发券方式】\n" + delivery_text.rstrip("。；; ") + "。")
        reminder_source = "\n".join((
            raw_text,
            str(product.get("custom_policy_summary") or product.get("custom_policy_raw") or ""),
            str(product.get("ai_summary") or ""),
        ))
        reminders = []
        if re.search(r"当天(?:购买|下单).{0,12}当天(?:使用|核销)|当天使用", reminder_source):
            reminders.append("请按商品规则在购买当天使用")
        if re.search(r"过期.{0,8}(?:不退|不退款|不补|不补发)", reminder_source):
            reminders.append("过期后的处理按当前商品退款政策执行")
        if reminders:
            sections.append("【提醒】\n" + "；".join(reminders) + "。")
        return "\n".join(sections)

    def refresh_first_reply(self, item_id: str, force: bool = False) -> Dict:
        product = self.get_v2_product(item_id)
        if not product:
            raise ValueError("商品不存在")
        if product.get("first_reply_manual") and not force:
            return product
        text = self.build_first_reply_text(product)
        with self._connect() as conn:
            conn.execute(
                """UPDATE v2_products SET first_reply_text=?,first_reply_manual=0,
                   first_reply_generated_at=?,first_reply_template_version=?,updated_at=? WHERE item_id=?""",
                (text, self._now(), FIRST_REPLY_TEMPLATE_VERSION, self._now(), item_id),
            )
        return self.get_v2_product(item_id)

    def set_product_enabled(self, item_id: str, enabled: bool) -> Dict:
        item_id = str(item_id or "").strip()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE v2_products SET enabled=?,updated_at=? WHERE item_id=?",
                (int(bool(enabled)), self._now(), item_id),
            )
            if not cur.rowcount:
                raise ValueError("商品不存在")
        return self.get_v2_product(item_id)

    def get_v2_product(self, item_id: str) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM v2_products WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["image_urls"] = json.loads(result.pop("image_urls_json") or "[]")
        except json.JSONDecodeError:
            result["image_urls"] = []
            result.pop("image_urls_json", None)
        try:
            result["structured"] = json.loads(result.pop("structured_json") or "{}")
        except json.JSONDecodeError:
            result["structured"] = {}
            result.pop("structured_json", None)
        try:
            result["ai_draft_structured"] = json.loads(
                result.pop("ai_draft_structured_json") or "{}"
            )
        except json.JSONDecodeError:
            result["ai_draft_structured"] = {}
            result.pop("ai_draft_structured_json", None)
        try:
            result["source_update"] = json.loads(result.pop("source_update_json") or "{}")
        except json.JSONDecodeError:
            result["source_update"] = {}
            result.pop("source_update_json", None)
        result["store_lists"] = self.bound_store_lists(item_id)
        result["skus"] = self.list_product_skus(item_id, result)
        result["time_rules"] = self.list_time_rules(item_id)
        result["image_assets"] = self.list_image_assets(item_id)
        return result

    def list_v2_products(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM v2_products ORDER BY updated_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["image_urls"] = json.loads(item.pop("image_urls_json") or "[]")
            except json.JSONDecodeError:
                item["image_urls"] = []
                item.pop("image_urls_json", None)
            try:
                item["structured"] = json.loads(item.pop("structured_json") or "{}")
            except json.JSONDecodeError:
                item["structured"] = {}
                item.pop("structured_json", None)
            try:
                item["ai_draft_structured"] = json.loads(
                    item.pop("ai_draft_structured_json") or "{}"
                )
            except json.JSONDecodeError:
                item["ai_draft_structured"] = {}
                item.pop("ai_draft_structured_json", None)
            try:
                item["source_update"] = json.loads(item.pop("source_update_json") or "{}")
            except json.JSONDecodeError:
                item["source_update"] = {}
                item.pop("source_update_json", None)
            item["store_lists"] = self.bound_store_lists(item["item_id"])
            item["skus"] = self.list_product_skus(item["item_id"], item)
            item["time_rules"] = self.list_time_rules(item["item_id"])
            item["image_assets"] = self.list_image_assets(item["item_id"])
            result.append(item)
        return result

    def save_image_asset(self, payload: Dict) -> Dict:
        item_id = str(payload.get("item_id") or "").strip()
        name = str(payload.get("name") or "").strip()
        file_value = str(payload.get("file_path") or "").strip()
        file_path = os.path.abspath(file_value) if file_value else ""
        if not item_id:
            raise ValueError("请先选择商品")
        if not name:
            raise ValueError("请填写图片名称")
        if file_path and not os.path.isfile(file_path):
            raise FileNotFoundError("关键词规则图片文件不存在")
        raw_words = payload.get("trigger_words") or []
        if isinstance(raw_words, str):
            raw_words = re.split(r"[\n,，;；]+", raw_words)
        words = list(dict.fromkeys(str(value).strip() for value in raw_words if str(value).strip()))
        if not words:
            raise ValueError("请至少填写一个触发词")
        now = self._now()
        asset_id = payload.get("id")
        with self._connect() as conn:
            if asset_id:
                conn.execute(
                    """UPDATE product_image_assets SET item_id=?,name=?,purpose=?,
                       trigger_words_json=?,reply_text=?,file_path=?,original_name=?,
                       enabled=?,updated_at=? WHERE id=?""",
                    (
                        item_id, name, str(payload.get("purpose") or "").strip(),
                        json.dumps(words, ensure_ascii=False),
                        str(payload.get("reply_text") or "").strip(), file_path,
                        str(payload.get("original_name") or os.path.basename(file_path)),
                        int(bool(payload.get("enabled", True))), now, int(asset_id),
                    ),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO product_image_assets(
                       item_id,name,purpose,trigger_words_json,reply_text,file_path,
                       original_name,enabled,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item_id, name, str(payload.get("purpose") or "").strip(),
                        json.dumps(words, ensure_ascii=False),
                        str(payload.get("reply_text") or "").strip(), file_path,
                        str(payload.get("original_name") or os.path.basename(file_path)),
                        int(bool(payload.get("enabled", True))), now, now,
                    ),
                )
                asset_id = cur.lastrowid
        self.add_event("image_asset_saved", f"保存套餐图片：{name}", {"item_id": item_id, "asset_id": asset_id})
        return self.get_image_asset(int(asset_id))

    def get_image_asset(self, asset_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM product_image_assets WHERE id=?", (int(asset_id),)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["trigger_words"] = json.loads(item.pop("trigger_words_json") or "[]")
        except json.JSONDecodeError:
            item["trigger_words"] = []
            item.pop("trigger_words_json", None)
        return item

    def list_image_assets(self, item_id: str) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM product_image_assets WHERE item_id=? ORDER BY id DESC",
                (str(item_id),),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            try:
                item["trigger_words"] = json.loads(item.pop("trigger_words_json") or "[]")
            except json.JSONDecodeError:
                item["trigger_words"] = []
                item.pop("trigger_words_json", None)
            output.append(item)
        return output

    def promotion_qr_asset(self, item_id: str) -> Optional[Dict]:
        """Return the enabled QR asset reserved for promotion-mode sales."""
        return next((
            asset for asset in self.list_image_assets(str(item_id or ""))
            if str(asset.get("purpose") or "") == PROMOTION_QR_PURPOSE
            and bool(asset.get("enabled")) and str(asset.get("file_path") or "").strip()
        ), None)

    def promotion_purchase_reply(self, product: Dict) -> Dict:
        links = self.promotion_links(product)
        qr_asset = self.promotion_qr_asset(str(product.get("item_id") or ""))
        lines = ["这款商品请不要在闲鱼页面下单或付款，仅通过卖家提供的推广渠道购买。"]
        if links:
            lines.append("请使用微信打开以下推广链接：\n" + "\n".join(links))
        if qr_asset:
            lines.append("也可以使用微信扫描我发送的二维码，进入推广页面选择商品并付款。")
        if not links and not qr_asset:
            lines.append("当前推广链接和二维码尚未配置，请暂时不要下单，等待卖家补充购买入口。")
        else:
            lines.append("商品规格和实际支付金额请以推广购买页面展示为准。")
        return {
            "reply": "\n\n".join(lines),
            "source": "人工设置的推广购买流程",
            "decision": "allow",
            "kind": "promotion_purchase",
            **({"image_asset_id": qr_asset["id"]} if qr_asset else {}),
        }

    def delete_image_asset(self, asset_id: int) -> Optional[Dict]:
        asset = self.get_image_asset(asset_id)
        if not asset:
            return None
        with self._connect() as conn:
            conn.execute("DELETE FROM product_image_assets WHERE id=?", (int(asset_id),))
        self.add_event("image_asset_deleted", f"删除套餐图片：{asset['name']}", {"asset_id": int(asset_id)})
        return asset

    def resolve_image_asset(self, item_id: str, message: str, scope_id: str) -> Optional[Dict]:
        """Match only the current product. Clear keyword hits auto-send; fuzzy hits require review."""
        message_text = str(message or "").strip()
        message_norm = normalize_text(message_text)
        if not message_norm:
            return None
        candidates = []
        fuzzy = []
        for asset in self.list_image_assets(item_id):
            if str(asset.get("purpose") or "") == PROMOTION_QR_PURPOSE:
                continue
            if not asset.get("enabled"):
                continue
            words = [word for word in asset.get("trigger_words", []) if str(word).strip()]
            normalized_words = [normalize_text(word) for word in words if normalize_text(word)]
            exact_hits = [word for word in normalized_words if word in message_norm]
            if exact_hits:
                candidates.append((max(len(word) for word in exact_hits), asset))
                continue
            searchable = normalized_words + [normalize_text(asset.get("name")), normalize_text(asset.get("purpose"))]
            score = max((SequenceMatcher(None, message_norm, value).ratio() for value in searchable if value), default=0)
            if score >= 0.58 and any(word in message_text for word in ("图", "图片", "套餐", "菜单", "看看", "发我")):
                fuzzy.append((score, asset))
        if candidates:
            candidates.sort(key=lambda value: value[0], reverse=True)
            top_score = candidates[0][0]
            top = [asset for score, asset in candidates if score == top_score]
            if len(top) > 1:
                return {"status": "review", "asset": top[0], "reason": "命中多张套餐图片，需要人工确认"}
            asset = top[0]
            cutoff = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
            with self._connect() as conn:
                recent = conn.execute(
                    """SELECT id FROM image_send_log WHERE asset_id=? AND scope_id=?
                       AND status='sent' AND created_at>=? LIMIT 1""",
                    (asset["id"], scope_id, cutoff),
                ).fetchone()
            if recent:
                return {"status": "cooldown", "asset": asset, "reason": "同一会话30分钟内不重复发送同一图片"}
            return {"status": "allow", "asset": asset, "reason": "明确命中套餐图片触发词"}
        if fuzzy:
            fuzzy.sort(key=lambda value: value[0], reverse=True)
            return {"status": "review", "asset": fuzzy[0][1], "reason": "套餐图片为模糊匹配，需要人工确认"}
        return None

    def record_image_send(
        self, asset_id: int, item_id: str, scope_id: str, chat_id: str,
        user_id: str, status: str, remote_url: str = "", error: str = "",
    ):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO image_send_log(
                   asset_id,item_id,scope_id,chat_id,user_id,status,remote_url,error,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    int(asset_id), str(item_id), str(scope_id), str(chat_id), str(user_id),
                    str(status), str(remote_url), str(error), self._now(),
                ),
            )
        self.add_event(
            "image_sent" if status == "sent" else "image_send_error",
            f"套餐图片{('已发送' if status == 'sent' else '发送失败')}：{asset_id}",
            {"asset_id": int(asset_id), "item_id": item_id, "scope_id": scope_id, "error": error},
        )

    def list_versions(self, item_id: str) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_versions WHERE item_id=? ORDER BY id DESC LIMIT 30",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def restore_version(self, item_id: str, version_id: int) -> Dict:
        """Enable an old snapshot by creating a new, auditable version."""
        with self._connect() as conn:
            product = conn.execute(
                "SELECT * FROM v2_products WHERE item_id=?", (item_id,)
            ).fetchone()
            version = conn.execute(
                "SELECT * FROM knowledge_versions WHERE id=? AND item_id=?",
                (int(version_id), item_id),
            ).fetchone()
            if not product or not version:
                raise ValueError("知识库历史版本不存在")
            now = self._now()
            first_reply = str(version["first_reply_text"] or "").strip()
            conn.execute(
                """UPDATE v2_products SET raw_text=?,ai_summary=?,structured_json=?,
                   first_reply_text=CASE WHEN ?<>'' THEN ? ELSE first_reply_text END,
                   first_reply_enabled=?,first_reply_manual=CASE WHEN ?<>'' THEN 1 ELSE 0 END,
                   manual_edited=1,sync_status='manual_edited',updated_at=? WHERE item_id=?""",
                (
                    version["raw_text"], version["ai_summary"], version["structured_json"],
                    first_reply, first_reply, int(version["first_reply_enabled"]),
                    first_reply, now, item_id,
                ),
            )
            conn.execute(
                """INSERT INTO knowledge_versions(
                   item_id,raw_text,ai_summary,structured_json,note,
                   first_reply_text,first_reply_enabled,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    item_id, version["raw_text"], version["ai_summary"],
                    version["structured_json"], f"启用历史版本 #{version_id}",
                    first_reply, int(version["first_reply_enabled"]), now,
                ),
            )
        self.save_product(item_id, product["title"], version["raw_text"], bool(product["enabled"]))
        if not first_reply:
            self.refresh_first_reply(item_id, force=True)
        self.add_event(
            "knowledge_version_restored", f"商品 {item_id} 已启用历史版本 #{version_id}",
            {"item_id": item_id, "version_id": int(version_id)},
        )
        return self.get_v2_product(item_id)

    @staticmethod
    def _knowledge_query_hints(query: str) -> List[str]:
        """Expand short buyer questions into vocabulary used by the knowledge base."""
        value = normalize_text(query).lower()
        groups = [
            (("多少钱", "价格", "售价", "便宜", "怎么买", "面额"),
             ("商品规格", "价格", "售价", "面额", "代金券", "发", "sku")),
            (("叠加", "几张", "两张", "一起用", "同时用", "能用几"),
             ("叠加规则", "叠加", "最多", "同面额", "不同面额", "张")),
            (("时间", "今天", "明天", "周末", "工作日", "节假日", "几点", "营业", "有效期", "过期"),
             ("使用时间", "营业时间", "工作日", "周末", "节假日", "有效期", "过期", "当天")),
            (("门店", "哪里", "哪家", "地址", "可以用", "能用吗"),
             ("适用门店", "门店", "城市", "地址", "分店", "可用")),
            (("退款", "退货", "退换", "不能用", "用不了", "售后", "不想要"),
             ("退款", "退换货", "售后", "质量问题", "手续费", "仅退款", "72")),
            (("怎么发", "发码", "券码", "核销", "怎么领取", "哪里看", "链接", "自动发货"),
             ("发券方式", "发货", "券码", "核销", "领取", "链接", "使用方式")),
            (("怎么用", "规则", "堂食", "外卖", "打包", "包间", "预约", "优惠"),
             ("使用规则", "堂食", "外卖", "打包", "包间", "预约", "优惠")),
            (("多重", "重量", "几斤", "多少斤", "几人", "套餐", "包含", "有什么"),
             ("商品规格", "重量", "斤", "人餐", "套餐", "包含", "菜品")),
        ]
        hints: List[str] = []
        for triggers, expansions in groups:
            if any(trigger in value for trigger in triggers):
                hints.extend(expansions)
        hints.extend(re.findall(r"\d+(?:\.\d+)?", value))
        # Longer literal fragments help with brands, dishes and store names.
        hints.extend(re.findall(r"[\u4e00-\u9fff]{2,8}", value))
        return list(dict.fromkeys(hint for hint in hints if hint))

    @classmethod
    def _trim_knowledge_source(
        cls, text: str, query: str, max_chars: int, fallback_chars: int = 500
    ) -> str:
        """Return only blocks related to the question, preserving their source order."""
        source = str(text or "").strip()
        if not source or not query:
            return source
        hints = cls._knowledge_query_hints(query)
        if not hints:
            return "未找到与当前问题直接相关的段落。"

        # Keep headings attached to their content. Blank lines and new headings are
        # natural boundaries in both manual knowledge and AI summaries.
        blocks = [
            block.strip()
            for block in re.split(r"\n\s*\n|(?=【[^】\n]{1,30}】)", source)
            if block.strip()
        ]
        selected: List[str] = []
        used = 0
        for block in blocks:
            lowered = block.lower()
            score = sum(1 for hint in hints if hint.lower() in lowered)
            if not score:
                continue
            candidate = block
            # Raw platform JSON can be one enormous block. Keep windows around
            # matching fields instead of forwarding the complete payload.
            if len(candidate) > max_chars:
                windows: List[str] = []
                for hint in hints:
                    start = 0
                    while True:
                        pos = lowered.find(hint.lower(), start)
                        if pos < 0:
                            break
                        left = max(0, pos - 180)
                        right = min(len(candidate), pos + len(hint) + 360)
                        snippet = candidate[left:right].strip(" ,\n")
                        if snippet and snippet not in windows:
                            windows.append(snippet)
                        start = pos + len(hint)
                        if sum(len(value) for value in windows) >= max_chars:
                            break
                    if sum(len(value) for value in windows) >= max_chars:
                        break
                candidate = "\n…\n".join(windows) or candidate[:fallback_chars]
            remaining = max_chars - used
            if remaining <= 0:
                break
            candidate = candidate[:remaining]
            selected.append(candidate)
            used += len(candidate)
        return "\n\n".join(selected) if selected else "未找到与当前问题直接相关的段落。"

    def build_merged_knowledge(
        self, item_id: str, platform_summary: str, query: str = ""
    ) -> str:
        product = self.get_v2_product(item_id)
        if not product:
            return super().build_merged_knowledge(item_id, platform_summary)
        global_rules = self.get_policies().get("global_system_prompt", "")
        platform_source = product.get("platform_summary") or platform_summary
        aftersale_policy = self.effective_aftersale_policy(item_id)
        raw_source = self._trim_knowledge_source(product.get("raw_text"), query, 1800)
        ai_source = self._trim_knowledge_source(product.get("ai_summary"), query, 1400)
        platform_source = self._trim_knowledge_source(platform_source, query, 800)
        aftersale_hints = ("退款", "退货", "退换", "不能用", "用不了", "售后", "发货", "发码", "券码", "过期")
        include_aftersale = not query or any(word in normalize_text(query) for word in aftersale_hints)
        aftersale_source = (
            self._trim_knowledge_source(aftersale_policy, query, 900)
            if include_aftersale else "本问题未涉及发货或售后，已省略。"
        )
        return (
            f"【最高规则（不可被后续内容覆盖）】\n{global_rules}\n"
            "【强制信息优先级】1.用户原始资料；2.AI归纳摘要；3.闲鱼页面资料。"
            "发生冲突时必须使用更高优先级信息。没有明确资料时必须说暂未确认，禁止猜测。\n"
            "【推荐限制】只允许回答和推荐当前商品，禁止推荐任何其他商品或卡券。\n"
            "【知识裁剪说明】以下仅保留与买家当前问题相关的资料段落，资料优先级不变。\n"
            f"【当前商品ID】{item_id}\n"
            f"【当前商品名称】{product['title'] or '未命名'}\n"
            f"【用户原始资料（最高优先级）】\n{raw_source or '未配置'}\n"
            f"【AI归纳摘要（仅帮助阅读，不得覆盖原始资料）】\n{ai_source or '未归纳'}\n"
            f"【当前商品适用的发货与退款政策】\n{aftersale_source or '未配置'}\n"
            f"【闲鱼页面资料（只补充缺失项）】\n{platform_source or '未获取'}"
        )

    @staticmethod
    def _read_rows(path: str) -> Iterable[Dict]:
        def canonical_headers(values) -> List[str]:
            headers = [classify_store_header(value) for value in values]
            normalized = [normalize_store_header(value) for value in values]
            # Merchant sheets often use 门店名称 for the brand and 分店名 for
            # the physical branch. The sibling column removes that ambiguity.
            if "分店名" in normalized or "分店名称" in normalized:
                headers = [
                    "brand" if name == "门店名称" else field
                    for name, field in zip(normalized, headers)
                ]
            return headers

        def rows_from_values(rows) -> Iterable[Dict]:
            headers = None
            for row_number, values in enumerate(rows, start=1):
                values = tuple(values or ())
                if headers is None:
                    candidate = canonical_headers(values)
                    recognized = {value for value in candidate if value}
                    if (
                        recognized.intersection({"brand", "branch"})
                        and len(recognized) >= 2
                    ):
                        headers = candidate
                        continue
                    if row_number >= 50:
                        return
                    continue
                item = {}
                for field, value in zip(headers, values):
                    if field and value is not None and str(value).strip() and not item.get(field):
                        item[field] = value
                if item:
                    yield item

        extension = os.path.splitext(path)[1].lower()
        if extension in {".xlsx", ".xlsm"}:
            try:
                from openpyxl import load_workbook
            except ImportError as exc:
                raise RuntimeError("缺少Excel读取组件，请重新安装第二版依赖") from exc
            book = load_workbook(path, read_only=True, data_only=True)
            try:
                for sheet in book.worksheets:
                    # Some merchant exports incorrectly declare the used range as A1
                    # although sheetData contains hundreds of cells. Resetting makes
                    # openpyxl stream the real rows instead of silently reading one cell.
                    if hasattr(sheet, "reset_dimensions"):
                        sheet.reset_dimensions()
                    yield from rows_from_values(sheet.iter_rows(values_only=True))
            finally:
                book.close()
            return
        if extension == ".csv":
            last_error = None
            for encoding in ("utf-8-sig", "gb18030"):
                try:
                    with open(path, "r", encoding=encoding, newline="") as handle:
                        yield from rows_from_values(csv.reader(handle))
                    return
                except UnicodeDecodeError as exc:
                    last_error = exc
            if last_error:
                raise ValueError("CSV编码无法识别，请另存为UTF-8或GB18030") from last_error
            return
        raise ValueError("仅支持 .xlsx、.xlsm、.csv 或 .txt 文件")

    @staticmethod
    def parse_store_text(text: str) -> Dict:
        """Parse province/city bracket text without guessing missing geography."""
        text = str(text or "").replace("\ufeff", "").strip()
        if not text:
            raise ValueError("请粘贴可用门店文本")
        province = ""
        city = ""
        rows = []
        warnings = []
        province_markers = ("省", "自治区", "特别行政区")
        municipalities = {"北京", "北京市", "上海", "上海市", "天津", "天津市", "重庆", "重庆市"}
        city_provinces = {
            "武汉": "湖北省", "黄石": "湖北省", "十堰": "湖北省", "宜昌": "湖北省", "襄阳": "湖北省", "荆州": "湖北省", "荆门": "湖北省", "孝感": "湖北省", "鄂州": "湖北省",
            "苏州": "江苏省", "南京": "江苏省", "无锡": "江苏省", "常州": "江苏省", "南通": "江苏省", "扬州": "江苏省", "徐州": "江苏省",
            "南昌": "江西省", "九江": "江西省", "赣州": "江西省", "上饶": "江西省", "安庆": "安徽省", "合肥": "安徽省",
            "深圳": "广东省", "广州": "广东省", "佛山": "广东省", "东莞": "广东省", "珠海": "广东省", "成都": "四川省",
            "厦门": "福建省", "泉州": "福建省", "福州": "福建省", "长沙": "湖南省", "郑州": "河南省", "贵阳": "贵州省", "三亚": "海南省", "大连": "辽宁省",
            "北京": "北京市", "上海": "上海市", "天津": "天津市", "重庆": "重庆市",
        }
        ignored_labels = {"适用门店", "可用门店", "门店列表", "商品信息", "使用规则", "使用时间", "叠加规则"}
        declared = re.search(r"全国\s*(\d+)\s*城[^\n]{0,20}?共\s*(\d+)\s*家", text)
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            match = re.match(r"^【([^】]+)】\s*(.*)$", line)
            label = match.group(1).strip() if match else ""
            payload = match.group(2).strip() if match else line
            if label:
                if label.endswith(province_markers) or label in municipalities:
                    province = label
                    city = label.rstrip("市") if label in municipalities else ""
                    if not payload:
                        continue
                elif label in ignored_labels:
                    city = ""
                    continue
                else:
                    city = label.rstrip("市")
                    province = city_provinces.get(city, "")
            if not payload:
                continue
            names = [
                value.strip(" \t，,、；;。")
                for value in re.split(r"[、,，;；]+", payload)
                if value.strip(" \t，,、；;。")
            ]
            if not city:
                warnings.append(f"第{line_number}行未识别城市：{line[:30]}")
            for name in names:
                # Do not treat explanatory prose as an invented store.
                if len(name) > 80 or re.search(r"全国\s*\d+城|共\s*\d+家|可用门店", name):
                    warnings.append(f"第{line_number}行内容过长，已跳过")
                    continue
                rows.append({
                    "brand": "",
                    "branch": name,
                    "province": province,
                    "city": city,
                    "district": "",
                    "address": "",
                    "phone": "",
                    "business_hours": "",
                })
        if not rows:
            raise ValueError("文本中没有识别到门店，请使用【省份】【城市】门店1、门店2的格式")
        rows = V2Store._normalize_store_records(rows)
        provinces = sorted({row["province"] for row in rows if row["province"]})
        cities = sorted({row["city"] for row in rows if row["city"]})
        count_mismatch = False
        if declared:
            expected_cities, expected_stores = map(int, declared.groups())
            count_mismatch = expected_cities != len(cities) or expected_stores != len(rows)
            if count_mismatch:
                warnings.append(f"原文声明{expected_cities}城{expected_stores}家，实际只解析到{len(cities)}城{len(rows)}家，已阻止直接替换")
        return {
            "records": rows,
            "store_count": len(rows),
            "province_count": len(provinces),
            "city_count": len(cities),
            "provinces": provinces,
            "cities": cities,
            "warnings": list(dict.fromkeys(warnings)),
            "count_mismatch": count_mismatch,
        }

    @staticmethod
    def _normalize_store_records(rows: Iterable[Dict]) -> List[Dict]:
        records = []
        seen = set()
        for row in rows:
            record = {key: str(row.get(key) or "").strip() for key in STORE_COLUMN_ALIASES}
            if not record["branch"] and not record["brand"]:
                continue
            if V2Store._looks_like_product_title(record["branch"]):
                continue
            fingerprint = tuple(
                normalize_text(record[key])
                for key in (
                    "brand", "branch", "province", "city", "district",
                    "address", "phone", "business_hours",
                )
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            record["normalized"] = normalize_text(" ".join(record.values()))
            records.append(record)
        return records

    def _save_store_records(
        self, records: Iterable[Dict], name: str, source_file: str, item_ids: List[str],
        replace_item_bindings: bool = False,
    ) -> Dict:
        records = self._normalize_store_records(records)
        if not records:
            raise ValueError("没有识别到可导入的门店")
        now = self._now()
        list_name = str(name or "").strip() or os.path.splitext(source_file)[0] or "可用门店"
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO store_lists(name,source_file,store_count,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (list_name, source_file, len(records), now, now),
            )
            list_id = cursor.lastrowid
            conn.executemany(
                """INSERT INTO stores(
                    list_id,brand,branch,province,city,district,address,phone,business_hours,normalized
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [(
                    list_id, item["brand"], item["branch"], item["province"], item["city"],
                    item["district"], item["address"], item["phone"],
                    item["business_hours"], item["normalized"],
                ) for item in records],
            )
            for item_id in {str(value).strip() for value in item_ids if str(value).strip()}:
                if replace_item_bindings:
                    conn.execute("DELETE FROM product_store_lists WHERE item_id=?", (item_id,))
                conn.execute(
                    "INSERT OR IGNORE INTO product_store_lists(item_id,list_id) VALUES(?,?)",
                    (item_id, list_id),
                )
        self.add_event("store_import", f"导入门店表：{list_name}", {"count": len(records)})
        return {"id": list_id, "name": list_name, "store_count": len(records)}

    def preview_store_text(self, text: str) -> Dict:
        parsed = self.parse_store_text(text)
        preview = []
        grouped = {}
        for row in parsed["records"]:
            key = (row.get("province") or "未标注省份", row.get("city") or "未标注城市")
            grouped.setdefault(key, []).append(row["branch"])
        for (province, city), names in grouped.items():
            preview.append({"province": province, "city": city, "stores": list(dict.fromkeys(names))})
        return {key: value for key, value in parsed.items() if key != "records"} | {"preview": preview}

    def import_store_text(self, text: str, name: str, item_ids: List[str], source_file: str = "粘贴文本.txt", replace_item_bindings: bool = False) -> Dict:
        parsed = self.parse_store_text(text)
        if parsed.get("count_mismatch"):
            raise ValueError("；".join(parsed["warnings"]))
        result = self._save_store_records(parsed["records"], name, source_file, item_ids, replace_item_bindings)
        result["warnings"] = parsed["warnings"]
        result["province_count"] = parsed["province_count"]
        result["city_count"] = parsed["city_count"]
        return result

    def sync_platform_store_list(
        self, item_id: str, title: str, description: str, structured: Optional[Dict] = None
    ) -> Optional[Dict]:
        """Replace only the store list derived from the Xianyu page; manual lists stay untouched."""
        item_id = str(item_id or "").strip()
        source_file = f"闲鱼页面自动提取:{item_id}"
        records = []
        structured = structured if isinstance(structured, dict) else {}
        if re.search(r"【[^】]+】", str(description or "")):
            try:
                parsed = self.parse_store_text(description)
                if parsed.get("count_mismatch"):
                    return None
                records = parsed["records"]
            except ValueError:
                records = []
        raw_stores = structured.get("stores") or structured.get("适用门店") or []
        if isinstance(raw_stores, dict):
            raw_stores = list(raw_stores.values())
        if not records and isinstance(raw_stores, list):
            for entry in raw_stores:
                if isinstance(entry, str):
                    records.append({"branch": entry})
                elif isinstance(entry, dict):
                    records.append({
                        "brand": entry.get("brand") or entry.get("品牌") or "",
                        "branch": entry.get("branch") or entry.get("门店") or entry.get("门店名") or "",
                        "province": entry.get("province") or entry.get("省") or "",
                        "city": entry.get("city") or entry.get("市") or "",
                        "district": entry.get("district") or entry.get("区县") or "",
                        "address": entry.get("address") or entry.get("地址") or "",
                        "phone": entry.get("phone") or entry.get("电话") or "",
                        "business_hours": entry.get("business_hours") or entry.get("营业时间") or "",
                    })
        records = self._normalize_store_records(records)
        with self._connect() as conn:
            old_ids = [row["id"] for row in conn.execute(
                "SELECT id FROM store_lists WHERE source_file=?", (source_file,)
            ).fetchall()]
            for list_id in old_ids:
                conn.execute("DELETE FROM product_store_lists WHERE list_id=?", (list_id,))
                conn.execute("DELETE FROM product_sku_store_lists WHERE list_id=?", (list_id,))
                conn.execute("DELETE FROM stores WHERE list_id=?", (list_id,))
                conn.execute("DELETE FROM store_lists WHERE id=?", (list_id,))
        if not records:
            return None
        return self._save_store_records(
            records, f"{title or item_id}｜闲鱼页面门店", source_file, [item_id]
        )

    def delete_store_list(self, list_id: int) -> Dict:
        list_id = int(list_id)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM store_lists WHERE id=?", (list_id,)).fetchone()
            if not row:
                raise ValueError("门店表不存在")
            affected = conn.execute(
                "SELECT COUNT(*) AS count FROM product_store_lists WHERE list_id=?", (list_id,)
            ).fetchone()["count"]
            affected_skus = conn.execute(
                "SELECT COUNT(*) AS count FROM product_sku_store_lists WHERE list_id=?", (list_id,)
            ).fetchone()["count"]
            conn.execute("DELETE FROM product_store_lists WHERE list_id=?", (list_id,))
            conn.execute("DELETE FROM product_sku_store_lists WHERE list_id=?", (list_id,))
            conn.execute("DELETE FROM stores WHERE list_id=?", (list_id,))
            conn.execute("DELETE FROM store_lists WHERE id=?", (list_id,))
        self.add_event(
            "store_list_deleted", f"删除门店表：{row['name']}",
            {"list_id": list_id, "affected_products": affected, "affected_skus": affected_skus},
        )
        return {"id": list_id, "name": row["name"], "affected_products": affected,
                "affected_skus": affected_skus}

    def import_store_list(self, path: str, name: str, item_ids: List[str], replace_item_bindings: bool = False) -> Dict:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            filename = os.path.basename(path) or "未选择文件"
            raise FileNotFoundError(f"未找到门店表格：{filename}，请重新选择后导入")
        if os.path.splitext(path)[1].lower() == ".txt":
            last_error = None
            for encoding in ("utf-8-sig", "gb18030"):
                try:
                    with open(path, "r", encoding=encoding) as handle:
                        return self.import_store_text(
                            handle.read(), name, item_ids, os.path.basename(path), replace_item_bindings
                        )
                except UnicodeDecodeError as exc:
                    last_error = exc
            raise ValueError("TXT编码无法识别，请另存为UTF-8或GB18030") from last_error
        records = self._normalize_store_records(self._read_rows(path))
        if not records:
            raise ValueError("表格中没有识别到门店，请检查表头是否包含店名或分店名")
        return self._save_store_records(records, name, os.path.basename(path), item_ids, replace_item_bindings)

    def list_store_lists(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT sl.*, GROUP_CONCAT(psl.item_id) AS item_ids
                   FROM store_lists sl
                   LEFT JOIN product_store_lists psl ON psl.list_id=sl.id
                   GROUP BY sl.id ORDER BY sl.id DESC"""
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["item_ids"] = [value for value in (item.pop("item_ids") or "").split(",") if value]
            with self._connect() as conn:
                item["sku_binding_count"] = int(conn.execute(
                    "SELECT COUNT(*) FROM product_sku_store_lists WHERE list_id=?", (item["id"],)
                ).fetchone()[0])
            output.append(item)
        return output

    def bound_store_lists(self, item_id: str) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT sl.* FROM store_lists sl
                   JOIN product_store_lists psl ON psl.list_id=sl.id
                   WHERE psl.item_id=? ORDER BY sl.id DESC""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @classmethod
    def sku_key_for_option(cls, option: Dict) -> str:
        source_id = str(option.get("sku_id") or option.get("id") or option.get("option_id") or "").strip()
        if source_id:
            return f"source:{source_id}"
        identity = "|".join((
            str(option.get("option_type") or ""), normalize_text(option.get("name") or "").lower(),
            ",".join(map(str, option.get("people_counts") or [])),
            ",".join(option.get("audience_types") or []), ",".join(option.get("day_types") or []),
            ",".join(option.get("meal_periods") or []),
        ))
        return "local:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

    @classmethod
    def _configuration_voucher_options(cls, product: Dict) -> List[Dict]:
        """Expose corroborated voucher names for store binding before prices sync."""
        structured = product.get("structured") or {}
        if not structured and product.get("structured_json"):
            try:
                structured = json.loads(product.get("structured_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                structured = {}
        if not isinstance(structured, dict):
            return []
        records = []
        for key in ("sku_profiles", "products", "product_options", "skus", "sku", "商品规格", "商品列表", "规格"):
            value = structured.get(key)
            if isinstance(value, list):
                records.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                records.extend(item for item in value.values() if isinstance(item, dict))
        raw_text = normalize_text(product.get("raw_text") or "")
        output = []
        for record in records:
            option = cls._normalize_product_option(record)
            if not option:
                continue
            name = str(option.get("name") or "")
            face = cls._format_number(option.get("face_value") or "")
            platforms = [value for value in ("美团", "抖音", "小程序") if value in name]
            corroborated = normalize_text(name) in raw_text
            if not corroborated and face:
                corroborated = any(
                    platform in raw_text and re.search(
                        rf"{re.escape(platform)}[^\n。；]{{0,12}}(?<!\d){re.escape(face)}(?:\.0+)?\s*(?:元|代金券|券)",
                        raw_text,
                    )
                    for platform in platforms
                )
            if not corroborated:
                continue
            if platforms and face:
                option["sku_id"] = f"platform:{platforms[0]}:{face}"
            option.update({
                "option_type": "voucher",
                "people_counts": cls._people_counts(name),
                "day_types": cls._option_day_types_from_fields(
                    name, option.get("applicable_time"),
                ),
                "meal_periods": cls._option_meal_periods_from_fields(
                    name, option.get("applicable_time"),
                ),
                "audience_types": cls._audience_types(
                    f"{name} {option.get('applicable_time') or ''}"
                ),
                "price_pending": not bool(option.get("sale_price")),
            })
            output.append(option)
        return output

    def _store_count_for_lists(self, list_ids: List[int]) -> int:
        ids = sorted({int(value) for value in list_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            return int(conn.execute(
                f"SELECT COUNT(*) FROM stores WHERE list_id IN ({placeholders})", ids
            ).fetchone()[0])

    def list_product_skus(self, item_id: str, product: Optional[Dict] = None) -> List[Dict]:
        if product is None:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM v2_products WHERE item_id=?", (item_id,)).fetchone()
            if not row:
                return []
            product = dict(row)
            try:
                product["structured"] = json.loads(product.get("structured_json") or "{}")
            except json.JSONDecodeError:
                product["structured"] = {}
        # The per-SKU store editor is configuration, not semantic Q&A.  Seed it
        # from the marketplace SKU payload first so it works before AI knowledge
        # has been generated or re-summarized.
        options = self._platform_configuration_options(product)
        known_keys = {self.sku_key_for_option(option) for option in options}
        for option in self.extract_sale_options(product):
            key = self.sku_key_for_option(option)
            same = next((
                existing for existing in options
                if self.sku_key_for_option(existing) == key
                or (
                    normalize_text(existing.get("name") or "").lower()
                    == normalize_text(option.get("name") or "").lower()
                    and str(existing.get("sale_price") or "")
                    == str(option.get("sale_price") or "")
                )
            ), None)
            if same:
                for field in (
                    "face_value", "sale_price", "composition", "max_stack", "option_type",
                    "people_counts", "audience_types", "day_types", "meal_periods",
                ):
                    if field == "max_stack" and same.get("_platform_sku"):
                        continue
                    if same.get(field) in (None, "", []):
                        same[field] = option.get(field)
                continue
            if key not in known_keys:
                options.append(option)
                known_keys.add(key)
        for option in self._configuration_voucher_options(product):
            key = self.sku_key_for_option(option)
            same = next((
                existing for existing in options
                if normalize_text(existing.get("name") or "").lower()
                == normalize_text(option.get("name") or "").lower()
                and str(existing.get("sale_price") or "")
                == str(option.get("sale_price") or "")
            ), None)
            if same is None:
                face = self._format_number(option.get("face_value") or "")
                platforms = {
                    value for value in ("美团", "抖音", "小程序")
                    if value in str(option.get("name") or "")
                }
                compatible = [
                    existing for existing in options
                    if face
                    and self._format_number(existing.get("face_value") or "") == face
                    and (
                        not platforms
                        or platforms.intersection({
                            value for value in ("美团", "抖音", "小程序")
                            if value in str(existing.get("name") or "")
                        })
                    )
                ]
                same = compatible[0] if len(compatible) == 1 else None
            if same:
                for field in ("face_value", "composition", "max_stack"):
                    if field == "max_stack" and same.get("_platform_sku"):
                        continue
                    if not same.get(field) and option.get(field):
                        same[field] = option[field]
                continue
            if key not in known_keys:
                options.append(option)
                known_keys.add(key)
        with self._connect() as conn:
            rules = {row["sku_key"]: dict(row) for row in conn.execute(
                "SELECT * FROM product_sku_store_rules WHERE item_id=?", (item_id,)
            ).fetchall()}
            bindings = {}
            for row in conn.execute(
                "SELECT sku_key,list_id FROM product_sku_store_lists WHERE item_id=?", (item_id,)
            ).fetchall():
                bindings.setdefault(row["sku_key"], []).append(int(row["list_id"]))
        default_ids = [int(row["id"]) for row in self.bound_store_lists(item_id)]
        output = []
        for option in options:
            key = self.sku_key_for_option(option)
            legacy_option = dict(option)
            legacy_option.pop("sku_id", None)
            legacy_key = self.sku_key_for_option(legacy_option)
            legacy_keys = [legacy_key]
            face = self._format_number(option.get("face_value") or "")
            platforms = [
                value for value in ("美团", "抖音", "小程序")
                if value in str(option.get("name") or "")
            ]
            if platforms and face:
                legacy_keys.append(f"source:platform:{platforms[0]}:{face}")
            if key not in rules and key not in bindings:
                inherited_key = next((
                    candidate for candidate in legacy_keys
                    if candidate in rules or candidate in bindings
                ), "")
                if inherited_key:
                    key = inherited_key
            rule = rules.get(key) or {}
            source_sale_price = str(option.get("sale_price") or "").strip()
            sale_price_override = str(rule.get("sale_price_override") or "").strip()
            effective_sale_price = sale_price_override or source_sale_price
            mode = rule.get("mode") if rule.get("mode") in {"inherit", "custom"} else "inherit"
            rule_status = str(rule.get("status") or "active").strip().lower()
            option_available = str(option.get("availability") or "available") == "available"
            sellable = option_available and rule_status not in {
                "inactive", "disabled", "deleted", "offline", "off_shelf", "下架", "售罄",
            }
            own_ids = bindings.get(key, [])
            effective_ids = own_ids if mode == "custom" else default_ids
            output.append({
                "sku_key": key, "sku_name": str(option.get("name") or "商品规格"),
                "sale_price": effective_sale_price,
                "source_sale_price": source_sale_price,
                "sale_price_override": sale_price_override,
                "face_value": str(option.get("face_value") or ""),
                "composition": str(option.get("composition") or ""),
                "max_stack": str(option.get("max_stack") or ""),
                "option_type": str(option.get("option_type") or ""),
                "people_counts": option.get("people_counts") or [],
                "audience_types": option.get("audience_types") or [],
                "day_types": option.get("day_types") or [], "meal_periods": option.get("meal_periods") or [],
                "mode": mode, "status": rule.get("status") or "active",
                "availability": option.get("availability") or "available",
                "stock": str(option.get("stock") or ""),
                "availability_explicit": bool(option.get("availability_explicit")),
                "sellable": sellable,
                "price_pending": not bool(effective_sale_price),
                "list_ids": own_ids, "effective_list_ids": effective_ids,
                "own_store_count": self._store_count_for_lists(own_ids),
                "effective_store_count": self._store_count_for_lists(effective_ids),
                "configuration_ready": mode != "custom" or bool(own_ids), "inherited": mode != "custom",
            })
        return output

    def set_sku_store_rule(self, item_id: str, sku_key: str, sku_name: str,
                           mode: str, list_ids: Optional[List[int]] = None,
                           sale_price: object = "") -> Dict:
        item_id, sku_key = str(item_id or "").strip(), str(sku_key or "").strip()
        mode = str(mode or "inherit").strip()
        if not item_id or not sku_key:
            raise ValueError("请选择商品规格")
        if mode not in {"inherit", "custom"}:
            raise ValueError("门店适用方式无效")
        sale_price = str(sale_price or "").strip()
        if sale_price:
            try:
                normalized_price = self._format_number(Decimal(sale_price))
                if Decimal(normalized_price) <= 0:
                    raise ValueError
                sale_price = normalized_price
            except (InvalidOperation, ValueError):
                raise ValueError("规格售价必须是大于0的数字")
        valid_keys = {row["sku_key"]: row for row in self.list_product_skus(item_id)}
        if sku_key not in valid_keys:
            raise ValueError("商品规格不存在或已经失效")
        ids = sorted({int(value) for value in (list_ids or [])})
        if ids:
            placeholders = ",".join("?" for _ in ids)
            with self._connect() as conn:
                existing_ids = {int(row[0]) for row in conn.execute(
                    f"SELECT id FROM store_lists WHERE id IN ({placeholders})", ids
                ).fetchall()}
            if existing_ids != set(ids):
                raise ValueError("选择的门店表不存在或已被删除")
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO product_sku_store_rules(
                       item_id,sku_key,sku_name,sale_price_override,mode,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(item_id,sku_key) DO UPDATE SET
                   sku_name=excluded.sku_name,sale_price_override=excluded.sale_price_override,
                   mode=excluded.mode,status='active',updated_at=excluded.updated_at""",
                (
                    item_id, sku_key, sku_name or valid_keys[sku_key]["sku_name"],
                    sale_price, mode, "active", now, now,
                ),
            )
            conn.execute("DELETE FROM product_sku_store_lists WHERE item_id=? AND sku_key=?", (item_id, sku_key))
            if mode == "custom":
                conn.executemany(
                    "INSERT OR IGNORE INTO product_sku_store_lists(item_id,sku_key,list_id,created_at) VALUES(?,?,?,?)",
                    [(item_id, sku_key, list_id, now) for list_id in ids],
                )
        return next(row for row in self.list_product_skus(item_id) if row["sku_key"] == sku_key)

    def effective_store_list_ids(self, item_id: str, sku_key: str = "") -> tuple[List[int], str, bool]:
        sku_key = str(sku_key or "").strip()
        if not sku_key:
            return [int(row["id"]) for row in self.bound_store_lists(item_id)], "inherit", True
        sku = next((row for row in self.list_product_skus(item_id) if row["sku_key"] == sku_key), None)
        if not sku:
            return [], "missing", False
        return list(sku["effective_list_ids"]), sku["mode"], bool(sku["configuration_ready"])

    def match_message_skus(self, item_id: str, message: str,
                           product: Optional[Dict] = None) -> List[Dict]:
        """Conservatively match buyer wording to current real sale options."""
        product = product or self.get_v2_product(item_id) or {}
        skus = [sku for sku in self.list_product_skus(item_id, product) if sku.get("sellable", True)]
        options = self.extract_sale_options(product)
        by_key = {self.sku_key_for_option(option): option for option in options}
        text = normalize_text(message).lower()
        if not text:
            return []
        exact = [sku for sku in skus if normalize_text(sku.get("sku_name")).lower() in text
                 and len(normalize_text(sku.get("sku_name"))) >= 2]
        if exact:
            return exact
        slots = self._conditional_query_slots(message)
        requested_amount = self._requested_price_amount(message)
        requested_platforms = [name for name in ("抖音", "美团", "小程序") if name in text]
        if not requested_amount and requested_platforms:
            platform_face = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?", text)
            requested_amount = self._format_number(platform_face.group(1)) if platform_face else ""
        filters_present = bool(
            requested_amount or slots.get("people_counts") or slots.get("day_types")
            or slots.get("meal_periods") or slots.get("audience_types")
        )
        if not filters_present:
            return []
        matched = []
        for sku in skus:
            option = by_key.get(sku["sku_key"]) or sku
            if requested_platforms and not any(
                platform in str(option.get("name") or sku.get("sku_name") or "")
                for platform in requested_platforms
            ):
                continue
            if (
                not slots.get("audience_types")
                and set(sku.get("audience_types") or []).intersection(
                    {"child", "student", "senior", "female"}
                )
            ):
                continue
            if requested_amount:
                face = self._format_number(option.get("face_value"))
                name_has_amount = bool(re.search(
                    rf"(?<![\d.]){re.escape(requested_amount)}(?:\.0+)?\s*(?:元|块|面额|选项)",
                    str(option.get("name") or ""),
                ))
                if face != requested_amount and not name_has_amount:
                    continue
            if slots.get("people_counts") and not set(slots["people_counts"]) & set(sku.get("people_counts") or []):
                continue
            for key in ("day_types", "meal_periods", "audience_types"):
                requested = set(slots.get(key) or [])
                supported = (
                    set(self._effective_audience_types(sku))
                    if key == "audience_types" else set(sku.get(key) or [])
                )
                if requested and supported and not requested & supported:
                    break
            else:
                matched.append(sku)
        return matched

    @staticmethod
    def _sku_public_label(sku: Dict) -> str:
        name = str(sku.get("sku_name") or "该规格").strip()
        price = str(sku.get("sale_price") or "").strip()
        # Buyer-facing purchase options must preserve the marketplace SKU name
        # verbatim.  max_stack is a usage rule, not part of the SKU name; adding
        # it here invents labels such as “抖音50x4（可叠加4张）”.  If stacking
        # text really belongs to the SKU, it is already present in sku_name.
        return f"{name}（售价{price}元）" if price else f"{name}（售价待同步）"

    def reverse_store_sku_matches(self, item_id: str, query: str,
                                  product: Optional[Dict] = None) -> List[Dict]:
        """Return only configured current SKUs that can actually use the queried store."""
        product = product or self.get_v2_product(item_id) or {}
        output = []
        for sku in self.list_product_skus(item_id, product):
            if not sku.get("sellable", True):
                continue
            result = self.search_store(item_id, query, sku_key=sku["sku_key"])
            if result.get("status") == "available":
                output.append({**sku, "matches": result.get("matches") or []})
        return output

    @classmethod
    def _physical_store_key(cls, store: Dict) -> tuple:
        """Identify one physical branch across duplicated per-SKU store lists."""
        branch = cls._store_fuzzy_key(store.get("branch") or store.get("brand"))
        return (
            cls._area_key(store.get("province")),
            cls._area_key(store.get("city")),
            branch or normalize_match_text(store.get("address")),
        )

    def _multi_sku_store_scope(self, item_id: str, product: Optional[Dict] = None) -> Dict:
        """Build store scope and compare actual branches, not store-list ids."""
        product = product or self.get_v2_product(item_id) or {}
        skus = self.list_product_skus(item_id, product)
        ready_skus = []
        unknown_skus = []
        all_list_ids = set()
        for sku in skus:
            list_ids, mode, ready = self.effective_store_list_ids(item_id, sku["sku_key"])
            normalized_ids = tuple(sorted(int(value) for value in list_ids))
            enriched = {
                **sku,
                "effective_list_ids": list(normalized_ids),
                "mode": mode,
                "configuration_ready": bool(ready),
            }
            if ready:
                ready_skus.append(enriched)
                all_list_ids.update(normalized_ids)
            else:
                unknown_skus.append(enriched)

        rows_by_list = {list_id: [] for list_id in all_list_ids}
        if all_list_ids:
            ordered_ids = sorted(all_list_ids)
            placeholders = ",".join("?" for _ in ordered_ids)
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM stores WHERE list_id IN ({placeholders})", ordered_ids
                ).fetchall()
            for row in rows:
                value = dict(row)
                rows_by_list.setdefault(int(value["list_id"]), []).append(value)
        physical_sets = {
            frozenset(
                self._physical_store_key(row)
                for list_id in sku.get("effective_list_ids") or []
                for row in rows_by_list.get(int(list_id), [])
            )
            for sku in ready_skus
        }
        return {
            "skus": skus,
            "ready_skus": ready_skus,
            "unknown_skus": unknown_skus,
            "list_ids": sorted(all_list_ids),
            # Different list records may contain the same physical branches.
            # Only verified branch differences activate the multi-SKU narrowing.
            "stores_differ": len(physical_sets) > 1,
        }

    def _store_sku_matrix(self, item_id: str, stores: List[Dict], scope: Dict) -> List[Dict]:
        """Map each matched physical store to confirmed and unconfigured SKUs."""
        list_ids = sorted({
            int(list_id)
            for sku in scope.get("ready_skus") or []
            for list_id in sku.get("effective_list_ids") or []
        })
        rows_by_list = {list_id: [] for list_id in list_ids}
        if list_ids:
            placeholders = ",".join("?" for _ in list_ids)
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM stores WHERE list_id IN ({placeholders})",
                    list_ids,
                ).fetchall()
            for row in rows:
                value = dict(row)
                rows_by_list.setdefault(int(value["list_id"]), []).append(value)

        output = []
        for store in stores:
            store_key = self._physical_store_key(store)
            supported = []
            for sku in scope.get("ready_skus") or []:
                candidate_rows = [
                    row
                    for list_id in sku.get("effective_list_ids") or []
                    for row in rows_by_list.get(int(list_id), [])
                ]
                if any(self._physical_store_key(row) == store_key for row in candidate_rows):
                    supported.append(sku)
            output.append({
                "store": store,
                "supported_skus": supported,
                "unknown_skus": list(scope.get("unknown_skus") or []),
            })
        return output

    @classmethod
    def _store_display_name(cls, store: Dict) -> str:
        branch = str(store.get("branch") or store.get("brand") or "该门店").strip()
        city = str(store.get("city") or "").strip()
        return branch if not city or cls._area_key(city) in cls._area_key(branch) else city + branch

    @classmethod
    def _format_store_sku_matrix(cls, query: str, matrix: List[Dict],
                                 selected_skus: Optional[List[Dict]] = None) -> str:
        selected_skus = list(selected_skus or [])
        selected_keys = {str(sku.get("sku_key") or "") for sku in selected_skus}
        lines = []
        for row in matrix:
            store_name = cls._store_display_name(row.get("store") or {})
            supported = list(row.get("supported_skus") or [])
            supported_keys = {str(sku.get("sku_key") or "") for sku in supported}
            if selected_skus:
                usable = [sku for sku in selected_skus if str(sku.get("sku_key") or "") in supported_keys]
                unusable = [sku for sku in selected_skus if str(sku.get("sku_key") or "") not in supported_keys]
                direct = []
                if usable:
                    usable_names = cls._compact_sku_names(usable)
                    if len(usable) == 1:
                        maximum = cls._normalize_stack_limit(usable[0].get("max_stack"))
                        price = str(usable[0].get("sale_price") or "").strip()
                        details = []
                        if maximum:
                            details.append(f"可叠加{maximum}张")
                        if price:
                            details.append(f"售价{price}元")
                        direct.append(
                            usable_names + "可以使用"
                            + (f"（{'，'.join(details)}）" if details else "")
                        )
                    else:
                        direct.append(
                            usable_names + "可以使用；规格信息："
                            + "、".join(cls._sku_public_label(sku) for sku in usable)
                        )
                if unusable:
                    direct.append(cls._compact_sku_names(unusable) + "不能使用")
                if unusable and supported:
                    direct.append(
                        "可用规格为" + "、".join(cls._sku_public_label(sku) for sku in supported)
                    )
                line = f"【{store_name}】：{'；'.join(direct) or '暂无已确认的可用规格'}。"
            else:
                if supported:
                    labels = [cls._sku_public_label(sku) for sku in supported]
                    line = f"【{store_name}】：可用规格为{'、'.join(labels)}。"
                else:
                    line = f"【{store_name}】：暂无已确认的可用规格。"
            unknown = [
                sku for sku in row.get("unknown_skus") or []
                if not selected_keys or str(sku.get("sku_key") or "") in selected_keys
            ]
            if unknown:
                line += " " + cls._compact_sku_names(unknown) + "的门店资料暂未配置。"
            lines.append(line)
        if selected_skus and len(lines) == 1:
            return lines[0]
        heading = f"根据“{query}”查询结果："
        return "\n\n".join((heading, *lines, "请按对应门店支持的规格拍下。"))

    @classmethod
    def _compact_sku_names(cls, skus: List[Dict]) -> str:
        """Keep meaningful SKU names; compact only truly generic voucher labels."""
        faces = [
            cls._format_number(sku.get("face_value") or "")
            for sku in skus
        ]
        generic_vouchers = bool(skus) and all(faces) and all(
            (sku.get("option_type") or "voucher") == "voucher"
            and bool(re.fullmatch(
                rf"{re.escape(face)}(?:\.0+)?\s*(?:元)?\s*(?:代金券|优惠券|抵扣券|现金券|券)",
                str(sku.get("sku_name") or "").strip(),
            ))
            for sku, face in zip(skus, faces)
        )
        if generic_vouchers:
            return "、".join(f"{face}元" for face in faces) + "代金券"
        return "、".join(str(sku.get("sku_name") or "当前规格") for sku in skus)

    def _store_search_clarification(
        self, query: str, result: Dict, selected_skus: Optional[List[Dict]] = None,
    ) -> Optional[Dict]:
        """Turn uncertain store matches into a confirmation, never an availability claim."""
        status = str(result.get("status") or "")
        if status == "relation_query":
            relation = "、".join(result.get("relation_words") or []) or "附近"
            return {
                "reply": (
                    f"当前门店资料不能准确判断“{relation}”的位置关系。"
                    "请发送要核对的完整门店名、商场名或门店地址。"
                ),
                "source": "门店位置关系缺少可验证地址锚点",
                "decision": "allow", "kind": "stores_clarify",
                "store_matches": [], "store_query": query,
                "store_status": "location_relation_unverified",
            }
        if status == "excluded_query":
            return {
                "reply": "已排除该门店。请发送您实际要查询的完整门店名、商场名或所在城市。",
                "source": "门店排除问法缺少替代目标",
                "decision": "allow", "kind": "stores_clarify",
                "store_matches": [], "store_query": query,
                "store_status": "missing_target",
            }
        if status == "conflicting_scope":
            return {
                "reply": "消息中包含多个不同地区。请一次发送一个城市、区县或具体门店名称，我再准确核对。",
                "source": "门店查询包含冲突地域锚点",
                "decision": "allow", "kind": "stores_clarify",
                "store_matches": [], "store_query": query,
                "store_status": "conflicting_scope",
            }
        if status == "ambiguous_area":
            count = int(result.get("candidate_count") or len(result.get("matches") or []))
            return {
                "reply": (
                    f"“{query}”在多个地区匹配到{count}家同名或同类门店。"
                    "请补充城市或区县后再查询，我会按具体门店核对可用规格。"
                ),
                "source": "通用商场名称缺少地域锚点",
                "decision": "allow", "kind": "stores_clarify",
                "store_matches": [], "store_query": query,
                "store_status": "missing_area",
                "store_context_update": {
                    "pending_store_query": query,
                    "pending_store_query_mode": "generic_landmark",
                    "candidate_count": count,
                },
            }
        if status != "needs_confirmation":
            return None

        matches = list(result.get("matches") or [])
        candidates = matches[:3]
        names = [self._store_display_name(row) for row in candidates]
        if not names:
            return None
        if len(matches) > 3:
            reply = (
                f"根据“{query}”找到多家名称相近的门店，请再补充区县、商圈"
                "或完整门店名后查询。"
            )
        elif len(names) == 1:
            reply = f"您是想查询【{names[0]}】吗？请回复“是”确认，或发送完整门店名。"
        else:
            choices = "\n".join(f"{index}. 【{name}】" for index, name in enumerate(names, start=1))
            reply = (
                f"根据“{query}”找到以下名称相近的门店：\n\n{choices}\n\n"
                "请回复序号确认，或发送完整门店名。"
            )
        return {
            "reply": reply,
            "source": "同地域门店名称近似候选待买家确认",
            "decision": "allow", "kind": "stores_clarify",
            "store_matches": candidates, "store_query": query,
            "store_status": "candidate_confirmation",
            "store_context_update": {
                "pending_store_candidates": candidates,
                "pending_store_query": query,
                "pending_store_query_mode": "candidate_confirmation",
                "pending_selected_sku_keys": [
                    str(sku.get("sku_key") or "") for sku in (selected_skus or [])
                    if sku.get("sku_key")
                ],
            },
        }

    def resolve_store_candidate_followup(
        self, item_id: str, product: Dict, message: str,
        store_context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Resolve a strict yes/ordinal reply against locally stored candidates."""
        context = store_context if isinstance(store_context, dict) else {}
        compact = re.sub(r"[\s，,。.!！?？~～]+", "", str(message or ""))
        all_candidates = list(
            context.get("pending_store_candidates") or context.get("matches") or []
        )

        if context.get("status") in {"unavailable", "unconfigured", "area_fallback"} and re.fullmatch(
            r"(?:这家(?:店)?|这个店|该店|那里|刚才那家|那家店)?"
            r"(?:可以|能|可不可以|能不能)?(?:买|购买|拍|下单|用|使用)"
            r"(?:吗|嘛|么|不|否)?",
            compact,
        ):
            query = str(context.get("pending_store_query") or context.get("query") or "该门店").strip()
            return {
                "reply": f"目前尚未确认“{query}”属于可用门店，暂不建议按该门店需求购买。",
                "source": "上一轮门店查询尚未确认",
                "decision": "deny", "kind": "stores",
                "store_matches": [], "store_query": query,
                "store_status": str(context.get("status") or "unavailable"),
            }

        # A short area answer such as “武汉的” narrows the stores returned by
        # the immediately preceding query. Do not discard that candidate set and
        # start a fresh literal search for the meaningless suffix “的”.
        area_text = re.sub(r"(?:的|那边|这边|地区|门店|店)$", "", compact)
        area_key = self._area_key(area_text)
        if area_key and len(all_candidates) > 1:
            area_matches = [
                row for row in all_candidates
                if area_key in {
                    self._area_key(row.get("province")),
                    self._area_key(row.get("city")),
                    self._area_key(row.get("district")),
                }
            ]
            if area_matches:
                query = area_text
                return {
                    "reply": self.format_store_matches(area_matches, query, message),
                    "source": "当前会话最近一次门店候选按地区缩小范围",
                    "decision": "allow", "kind": "stores",
                    "store_matches": area_matches, "store_query": query,
                    "store_status": "available",
                }

        if context.get("status") != "candidate_confirmation":
            return None
        candidates = list(context.get("pending_store_candidates") or context.get("matches") or [])[:3]
        if not candidates:
            return None
        selected_index = None
        if len(candidates) == 1 and re.fullmatch(r"(?:是|是的|对|对的|嗯|好的|可以|没错|就是)", compact):
            selected_index = 0
        else:
            ordinal_patterns = (
                (0, r"(?:第)?一(?:家|个)?|第1(?:家|个)?|1"),
                (1, r"(?:第)?二(?:家|个)?|第2(?:家|个)?|2"),
                (2, r"(?:第)?三(?:家|个)?|第3(?:家|个)?|3"),
            )
            for index, pattern in ordinal_patterns:
                if re.fullmatch(pattern, compact):
                    selected_index = index
                    break
        if re.fullmatch(r"(?:不是|不对|都不是|不是这家|不是的)", compact):
            return {
                "reply": "好的，请发送更完整的城市、区县、商圈或门店名称，我重新查询。",
                "source": "买家否定门店近似候选",
                "decision": "allow", "kind": "stores_clarify",
                "store_matches": [], "store_query": str(context.get("query") or ""),
                "store_status": "missing_query",
            }
        if selected_index is None:
            return None
        if selected_index >= len(candidates):
            return {
                "reply": "候选结果中没有这个序号，请回复已列出的序号或发送完整门店名。",
                "source": "门店近似候选序号超出范围",
                "decision": "allow", "kind": "stores_clarify",
                "store_matches": candidates,
                "store_query": str(context.get("query") or ""),
                "store_status": "candidate_confirmation",
                "store_context_update": {
                    "pending_store_candidates": candidates,
                    "pending_store_query": str(context.get("pending_store_query") or context.get("query") or ""),
                    "pending_store_query_mode": "candidate_confirmation",
                    "pending_selected_sku_keys": list(context.get("pending_selected_sku_keys") or []),
                },
            }

        match = candidates[selected_index]
        query = self._store_display_name(match)
        skus = self.list_product_skus(item_id, product)
        selected_keys = set(context.get("pending_selected_sku_keys") or [])
        selected_skus = [sku for sku in skus if sku.get("sku_key") in selected_keys]
        scope = self._multi_sku_store_scope(item_id, product)
        if scope.get("stores_differ"):
            matrix = self._store_sku_matrix(item_id, [match], scope)
            return {
                "reply": self._format_store_sku_matrix(query, matrix, selected_skus),
                "source": "买家确认门店候选后按本地门店与规格对应关系核验",
                "decision": "allow", "kind": "stores_sku_recommendation",
                "store_matches": [match], "store_query": query,
                "store_status": "available", "store_sku_matrix": matrix,
            }
        prefix = f"{self._sku_public_label(selected_skus[0])}：" if len(selected_skus) == 1 else ""
        return {
            "reply": prefix + self.format_store_matches([match], query, str(message or "")),
            "source": "买家确认门店候选后按当前商品门店表核验",
            "decision": "allow", "kind": "stores",
            "store_matches": [match], "store_query": query,
            "store_status": "available",
        }

    def _explicit_store_skus(self, item_id: str, message: str,
                             product: Optional[Dict] = None) -> List[Dict]:
        product = product or self.get_v2_product(item_id) or {}
        matched = self.match_message_skus(item_id, message, product)
        if matched:
            return matched
        multiplier = re.search(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?\s*[xX×*]\s*"
            r"(\d+)\s*(?:张)?\s*(?:的)?\s*(?:代金券|优惠券|券)?",
            str(message or ""),
        )
        if multiplier:
            # “100x2的券” means two 100-yuan coupons.  The quantity must not
            # be reclassified as a separate 2-yuan denomination.
            values = {self._format_number(multiplier.group(1))}
        else:
            values = {
                self._format_number(value)
                for match in re.finditer(
                    r"(?<!\d)(\d+(?:\.\d+)?)\s*元?\s*(?:代金券|优惠券|券|面额)|"
                    r"(?:代金券|优惠券|券|面额)\s*(\d+(?:\.\d+)?)",
                    str(message or ""),
                )
                for value in match.groups() if value
            }
        if not values and re.search(r"可以用|能用|可用|适用|使用", str(message or "")):
            values = {
                self._format_number(match.group(1))
                for match in re.finditer(
                    r"(?<![\d.])(\d+(?:\.\d+)?)(?:元)?"
                    r"(?!\s*(?:年|月|日|号|路|街|道|点|个|人|位|张|桌|份))",
                    str(message or ""),
                )
            }
        if not values:
            return []
        return [
            sku for sku in self.list_product_skus(item_id, product)
            if self._format_number(sku.get("face_value") or "") in values
        ]

    def _all_store_scope_reply(self, item_id: str, product: Dict,
                               message: str) -> Optional[Dict]:
        """Answer nationwide/all-store questions before generic multi-intent parsing."""
        if not re.search(
            r"(?:所有|全部|全国|每家|每个|任意).{0,5}(?:门店|店铺|店).{0,5}(?:通用|可用|能用|可以用)|"
            r"(?:门店|店铺|店).{0,5}(?:都|全部|所有).{0,4}(?:通用|可用|能用|可以用)|"
            r"(?:全国|全城)(?:门店|店铺|店)?(?:都|全部)?(?:通用|可用|能用|可以用)|"
            r"^(?:通用吗|都能用吗|都可以用吗|都可用吗)[？?。！!]*$",
            str(message or ""),
        ):
            return None

        selected_skus = self._explicit_store_skus(item_id, message, product)
        if self._multi_sku_store_scope(item_id, product).get("stores_differ") and len(selected_skus) != 1:
            return {
                "reply": "不同商品规格的适用门店可能不同。请发送要购买的规格名称、面额或人数，我按对应规格为您准确查询。",
                "source": "当前商品不同规格绑定了不同门店表",
                "decision": "allow", "kind": "stores_clarify",
            }

        multiplier = re.search(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?\s*[xX×*]\s*(\d+)\s*(?:张)?",
            str(message or ""),
        )
        if selected_skus:
            face = self._format_number(selected_skus[0].get("face_value") or "")
            subject = f"{face}元代金券" if face else "该规格代金券"
        else:
            title = str(product.get("title") or "")
            subject = "鱼酷烤鱼券" if "鱼酷" in title else "当前商品卡券"
        if multiplier and self._format_number(multiplier.group(1)) in subject:
            subject += f"（购买{int(multiplier.group(2))}张）"
        return {
            "reply": f"{subject}需在指定门店使用，不是全国所有门店通用。\n\n具体可用门店，请发送城市或店面名称进行查询。",
            "source": "当前商品绑定的指定适用门店",
            "decision": "allow", "kind": "stores_scope",
        }

    def _strip_store_sku_edges(
        self, item_id: str, query: str, product: Optional[Dict] = None,
    ) -> str:
        """Remove a real denomination only at a store-query boundary."""
        value = str(query or "").strip()
        faces = sorted({
            self._format_number(sku.get("face_value") or "")
            for sku in self.list_product_skus(item_id, product)
            if self._format_number(sku.get("face_value") or "")
        }, key=len, reverse=True)
        for face in faces:
            token = rf"{re.escape(face)}\s*(?:元)?\s*(?:的)?\s*(?:代金券|优惠券|券)?"
            value = re.sub(rf"^\s*{token}", "", value).strip()
            value = re.sub(rf"{token}\s*$", "", value).strip()
        return value

    def resolve_multi_sku_store_query(
        self, item_id: str, product: Dict, message: str,
        store_context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Resolve store questions against all SKU lists without city fallback."""
        scope = self._multi_sku_store_scope(item_id, product)
        if not scope.get("stores_differ"):
            return None

        raw_query = extract_store_query(
            message, product=product, product_brand=self.extract_brand(product)
        )
        raw_query = self._strip_store_sku_edges(item_id, raw_query, product)
        pending_area = str((store_context or {}).get("pending_store_query") or "").strip()
        pending_mode = str((store_context or {}).get("pending_store_query_mode") or "")
        query = raw_query
        if pending_area and query and pending_mode == "generic_landmark":
            query = query + pending_area
        elif pending_area and query and not any(
                self._area_key(query).startswith(self._area_key(area))
                for area in KNOWN_CITY_NAMES | KNOWN_PROVINCE_NAMES if len(self._area_key(area)) >= 2
        ):
            query = pending_area + query
        if not is_meaningful_store_query(query):
            return None
        if not scope.get("list_ids"):
            return {
                "reply": "当前各商品规格都还没有配置可用门店资料，暂时无法准确查询。",
                "source": "多规格门店资料均未配置",
                "decision": "review", "kind": "stores",
                "store_matches": [], "store_query": query,
                "store_status": "unconfigured",
            }

        result = self.search_store(
            item_id, query, list_ids_override=scope["list_ids"]
        )
        resolved_query = str(result.get("resolved_query") or query).strip()
        if result.get("status") != "available":
            resolved_query = str(result.get("specific_query") or resolved_query).strip()
        if not self.is_explicit_store_query(message, resolved_query, result):
            return None
        selected_skus = self._explicit_store_skus(item_id, message, product)
        clarification = self._store_search_clarification(
            resolved_query, result, selected_skus,
        )
        if clarification:
            return clarification
        if result.get("status") == "area_fallback":
            matches = list(result.get("matches") or [])
            matrix = self._store_sku_matrix(item_id, matches, scope)
            return {
                "reply": self.format_store_area_fallback(
                    result, resolved_query, message,
                ),
                "source": "具体门店未匹配，返回所提市区的全部可用门店及规格",
                "decision": "allow", "kind": "stores_sku_recommendation",
                "store_matches": matches, "store_query": resolved_query,
                "store_status": "area_fallback", "store_sku_matrix": matrix,
            }
        if result.get("status") != "available":
            return {
                "reply": self.format_store_unavailable(
                    resolved_query,
                    area_only=bool((result.get("province") or result.get("city"))
                                   and not result.get("search_term")),
                ),
                "source": "多规格商品级门店全集未匹配",
                "decision": "deny", "kind": "stores",
                "store_matches": [], "store_query": resolved_query,
                "store_status": "unavailable",
            }

        matches = list(result.get("matches") or [])
        if len(matches) > 3:
            return {
                "reply": (
                    f"根据“{resolved_query}”匹配到多家可用门店，请补充区县、商圈、"
                    "商场名称或完整门店名，我再帮您准确查询对应卡券规格。"
                ),
                "source": "多规格门店查询超过3家需缩小范围",
                "decision": "allow", "kind": "stores_clarify",
                "store_matches": matches, "store_query": resolved_query,
                "store_status": "too_many",
                "store_context_update": {
                    "pending_store_query": resolved_query,
                    "candidate_count": len(matches),
                },
            }

        unconfigured_selected = [
            sku for sku in selected_skus if not sku.get("configuration_ready")
        ]
        if unconfigured_selected:
            labels = "、".join(self._sku_public_label(sku) for sku in unconfigured_selected)
            return {
                "reply": f"{labels}暂未配置适用门店资料，无法准确确认{resolved_query}是否可用。",
                "source": "买家指定规格的门店资料未配置",
                "decision": "review", "kind": "stores",
                "store_matches": matches, "store_query": resolved_query,
                "store_status": "unconfigured",
            }
        matrix = self._store_sku_matrix(item_id, matches, scope)
        reply = self._format_store_sku_matrix(resolved_query, matrix, selected_skus)
        return {
            "reply": reply,
            # Store, SKU and price are one answer.  Do not split paragraphs into
            # separate Xianyu messages or the buyer loses their relationship.
            "reply_parts": [],
            "source": "多规格商品级门店与规格对应关系",
            "decision": "allow", "kind": "stores_sku_recommendation",
            "store_matches": matches, "store_query": resolved_query,
            "store_status": "available", "store_sku_matrix": matrix,
            "query_context_update": ({
                "selected_sku_key": selected_skus[0]["sku_key"],
                "selected_sku_name": selected_skus[0].get("sku_name", ""),
            } if len(selected_skus) == 1 else {}),
        }

    @classmethod
    def _sku_amount_followup(cls, message: str) -> str:
        """Extract a denomination from a short colloquial SKU follow-up."""
        compact = re.sub(r"[\s，,。.!！?？~～]+", "", str(message or ""))
        match = re.fullmatch(
            r"(?:那|那么|那请问|再问下|再问一下|请问|还有)?"
            r"(?:(?:美团|抖音|小程序))?"
            r"(\d+(?:\.\d+)?)\s*(?:元|块|块钱)?"
            r"(?:的|那个|那款|这一款|这个|这个规格|规格|面额|代金券|优惠券|抵扣券|现金券|券)?"
            r"(?:呢|有吗|有没有|也有吗|也能用吗|也可以用吗|"
            r"能不能(?:用|使用|买|拍)|可不可以(?:用|使用|买|拍)|"
            r"是不是(?:能用|可以用|可用|不能用)|"
            r"可以(?:用|使用|买|拍)?吗|能(?:用|使用|买|拍)?吗|可用吗|"
            r"不能用吗|不可以用吗|不适用吗|行不行|行吗|支持吗|适用吗|怎么样|咋样)?",
            compact,
        )
        return cls._format_number(match.group(1)) if match else ""

    @classmethod
    def _amount_plan_request(cls, message: str) -> str:
        """Extract one consumption amount from common plan/recommendation wording.

        This intentionally excludes SKU availability wording such as ``300能用吗``.
        Exact-SKU availability must remain attached to the latest store matrix,
        while consumption, bill and recommendation wording asks for a purchase plan.
        """
        compact = re.sub(r"[\s，,。.!！?？；;：:~～]+", "", str(message or ""))
        if re.search(r"退款|退货|退钱|售后|不想要|买多了|拍多了|申请退", compact):
            return ""
        if re.search(
            r"(?:能不能|可不可以|有没有|有无|有没|是否有|可以不可以)"
            r"[^\d]{0,6}\d+(?:\.\d+)?(?:元|块)?(?:代金券|优惠券|券)",
            compact,
        ):
            return ""
        amounts = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", compact)
        if not amounts:
            chinese_amounts = re.findall(
                r"([零一二两三四五六七八九十百千]+)"
                r"(?=(?:元|块|怎么|如何|咋|买|拍|要|来|下单|消费|账单|预算|$))",
                compact,
            )
            converted = [cls._chinese_count(value) for value in chinese_amounts]
            amounts = [str(value) for value in converted if value and value >= 20]
        if len(amounts) != 1:
            return ""
        consumption_words = bool(re.search(
            r"消费|吃了|吃到|用餐|账单|结账|买单|应付|实付|"
            r"总价|合计|一共|总共|预算|花了|花费",
            compact,
        ))
        plan_words = bool(re.search(
            r"怎么(?:拍|买|凑|配|搭配|选|下单|弄|整|抵扣)|"
            r"如何(?:拍|买|凑|配|搭配|选|下单|抵扣)|"
            r"咋(?:拍|买|凑|配|选|弄|整)|"
            r"(?:买|拍|选)(?:哪个|哪种|什么券)|用什么券|配什么券|"
            r"(?:要|该|需要)(?:买|拍|下单)?几张|能买什么|"
            r"给(?:我)?(?:一个|个)?方案|有(?:什么|啥)方案|推荐(?:一下)?|"
            r"怎么办|怎么最划算|如何最划算|哪个最划算|哪种最划算|"
            r"怎么划算|如何划算|怎么合适|哪个合适|能抵多少|抵扣多少|"
            r"(?:给我|帮我|我要|要|来|买|拍|下单|购|整)\d+(?:\.\d+)?(?:元|块)?(?:的)?|"
            r"\d+(?:\.\d+)?(?:元|块)?(?:的)?(?:买|拍|下单)(?:几张|多少张)",
            compact,
        ))
        if not plan_words and not re.search(r"\d", compact):
            plan_words = bool(re.search(
                r"(?:给我|帮我|我要|要|来|买|拍|下单|购|整)?"
                r"[零一二两三四五六七八九十百千]+(?:元|块)?(?:的)?"
                r"(?:怎么拍|怎么买|如何拍|如何买|拍几张|买几张)?",
                compact,
            ))
        if not (consumption_words or plan_words):
            return ""
        return cls._format_number(amounts[0])

    def _store_aware_amount_plan_reply(
        self, item_id: str, product: Dict, amount: str,
        store_context: Optional[Dict] = None, force_consumption_kind: bool = False,
    ) -> Optional[Dict]:
        """Build amount plans from only the SKUs valid for each recent store."""
        context = store_context if isinstance(store_context, dict) else {}
        matrix = list(context.get("store_sku_matrix") or [])

        def sku_key(sku: Dict) -> str:
            return str(sku.get("sku_key") or sku.get("sku_name") or "")

        def options_for(skus: List[Dict]) -> List[Dict]:
            seen = set()
            options = []
            for sku in skus:
                key = sku_key(sku)
                if not key or key in seen or not sku.get("sellable", True):
                    continue
                seen.add(key)
                options.append({**sku, "name": str(sku.get("sku_name") or "商品规格")})
            return options

        def build(options: List[Dict]) -> Dict:
            if not options:
                return {
                    "reply": "当前门店没有可用的代金券规格，暂时无法给出凑单方案。",
                    "source": "当前门店可用SKU为空",
                    "decision": "deny", "kind": "consumption_plan",
                }
            planned = self.amount_inquiry_plan_reply(
                product, amount, available_options=options,
            )
            if planned:
                planned = dict(planned)
                planned_reply = str(planned.get("reply") or "")
                if planned.get("decision") != "allow" and (
                    "使用张数限制" in planned_reply or "无法准确组成" in planned_reply
                ):
                    closest_reply = self.consumption_plan_reply(
                        product, amount, available_options=options,
                    )
                    if closest_reply:
                        return {
                            "reply": closest_reply,
                            "source": "当前门店可用SKU与最多使用张数下的不超额方案",
                            "decision": "allow",
                            "kind": (
                                "consumption_plan" if force_consumption_kind
                                else "redemption_plan"
                            ),
                        }
                if force_consumption_kind and planned.get("decision") == "allow":
                    if "可选组合方案" not in str(planned.get("reply") or ""):
                        legacy_reply = self.consumption_plan_reply(
                            product, amount, available_options=options,
                        )
                        if legacy_reply:
                            planned["reply"] = legacy_reply
                    planned["kind"] = "consumption_plan"
                return planned
            reply = self.consumption_plan_reply(
                product, amount, available_options=options,
            )
            return {
                "reply": reply,
                "source": "当前门店可用SKU、售价、库存与最多使用张数",
                "decision": "review" if "需要人工核实" in reply else "allow",
                "kind": "consumption_plan" if force_consumption_kind else "redemption_plan",
            }

        # A city/area query may leave several stores in context.  When their
        # supported SKU sets differ, calculate each store separately instead of
        # taking a union that could recommend an unusable denomination.
        row_signatures = {
            tuple(sorted(sku_key(sku) for sku in row.get("supported_skus") or []
                         if sku.get("sellable", True)))
            for row in matrix
        }
        if len(matrix) > 1 and len(row_signatures) > 1:
            sections = []
            decisions = []
            for index, row in enumerate(matrix, 1):
                result = build(options_for(list(row.get("supported_skus") or [])))
                decisions.append(str(result.get("decision") or "allow"))
                store_name = self._store_display_name(row.get("store") or {})
                detail = re.sub(r"\s*\n+\s*", " ", str(result.get("reply") or "")).strip()
                sections.append(f"{index}. 【{store_name}】：{detail}")
            return {
                "reply": (
                    f"按刚才查询到的门店，{amount}元消费的购券方案不同：\n\n"
                    + "\n\n".join(sections)
                ),
                "source": "逐门店使用各自可用SKU、售价、库存与最多使用张数计算",
                "decision": "allow" if "allow" in decisions else "deny",
                "kind": "consumption_plan",
                "store_sku_matrix": matrix,
            }

        if matrix:
            scoped_skus = [
                sku for row in matrix for sku in row.get("supported_skus") or []
            ]
        else:
            scoped_skus = [
                sku for sku in self.list_product_skus(item_id, product)
                if sku.get("sellable", True)
            ]
        return build(options_for(scoped_skus))

    def resolve_store_matrix_followup(
        self, item_id: str, product: Dict, message: str,
        store_context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Answer only strict follow-ups to a recent <=3-store SKU matrix."""
        # Store-aware yes/no answers are valid only when SKU applicability
        # truly differs by physical store. Shared store scopes must keep amount
        # follow-ups on the ordinary purchase-recommendation path.
        if not self._multi_sku_store_scope(item_id, product).get("stores_differ"):
            return None
        context = store_context if isinstance(store_context, dict) else {}
        matrix = list(context.get("store_sku_matrix") or [])
        if not matrix:
            return None
        text = str(message or "").strip()
        compact = re.sub(r"[\s，,。.!！?？~～]+", "", text)
        if not compact:
            return None

        # An explicit amount-plan request always asks what to buy, even when
        # its number happens to equal a real SKU denomination.
        if self._amount_plan_request(text):
            return None

        ordinal = None
        ordinal_patterns = (
            (0, r"第一(?:家|个)|第1(?:家|个)|前面那家"),
            (1, r"第二(?:家|个)|第2(?:家|个)|后面那家"),
            (2, r"第三(?:家|个)|第3(?:家|个)"),
        )
        for index, pattern in ordinal_patterns:
            if re.search(pattern, compact):
                ordinal = index
                break

        # Treat short SKU questions as continuations of the most recent store
        # lookup. This must stay generic: every real SKU/denomination can be
        # queried, and buyers commonly omit both the store name and “能用”.
        amount_followup = self._sku_amount_followup(text)
        selected_skus = self._explicit_store_skus(item_id, text, product)
        # Buyers also refer to a SKU by its sale price after a store result,
        # e.g. “138那个不能用吗”. Only apply this alias to availability wording
        # so an ordinary “138多少钱” still follows the price/plan route.
        if not selected_skus and re.search(
            r"能用|可以用|可用|不能用|不可以用|不可用|用不了|支持|适用|有吗|能买吗|能拍",
            compact,
        ):
            mentioned_amounts = {
                self._format_number(value)
                for value in re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", text)
            }
            if mentioned_amounts:
                selected_skus = [
                    sku for sku in self.list_product_skus(item_id, product)
                    if self._format_number(sku.get("sale_price") or "") in mentioned_amounts
                ]
        if not selected_skus:
            bare_amount = amount_followup
            if bare_amount:
                selected_skus = [
                    sku for sku in self.list_product_skus(item_id, product)
                    if self._format_number(sku.get("face_value") or "") == bare_amount
                ]
        # A short amount that is not a real SKU (for example “400的呢” when the
        # listing only has 100/200/300) is a consumption target, not a question
        # about a fictional 400-yuan SKU.
        if amount_followup and not selected_skus:
            return None

        sku_followup_words = bool(re.search(
            r"(?:呢|有吗|有没有|也有吗|能用吗|可以用吗|可用吗|"
            r"能不能用|可不可以用|不能用吗|不可以用吗|不适用吗|"
            r"能使用吗|可以使用吗|能买吗|可以买吗|能拍吗|可以拍吗|"
            r"行不行|行吗|支持吗|适用吗|怎么样|咋样)$",
            compact,
        ))
        generic_followup_words = bool(re.search(
            r"这家|这个店|该店|那家|刚才|上面|这些店|这几个|"
            r"第一(?:家|个)|第二(?:家|个)|第三(?:家|个)|第[123](?:家|个)|"
            r"哪些面额|什么面额|哪些规格|什么规格|全部规格|所有规格|"
            r"其他规格|别的规格|其他的|别的呢|还有别的吗|买哪|"
            r"可以用吗|能用吗|可用吗|的呢|怎么样|咋样",
            compact,
        ))
        all_spec_followup = bool(re.search(
            r"所有规格|全部规格|全规格|每个规格|各个规格|"
            r"其他规格|别的规格|其他的|别的呢|还有别的吗|"
            r"(?:规格|面额|代金券|优惠券|券).{0,4}都(?:能|可以|可)?用",
            compact,
        ))
        if all_spec_followup and not selected_skus:
            selected_skus = [
                sku for sku in self.list_product_skus(item_id, product)
                if sku.get("sellable", True)
            ]
        # Price, quantity and purchase-plan questions have their own handlers.
        # Only availability-style ellipses inherit the previous store here.
        product_question = bool(re.search(
            r"多少钱|什么价|价格|售价|几折|怎么卖|怎么收|"
            r"怎么买|如何买|怎么拍|如何拍|买几张|拍几张|叠加|几张",
            compact,
        ))
        generic_price_followup = bool(re.fullmatch(
            r"(?:(?:这家|这个店|该店|那家|刚才那家|"
            r"第一(?:家|个)|第二(?:家|个)|第三(?:家|个)|第[123](?:家|个))的?)?"
            r"(?:什么价格呢?|什么价呢?|价格(?:多少|呢)?|多少钱呢?|多钱呢?|卖多少|怎么卖)",
            compact,
        ))
        if generic_price_followup and not selected_skus:
            if ordinal is not None:
                if ordinal >= len(matrix):
                    return {
                        "reply": "刚才的查询结果中没有这家门店，请发送完整门店名称重新查询。",
                        "source": "门店上下文序号超出候选范围",
                        "decision": "allow", "kind": "stores_clarify",
                        "store_matches": [row.get("store") or {} for row in matrix],
                        "store_query": str(context.get("query") or ""),
                        "store_status": "ambiguous",
                    }
                price_rows = [matrix[ordinal]]
            elif len(matrix) == 1:
                price_rows = matrix
            else:
                names = "、".join(
                    self._store_display_name(row.get("store") or {}) for row in matrix
                )
                return {
                    "reply": f"刚才查询到多家门店：{names}。请发送完整门店名，或回复第一家、第二家再查询价格。",
                    "source": "多门店上下文中的价格指代不明确",
                    "decision": "allow", "kind": "stores_clarify",
                    "store_matches": [row.get("store") or {} for row in matrix],
                    "store_query": str(context.get("query") or ""),
                    "store_status": "ambiguous",
                }
            query = str(context.get("query") or "刚才查询的门店")
            return {
                "reply": self._format_store_sku_matrix(query, price_rows),
                "source": "当前会话最近一次门店可用SKU及实时售价",
                "decision": "allow", "kind": "stores_sku_recommendation",
                "store_matches": [row.get("store") or {} for row in price_rows],
                "store_query": query, "store_status": "available",
                "store_sku_matrix": price_rows,
            }
        spec_followup = bool(amount_followup) or bool(selected_skus and sku_followup_words)
        if product_question or not (spec_followup or generic_followup_words or all_spec_followup):
            return None

        # A new explicit location always overrides old context and is handled
        # by the ordinary store resolver below.
        new_query = extract_store_query(
            text, product=product, product_brand=self.extract_brand(product)
        )
        if ordinal is None and not spec_followup and is_meaningful_store_query(new_query) and not re.fullmatch(
            r"(?:这家|这个店|该店|那家店|那里|刚才那家|这些店|这几个店)",
            normalize_text(new_query),
        ):
            return None

        rows = matrix
        if ordinal is not None:
            if ordinal >= len(matrix):
                return {
                    "reply": "刚才的查询结果中没有这家门店，请发送完整门店名称重新查询。",
                    "source": "门店上下文序号超出候选范围",
                    "decision": "allow", "kind": "stores_clarify",
                    "store_matches": [row.get("store") or {} for row in matrix],
                    "store_query": str(context.get("query") or ""),
                    "store_status": "ambiguous",
                }
            rows = [matrix[ordinal]]
        elif len(matrix) > 1 and re.search(r"这家|这个店|该店|那家店", compact) and not selected_skus:
            names = "、".join(
                self._store_display_name(row.get("store") or {}) for row in matrix
            )
            return {
                "reply": f"刚才查询到多家门店：{names}。请发送完整门店名，或回复第一家、第二家进行确认。",
                "source": "多门店上下文中的单数指代不明确",
                "decision": "allow", "kind": "stores_clarify",
                "store_matches": [row.get("store") or {} for row in matrix],
                "store_query": str(context.get("query") or ""),
                "store_status": "ambiguous",
            }

        unconfigured = [
            sku for sku in selected_skus if not sku.get("configuration_ready")
        ]
        if unconfigured:
            labels = "、".join(self._sku_public_label(sku) for sku in unconfigured)
            return {
                "reply": f"{labels}暂未配置适用门店资料，无法准确确认。",
                "source": "上下文追问所指定规格的门店资料未配置",
                "decision": "review", "kind": "stores",
                "store_matches": [row.get("store") or {} for row in rows],
                "store_query": str(context.get("query") or ""),
                "store_status": "unconfigured",
            }

        query = str(context.get("query") or "刚才查询的门店")
        result = {
            "reply": self._format_store_sku_matrix(query, rows, selected_skus),
            "source": "当前会话最近一次门店—规格查询结果",
            "decision": "allow", "kind": "stores_sku_recommendation",
            "store_matches": [row.get("store") or {} for row in rows],
            "store_query": query, "store_status": "available",
            "store_sku_matrix": rows,
        }
        if len(selected_skus) == 1:
            result["query_context_update"] = {
                "selected_sku_key": selected_skus[0]["sku_key"],
                "selected_sku_name": selected_skus[0].get("sku_name", ""),
            }
        return result

    @staticmethod
    def _area_key(value: object) -> str:
        value = normalize_text(value)
        return re.sub(r"(?:壮族自治区|回族自治区|维吾尔自治区|特别行政区|自治区|省|市)$", "", value)

    @staticmethod
    def _store_fuzzy_key(value: object) -> str:
        value = normalize_text(value)
        for token in (
            "购物中心", "购物广场", "商业广场", "百货商场", "百货", "门店", "分店", "旗舰店", "街道",
            "商场", "广场", "省", "市", "区", "县", "镇", "乡", "村", "店",
        ):
            value = value.replace(token, "")
        return value

    @staticmethod
    def _pinyin_key(value: object) -> str:
        """Pinyin key for controlled homophone matching inside known stores."""
        if lazy_pinyin is None or Style is None:
            return ""
        text = normalize_match_text(value)
        if not text:
            return ""
        try:
            return "".join(lazy_pinyin(text, style=Style.NORMAL, errors="ignore")).lower()
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _admin_key(value: object) -> str:
        value = normalize_match_text(value)
        return re.sub(
            r"(?:壮族自治区|回族自治区|维吾尔自治区|特别行政区|自治区|"
            r"省|市|区|县|旗|镇|乡|街道)$",
            "", value,
        )

    @classmethod
    def _local_branch_key(cls, row: Dict, branch_key: str) -> str:
        value = branch_key
        prefixes = sorted({
            cls._store_fuzzy_key(row.get(field))
            for field in ("province", "city", "district")
            if row.get(field)
        }, key=len, reverse=True)
        for prefix in prefixes:
            if len(prefix) >= 2 and value.startswith(prefix) and len(value) > len(prefix) + 1:
                value = value[len(prefix):]
        return value

    @staticmethod
    def _within_one_edit(left: str, right: str) -> bool:
        """Small typo gate used only after a geographic scope was established."""
        if left == right:
            return True
        if abs(len(left) - len(right)) > 1 or min(len(left), len(right)) < 2:
            return False
        if len(left) == len(right):
            return sum(a != b for a, b in zip(left, right)) <= 1
        short, long = (left, right) if len(left) < len(right) else (right, left)
        index_short = index_long = differences = 0
        while index_short < len(short) and index_long < len(long):
            if short[index_short] == long[index_long]:
                index_short += 1
                index_long += 1
                continue
            differences += 1
            index_long += 1
            if differences > 1:
                return False
        return True

    @staticmethod
    def _store_query_target(value: object) -> Dict:
        """Keep one explicit correction target and expose unsafe relations.

        A correction such as “不是梦时代，是万象城” has a clear right-hand
        target.  A relation such as “梦时代旁边” cannot be proved from a store
        table, so the caller must ask for an exact store or address.
        """
        original = str(value or "").strip()
        target = original
        corrected = False
        correction = re.search(
            r"(?:不是|不要|别查|不查).{1,40}?"
            r"(?:而是|改成|[，,；;]\s*(?:是|查|要))\s*(.{2,80})$",
            original,
        )
        if correction:
            target = correction.group(1).strip(" ，,。；;：:!?！？")
            corrected = bool(target)
        relation_words = [word for word in STORE_RELATION_WORDS if word in target]
        excluded_without_target = bool(
            not corrected and re.search(r"(?:不是|不要|别查|不查)", target)
        )
        return {
            "original": original,
            "target": target or original,
            "corrected": corrected,
            "relation_words": relation_words,
            "excluded_without_target": excluded_without_target,
        }

    @classmethod
    def _clean_store_search_term(cls, value: object) -> str:
        """Remove conversational filler after grounded entities were parsed."""
        key = cls._store_fuzzy_key(value)
        for word in sorted(STORE_QUERY_NEUTRAL_WORDS, key=len, reverse=True):
            key = key.replace(cls._store_fuzzy_key(word), "")
        # Some earlier cleanup paths remove “有吗” from “还有吗” first.  The
        # remaining sentence-final “还” is conversational filler, never part of
        # the grounded branch name.
        key = re.sub(r"还$", "", key)
        return key.strip()

    @classmethod
    def _grounded_store_aliases(cls, value: object, field: str) -> List[str]:
        """Return conservative aliases derived from one configured field."""
        display = str(value or "").strip()
        if not display:
            return []
        aliases = {cls._store_fuzzy_key(display)}
        if field == "brand":
            aliases.update(
                cls._store_fuzzy_key(part)
                for part in re.split(r"[·•・|丨/\\\s]+", display)
                if len(normalize_match_text(part)) >= 2
            )
        elif field == "branch":
            normalized = normalize_match_text(display)
            for suffix in ("旗舰店", "分店", "门店", "店"):
                suffix_key = normalize_match_text(suffix)
                if normalized.endswith(suffix_key) and len(normalized) > len(suffix_key) + 1:
                    aliases.add(cls._store_fuzzy_key(normalized[:-len(suffix_key)]))
        return sorted(
            (alias for alias in aliases if len(alias) >= 2),
            key=len, reverse=True,
        )

    @classmethod
    def _store_scope_conflict(cls, rows: List[Dict], query: str) -> bool:
        """Reject explicit alternatives that point at incompatible regions."""
        query_key = normalize_match_text(query)
        if not re.search(r"还是|或者|或是|[、/]", str(query or "")):
            return False
        for field in ("province", "city", "district"):
            hits = {
                cls._admin_key(row.get(field))
                for row in rows
                if row.get(field)
                and len(cls._admin_key(row.get(field))) >= 2
                and cls._admin_key(row.get(field)) in query_key
            }
            if len(hits) > 1:
                return True
        return False

    @classmethod
    def _store_match_evidence(
        cls, row: Dict, query_key: str, province_scope: str,
        city_scope: str, district_scope: str, brand_alias: str = "",
    ) -> Dict:
        """Score independent, locally grounded anchors for one store row."""
        branch_key = cls._store_fuzzy_key(row.get("branch"))
        local_branch_key = cls._local_branch_key(row, branch_key)
        combined_key = cls._store_fuzzy_key("".join(
            str(row.get(field) or "") for field in ("district", "branch")
        ))
        address_key = cls._store_fuzzy_key(row.get("address"))
        aliases = list(dict.fromkeys(
            cls._grounded_store_aliases(row.get("branch"), "branch")
            + ([local_branch_key] if len(local_branch_key) >= 2 else [])
            + ([combined_key] if len(combined_key) >= 2 else [])
        ))
        evidence = []
        score = 0
        quality = "none"
        matched_anchor = ""

        if province_scope:
            evidence.append({"type": "province", "value": str(row.get("province") or ""), "score": 20})
        if city_scope:
            evidence.append({"type": "city", "value": str(row.get("city") or ""), "score": 25})
        if district_scope:
            evidence.append({"type": "district", "value": str(row.get("district") or ""), "score": 35})
        if brand_alias:
            evidence.append({"type": "brand", "value": str(row.get("brand") or ""), "score": 20})

        exact_aliases = [alias for alias in aliases if query_key == alias]
        if exact_aliases:
            matched_anchor = max(exact_aliases, key=len)
            score, quality = 100, "exact"
            evidence.append({"type": "branch", "value": matched_anchor, "score": 100})
        else:
            contained_aliases = [
                alias for alias in aliases
                if alias and (query_key in alias or alias in query_key)
            ]
            if contained_aliases:
                candidate = max(contained_aliases, key=len)
                matched_anchor = candidate if candidate in query_key else query_key
                score, quality = 90, "contained"
                evidence.append({"type": "branch", "value": candidate, "score": 90})

        # Some buyers naturally reverse a mall/operator name and its local
        # qualifier ("凯德武胜" vs canonical "武胜凯德店"). Accept only an
        # exact character multiset inside an already grounded administrative
        # scope; this is intentionally narrower than fuzzy matching.
        reordered_query = query_key[:-1] if query_key.endswith(("路", "街")) else query_key
        if (
            score < 90 and (province_scope or city_scope or district_scope)
            and len(reordered_query) >= 4
        ):
            reordered_aliases = [
                alias for alias in aliases
                if len(alias) == len(reordered_query)
                and sorted(alias) == sorted(reordered_query)
            ]
            if reordered_aliases:
                candidate = max(reordered_aliases, key=len)
                score, quality = 95, "reordered"
                matched_anchor = query_key
                evidence.append({"type": "reordered_branch", "value": candidate, "score": 95})

        if query_key and address_key and query_key in address_key:
            address_score = 90 if re.search(r"\d+号", query_key) else 85
            if address_score > score:
                score, quality = address_score, "address"
                matched_anchor = query_key
            evidence.append({"type": "address", "value": query_key, "score": address_score})

        ratios = [
            (SequenceMatcher(None, query_key, alias).ratio(), alias)
            for alias in aliases if query_key and alias
        ]
        best_ratio, best_alias = max(ratios, default=(0.0, ""))
        if score < 75 and best_ratio >= 0.76:
            score, quality = round(best_ratio * 80), "fuzzy"
            matched_anchor = query_key
            evidence.append({"type": "fuzzy_branch", "value": best_alias, "score": score})

        if (
            score < 85 and (province_scope or city_scope or district_scope)
            and local_branch_key and cls._within_one_edit(query_key, local_branch_key)
        ):
            score, quality = 85, "one_edit"
            matched_anchor = query_key
            evidence.append({"type": "one_edit_branch", "value": local_branch_key, "score": 85})

        query_pinyin = cls._pinyin_key(query_key)
        pinyin_keys = {
            cls._pinyin_key(alias) for alias in aliases if alias
        }
        pinyin_keys.discard("")
        if len(query_pinyin) >= 6 and any(
            query_pinyin == key or query_pinyin in key
            or ((province_scope or city_scope or district_scope) and key in query_pinyin)
            for key in pinyin_keys
        ):
            if quality != "exact":
                score, quality = max(score, 90), "phonetic"
                matched_anchor = query_key
            evidence.append({"type": "unique_phonetic_candidate", "value": query_key, "score": 90})

        unexplained = query_key
        removable = [matched_anchor, brand_alias]
        removable.extend(
            cls._admin_key(row.get(field)) for field in ("province", "city", "district")
        )
        for alias in sorted((value for value in removable if len(value) >= 2), key=len, reverse=True):
            unexplained = unexplained.replace(alias, "", 1)
        unexplained = cls._clean_store_search_term(unexplained)
        if unexplained:
            score = max(0, score - 25)
            evidence.append({"type": "unresolved", "value": unexplained, "score": -25})

        return {
            "score": int(score), "quality": quality,
            "evidence": evidence, "unresolved": unexplained,
        }

    @classmethod
    def _best_location_query(cls, rows: List[Dict], query: str) -> str:
        """Extract the most specific known location from a noisy buyer sentence.

        The caller keeps the original sentence for all non-store intents.  This
        helper works on a punctuation/whitespace-free copy and only returns
        administrative units, streets, malls or branches grounded in the store
        table (plus the built-in province/city list).  Words from another
        question therefore cannot become part of a fuzzy store key.
        """
        query_key = normalize_match_text(query)
        if not query_key:
            return ""
        query_province, query_city, query_district, _ = cls._extract_store_scope(
            rows, normalize_text(query)
        )

        row_candidates = []
        area_candidates = []
        street_candidates = []
        landmark_aliases = GENERIC_STORE_LANDMARKS

        def variants(value: object, suffixes=()):
            display = str(value or "").strip()
            key = normalize_match_text(display)
            if not key:
                return []
            output = [(key, display)]
            for suffix in suffixes:
                suffix_key = normalize_match_text(suffix)
                if key.endswith(suffix_key) and len(key) > len(suffix_key) + 1:
                    output.append((key[:-len(suffix_key)], display))
            return output

        for row in rows:
            province = str(row.get("province") or "").strip()
            city = str(row.get("city") or "").strip()
            district = str(row.get("district") or "").strip()
            branch = str(row.get("branch") or "").strip()

            row_area_hit = False
            for value, suffixes in (
                (province, ("省", "市", "自治区", "特别行政区")),
                (city, ("市",)),
                (district, ("区", "县")),
            ):
                if any(
                    len(key) >= 2 and key in query_key
                    for key, _ in variants(value, suffixes)
                ):
                    row_area_hit = True
                    break

            branch_hits = []
            full_branch_hit = False
            for key, _ in variants(branch, ("旗舰店", "分店", "门店", "店")):
                fuzzy_key = cls._store_fuzzy_key(key)
                for alias in dict.fromkeys((key, fuzzy_key)):
                    if len(alias) >= 2 and alias in query_key:
                        branch_hits.append(alias)
                        full_branch_hit = True
            # Buyers often omit a district prefix stored in the formal branch
            # name: “深圳壹方城” must match “深圳 / 宝安壹方城店”.  Only enable this
            # smaller landmark unit when the same row is anchored by a city,
            # province or district from the buyer text, avoiding a cross-city
            # match on a generic name such as “万达”.
            branch_key = normalize_match_text(branch)
            if row_area_hit:
                for landmark in landmark_aliases:
                    alias = normalize_match_text(landmark)
                    row_scope_compatible = (
                        (not query_province or not province or cls._admin_key(province) == query_province)
                        and (not query_city or cls._admin_key(city) == query_city)
                        and (not query_district or cls._admin_key(district) == query_district)
                    )
                    if (
                        row_scope_compatible and len(alias) >= 2
                        and alias in query_key and alias in branch_key
                    ):
                        branch_hits.append(alias)
            if branch_hits:
                best_alias = max(branch_hits, key=len)
                # Without a grounded province/city/district, do not erase an
                # extra buyer qualifier merely because the tail resembles a
                # branch.  Example: “青山印象城店” must not silently become the
                # unrelated bare “印象城店”. It will remain a fuzzy candidate
                # that requires confirmation below.
                if not row_area_hit:
                    unexplained = query_key.replace(best_alias, "", 1)
                    brand_key = normalize_match_text(row.get("brand"))
                    if brand_key:
                        unexplained = unexplained.replace(brand_key, "", 1)
                    if len(unexplained) >= 2:
                        continue
                pieces = []
                rendered_branch = branch if full_branch_hit else best_alias
                for value, suffixes in (
                    (province, ("省", "市", "自治区", "特别行政区")),
                    (city, ("市",)),
                    (district, ("区", "县")),
                ):
                    if not value:
                        continue
                    keys = [key for key, _ in variants(value, suffixes)]
                    if any(len(key) >= 2 and key in query_key for key in keys):
                        # Compare the area against the fragment that will
                        # actually be returned.  The formal branch may contain
                        # “杭州” while a buyer only typed “杭州万象城”; comparing
                        # against the formal branch used to erase the city and
                        # turn this into a cross-city “万象城” lookup.
                        current = normalize_match_text("".join(pieces) + rendered_branch)
                        if not any(key and key in current for key in keys):
                            pieces.append(value)
                pieces.append(rendered_branch)
                canonical = "".join(pieces)
                row_candidates.append((len(best_alias), canonical))

            matched_areas = {}
            for name, weight, value, suffixes in (
                ("district", 4, district, ("区", "县")),
                ("city", 3, city, ("市",)),
                ("province", 2, province, ("省", "市", "自治区", "特别行政区")),
            ):
                hits = [
                    key for key, _ in variants(value, suffixes)
                    if len(key) >= 2 and key in query_key
                ]
                if hits:
                    matched_areas[name] = (weight, max(len(key) for key in hits), value)
            if matched_areas:
                most_specific = max(matched_areas.values(), key=lambda value: value[0])
                hierarchy = [
                    matched_areas[name][2]
                    for name in ("province", "city", "district")
                    if name in matched_areas
                ]
                canonical = "".join(
                    value for index, value in enumerate(hierarchy)
                    if normalize_match_text(value) not in normalize_match_text(
                        "".join(hierarchy[index + 1:])
                    )
                )
                area_candidates.append((
                    most_specific[0],
                    sum(value[1] for value in matched_areas.values()),
                    canonical,
                ))

            address = str(row.get("address") or "").strip()
            normalized_address = unicodedata.normalize("NFKC", address)
            for suffix_match in re.finditer(r"街道|大道|路|街|巷", normalized_address):
                end = suffix_match.end()
                start_floor = max(0, end - 24)
                for start in range(start_floor, max(start_floor, end - 1)):
                    street = normalized_address[start:end].strip(" ，,。；;：:0123456789")
                    street_key = normalize_match_text(street)
                    # Two-character address tails such as "胜路" are too weak:
                    # they previously hijacked a precise mall query and matched
                    # an unrelated branch whose address happened to contain it.
                    if len(street_key) >= 3 and street_key in query_key:
                        street_candidates.append((len(street_key), street))

        if row_candidates:
            return max(row_candidates, key=lambda value: (value[0], len(value[1])))[1]
        if street_candidates:
            return max(street_candidates, key=lambda value: value[0])[1]
        store_specific_tail = any(
            marker in query_key for marker in (
                "门店", "分店", "旗舰店", "购物中心", "商业广场", "商场", "商圈",
                "万达", "万象城", "万象汇", "壹方城", "壹方天地", "万科里",
                "海岸城", "天街", "银泰", "吾悦", "大悦城", "来福士",
                "太古里", "印象城", "奥特莱斯", "ifs", "mall", "店",
                "凯德",
            )
        )
        if area_candidates and not store_specific_tail:
            _, _, _, remainder = cls._extract_store_scope(rows, normalize_text(query))
            remainder = re.sub(
                r"(?:可以|能|可)?直接(?:拍|买|购买|下单).*$|"
                r"(?:拍下|下单|购买)(?:后)?.*$",
                "", remainder,
            )
            remainder = re.sub(r"\d+(?:\.\d+)?", "", remainder)
            for noise in (
                "可以使用吗", "可以用吗", "能不能用", "可不可以用", "是否可用",
                "可以使用", "可以用", "能用", "可用", "适用", "支持",
                "有哪些门店", "哪些门店", "有门店吗", "有吗", "有没有",
                "今天", "今日", "现在", "当前", "什么优惠", "有啥优惠",
                "有什么优惠", "优惠活动", "活动", "优惠", "折扣",
                "请问", "老板", "亲", "您好", "你好", "是吧", "的那个", "那个",
                "可以", "是", "吗", "嘛", "么", "呀", "呢", "吧",
            ):
                remainder = remainder.replace(normalize_text(noise), "")
            if not remainder:
                return max(area_candidates, key=lambda value: (value[0], value[1]))[2]
            # A meaningful suffix remains (for example “济南弘扬”). Keep the
            # whole local query so the suffix can be matched inside that city;
            # never silently downgrade it to the city alone.
            return ""
        if store_specific_tail:
            # Keep an unmatched mall/branch term intact. Reducing “焦作万达” or
            # “郑州万象城” to the city alone would turn a precise negative query
            # into a false city-wide positive result.
            return ""

        # A city/province that is absent from the current store table still has
        # to produce a clean negative lookup instead of a fuzzy cross-city hit.
        known = []
        for weight, values, suffixes in (
            (3, KNOWN_CITY_NAMES, ("市",)),
            (2, KNOWN_PROVINCE_NAMES, ("省", "市", "自治区", "特别行政区")),
        ):
            for value in values:
                for key, _ in variants(value, suffixes):
                    if len(key) >= 2 and key in query_key:
                        match = re.search(re.escape(key), query_key)
                        known.append((weight, len(key), -(match.start() if match else 0), value))
        return max(known, default=(0, 0, 0, ""))[3]

    @classmethod
    def _extract_store_scope(cls, rows: List[Dict], query_norm: str):
        """Find all administrative tokens and keep the smallest grounded unit.

        Buyer text commonly contains a hierarchy such as “山东潍坊寿光万达”.
        The last equally-ranked unit is normally the smaller county-level city,
        while a district/borough always outranks a city and a city outranks a
        province.  All detected hierarchy tokens are removed from the local
        branch search term so they cannot become fuzzy noise.
        """
        query_key = normalize_match_text(query_norm)
        field_values = {
            "province": {
                cls._admin_key(value) for value in KNOWN_PROVINCE_NAMES
            } | {
                cls._admin_key(row.get("province")) for row in rows if row.get("province")
            },
            "city": {
                cls._admin_key(value) for value in KNOWN_CITY_NAMES
            } | {
                cls._admin_key(value) for value in KNOWN_SUBCITY_TO_CITY
            } | {
                cls._admin_key(row.get("city")) for row in rows if row.get("city")
            },
            "district": {
                cls._admin_key(row.get("district")) for row in rows if row.get("district")
            },
        }
        grounded_values = {
            field: {
                cls._admin_key(row.get(field)) for row in rows if row.get(field)
            }
            for field in ("province", "city", "district")
        }
        all_admin_values = {
            value for values in field_values.values() for value in values if len(value) >= 2
        }
        hits = {"province": [], "city": [], "district": []}
        removable = []
        suffixes = {
            "province": ("壮族自治区", "回族自治区", "维吾尔自治区", "特别行政区", "自治区", "省", "市"),
            "city": ("市",),
            "district": ("区", "县", "旗", "市"),
        }
        for field, values in field_values.items():
            for value in sorted((item for item in values if len(item) >= 2), key=len, reverse=True):
                variants = tuple(dict.fromkeys((*(value + suffix for suffix in suffixes[field]), value)))
                best = None
                for variant in variants:
                    start = query_key.rfind(variant)
                    # A built-in city/province absent from this store table is
                    # useful for a clean negative only at the beginning of the
                    # cleaned query. This prevents “南山西丽” from inventing the
                    # cross-boundary province token “山西”.
                    if start > 0 and value not in grounded_values[field]:
                        prefix = query_key[:start]
                        if not any(prefix.endswith(area) for area in all_admin_values):
                            continue
                    if start >= 0 and (best is None or len(variant) > len(best[2])):
                        best = (start, start + len(variant), variant)
                if best:
                    hits[field].append((best[0], value))
                    removable.append((best[0], best[1]))

        def last_hit(field: str) -> str:
            return max(hits[field], default=(-1, ""), key=lambda item: item[0])[1]

        province = last_hit("province")
        city = last_hit("city")
        city = KNOWN_SUBCITY_TO_CITY.get(city, city)
        district = last_hit("district")
        chars = list(query_key)
        for start, end in removable:
            for index in range(start, end):
                chars[index] = ""
        remainder = "".join(chars)
        # Store spreadsheets do not always provide a district column. Once a
        # city/province is grounded, remove one explicit leading district token
        # from the branch remainder instead of treating it as fuzzy name noise.
        if (province or city) and not district:
            remainder = re.sub(
                r"^[\u4e00-\u9fff]{2,6}?(?:区|县|旗)(?=[\u4e00-\u9fffA-Za-z0-9])",
                "", remainder, count=1,
            )
        return province, city, district, remainder

    @classmethod
    def _parse_store_query_entities(cls, rows: List[Dict], query: str) -> Dict:
        """Split a store query into administrative, brand and branch entities.

        This is deliberately data-grounded: brands and branches must exist in
        the current product's store rows. Connectors are removed only after an
        entity has been recognized, so real store names are never globally
        rewritten.
        """
        query_norm = normalize_text(query)
        province, city, district, remainder = cls._extract_store_scope(rows, query_norm)
        remainder = re.sub(r"^(?:的|在|位于)+|(?:的|那边|这边|地区)$", "", remainder)
        remainder_key = cls._store_fuzzy_key(remainder)
        full_key = cls._store_fuzzy_key(query_norm)

        brands = sorted({
            (alias, cls._store_fuzzy_key(row.get("brand")), str(row.get("brand") or "").strip())
            for row in rows if row.get("brand")
            for alias in cls._grounded_store_aliases(row.get("brand"), "brand")
        }, key=lambda item: len(item[0]), reverse=True)
        brand_alias = brand_key = brand = ""
        for candidate_alias, candidate_key, candidate in brands:
            if len(candidate_alias) >= 2 and candidate_alias in full_key:
                brand_alias, brand_key, brand = candidate_alias, candidate_key, candidate
                break
        if brand_alias:
            remainder_key = remainder_key.replace(brand_alias, "", 1)
            remainder_key = re.sub(r"^(?:的|在|位于)+|(?:的|那边|这边|地区)$", "", remainder_key)
        remainder_key = cls._clean_store_search_term(remainder_key)

        branches = sorted(
            {
                (cls._store_fuzzy_key(row.get("branch")), str(row.get("branch") or "").strip())
                for row in rows if row.get("branch")
            },
            key=lambda item: len(item[0]), reverse=True,
        )
        branch_key = branch = ""
        for candidate_key, candidate in branches:
            if len(candidate_key) >= 2 and (
                remainder_key == candidate_key or full_key == candidate_key
            ):
                branch_key, branch = candidate_key, candidate
                break

        return {
            "province": province, "city": city, "district": district,
            "brand": brand, "brand_key": brand_key, "brand_alias": brand_alias,
            "branch": branch, "branch_key": branch_key,
            "search_term": "" if branch else remainder_key,
        }

    def search_store(self, item_id: str, query: str, limit: Optional[int] = None,
                     sku_key: str = "", list_ids_override: Optional[List[int]] = None) -> Dict:
        query_meta = self._store_query_target(query)
        query = str(query_meta["target"] or query)
        query_norm = normalize_text(query)
        # Intent words such as “问题/门店/查询” are not locations.  Refuse them
        # before fuzzy matching so a generic question can never accidentally
        # match a branch such as “广场店”.
        if not is_meaningful_store_query(query):
            return {"status": "missing_query", "matches": []}
        # A standalone number is a denomination/amount, never a store name.
        if re.fullmatch(r"\d+(?:\.\d+)?(?:元)?", query_norm):
            return {"status": "unavailable", "matches": []}
        if list_ids_override is None:
            list_ids, mode, ready = self.effective_store_list_ids(item_id, sku_key)
        else:
            list_ids = sorted({int(value) for value in list_ids_override})
            mode, ready = "product_union", bool(list_ids)
        if not ready:
            return {"status": "sku_store_unconfigured", "matches": [], "sku_key": sku_key}
        if not list_ids:
            return {"status": "no_store_list", "matches": [], "sku_key": sku_key}
        placeholders = ",".join("?" for _ in list_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT s.*, sl.name AS list_name FROM stores s
                    JOIN store_lists sl ON sl.id=s.list_id
                    WHERE s.list_id IN ({placeholders})""",
                list_ids,
            ).fetchall()
        if not rows:
            return {"status": "no_store_list", "matches": []}
        rows = [
            dict(row) for row in rows
            if not self._looks_like_product_title(
                str(row["branch"] or row["brand"] or "")
            )
        ]
        input_query = query
        cleaned_query = extract_store_query(query)
        if is_meaningful_store_query(cleaned_query):
            query = cleaned_query
            query_norm = normalize_text(query)

        alias_key = normalize_match_text(query)
        canonical_alias = next((
            canonical for alias, canonical in STORE_QUERY_ALIASES.items()
            if normalize_match_text(alias) == alias_key
        ), "")
        if canonical_alias:
            alias_matches = [
                row for row in rows
                if normalize_match_text(row.get("branch")) == normalize_match_text(canonical_alias)
            ]
            if alias_matches:
                for item in alias_matches:
                    item["score"] = 1.0
                    item["match_score"] = 100
                    item["match_quality"] = "alias"
                    item["match_evidence"] = [{
                        "type": "configured_alias", "value": canonical_alias, "score": 100,
                    }]
                    item["unresolved_terms"] = []
                return {
                    "status": "available", "matches": alias_matches,
                    "province": "", "city": "", "district": "",
                    "search_term": query, "sku_key": sku_key, "mode": mode,
                    "resolved_query": query,
                }
        if query_meta["relation_words"]:
            return {
                "status": "relation_query", "matches": [],
                "resolved_query": query,
                "specific_query": query,
                "relation_words": query_meta["relation_words"],
            }
        if query_meta["excluded_without_target"]:
            return {
                "status": "excluded_query", "matches": [],
                "resolved_query": query, "specific_query": query,
            }
        if self._store_scope_conflict(rows, query):
            return {
                "status": "conflicting_scope", "matches": [],
                "resolved_query": query, "specific_query": query,
            }
        entities = self._parse_store_query_entities(rows, query_norm)
        original_scope = (
            entities["province"], entities["city"], entities["district"],
            entities["search_term"],
        )
        original_area = any(original_scope[:3])
        original_term = self._store_fuzzy_key(original_scope[3])
        exact_brand_area_query = original_area and bool(entities["brand"])
        # “宜家武汉/武汉宜家” already contains two reliable entities. Fuzzy
        # branch recovery must not rewrite it to an invented full branch name.
        has_street_number = bool(re.search(r"(?:路|街|道|巷)\s*\d+号", str(query or "")))
        resolved_query = (
            "" if exact_brand_area_query or has_street_number
            else self._best_location_query(rows, input_query)
        )
        if resolved_query:
            query = resolved_query
            query_norm = normalize_text(query)
        province_scope, city_scope, district_scope, search_term = self._extract_store_scope(rows, query_norm)
        if province_scope:
            rows = [
                row for row in rows
                if self._admin_key(row.get("province")) == province_scope
                or (city_scope and not self._area_key(row.get("province")))
            ]
        if city_scope:
            rows = [row for row in rows if self._admin_key(row.get("city")) == city_scope]
        if district_scope:
            rows = [row for row in rows if self._admin_key(row.get("district")) == district_scope]
        # Keep the buyer's grounded city/district scope before applying any
        # brand or branch filters. If the concrete branch cannot be found we
        # can still offer every configured branch in that exact area without
        # turning the failed branch lookup into a false availability claim.
        area_rows = list(rows)
        if entities["brand"]:
            rows = [
                row for row in rows
                if self._store_fuzzy_key(row.get("brand")) == entities["brand_key"]
            ]
            search_term = search_term.replace(entities["brand_alias"], "", 1)
            search_term = re.sub(
                r"^(?:的|在|位于)+|(?:的|那边|这边|地区)$", "", search_term
            )
        if entities["branch"]:
            rows = [
                row for row in rows
                if self._store_fuzzy_key(row.get("branch")) == entities["branch_key"]
            ]
            search_term = ""

        # Buyers often repeat the brand after a city (for example
        # “深圳同仁四季有吗”).  A brand is not a concrete branch name; after the
        # region has been fixed, return all branches in that region.  A very
        # small typo is accepted only here, where both the region and a
        # four-character brand provide strong evidence.
        if (province_scope or city_scope or district_scope) and search_term:
            term_key = self._store_fuzzy_key(search_term)
            brand_keys = {
                self._store_fuzzy_key(row.get("brand"))
                for row in rows if row.get("brand")
            }
            brand_only = any(
                term_key and brand_key and (
                    term_key == brand_key
                    or term_key in brand_key
                    or brand_key in term_key
                    or (
                        len(term_key) >= 4 and len(brand_key) >= 4
                        and SequenceMatcher(None, term_key, brand_key).ratio() >= 0.74
                    )
                )
                for brand_key in brand_keys
            )
            if brand_only:
                search_term = ""

        search_term = self._clean_store_search_term(search_term)

        # A province/city-only question must return every branch in that scope.
        if (province_scope or city_scope or district_scope) and not search_term:
            for item in rows:
                item["score"] = 1.1
                item["match_score"] = 100
                item["match_quality"] = "area"
                item["match_evidence"] = [
                    {"type": field, "value": str(item.get(field) or ""), "score": score}
                    for field, score, scope in (
                        ("province", 20, province_scope),
                        ("city", 25, city_scope),
                        ("district", 35, district_scope),
                    ) if scope
                ]
                item["unresolved_terms"] = []
            matches = rows
        else:
            query_key = self._clean_store_search_term(search_term or query_norm)
            strong = []
            uncertain = []
            for item in rows:
                store_name = str(item.get("branch") or item.get("brand") or "").strip()
                if self._looks_like_product_title(store_name):
                    continue
                scored = self._store_match_evidence(
                    item, query_key, province_scope, city_scope, district_scope,
                    entities.get("brand_alias") or "",
                )
                item["score"] = round(scored["score"] / 100, 3)
                item["match_score"] = scored["score"]
                item["match_quality"] = scored["quality"]
                item["match_evidence"] = scored["evidence"]
                item["unresolved_terms"] = [scored["unresolved"]] if scored["unresolved"] else []
                if scored["score"] >= 85 and not scored["unresolved"]:
                    strong.append(item)
                elif scored["score"] >= 60:
                    uncertain.append(item)
            matches = strong
            if matches:
                best = max(value["match_score"] for value in matches)
                matches = [value for value in matches if value["match_score"] >= best - 15]
            elif uncertain:
                best = max(value["match_score"] for value in uncertain)
                matches = [value for value in uncertain if value["match_score"] >= best - 5]
        matches.sort(
            key=lambda value: (
                -value["score"], value.get("province") or "", value.get("city") or "",
                value.get("branch") or value.get("brand") or "",
            )
        )
        unique = []
        seen = set()
        for item in matches:
            key = self._physical_store_key(item)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        matches = unique[: max(1, int(limit))] if limit else unique
        scoped_location = "".join(filter(None, (
            district_scope or city_scope or province_scope,
            search_term,
        )))
        output_query = query if resolved_query else (
            scoped_location
            if matches and is_meaningful_store_query(scoped_location)
            else input_query
        )
        scoped_query = (
            scoped_location
            if (matches or resolved_query) and is_meaningful_store_query(scoped_location)
            else output_query
        )
        generic_key = self._store_fuzzy_key(search_term or query_norm)
        generic_without_area = (
            not (province_scope or city_scope or district_scope)
            and generic_key in {self._store_fuzzy_key(value) for value in GENERIC_STORE_LANDMARKS}
        )
        if generic_without_area and len(matches) > 1:
            return {
                "status": "ambiguous_area", "matches": matches,
                "province": province_scope, "city": city_scope,
                "district": district_scope, "search_term": search_term,
                "resolved_query": output_query, "specific_query": scoped_query,
                "candidate_count": len(matches),
            }
        if not matches:
            fallback_area = district_scope or city_scope
            if fallback_area and search_term and area_rows:
                fallback_matches = []
                fallback_seen = set()
                for item in sorted(
                    area_rows,
                    key=lambda value: (
                        value.get("province") or "", value.get("city") or "",
                        value.get("district") or "",
                        value.get("branch") or value.get("brand") or "",
                    ),
                ):
                    key = self._physical_store_key(item)
                    if key in fallback_seen:
                        continue
                    fallback_seen.add(key)
                    enriched = dict(item)
                    enriched["score"] = 1.0
                    enriched["match_score"] = 100
                    enriched["match_quality"] = "area_fallback"
                    enriched["match_evidence"] = [{
                        "type": "district" if district_scope else "city",
                        "value": fallback_area,
                        "score": 100,
                    }]
                    enriched["unresolved_terms"] = []
                    fallback_matches.append(enriched)
                if fallback_matches:
                    area_query = "".join(filter(None, (
                        city_scope if district_scope else "", fallback_area,
                    )))
                    return {
                        "status": "area_fallback", "matches": fallback_matches,
                        "province": province_scope, "city": city_scope,
                        "district": district_scope, "search_term": search_term,
                        "resolved_query": output_query, "specific_query": scoped_query,
                        "fallback_area_query": area_query or fallback_area,
                        "sku_key": sku_key, "mode": mode,
                    }
            return {
                "status": "unavailable", "matches": [], "province": province_scope,
                "city": city_scope, "district": district_scope, "search_term": search_term,
                "resolved_query": output_query, "specific_query": scoped_query,
            }
        unique_high_phonetic = bool(
            len(matches) == 1
            and matches[0].get("match_quality") == "phonetic"
            and int(matches[0].get("match_score") or 0) >= 90
            and not matches[0].get("unresolved_terms")
        )
        unique_high_typo = bool(
            len(matches) == 1
            and bool(province_scope or city_scope or district_scope)
            and matches[0].get("match_quality") == "one_edit"
            and int(matches[0].get("match_score") or 0) >= 85
            and not matches[0].get("unresolved_terms")
        )
        close_competing_candidates = bool(
            len(generic_key) >= 4 and search_term and len(matches) > 1
            and int(matches[0].get("match_score") or 0)
            - int(matches[1].get("match_score") or 0) < 20
        )
        uncertain_evidence = bool(
            matches and all(
                int(item.get("match_score") or 0) < 85
                or bool(item.get("unresolved_terms"))
                or item.get("match_quality") == "fuzzy"
                for item in matches
            )
        )
        if (
            close_competing_candidates
            or (uncertain_evidence and not (unique_high_phonetic or unique_high_typo))
        ):
            return {
                "status": "needs_confirmation", "matches": matches,
                "province": province_scope, "city": city_scope,
                "district": district_scope, "search_term": search_term,
                "sku_key": sku_key, "mode": mode, "resolved_query": output_query,
                "specific_query": scoped_query,
            }
        return {
            "status": "available", "matches": matches, "province": province_scope,
            "city": city_scope, "district": district_scope, "search_term": search_term,
            "sku_key": sku_key, "mode": mode,
            "resolved_query": output_query,
        }

    @staticmethod
    def _looks_like_product_title(value: str) -> bool:
        value = str(value or "").strip()
        if not value:
            return True
        product_markers = (
            "代金券", "套餐", "团购", "优惠", "搜索标签", "聚餐", "美食",
            "堂食", "包间", "现货秒发", "自动发货", "全场通用",
        )
        return (
            len(value) > 32
            or sum(marker in value for marker in product_markers) >= 2
            or "搜索标签" in value
        )

    @staticmethod
    def format_store_reply(match: Dict, message: str = "") -> str:
        """Reply with only the requested store details; partial fields remain usable."""
        message = str(message or "")
        display = match.get("branch") or match.get("brand") or "该门店"
        parts = [f"{display}可用。"]
        requested_missing = []
        detail_rules = (
            (("地址", "位置", "在哪", "怎么走"), "address", "地址"),
            (("电话", "号码", "联系"), "phone", "电话"),
            (("营业", "开门", "关门", "打烊", "几点"), "business_hours", "营业时间"),
        )
        for words, field, label in detail_rules:
            if any(word in message for word in words):
                value = str(match.get(field) or "").strip()
                if value:
                    parts.append(f"{label}：{value}。")
                else:
                    requested_missing.append(label)
        if requested_missing:
            parts.append(f"当前可用门店资料暂未提供{'、'.join(requested_missing)}。")
        return "".join(parts)

    @staticmethod
    def format_store_matches(matches: List[Dict], query: str, message: str = "") -> str:
        if not matches:
            return V2Store.format_store_unavailable(query)
        grouped = {}
        needs_address = any(word in str(message or "") for word in ("地址", "位置", "在哪", "怎么走"))
        needs_phone = any(word in str(message or "") for word in ("电话", "号码", "联系"))
        for match in matches:
            city = str(match.get("city") or match.get("province") or "其他地区").strip()
            name = str(match.get("branch") or match.get("brand") or "未命名门店").strip()
            details = []
            if needs_address and match.get("address"):
                details.append(str(match["address"]).strip())
            if needs_phone and match.get("phone"):
                details.append(str(match["phone"]).strip())
            display = f"{name}（{'；'.join(details)}）" if details else name
            grouped.setdefault(city, []).append(display)
        lines = []
        for city, names in grouped.items():
            lines.append(f"{city}：{'、'.join(dict.fromkeys(names))}")
        query_value = str(query or "该关键词").strip(" \t\r\n，,。！？!?~～") or "该关键词"
        displays = []
        for city, names in grouped.items():
            if len(grouped) == 1:
                displays.extend(names)
            else:
                displays.extend(f"{city}{name}" for name in names)
        return f"可以用，根据“{query_value}”查询到可用门店：{'、'.join(dict.fromkeys(displays))}。"

    @classmethod
    def format_store_area_fallback(cls, result: Dict, query: str, message: str = "") -> str:
        """List area alternatives while making clear the requested branch was not verified."""
        area = str(result.get("fallback_area_query") or result.get("district")
                   or result.get("city") or "该地区").strip()
        matches = list(result.get("matches") or [])
        listing = cls.format_store_matches(matches, area, message)
        marker = f"可以用，根据“{area}”查询到可用门店："
        if listing.startswith(marker):
            listing = f"以下为{area}的所有可用门店：" + listing[len(marker):]
        value = str(query or "该门店").strip(" \t\r\n，,。！？!?~～") or "该门店"
        return f"根据“{value}”未查询到可用门店。\n{listing}"

    @classmethod
    def format_store_unavailable(cls, query: str, area_only: bool = False) -> str:
        value = str(query or "该关键词").strip(" \t\r\n，,。！？!?~～") or "该关键词"
        if area_only:
            return f"根据“{value}”未查询到可用门店。当前商品在该地区暂无已配置的可用门店。"
        return (
            f"根据“{value}”未查询到可用门店。\n"
            "请补充所在城市或区县，我为您查询该地区的所有可用门店。"
        )

    @classmethod
    def is_explicit_store_query(cls, message: str, query: str, result: Optional[Dict] = None) -> bool:
        """Recognize places and shopping-centre names before generic “有吗” rules."""
        raw_query = str(query or "").strip(" \t\r\n，,。！？!?~～")
        message_key = normalize_text(message)
        query_key = normalize_text(query)
        compact_message = re.sub(r"[\s，,。.!！?？~～]+", "", str(message or "")).lower()
        usage_subject = bool(re.search(
            r"锅底|酒水|饮料|菜品|餐品|套餐|包间|堂食|外带|打包|外卖",
            message_key,
        ))
        grounded_store_match = bool(
            result and result.get("status") == "available"
            and any(
                int(match.get("match_score") or 0) >= 85
                and not match.get("unresolved_terms")
                for match in result.get("matches") or []
            )
        )
        known_areas = {
            cls._area_key(value) for value in KNOWN_CITY_NAMES | KNOWN_PROVINCE_NAMES
        }
        has_location = (
            any(word in message_key for word in STORE_LANDMARK_WORDS)
            or any(area and area in message_key for area in known_areas if len(area) >= 2)
            or bool(re.search(r"(?:省|市|区|县|镇|乡|村|街道|大道|路|街|巷|商圈|商场|广场|购物中心|门店|分店|店)", raw_query))
            or grounded_store_match
        )
        temporal_subject = bool(re.search(
            r"今天|今日|今晚|今早|今中午|明天|明日|明早|明晨|明中午|明午|明晚|后天|"
            r"周末|工作日|平日|节假日|法定假日|"
            r"中秋(?:节|期间)?|国庆(?:节|期间)?|春节|元旦|劳动节|"
            r"(?:\d{4}[年./-])?\d{1,2}[月./-]\d{1,2}(?:日|号)?",
            message_key,
        ))
        if temporal_subject and not has_location:
            return False
        if usage_subject and not has_location:
            return False
        # These sentences express store intent but contain no actual location.
        # They must reach the clarification reply instead of falling through to
        # the model, and must never use an intent word as a fuzzy store key.
        if not is_meaningful_store_query(query) and re.fullmatch(
            r"(?:适用门店(?:相关)?问题|门店(?:相关)?问题|"
            r"想(?:问|咨询)(?:一下|下)?(?:适用)?门店|"
            r"(?:这个|这张)?券?(?:有哪些|有什么|哪些)(?:适用|可用)?(?:门店|店)|"
            r"(?:哪里|哪儿)(?:可以|能|可)?(?:使用|用)|"
            r"(?:有哪些|有什么|哪些)(?:地方|门店|店)(?:可以|能|可)?(?:使用|用)?)",
            compact_message,
        ):
            return True
        if re.fullmatch(r"\d+(?:\.\d+)?(?:元)?", raw_query):
            return False
        if re.search(
            r"多重|重量|几斤|多少斤|多少克|口味|味道|辣不辣|有什么菜|"
            r"包含什么|有米饭|有配菜|几个人|适合几人",
            message_key,
        ):
            return False
        if re.search(
            r"\d+(?:\.\d+)?(?:元)?(?:有吗|有没有|多少钱|怎么拍|怎么买)|"
            r"(?:套餐|代金券|优惠券|面额).{0,12}(?:有吗|有没有|多少钱)",
            message_key,
        ):
            return False
        if any(word in message_key for word in STORE_LANDMARK_WORDS):
            return True
        if any(query_key.startswith(area) for area in known_areas if len(area) >= 2):
            return True
        if re.search(
            r"(?:省|市|区|县|镇|乡|村|街道|大道|路|街|巷|"
            r"商圈|商场|广场|购物中心|门店|分店|店)$",
            raw_query,
        ):
            return True
        if re.search(r"(?:门店|分店|店)$", compact_message):
            return True
        # A bare store name is accepted only for an exact/contained branch-name
        # match. A weak fuzzy result is not enough to invent store intent.
        if result and result.get("status") == "available":
            for match in result.get("matches") or []:
                if (
                    int(match.get("match_score") or 0) >= 85
                    and not match.get("unresolved_terms")
                ):
                    return True
                branch_key = cls._store_fuzzy_key(match.get("branch") or match.get("brand"))
                row_areas = {
                    cls._area_key(match.get("province")),
                    cls._area_key(match.get("city")),
                }
                if query_key in row_areas:
                    return True
                if query_key and branch_key and (
                    query_key == branch_key or query_key in branch_key or branch_key in query_key
                ):
                    return True
        return False

    def bind_store_list(self, list_id: int, item_ids: List[str]):
        with self._connect() as conn:
            conn.execute("DELETE FROM product_store_lists WHERE list_id=?", (list_id,))
            for item_id in {str(value).strip() for value in item_ids if str(value).strip()}:
                conn.execute(
                    "INSERT INTO product_store_lists(item_id,list_id) VALUES(?,?)",
                    (item_id, list_id),
                )

    def set_product_store_lists(self, item_id: str, list_ids: List[int]):
        item_id = str(item_id or "").strip()
        if not item_id:
            raise ValueError("商品ID不能为空")
        normalized_ids = {int(value) for value in list_ids if str(value).strip()}
        with self._connect() as conn:
            conn.execute("DELETE FROM product_store_lists WHERE item_id=?", (item_id,))
            for list_id in normalized_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO product_store_lists(item_id,list_id) VALUES(?,?)",
                    (item_id, list_id),
                )
        self.add_event("store_binding", f"更新商品 {item_id} 的门店表", {"list_ids": sorted(normalized_ids)})

    def list_stores(self, list_id: int, query: str = "", limit: int = 100) -> List[Dict]:
        query_norm = normalize_text(query)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM stores WHERE list_id=? ORDER BY city,branch LIMIT ?",
                (int(list_id), min(max(int(limit), 1), 500)),
            ).fetchall()
        output = [dict(row) for row in rows]
        if query_norm:
            output = [row for row in output if query_norm in row.get("normalized", "")]
        return output

    def replace_time_rules(self, item_id: str, rules: List[Dict]):
        with self._connect() as conn:
            conn.execute("DELETE FROM time_rules WHERE item_id=?", (item_id,))
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    continue
                conn.execute(
                    """INSERT INTO time_rules(
                        item_id,label,day_type,start_time,end_time,allowed,reply,next_hint,enabled
                    ) VALUES(?,?,?,?,?,?,?,?,1)""",
                    (
                        item_id,
                        str(rule.get("label") or f"规则{index + 1}"),
                        str(rule.get("day_type") or "any"),
                        str(rule.get("start_time") or "00:00"),
                        str(rule.get("end_time") or "23:59"),
                        int(bool(rule.get("allowed", True))),
                        str(rule.get("reply") or ""),
                        str(rule.get("next_hint") or ""),
                    ),
                )

    def list_time_rules(self, item_id: str) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM time_rules WHERE item_id=? AND enabled=1 ORDER BY id",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _time_in_range(now_value: str, start: str, end: str) -> bool:
        if start <= end:
            return start <= now_value <= end
        return now_value >= start or now_value <= end

    def evaluate_time(self, item_id: str, at: Optional[str] = None) -> Dict:
        if at:
            parsed = datetime.fromisoformat(at)
            current = parsed.replace(tzinfo=CHINA_TZ) if parsed.tzinfo is None else parsed.astimezone(CHINA_TZ)
        else:
            current = datetime.now(CHINA_TZ)
        day_type = "weekend" if current.weekday() >= 5 else "weekday"
        now_value = current.strftime("%H:%M")
        hour_value = current.hour + current.minute / 60
        if 5 <= hour_value < 10.5:
            meal_period = "早餐"
        elif 10.5 <= hour_value < 14.5:
            meal_period = "午餐"
        elif 14.5 <= hour_value < 17:
            meal_period = "下午茶"
        elif 17 <= hour_value < 21.5:
            meal_period = "晚餐"
        else:
            meal_period = "夜间"
        matched = []
        for rule in self.list_time_rules(item_id):
            if rule["day_type"] not in {"any", day_type}:
                continue
            if self._time_in_range(now_value, rule["start_time"], rule["end_time"]):
                matched.append(rule)
        blocked = next((rule for rule in matched if not rule["allowed"]), None)
        allowed = next((rule for rule in matched if rule["allowed"]), None)
        decisive = blocked or allowed
        return {
            "now": current.isoformat(timespec="minutes"),
            "day_type": day_type,
            "day_label": "周末" if day_type == "weekend" else "工作日",
            "weekday_label": "周一周二周三周四周五周六周日"[current.weekday() * 2 : current.weekday() * 2 + 2],
            "meal_period": meal_period,
            "status": "blocked" if blocked else ("allowed" if allowed else "unknown"),
            "rule": decisive,
        }

    def current_use_reply(self, item_id: str, product: Dict, message: str) -> Optional[Dict]:
        """Resolve current redemption separately from stock and instant delivery."""
        text = str(message or "").strip()
        redemption_now = bool(re.search(
            r"(?:现在|当前|这会儿|此刻)(?:就)?(?:能|可以|可不可以|能不能)"
            r"(?:直接)?(?:使用|用|核销)|(?:现在|当前|这会儿|此刻)(?:能用|可用)",
            text,
        ))
        instant_after_buy = bool(re.search(
            r"(?:买了|拍下|下单|付款)(?:后)?(?:马上|立刻|立即|当场|直接|就)"
            r"(?:能|可以)?(?:使用|用|核销)",
            text,
        ))
        colloquial_now = bool(re.fullmatch(
            r"(?:现在|当前|这会儿|此刻)(?:团|囤|买|拍|购买|下单)?"
            r"(?:可以|能|行)(?:吗|嘛|么|不)?[？?。！!]*",
            re.sub(r"\s+", "", text),
        ))
        if not (redemption_now or instant_after_buy or colloquial_now):
            return None

        knowledge = "\n".join((str(product.get("raw_text") or ""), str(product.get("ai_summary") or "")))
        if instant_after_buy and re.search(r"需(?:提前)?预约|必须预约|预约后", knowledge) \
                and not re.search(r"无需预约|免预约", knowledge):
            return {
                "reply": "不能直接使用。当前商品资料明确要求先预约，购买后请按商品说明完成预约再到店核销。",
                "source": "当前商品预约规则", "decision": "deny", "kind": "time",
            }

        issuance = ""
        if instant_after_buy:
            if re.search(r"自动发|秒发|即时发|立即发|付款后发送|付款后发", knowledge):
                issuance = "付款并收到券码后，"
            else:
                issuance = "商品资料未明确券码到账速度；收到有效券码后，"

        evaluated = self.evaluate_time(item_id)
        if evaluated["status"] == "blocked":
            rule = evaluated.get("rule") or {}
            reply = (
                f"现在是{evaluated['weekday_label']}{evaluated['meal_period']}时段，"
                "根据当前商品已配置的使用时间，该券现在不能使用。"
            )
            if rule.get("reply"):
                reply += str(rule["reply"]).strip()
            return {
                "reply": reply, "source": f"当前商品时间规则：{rule.get('label', '')}",
                "decision": "deny", "kind": "time",
            }
        if evaluated["status"] == "allowed":
            rule = evaluated.get("rule") or {}
            return {
                "reply": (
                    f"{issuance}现在是{evaluated['weekday_label']}{evaluated['meal_period']}时段，"
                    "根据当前商品已配置的使用时间，该券现在可以使用。"
                ),
                "source": f"当前商品时间规则：{rule.get('label', '')}",
                "decision": "allow", "kind": "time",
            }

        options = [
            item for item in self.extract_sale_options(product)
            if item.get("sale_price") and self._is_sellable_option(item)
        ]
        current_day = str(evaluated.get("day_type") or "")
        period_key = {
            "早餐": "breakfast", "午餐": "lunch", "下午茶": "afternoon_tea", "晚餐": "dinner",
        }.get(str(evaluated.get("meal_period") or ""), "")
        has_weekend = any("weekend" in (item.get("day_types") or []) for item in options)
        has_holiday = any("holiday" in (item.get("day_types") or []) for item in options)
        holiday_covers = current_day == "weekend" and has_holiday and not has_weekend
        day_matches = [
            item for item in options
            if self._option_matches_time(item, current_day, "", holiday_covers)
        ]
        if period_key:
            current_matches = [
                item for item in day_matches
                if self._option_matches_time(item, current_day, period_key, holiday_covers)
            ]
        else:
            current_matches = [
                item for item in day_matches
                if not item.get("meal_periods") or "any" in (item.get("meal_periods") or [])
            ]

        requested_audiences = set(self._audience_types(text))
        if requested_audiences:
            current_matches = [
                item for item in current_matches
                if any(
                    self._option_supports_audience(item, kind)
                    for kind in requested_audiences
                )
            ]

        if options and not day_matches:
            return {
                "reply": (
                    f"现在是{evaluated['day_label']}，本店目前没有“{evaluated['day_label']}可用”"
                    "这一有货规格，暂时无法通过当前商品购买，您可以到店咨询。"
                ),
                "source": "当前商品现有有货SKU的适用日期",
                "decision": "deny", "kind": "time",
            }
        explicit_meals = any(item.get("meal_periods") for item in day_matches)
        if day_matches and explicit_meals and not current_matches:
            return {
                "reply": (
                    f"现在是{evaluated['meal_period']}时段，本店目前没有“当前时段可用”"
                    "这一有货规格，暂时无法通过当前商品购买，您可以到店咨询。"
                ),
                "source": "当前商品现有有货SKU的用餐时段",
                "decision": "deny", "kind": "time",
            }
        if not current_matches:
            return {
                "reply": "门店营业时段可用就可以了。",
                "source": "当前商品未配置额外的实时使用时段限制",
                "decision": "allow", "kind": "time",
            }

        audience_groups = {
            audience
            for item in current_matches
            for audience in self._effective_audience_types(item)
        }
        if len(audience_groups) > 1 and not self._audience_types(text):
            return {
                "reply": "当前有多个票种，请告诉我是成人、儿童、学生、老人还是女士使用，我再按有货SKU确认。",
                "source": "当前时段存在多个身份票种",
                "decision": "allow", "kind": "time_clarify",
            }

        return {
            "reply": "门店营业时段可用就可以了。",
            "source": "当前商品当日有可用规格且未配置额外实时限制",
            "decision": "allow", "kind": "time",
        }

    @staticmethod
    def _paid_amount_from_text(message: str) -> Optional[Decimal]:
        patterns = (
            r"(?:订单)?实付(?:金额)?\s*[：:]?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)",
            r"(?:我|买家)?付了\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元",
            r"订单金额\s*[：:]?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)",
        )
        for pattern in patterns:
            match = re.search(pattern, str(message or ""))
            if match:
                try:
                    return Decimal(match.group(1))
                except InvalidOperation:
                    return None
        return None

    @classmethod
    def _order_payment_state(
        cls, order_context: Optional[Dict] = None, message: str = "",
        actual_paid_amount: object = None,
    ) -> str:
        """Return only payment states supported by order or message evidence."""
        context = order_context if isinstance(order_context, dict) else {}
        text = re.sub(r"\s+", "", str(message or ""))
        # Current explicit buyer evidence wins over an older order card.
        if re.search(r"(?:还没|没有|未|尚未)(?:付钱|付款)|待付款", text):
            return "unpaid"
        if re.search(r"已经付款|已付款|付款了|付过款|钱已经付|买了|购买后|收到券|收到码|收到链接", text):
            return "paid"
        if actual_paid_amount not in (None, "") or cls._paid_amount_from_text(message) is not None:
            return "paid"

        status = re.sub(r"\s+", "", str(context.get("status") or ""))
        if any(word in status for word in (
            "等待卖家发货", "待发货", "已付款", "已发货", "等待确认收货", "确认收货",
            "交易成功", "退款", "退货", "售后", "纠纷",
        )):
            return "paid"
        if any(word in status for word in ("等待买家付款", "待付款", "未付款")):
            # A pushed waiting-payment card is only a historical event.  The
            # buyer may pay immediately afterwards without another event being
            # delivered, so it must not be used to reject a later refund
            # request.  Only a current order-detail lookup may mark the status
            # as verified; explicit buyer text is handled above.
            if context.get("payment_state_verified") is True:
                return "unpaid"
            return "unknown"
        return "unknown"

    @staticmethod
    def _aftersale_context(stage: str, prompt: str, payment_state: str = "unknown") -> Dict:
        return {
            "intent": "aftersale",
            "aftersale_stage": stage,
            "aftersale_prompt": prompt,
            "aftersale_payment_state": payment_state,
        }

    @classmethod
    def _personal_refund_reply(cls, message: str, actual_paid_amount: object = None) -> str:
        amount = None
        if actual_paid_amount not in (None, ""):
            try:
                amount = Decimal(str(actual_paid_amount))
            except InvalidOperation:
                amount = None
        if amount is None:
            amount = cls._paid_amount_from_text(message)
        if amount is not None and amount >= 0:
            refund_amount = (amount * Decimal("0.95")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            amount_line = f"4. 退款金额填写{refund_amount:.2f}元（订单实付{amount:.2f}元×0.95）。"
        else:
            amount_line = "4. 退款金额填写该订单实付金额的95%；如需我计算，请发送订单实付金额。"
        return (
            "如果卡券尚未核销且仍在有效期内，因个人原因需要取消，可以按当前商品规则申请退款。"
            "非卡券质量问题的退款（如排不上队、不想使用、买多了、别人买单了、行程变化等）"
            "当前规则会按订单实付金额扣除5%手续费，您可以按以下步骤操作：\n"
            "1. 打开“我的订单”；\n"
            "2. 选择“仅退款”；\n"
            "3. 退款页面货物状态选择“已收到货”；\n"
            f"{amount_line}\n"
            "提交后请以订单退款页面的审核结果为准。"
        )

    def store_negative_confirmation_reply(
        self, item_id: str, product: Dict, message: str,
        store_context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Keep store-specific negative questions out of aftersales handling."""
        text = str(message or "").strip()
        if not re.search(r"不能用|不可以用|不可用|用不了|不行(?:吗|嘛|么|\?|？)", text):
            return None
        # These phrases describe an actual fulfilled-order failure, not a
        # pre-sale question about whether a store is covered.
        if re.search(
            r"券码|码无效|核销失败|无法核销|店员.{0,8}(?:不让用|不能核销|核销不了)|"
            r"(?:买了|付款后|收到后|已付款|已经付款).{0,10}(?:不能用|用不了)|退款|退钱",
            text,
        ):
            return None

        positive_text = re.sub(
            r"能不能(?:使用|用)|可不可以(?:使用|用)|"
            r"(?:不能|不可以|不可)(?:使用|用)?|用不了|不行",
            "可以用", text,
        )
        query = extract_store_query(positive_text)
        contextual = bool(store_context) and bool(re.fullmatch(
            r"(?:这家(?:店)?|这个店|该店|那里|刚才那家|那家店|确定|真的)?"
            r"(?:不能用|不可以用|不可用|用不了|不行)(?:吗|嘛|么|\?|？|。|！|!)*",
            re.sub(r"\s+", "", text),
        ))
        if contextual:
            query = str(store_context.get("query") or "").strip()
        if not is_meaningful_store_query(query):
            return None

        matched_skus = self.match_message_skus(item_id, text, product)
        selected_sku = matched_skus[0] if len(matched_skus) == 1 else None
        if not selected_sku and store_context:
            prior_key = str(store_context.get("selected_sku_key") or "")
            selected_sku = next((sku for sku in self.list_product_skus(item_id, product)
                                 if sku["sku_key"] == prior_key), None)
        selected_key = (selected_sku or {}).get("sku_key", "")
        raw_result = self.search_store(item_id, positive_text, sku_key=selected_key)
        result = (
            raw_result if (
                raw_result.get("resolved_query")
                and normalize_text(raw_result.get("resolved_query")) != normalize_text(positive_text)
            )
            else self.search_store(item_id, query, sku_key=selected_key)
        )
        query = str(result.get("resolved_query") or query).strip()
        if result.get("status") != "available":
            query = str(result.get("specific_query") or query).strip()
        prefix = f"{self._sku_public_label(selected_sku)}：" if selected_sku else ""
        clarification = self._store_search_clarification(
            query, result, [selected_sku] if selected_sku else [],
        )
        if clarification:
            return clarification
        if result.get("status") == "available":
            if contextual:
                names = "、".join(dict.fromkeys(
                    str(match.get("branch") or match.get("brand") or "").strip()
                    for match in result.get("matches") or []
                    if str(match.get("branch") or match.get("brand") or "").strip()
                ))
                if names:
                    return {
                        "reply": f"可以用，{names}在当前商品的可用门店范围内。",
                        "source": "当前会话最近一次门店查询重新核验",
                        "decision": "allow", "kind": "stores",
                        "store_matches": result["matches"], "store_query": query,
                        "store_status": "available",
                    }
            return {
                "reply": prefix + self.format_store_matches(result["matches"], query, text),
                "source": "门店否定问法按适用门店查询", "decision": "allow", "kind": "stores",
                "store_matches": result["matches"], "store_query": query,
                "store_status": "available",
            }
        if result.get("status") == "area_fallback":
            return {
                "reply": self.format_store_area_fallback(result, query, text),
                "source": "否定追问重新核验后返回所提市区的全部可用门店",
                "decision": "allow", "kind": "stores",
                "store_matches": list(result.get("matches") or []),
                "store_query": query, "store_status": "area_fallback",
            }
        if result.get("status") == "unavailable":
            return {
                "reply": prefix + self.format_store_unavailable(
                    query, area_only=bool((result.get("province") or result.get("city"))
                                          and not result.get("search_term")),
                ),
                "source": "门店否定问法按适用门店查询", "decision": "deny", "kind": "stores",
                "store_matches": [], "store_query": query, "store_status": "unavailable",
            }
        if result.get("status") == "sku_store_unconfigured":
            return {
                "reply": f"{prefix}当前规格暂未配置适用门店资料，无法准确确认该门店是否可用。",
                "source": "当前规格门店资料未配置", "decision": "allow", "kind": "stores",
                "store_matches": [], "store_query": query, "store_status": "unconfigured",
            }
        if result.get("status") == "no_store_list":
            return {
                "reply": "当前商品暂未配置可用门店资料，暂时无法准确确认该门店是否可用。",
                "source": "门店表缺失", "decision": "allow", "kind": "stores",
                "store_matches": [], "store_query": query, "store_status": "unconfigured",
            }
        return None

    @classmethod
    def usage_restrictions_reply(cls, product: Dict) -> str:
        """Return only grounded usage limits for a targeted restriction question."""
        facts = (product.get("structured") or {}).get("facts") or {}
        raw_text = str(product.get("raw_text") or "")
        options = cls.extract_product_options(product)
        raw_restrictions = [
            part.strip(" \t。；;，,")
            for part in re.split(r"[\r\n。；;]+", raw_text)
            if re.search(
                r"仅限|不可|不能|不支持|最多|上限|叠加|混用|"
                r"营业时间|前台|预约|包间|堂食|打包|找零|超出",
                part,
            )
            and not re.search(r"(?:售价|价格)\s*\d", part)
        ]
        candidates = (
            cls._stack_rule(facts, raw_text, options),
            cls._use_time_text(product),
            cls._use_rule_text(product),
            *raw_restrictions,
        )
        parts = []
        seen = set()
        for candidate in candidates:
            for part in re.split(r"[\r\n；;]+", str(candidate or "")):
                value = part.strip(" \t。；;，,")
                key = normalize_match_text(value)
                if value and key not in seen:
                    seen.add(key)
                    parts.append(value)
        if not parts:
            return "当前商品资料暂未明确说明其他使用限制。"
        return "；".join(parts) + "。"

    @classmethod
    def usage_subject_reply(cls, product: Dict, message: str) -> Optional[Dict]:
        """Separate coupon coverage from SKU existence and evaluate explicit rule scope."""
        text = str(message or "")
        alias_groups = (
            ("锅底", ("锅底", "锅底费", "汤底", "汤底费", "底料")),
            ("酒水", ("酒水", "酒类", "酒品", "啤酒", "白酒", "红酒")),
            ("饮料", ("饮料", "饮品", "软饮")),
            ("菜品", ("菜品", "餐品", "单品")),
            ("套餐", ("套餐", "团购套餐")),
            ("包间", ("包间", "包厢", "包房")),
            ("堂食", ("堂食",)),
            ("打包", ("打包", "外带")),
        )
        matched_group = next((group for group in alias_groups if any(alias in text for alias in group[1])), None)
        subject = matched_group[0] if matched_group else ""
        aliases = matched_group[1] if matched_group else ()
        asks_coverage = bool(
            re.search(r"可以|能|可用|使用|支持|不可以|不能|包括|包含|抵扣", text)
        ) or any(
            re.fullmatch(
                rf"{re.escape(alias)}(?:呢|吗|嘛|么)?[？?。！!]*",
                text.strip(),
            )
            for alias in aliases
        )
        if not subject or not asks_coverage:
            return None
        message_key = normalize_text(message)
        known_areas = {
            cls._area_key(value) for value in KNOWN_CITY_NAMES | KNOWN_PROVINCE_NAMES
        }
        if (
            any(word in message_key for word in STORE_LANDMARK_WORDS)
            or any(area and area in message_key for area in known_areas if len(area) >= 2)
            or re.search(r"(?:省|市|区|县|镇|乡|村|路|街|商场|广场|购物中心|门店|分店|店)", message_key)
        ):
            return None

        sku_intent = bool(re.search(
            r"有吗|有没有|有无|多少钱|什么价|价格|怎么买|怎么拍|卖吗|"
            r"几张|几份|规格",
            text,
        ))
        if subject == "套餐" and not re.search(r"代金券|抵扣券|现金券|优惠券|抵扣", text):
            sku_intent = True
        if sku_intent:
            matches = [
                option for option in cls.extract_sale_options(product)
                if any(alias in str(option.get("name") or "") for alias in aliases)
            ]
            if not matches:
                return {
                    "reply": f"当前商品没有“{subject}”这一在售规格。",
                    "source": "当前商品真实SKU列表", "decision": "allow",
                    "kind": "sku_availability",
                }
            return None

        knowledge = cls._product_rule_knowledge(product)
        relevant_clauses = [
            value.strip(" \t，,。；;")
            for value in re.split(r"[\r\n。；;]+", knowledge)
            if any(alias in value for alias in aliases)
        ]
        availability_clause = next((clause for clause in relevant_clauses if re.search(
            r"(?:部分|个别)?(?:菜品|餐品|单品)[^。；\n]{0,40}"
            r"(?:时令|售罄|无货|缺货|不可抗|无法提供)", clause
        )), "")
        local_exception = next((clause for clause in relevant_clauses if re.search(
            r"(?:使用范围)?例外[：:]?|但[^。；\n]{0,80}(?:不可|不能|不支持|不适用|不参与|不抵扣)",
            clause,
        )), "")
        negative = next((clause for clause in relevant_clauses if re.search(
            r"不可(?!抗)|不能|不支持|不适用|除外|不参与|不抵扣", clause
        ) and clause != local_exception), "")
        exclusion_segments = []
        for pattern in (
            r"除([^。；\n]{1,80}?)外[^。；\n]{0,30}(?:全场通用|全场可用|均可使用|都可使用)",
            r"(?:全场通用|全场可用|均可使用|都可使用)[，,：:]?([^。；\n]{1,80}?)除外",
        ):
            exclusion_segments.extend(match.group(1) for match in re.finditer(pattern, knowledge))
        excluded = next((
            segment for segment in exclusion_segments
            if any(alias in segment for alias in aliases)
        ), "")
        global_scope = bool(re.search(
            r"全场通用|全场可用|除[^。；\n]{1,80}外[^。；\n]{0,30}(?:均可使用|都可使用)",
            knowledge,
        ))
        positive = next((clause for clause in relevant_clauses if re.search(
            r"(?:可以|可|支持)[^。；\n]{0,8}(?:使用|抵扣)|"
            r"(?:使用|抵扣)[^。；\n]{0,8}(?:可以|可|支持)|可用", clause
        ) and not re.search(r"不可|不能|不支持|不适用", clause)), "")

        if local_exception and subject == "菜品":
            exception_text = re.sub(
                r"^(?:使用范围)?例外[：:]?\s*", "", local_exception,
            ).strip(" \t，,。；;")
            exclusion_text = "、".join(dict.fromkeys(
                value.strip(" ，,、") for value in exclusion_segments if value.strip(" ，,、")
            ))
            if global_scope:
                scope = f"除{exclusion_text}外，其他菜品可以使用当前代金券抵扣" if exclusion_text else (
                    "其他菜品可以使用当前代金券抵扣"
                )
                reply = f"{scope}；但{exception_text}。"
            else:
                reply = (
                    "当前规则只明确排除了以下范围，并不表示所有菜品都不可用："
                    f"{exception_text}。其他菜品是否可用请以适用门店实际规则为准。"
                )
        elif negative or excluded:
            evidence = negative or f"除{excluded}外全场通用"
            reply = f"不可以，当前代金券不可用于{subject}。商品规则：{evidence}。"
        elif availability_clause and subject == "菜品":
            reply = (
                "当前资料没有说明代金券不能用于菜品；仅提示部分菜品可能因时令、售罄"
                "或其他不可抗因素无法提供，具体以门店实际供应为准。"
            )
        elif global_scope:
            exclusion_text = "、".join(dict.fromkeys(
                value.strip(" ，,、") for value in exclusion_segments if value.strip(" ，,、")
            ))
            rule = f"除{exclusion_text}外全场通用" if exclusion_text else "全场通用"
            reply = f"{subject}消费可以使用当前代金券抵扣，商品规则为{rule}。"
        elif positive:
            reply = f"{subject}消费可以使用当前代金券抵扣。商品规则：{positive}。"
        else:
            reply = f"当前商品资料没有明确说明{subject}是否可用，暂时无法准确确认。"
        return {
            "reply": reply, "source": "当前商品使用规则",
            "decision": "allow", "kind": "usage_scope",
        }

    def resolve_multi_question(
        self, item_id: str, product: Dict, message: str,
        actual_paid_amount: object = None, store_context: Optional[Dict] = None,
        order_context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Resolve the small set of independent questions buyers commonly combine."""
        text = str(message or "").strip()
        tasks = []

        def add_task(kind: str, label: str, match, payload=None, position=None):
            if match and not any(task[1] == kind for task in tasks):
                tasks.append((match.start() if position is None else position, kind, label, payload))

        usage_match = re.search(r"怎么用|如何使用|怎么核销|如何核销|到店怎么操作", text)
        add_task("usage", "使用方式", usage_match)

        value_match = re.search(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?\s*"
            r"(?:(?:拍下|下单|购买)(?:后)?(?:就|可|可以)?\s*)?(?:直接\s*)?"
            r"(?:可?抵(?:用|扣)?|代)\s*(\d+(?:\.\d+)?)\s*(?:元|块)?",
            text,
        )
        add_task("voucher_value", "售价与面额", value_match)

        stacking_match = re.search(
            r"一次(?:可以|能)?用(?:几|多少)张|(?:可以|能)用(?:几|多少)张|"
            r"(?:每次|一桌)?(?:最多)(?:可以|能)?用(?:几|多少)张|"
            r"每次用(?:几|多少)张|一桌用(?:几|多少)张",
            text,
        )
        add_task("stacking", "使用张数", stacking_match)

        restriction_match = re.search(
            r"有什么限制(?:条件)?|有哪些限制(?:条件)?|使用限制|限制条件|使用条件(?:是什么|有哪些|呢|吗)?",
            text,
        )
        add_task("restrictions", "使用限制", restriction_match)

        timing_match = re.search(
            r"(?:吃完|吃了|用餐后|消费后|结账前|买单前)"
            r"[^。！？\n]{0,10}(?:再|才)?(?:买|拍|购买|下单)(?:对吧|是吧|吗)?",
            text,
        )
        timing_reply = self.purchase_timing_reply(product, text) if timing_match else ""
        if timing_reply:
            add_task("purchase_timing", "购买时机", timing_match, timing_reply)

        purchase_match = re.search(
            r"直接(?:拍|买)|(?:可以|能|可不可以|能不能)(?:直接|直)?(?:拍|买|购买)|"
            r"(?:现在|当前)(?:就)?(?:拍|买|购买|下单)",
            text,
        )
        purchase_reply = self.direct_coupon_purchase_reply(product, text) if purchase_match else ""
        if purchase_match and not purchase_reply:
            purchase_reply = (
                "可以现在拍下。" if re.search(r"(?:现在|当前)(?:就)?(?:拍|买|购买|下单)", text)
                else "可以直接拍下。"
            )
        if purchase_reply:
            add_task("purchase", "购买", purchase_match, purchase_reply)

        store_query = extract_store_query(
            text, product=product, product_brand=self.extract_brand(product)
        )
        store_query = self._strip_store_sku_edges(item_id, store_query, product)
        store_probe = None
        resolved_store_query = store_query
        store_trigger = re.search(
            r"(?:可以|能|可)(?:在这)?(?:使用|用)|是否可用|适用|"
            r"门店|店铺|总店|旗舰店|分店|商场|商圈|购物中心|广场|天虹|万达|万象城|万象汇|"
            r"壹方城|壹方天地|万科里|天街|银泰|吾悦|大悦城|来福士|"
            r"太古里|印象城|奥特莱斯|IFS|MALL|"
            r"[\u4e00-\u9fffA-Za-z0-9]{2,24}店(?="
            r"还有吗|还有么|还有嘛|还有没有|有货吗|有没有|有吗|"
            r"能用吗|可以用吗|多少|多钱|价格|售价|几元|几块|[？?]|$)",
            text,
            re.I,
        )
        # A location statement is itself a store query. Probe the configured
        # store dictionary so “深圳五和店三人明天中午” and a bare city/address
        # can join the condition-price task without requiring “能用吗”.
        statement_probe = None
        if not store_trigger and is_meaningful_store_query(store_query):
            statement_scope = self._multi_sku_store_scope(item_id, product)
            statement_union = (
                statement_scope.get("list_ids")
                if statement_scope.get("stores_differ") else None
            )
            statement_probe = self.search_store(
                item_id, text, list_ids_override=statement_union,
            )
            statement_query = str(
                statement_probe.get("resolved_query") or store_query
            ).strip()
            if self.is_explicit_store_query(text, statement_query, statement_probe):
                store_trigger = re.search(
                    re.escape(statement_query), text, re.I,
                ) or re.match(r".", text)
        if store_trigger:
            multi_store_scope = self._multi_sku_store_scope(item_id, product)
            union_ids = (
                multi_store_scope.get("list_ids")
                if multi_store_scope.get("stores_differ") else None
            )

            def probe_store(value: str) -> Dict:
                return self.search_store(
                    item_id, value, list_ids_override=union_ids,
                )

            # First let the configured store dictionary extract the smallest
            # grounded location directly from the untouched sentence.  Only
            # fall back to phrase deletion when no known location was found.
            raw_probe = statement_probe or (
                probe_store(text) if is_meaningful_store_query(text) else None
            )
            if (
                raw_probe and raw_probe.get("resolved_query")
                and normalize_text(raw_probe.get("resolved_query")) != normalize_text(text)
            ):
                store_probe = raw_probe
            elif is_meaningful_store_query(store_query):
                store_probe = probe_store(store_query)
            resolved_store_query = str(
                (store_probe or {}).get("resolved_query") or store_query
            ).strip()
            # A long sentence may contain one exact configured branch plus SKU
            # words that remain after phrase stripping. Prefer that real branch
            # entity over feeding the whole sentence back into store routing.
            contained_branches = []
            for match_row in (store_probe or {}).get("matches") or []:
                branch = str(match_row.get("branch") or "").strip()
                branch_key = self._store_fuzzy_key(branch)
                if branch_key and branch_key in self._store_fuzzy_key(text):
                    contained_branches.append(branch)
            contained_branches = list(dict.fromkeys(contained_branches))
            has_unresolved_store_terms = any(
                match_row.get("unresolved_terms")
                for match_row in (store_probe or {}).get("matches") or []
            )
            if len(contained_branches) == 1 and (
                (store_probe or {}).get("status") == "needs_confirmation"
                or has_unresolved_store_terms
            ):
                resolved_store_query = contained_branches[0]
            if self.is_explicit_store_query(text, resolved_store_query, store_probe):
                explicit_store_skus = self._explicit_store_skus(item_id, text, product)
                explicit_store_sku_text = (
                    str(explicit_store_skus[0].get("sku_name") or "").strip()
                    if len(explicit_store_skus) == 1 else ""
                )
                position = min(
                    (text.find(value) for value in (
                        resolved_store_query,
                        self._area_key(resolved_store_query),
                    ) if value and text.find(value) >= 0),
                    default=store_trigger.start(),
                )
                add_task(
                    "store", "适用门店", store_trigger,
                    {
                        "query": resolved_store_query,
                        "sku_text": explicit_store_sku_text or next(iter(re.findall(
                            r"(?<!\d)\d+(?:\.\d+)?\s*元?\s*(?:的)?\s*(?:代金券|券)", text
                        )), ""),
                    },
                    position=position,
                )

        has_store_task = any(task[1] == "store" for task in tasks)
        conditional_child = None
        if has_store_task:
            sku_question_match = re.search(
                r"(?<!\d)\d+(?:\.\d+)?\s*元?\s*(?:的)?\s*(?:代金券|券)",
                text,
            )
            # Buyers often omit “元/代金券” in compact store questions such as
            # “深圳壹方城200可以用吗”.  Treat the standalone amount as a voucher
            # target only inside an already-confirmed store-usage question.
            if not sku_question_match and not re.search(
                r"(?:\d{4}[年./-])?\d{1,2}[月./-]\d{1,2}(?:日|号)?",
                text,
            ):
                numeric_candidates = list(re.finditer(
                    r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])\s*(?:元)?"
                    r"(?!\s*(?:年|月|日|号|点|个|人|位|张|桌|份|门店|分店|店))"
                    r"(?=[^。！？]{0,12}(?:可以|能|可)(?:使用|用))",
                    text,
                ))
                # Numbers are valid parts of many branch names (33小镇、壹方城
                # 3号店). They are not voucher amounts unless the buyer writes
                # an explicit currency/coupon marker. Skip such grounded branch
                # numbers and keep looking for a separate amount in the sentence.
                grounded_store_numbers = set()
                for match_row in (store_probe or {}).get("matches") or []:
                    for field in ("branch", "address"):
                        grounded_store_numbers.update(re.findall(
                            r"\d+(?:\.\d+)?", str(match_row.get(field) or "")
                        ))
                sku_question_match = next((
                    candidate for candidate in numeric_candidates
                    if not (
                        candidate.group(1) in grounded_store_numbers
                        and not re.search(r"元|代金券|券", candidate.group(0))
                    )
                ), None)
            sku_is_store_applicability = bool(
                multi_store_scope.get("stores_differ")
                and not re.search(r"有(?:没有|吗|么)|卖不卖|什么规格|哪些规格", text)
            )
            if (
                sku_question_match and not value_match and not purchase_reply
                and not sku_is_store_applicability
                and (store_probe or {}).get("status") != "area_fallback"
            ):
                requested_value = self._format_number(
                    sku_question_match.group(1) if sku_question_match.lastindex
                    else next(iter(re.findall(r"\d+(?:\.\d+)?", sku_question_match.group(0))), "")
                )
                sku_reply = self.sku_availability_reply(
                    product, f"{requested_value}元代金券有吗",
                )
                if sku_reply:
                    add_task("sku", "商品规格", sku_question_match, sku_reply)

            date_child = self.date_availability_reply(product, text)
            date_match = re.search(
                r"(?:\d{4}[-/.年])?\d{1,2}[-/.月]\d{1,2}(?:日|号)?"
                r"(?!\s*(?:公斤|千克|斤|克|kg|KG|元|块|折|人|位|张|份|套|桌))|"
                r"今天|明天|中秋|国庆|春节|元旦|劳动节",
                text,
            )
            if date_child and date_match:
                add_task("date", "使用日期", date_match, date_child)

            current_time_match = re.search(r"现在|当前|此刻|这会儿", text)
            current_time_child = (
                self.current_use_reply(item_id, product, text)
                if current_time_match else None
            )
            if current_time_child:
                add_task(
                    "current_time", "使用时间", current_time_match,
                    current_time_child,
                )

            condition_match = re.search(
                r"(?:\d{4}[年./-])?\d{1,2}[月./-]\d{1,2}(?:日|号)?|"
                r"今天|今晚|明天|明早|明中午|明晚|后天|周末|工作日|平日|节假日|早餐|中午|午餐|晚上|晚餐|晚市|"
                r"\d+\s*(?:人|位)|[一二两三四五六七八九十]+\s*(?:个)?(?:人|位)",
                text,
            )
            explicit_calendar_or_meal = bool(re.search(
                r"(?:\d{4}[年./-])?\d{1,2}[月./-]\d{1,2}(?:日|号)?|"
                r"今天|今晚|明天|明早|明中午|明晚|后天|周末|工作日|平日|节假日|早餐|中午|午餐|晚上|晚餐|晚市",
                text,
            ))
            # “单人满贯全天” can be the exact SKU name. When the buyer asks to
            # purchase that SKU at a named store, do not reinterpret “全天” as
            # a separate today/meal constraint and accidentally inject today's
            # weekday into the answer.
            if purchase_reply and not explicit_calendar_or_meal:
                condition_match = None
            conditional_child = (
                self.conditional_sale_reply(product, text, {}) if condition_match else None
            )
            condition_is_price = bool(re.search(
                r"多少钱|多钱|价格|售价|几元|几块", text,
            ))
            if conditional_child and (not date_child or condition_is_price):
                add_task(
                    "conditions",
                    "商品价格" if condition_is_price else "使用条件",
                    condition_match,
                    conditional_child,
                )
            elif not date_child:
                day_child = self.day_availability_reply(product, text)
                if day_child and condition_match:
                    add_task("day", "使用日期", condition_match, day_child)

        # A purchase-time sentence may also contain a usage-time question but
        # no location at all, e.g. “我现在拍，今晚能用吗”. Resolve its time
        # condition here so it can combine with the purchase answer, without
        # ever manufacturing a store query or asking for a city.
        if purchase_reply and not has_store_task:
            condition_match = re.search(
                r"(?:\d{4}[年./-])?\d{1,2}[月./-]\d{1,2}(?:日|号)?|"
                r"今天|今晚|明天|明早|明中午|明晚|后天|周末|工作日|平日|节假日|早餐|中午|午餐|晚上|晚餐|晚市",
                text,
            )
            if condition_match:
                conditional_child = self.conditional_sale_reply(product, text, {})
                if conditional_child:
                    add_task("conditions", "使用时间", condition_match, conditional_child)

        price_match = re.search(
            r"多少钱|多钱|什么价格|价格多少|价钱|售价|什么价|啥价|怎么卖|几元|几块|"
            r"[\u4e00-\u9fffA-Za-z0-9]{2,24}(?:总店|旗舰店|分店|门店|店|"
            r"商场|广场|购物中心)\s*(?:的)?多少(?:钱)?",
            text,
        )
        if price_match and not conditional_child:
            # Once a store entity is already bound to this question, do not
            # pass the branch text to the named-SKU price parser.  The generic
            # price resolver lists only current sellable options, while the
            # store child independently verifies that the branch is supported.
            price_question = "多少钱" if has_store_task else text
            price_reply = self.price_reply(product, price_question)
            if price_reply:
                add_task("price", "商品价格", price_match, price_reply)

        if len(tasks) < 2:
            return None

        replies = []
        child_results = []
        store_child_cache = None
        store_task = next((task for task in tasks if task[1] == "store"), None)
        if store_task:
            store_payload = dict(store_task[3] or {})
            store_query_value = str(store_payload.get("query") or "").strip()
            sku_text = str(store_payload.get("sku_text") or "").strip()
            child_store_context = dict(store_context or {})
            if not sku_text and conditional_child:
                selected_context = conditional_child.get("query_context_update") or {}
                for key in ("selected_sku_key", "selected_sku_name"):
                    if selected_context.get(key):
                        child_store_context[key] = selected_context[key]
            # The selected SKU is carried as structured context.  Do not prefix
            # its name to the synthetic store question: words such as “全天双人”
            # would be parsed a second time and could inject today's day type,
            # producing a contradictory condition reply instead of a store result.
            if (
                sku_text and multi_store_scope.get("stores_differ")
                and not child_store_context.get("selected_sku_key")
            ):
                explicit_skus = self.match_message_skus(item_id, sku_text, product)
                if len(explicit_skus) == 1:
                    child_store_context["selected_sku_key"] = explicit_skus[0]["sku_key"]
                    child_store_context["selected_sku_name"] = explicit_skus[0].get("sku_name", "")
            store_child_cache = self.resolve_deterministic(
                item_id, f"{store_query_value}可以用吗", actual_paid_amount,
                child_store_context or None, order_context, _allow_multi=False,
            )

        def store_scoped_price(fallback: str) -> str:
            child = store_child_cache or {}
            status = str(child.get("store_status") or "")
            if status and status != "available":
                return "该门店尚未确认属于当前商品适用范围，暂不推荐购买规格。"
            matrix = list(child.get("store_sku_matrix") or [])
            supported = []
            seen_keys = set()
            for row in matrix:
                for sku in row.get("supported_skus") or []:
                    key = str(sku.get("sku_key") or sku.get("sku_name") or "")
                    if key and key not in seen_keys and sku.get("sellable", True):
                        seen_keys.add(key)
                        supported.append(sku)
            if supported:
                return "该门店当前可用规格价格：" + "；".join(
                    self._sku_public_label(sku) for sku in supported
                ) + "。"
            return fallback

        for _, kind, label, payload in sorted(tasks, key=lambda value: value[0]):
            child = None
            if kind == "usage":
                reply = self.coupon_usage_instructions(product, text)
                if not reply:
                    reply = "商品知识暂未标明该规格的发券渠道，请说明具体规格或面额后再确认。"
            elif kind == "voucher_value":
                reply = self.voucher_value_confirmation_reply(product, text) or (
                    "当前商品资料中暂未找到能确认的售价与面额对应关系。"
                )
            elif kind == "stacking":
                reply = self.stacking_reply(product, text)
            elif kind == "restrictions":
                reply = self.usage_restrictions_reply(product)
            elif kind in {"purchase", "purchase_timing"}:
                reply = str(payload or "")
            elif kind in {"date", "day", "conditions", "current_time"}:
                child = dict(payload or {})
                reply = str(child.get("reply") or "").strip()
            elif kind == "price":
                reply = store_scoped_price(str(payload or ""))
            elif kind == "sku":
                reply = str(payload or "")
            else:
                store_payload = dict(payload or {})
                store_query_value = str(store_payload.get("query") or "").strip()
                child = store_child_cache
                reply = str((child or {}).get("reply") or "").strip()
                if not reply:
                    reply = f"暂时无法确认{store_query_value}是否属于当前商品的适用门店。"
            if child:
                child_results.append(child)
            replies.append((label, reply.rstrip(" \t\r\n")))

        ordered_tasks = sorted(tasks, key=lambda value: value[0])
        compact_store_condition = "store" in {
            kind for _, kind, _, _ in ordered_tasks
        } and {
            kind for _, kind, _, _ in ordered_tasks
        }.issubset({"store", "date", "day", "conditions", "current_time"})
        result = {
            "reply": "\n\n".join(
                reply if compact_store_condition else f"{index}. {label}：{reply}"
                for index, (label, reply) in enumerate(replies, start=1)
            ),
            "source": "多个独立问题分别使用当前商品与门店资料回答",
            "decision": (
                "review" if any(
                    child.get("decision") in {"review", "clarify"}
                    for child in child_results
                ) else "allow"
            ),
            "kind": "multi_intent",
            "resolved_intents": [
                kind for _, kind, _, _ in ordered_tasks
            ],
        }
        store_child = next((
            child for child in child_results if "store_matches" in child
        ), None)
        if store_child:
            for key in (
                "store_matches", "store_query", "store_status", "store_sku_matrix",
                "store_context_update", "query_context_update",
            ):
                if key in store_child:
                    result[key] = store_child[key]
        return result

    def resolve_semantic_analysis(
        self, item_id: str, message: str, analysis: Optional[Dict],
        actual_paid_amount: object = None, store_context: Optional[Dict] = None,
        order_context: Optional[Dict] = None,
        original_result: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Re-resolve model-extracted questions exclusively through local facts."""
        if not isinstance(analysis, dict) or analysis.get("needs_clarification"):
            return None
        labels = {
            "store": "适用门店", "sku": "商品规格", "price": "商品价格",
            "usage": "使用方式", "stacking": "使用张数", "restrictions": "使用限制",
            "purchase": "购买", "date": "使用日期", "conditions": "使用条件",
            "delivery": "发货方式", "aftersale": "售后问题",
        }

        def normalized_question(question: Dict) -> str:
            intent = str(question.get("intent") or "")
            evidence = str(question.get("evidence") or "").strip()
            slots = question.get("slots") if isinstance(question.get("slots"), dict) else {}
            sku_amount = self._format_number(slots.get("sku_amount") or "")
            paid_amount = self._format_number(slots.get("paid_amount") or "")
            face_value = self._format_number(slots.get("face_value") or "")
            if intent == "store":
                query = str(slots.get("store_query") or "").strip()
                if not query and question.get("uses_context"):
                    query = str((store_context or {}).get("query") or "").strip()
                sku_text = f"{sku_amount}元代金券" if sku_amount else ""
                return f"{sku_text}{query}可以用吗" if query else evidence
            if intent == "sku" and sku_amount:
                return f"{sku_amount}元代金券有吗"
            if intent == "price":
                if paid_amount and face_value:
                    return f"{paid_amount}拍下直接抵{face_value}吗"
                if sku_amount:
                    return f"{sku_amount}元代金券多少钱"
            if intent == "usage":
                return "怎么使用"
            if intent == "stacking":
                return f"{sku_amount + '元代金券' if sku_amount else ''}一次可以用几张"
            if intent == "restrictions":
                return "有什么使用限制"
            if intent == "purchase" and sku_amount:
                return f"{sku_amount}元代金券可以直接拍吗"
            if intent == "purchase" and re.search(r"有货|还有(?:吗|么|嘛|不)", evidence):
                # Normalize only the routing phrase. The stock answer itself is
                # still produced by the unchanged deterministic stock branch.
                return "有货吗"
            if intent == "delivery":
                return "怎么发货"
            return evidence

        resolved = []
        seen = set()
        for question in list(analysis.get("questions") or [])[:5]:
            if not isinstance(question, dict):
                continue
            intent = str(question.get("intent") or "").strip().lower()
            if intent not in labels:
                continue
            child_message = normalized_question(question)
            if not child_message:
                continue
            child = self.resolve_deterministic(
                item_id, child_message, actual_paid_amount,
                store_context, order_context, _allow_multi=False,
            )
            reply = str((child or {}).get("reply") or "").strip()
            if not child or not reply or child.get("decision") in {"silent", "silent_review"}:
                continue
            key = (str(child.get("kind") or ""), normalize_match_text(reply))
            if key in seen:
                continue
            seen.add(key)
            resolved.append((intent, labels[intent], child))

        # A complete existing deterministic answer is never replaced by one
        # model-inferred interpretation. Only a genuinely recovered compound
        # question may supersede a partial single-intent result.
        if len(resolved) == 1:
            if original_result:
                return None
            child = dict(resolved[0][2])
            child["source"] = f"语义辅助理解后由本地规则核验：{child.get('source', '')}"
            child["semantic_assisted"] = True
            return child
        if len(resolved) < 2:
            return None

        result = {
            "reply": "\n\n".join(
                f"{index}. {label}：{child['reply'].rstrip()}"
                for index, (_, label, child) in enumerate(resolved, start=1)
            ),
            "source": "大模型仅拆分问题，全部答案由当前商品本地规则重新核验",
            "decision": (
                "review" if any(
                    child.get("decision") in {"review", "clarify"}
                    for _, _, child in resolved
                ) else "allow"
            ),
            "kind": "semantic_multi_intent",
            "resolved_intents": [intent for intent, _, _ in resolved],
            "semantic_assisted": True,
        }
        store_child = next((
            child for _, _, child in resolved if "store_matches" in child
        ), None)
        if store_child:
            for key in (
                "store_matches", "store_query", "store_status", "store_sku_matrix",
                "store_context_update", "query_context_update",
            ):
                if key in store_child:
                    result[key] = store_child[key]
        return result

    def resolve_deterministic(
        self, item_id: str, message: str, actual_paid_amount: object = None,
        store_context: Optional[Dict] = None,
        order_context: Optional[Dict] = None,
        _allow_multi: bool = True,
    ) -> Optional[Dict]:
        """Resolve facts that must never be invented by the language model."""
        message = str(message or "").strip()
        compact = re.sub(r"[\s，,。.!！?？~～]+", "", message).lower()
        product = self.get_v2_product(item_id) or {}

        # Product status is the highest business gate. Once the listing is
        # offline, every buyer consultation gets the same deterministic answer;
        # no price/store/keyword/aftersale branch may leak through.
        if str(product.get("item_status") or "").strip().lower() in {
            "offline", "off_shelf", "offshelf", "deleted", "下架",
        }:
            return {
                "reply": "您好，该商品目前已下架，暂时无法购买，感谢您的关注。",
                "source": "当前商品已下架",
                "decision": "allow",
                "kind": "offline",
            }

        # This resolver never produces buyer-facing business answers for an
        # on-sale purchase-order product. Offline must stay above this silence
        # gate so an offline purchase-order listing is not swallowed.
        if str(product.get("coupon_type") or "").strip() == "purchase_order":
            return {
                "reply": "",
                "source": "代买单仅自动处理首次回复和纯寒暄",
                "decision": "silent",
                "kind": "purchase_order_other",
            }

        if contains_sensitive_voucher_data(message):
            return {
                "reply": (
                    "您发送的内容包含订单或券码信息，系统已隐藏其中的敏感部分。"
                    "请勿继续发送完整券码、密码或领取链接；该情况需要人工核实。"
                ),
                "source": "订单与券码敏感信息保护",
                "decision": "review",
                "kind": "sensitive_aftersale",
            }

        if re.fullmatch(r"(?:请)?(?:帮我)?(?:转|换|找)?(?:一下)?人工(?:客服)?", compact):
            return {
                "reply": "已切换至人工处理，请稍候。后续消息将保留给人工查看。",
                "source": "买家明确要求人工接管",
                "decision": "allow",
                "kind": "manual_handoff",
            }

        def policy_text() -> str:
            return str(self.effective_aftersale_policy(item_id) or "").strip()

        def policy_sentences(keywords, fallback: str) -> str:
            """Return only relevant sentences from the configured policy."""
            source = policy_text()
            if not source:
                return fallback
            parts = [
                part.strip(" \t\r\n；;。")
                for part in re.split(r"[\r\n]+|(?<=[。；;])", source)
                if part.strip(" \t\r\n；;。")
            ]
            selected = [part for part in parts if any(word in part for word in keywords)]
            return "\n\n".join(part.rstrip("。；; ") + "。" for part in selected) or fallback

        media_marker = bool(re.search(MEDIA_MARKER_PATTERN, message))
        if media_marker:
            text_only = strip_media_markers(message)
            if not text_only or is_media_dependent_query(text_only):
                context = store_context if isinstance(store_context, dict) else {}
                prior_query = str(
                    context.get("query") or context.get("pending_store_query") or ""
                ).strip()
                prior_matches = list(context.get("matches") or [])
                if "图片" in message and prior_query and prior_matches:
                    return {
                        "reply": self.format_store_matches(
                            prior_matches, prior_query, "可以用吗",
                        ),
                        "source": "图片沿用当前会话最近一次门店查询",
                        "decision": "allow", "kind": "stores",
                        "store_matches": prior_matches,
                        "store_query": prior_query,
                        "store_status": str(context.get("status") or "available"),
                    }
                if "图片" in message and prior_query:
                    return {
                        "reply": (
                            f"我已记录您上一条提到的“{prior_query}”，但暂时无法读取图片中的"
                            "补充信息。请把图片里的完整门店名称或地址发成文字，我继续核对。"
                        ),
                        "source": "图片无法识别但保留上一轮门店查询",
                        "decision": "allow", "kind": "media_context",
                    }
                return {
                    "reply": (
                        "暂时无法读取图片中的文字，请把完整门店名称或地址发成文字。"
                        if "图片" in message else
                        "暂时无法识别语音内容，请把问题发成文字。"
                    ),
                    "source": "图片或语音及其关联文字无法识别",
                    "decision": "allow",
                    "kind": "media",
                }
            # Complete text such as “深圳万达可以用吗” is independently
            # answerable even when it arrived next to an image.
            message = text_only
            compact = re.sub(r"[\s，,。.!！?？~～]+", "", message).lower()

        query_context = store_context if isinstance(store_context, dict) else {}
        context_is_aftersale = query_context.get("intent") == "aftersale"
        payment_state = self._order_payment_state(order_context, message, actual_paid_amount)
        if payment_state == "unknown" and context_is_aftersale:
            previous_payment_state = str(query_context.get("aftersale_payment_state") or "")
            # Paid is monotonic for the same order.  Unpaid is not: a buyer can
            # pay after the previous message, therefore never inherit it.
            if previous_payment_state == "paid":
                payment_state = previous_payment_state

        if compact in {"没看到", "没有看到", "没显示", "没有显示", "哪里写了", "没标注", "没有标注"}:
            if query_context.get("last_sku_catalog"):
                return {
                    "reply": self.coupon_catalog_reply(product),
                    "source": "重新展示当前商品SKU及其适用时间",
                    "decision": "allow", "kind": "coupon_catalog",
                    "query_context_update": {"last_sku_catalog": True},
                }

        if re.fullmatch(r"(?:请)?(?:帮我)?(?:改个价|改价|修改价格|价格改一下)", compact):
            return {
                "reply": "请问需要改成多少元？请发送您要购买的规格和目标金额。",
                "source": "改价操作缺少目标规格或金额",
                "decision": "allow", "kind": "price_change_clarify",
            }

        purchase_failure = re.fullmatch(
            r"(?:这个|该商品)?(?:拍不了|不能拍|无法拍|下不了单|不能下单|无法下单|点不了)"
            r"\s*(\d+(?:\.\d+)?)\s*(?:元|块)?(?:的)?(?:代金券|券|规格|选项)?",
            compact,
        ) or re.fullmatch(
            r"(\d+(?:\.\d+)?)\s*(?:元|块)?(?:的)?(?:代金券|券|规格|选项)?"
            r"(?:拍不了|不能拍|无法拍|下不了单|不能下单|无法下单|点不了)",
            compact,
        )
        if purchase_failure:
            if str(product.get("coupon_type") or "").strip() == "promotion":
                return self.promotion_purchase_reply(product)
            amount = self._format_number(purchase_failure.group(1))
            candidates = [
                option for option in self.extract_product_options(product)
                if self._format_number(option.get("face_value")) == amount
            ]
            if not candidates:
                reply = f"当前没有可购买的{amount}元代金券规格。"
                source = "当前商品没有对应的有货SKU"
            else:
                brand = self.extract_brand(product)
                show_time = len({
                    tuple(sorted(self._option_day_types(option))) for option in candidates
                }) > 1
                reply = (
                    f"系统资料显示{amount}元规格仍在售，请确认选择了对应的日期档位：\n"
                    + "\n".join(
                        self.format_product_option(option, brand, show_time=show_time)
                        for option in candidates
                    )
                    + "\n如果页面仍然无法下单，请发送页面提示文字。"
                )
                source = "当前商品真实有货SKU与买家下单失败反馈"
            return {
                "reply": reply, "source": source,
                "decision": "allow", "kind": "purchase_issue",
            }

        if re.fullmatch(
            r"(?:我)?现在(?:已经)?在(?:店里|门店|现场)(?:了)?(?:能|可以|可不可以|能不能)"
            r"(?:买|购买|下单|拍)(?:券|这个|该商品)?(?:吗|嘛|么)?",
            compact,
        ):
            if str(product.get("coupon_type") or "").strip() == "promotion":
                return self.promotion_purchase_reply(product)
            return {
                "reply": "可以，当前商品仍在售；请先按页面下单，并按商品规则在使用当天购买、当天使用。",
                "source": "当前商品在售状态与购买规则",
                "decision": "allow", "kind": "stock",
            }

        refund_request = bool(re.search(
            r"退款|退货|退钱|退一下|申请退|可以退|能退|退吗|给我退|帮我退|"
            r"我要退|想退|退了吧|退掉|退\s*\d+(?:\.\d+)?%|取消退款|取消订单",
            message,
        ))
        refund_status_question = bool(re.search(
            r"退款(?:进度|状态|到哪|到哪里|到账|什么时候|多久)|"
            r"(?:怎么|为什么|为何)(?:还)?没(?:退款|到账)|还没到账|退款没到|退款成功了吗",
            message,
        ))
        unredeemed_statement = bool(re.fullmatch(
            r"(?:我|这个|这张|券|券码|卡券)*(?:还)?(?:没|没有|未|尚未)"
            r"(?:验|核销|使用|用)(?:过)?(?:呢|啊|呀|哦)?",
            compact,
        ))
        unredeemed_evidence = bool(re.search(
            r"(?:还没|没有|未|尚未)(?:验|核销|使用|用)(?:过)?|没验",
            message,
        ))
        actual_failure = bool(re.search(
            r"(?:券码|卡券|码).{0,8}(?:不能用|用不了|无效|核销失败|无法核销|核销不了|发错|已过期)|"
            r"(?:不能用|用不了|无效|核销失败|无法核销|核销不了).{0,8}(?:券码|卡券|码)|"
            r"(?:买了|购买后|付款后|收到后|已付款|已经付款).{0,10}(?:不能用|用不了|核销不了|核销失败)|"
            r"(?:核销失败|无法核销|券码无效|码无效)",
            message,
        ))
        delivery_mismatch = bool(re.search(
            r"(?:发错|少发|多发|发成|收到).{0,20}(?:面额|\d+(?:\.\d+)?\s*元|"
            r"\d+\s*张).{0,10}(?:券|代金|的)|"
            r"(?:发了|收到)\s*\d+\s*张\s*\d+(?:\.\d+)?(?:元)?(?:的)?(?:券)?|"
            r"店里.{0,12}(?:把券)?退了",
            message,
        ))
        expiry_statement = any(
            word in message for word in
            ("收到就过期", "发来已过期", "刚收到就过期", "忘记使用", "忘了用", "没来得及用", "没去用", "过期了")
        )
        personal_reason = any(word in message for word in (
            "排不上队", "不想吃", "不想用", "不想要", "不需要了", "不用了",
            "行程", "临时有事", "买错", "拍错", "看错门店", "选错门店", "买错门店",
            "门店选错", "门店看错", "买多了", "别人买单", "他人买单",
            "个人原因", "去不了", "没时间",
        ))
        aftersale_followup = context_is_aftersale and bool(re.search(
            r"已付款|付款了|没付款|未付款|个人原因|不想|不用|买错|买多|"
            r"不能用|用不了|核销|券码|过期|收到|没验|没有验",
            message,
        ))
        aftersale_candidate = bool(
            refund_request or refund_status_question or unredeemed_evidence
            or actual_failure or delivery_mismatch or expiry_statement
            or personal_reason or aftersale_followup
        )

        # Promotion mode changes only the pre-sale purchase path. Aftersales,
        # store applicability, dates and usage rules continue through their
        # existing authoritative resolvers.
        if (
            str(product.get("coupon_type") or "").strip() == "promotion"
            and not aftersale_candidate
            and self.promotion_purchase_intent(message)
        ):
            return self.promotion_purchase_reply(product)

        if delivery_mismatch:
            if not refund_request:
                return {
                    "reply": (
                        "您反馈的发券面额或数量与订单预期不一致。请先核对订单规格和券包数量，"
                        "不要发送完整券码、密码或领取链接；如仍不一致，请提供脱敏后的订单规格和页面提示以便核实。"
                    ),
                    "source": "卡券发放异常先排查，买家尚未提出退款",
                    "decision": "allow", "kind": "coupon_troubleshooting",
                }
            return {
                "reply": (
                    "您反馈的发券面额、数量或退券结果与预期不一致，需要核对订单和实际发券记录。"
                    "请勿继续发送完整券码、密码或领取链接；请保留订单与券页面，等待人工核实。"
                ),
                "source": "发券面额、数量或退券结果异常",
                "decision": "review",
                "kind": "delivery_mismatch_review",
                "query_context_update": self._aftersale_context(
                    "delivery_mismatch", "请保留订单与券页面，等待人工核实。", payment_state,
                ),
            }

        short_aftersale_followup = bool(
            context_is_aftersale
            and (
                re.fullmatch(r"\s*[?？]+\s*", message)
                or compact in {"什么意思", "然后呢", "怎么办", "怎么处理", "怎么弄"}
            )
        )
        if short_aftersale_followup:
            prompt = str(query_context.get("aftersale_prompt") or "").strip() or (
                "需要先确认订单是否已经付款，以及卡券目前是尚未核销、已经过期，"
                "还是到店后无法核销，确认后才能准确判断处理方式。"
            )
            return {
                "reply": prompt,
                "source": "延续当前售后澄清问题",
                "decision": "allow",
                "kind": "aftersale_clarify",
                "query_context_update": self._aftersale_context(
                    str(query_context.get("aftersale_stage") or "status"),
                    prompt,
                    payment_state,
                ),
            }

        if unredeemed_statement and not refund_request and not actual_failure:
            prompt = "请问您的意思是卡券还没有核销吗？目前是暂时不用了想退款，还是到店后无法使用？"
            return {
                "reply": prompt,
                "source": "未核销状态不等于过期或退款原因",
                "decision": "allow",
                "kind": "aftersale_clarify",
                "query_context_update": self._aftersale_context(
                    "unredeemed_reason", prompt, payment_state,
                ),
            }

        if context_is_aftersale and compact in {
            "已付款", "已经付款", "付款了", "付过款", "还没付款", "没有付款", "未付款", "待付款",
        }:
            if payment_state == "unpaid":
                return {
                    "reply": "当前订单尚未付款，可以直接取消订单或不再付款，不需要发起售后退款。",
                    "source": "买家补充确认订单尚未付款",
                    "decision": "allow",
                    "kind": "unpaid_cancel",
                }
            prompt = "已了解订单已经付款。请问是暂时不用了想退款，还是到店后券码无法核销？"
            return {
                "reply": prompt,
                "source": "买家已补充付款状态，继续确认退款原因",
                "decision": "allow",
                "kind": "aftersale_clarify",
                "query_context_update": self._aftersale_context(
                    "refund_reason", prompt, "paid",
                ),
            }

        if context_is_aftersale and compact in {
            "已申请", "已经申请", "申请了", "提交了", "已经提交", "已经提交了",
        }:
            return {
                "reply": (
                    "已了解您反馈已经提交申请。当前无法直接核验申请状态；"
                    "请保留订单售后页面，人工处理时会据此核实。"
                ),
                "source": "仅复述买家提交状态，不虚构系统查询结果",
                "decision": "review",
                "kind": "refund_status_review",
                "query_context_update": self._aftersale_context(
                    "refund_status", "请保留订单售后页面，等待人工核实。", payment_state,
                ),
            }

        if context_is_aftersale and compact in {
            "没问题是吧", "没问题吧", "这样没问题吧", "这样可以吧", "没错吧",
        }:
            prompt = str(query_context.get("aftersale_prompt") or "").strip() or (
                "当前售后情况还没有核实完成，请保留订单和券码页面，等待人工确认。"
            )
            return {
                "reply": prompt,
                "source": "售后事项未核实前禁止无依据确认",
                "decision": "allow",
                "kind": "aftersale_clarify",
                "query_context_update": self._aftersale_context(
                    str(query_context.get("aftersale_stage") or "status"), prompt, payment_state,
                ),
            }

        if re.fullmatch(r"(?:我)?(?:已经|已)?付款(?:了)?", compact):
            return {
                "reply": "已了解您反馈订单已经付款。请问是要咨询发券、核销，还是申请退款？",
                "source": "仅使用买家自述的付款状态",
                "decision": "allow",
                "kind": "aftersale_clarify",
                "query_context_update": self._aftersale_context(
                    "paid_next_step", "请说明要咨询发券、核销还是退款。", "paid",
                ),
            }

        if re.fullmatch(r"(?:已|已经)?(?:申请|提交)(?:了)?", compact):
            return {
                "reply": "请问是已经提交退款申请吗？当前无法直接核验申请状态，请说明具体申请类型。",
                "source": "缺少可核验的申请类型和状态",
                "decision": "allow",
                "kind": "aftersale_clarify",
            }

        all_store_scope = self._all_store_scope_reply(item_id, product, message)
        if all_store_scope:
            return all_store_scope

        if _allow_multi and not aftersale_candidate:
            multi = self.resolve_multi_question(
                item_id, product, message, actual_paid_amount,
                store_context, order_context,
            )
            if multi:
                return multi

        # Amount-plan wording is broader than “怎么拍”: buyers also say
        # “账单400”“400买哪种券”“预算400给个方案”等。  It always means
        # purchase planning, not SKU/store availability.
        amount_plan = self._amount_plan_request(message)
        if amount_plan:
            # Only voucher products support amount-to-denomination arithmetic.
            # Package prices must continue to the semantic package resolver,
            # otherwise “消费200元怎么买” can invent a coupon-like plan.
            if self.extract_product_options(product):
                planned = self._store_aware_amount_plan_reply(
                    item_id, product, amount_plan, store_context,
                    force_consumption_kind=True,
                )
                if planned:
                    return planned
                reply = self.consumption_plan_reply(product, amount_plan)
                return {
                    "reply": reply,
                    "source": "当前商品真实规格与适用范围",
                    "decision": "review" if "需要人工核实" in reply else "allow",
                    "kind": "consumption_plan",
                }

        pure_greeting = re.fullmatch(
            r"(?:你好|您好|在吗|有人吗|哈喽|嗨|hi|hello|hey|有人不)(?:呀|啊|哦|呢|吗)?",
            compact,
            re.I,
        )
        if pure_greeting:
            return {
                "reply": "您好，请问想咨询当前商品的使用规则、适用门店还是发货问题？",
                "source": "首次问候固定回复",
                "decision": "allow",
                "kind": "greeting",
            }

        previous_price = dict((query_context or {}).get("price_filters") or {})
        if previous_price.get("target_amount") and re.search(
            r"(?:不是|不对|不一样|变价|价格有误|价格错了).{0,8}\d+(?:\.\d+)?|"
            r"\d+(?:\.\d+)?.{0,8}(?:不是|不对|不一样|变价)",
            message,
        ):
            options = self.extract_product_options(product)
            day_type = str(previous_price.get("day_type") or "")
            if day_type:
                options = self._filter_options_for_day(options, day_type)
            face = self._format_number(previous_price.get("target_amount"))
            matching = [
                option for option in options
                if self._format_number(option.get("face_value")) == face
            ]
            if len(matching) == 1:
                price = self._format_number(matching[0].get("sale_price"))
                label = {
                    "weekday": "工作日", "weekend": "周末", "holiday": "节假日",
                }.get(day_type, "当前日期")
                return {
                    "reply": f"您说得对，按{label}档位，{face}元代金券当前售价{price}元。",
                    "source": "当前会话已确认的日期档位与真实SKU价格",
                    "decision": "allow", "kind": "price",
                    "query_context_update": {"price_filters": previous_price},
                }

        if compact in {
            "还有吗", "还有么", "还有嘛", "还有不", "还有货吗", "有货吗", "能拍吗", "可以拍吗",
            "现在能买吗", "现在能拍吗", "当前能买吗", "当前能拍吗",
            "是不是卖完了", "是卖完了吗", "卖完了吗", "卖完了么", "售罄了吗", "没货了吗",
        }:
            offline = str(product.get("item_status") or "").lower() in {
                "offline", "off_shelf", "offshelf", "deleted", "下架",
            }
            sellout_question = compact in {
                "是不是卖完了", "是卖完了吗", "卖完了吗", "卖完了么", "售罄了吗", "没货了吗",
            }
            return {
                "reply": (
                    "当前商品已下架，暂时无法购买。" if offline
                    else "没有卖完，当前商品还在售，可以直接拍下。" if sellout_question
                    else "有的，当前商品还在售，可以直接拍下。"
                ),
                "source": "当前商品在售状态",
                "decision": "allow",
                "kind": "stock",
            }

        current_use = self.current_use_reply(item_id, product, message)
        if current_use:
            return current_use

        purchase_timing = self.purchase_timing_reply(product, message)
        if purchase_timing:
            return {
                "reply": purchase_timing,
                "source": "当前商品购买、发券与核销规则",
                "decision": "allow",
                "kind": "purchase_timing",
            }

        candidate_followup = self.resolve_store_candidate_followup(
            item_id, product, message, store_context,
        )
        if candidate_followup:
            return candidate_followup

        # Store applicability outranks product-wide amount/price lookup for a
        # strict follow-up such as “300的呢” after a verified store answer.
        store_followup = self.resolve_store_matrix_followup(
            item_id, product, message, store_context,
        )
        if store_followup:
            return store_followup

        if not (store_context or {}).get("price_filters"):
            context_matrix = list((store_context or {}).get("store_sku_matrix") or [])
            context_skus = []
            seen_context_skus = set()
            for row in context_matrix:
                for sku in row.get("supported_skus") or []:
                    key = str(sku.get("sku_key") or sku.get("sku_name") or "")
                    if key and key not in seen_context_skus and sku.get("sellable", True):
                        seen_context_skus.add(key)
                        context_skus.append(sku)
            plan_skus = context_skus or [
                sku for sku in self.list_product_skus(item_id, product)
                if sku.get("sellable", True)
            ]
            plan_options = [
                {**sku, "name": str(sku.get("sku_name") or "商品规格")}
                for sku in plan_skus
            ]
            plan_message = message
            scoped_amount = ""
            if (store_context or {}).get("matches"):
                amount_followup = self._sku_amount_followup(message)
                real_faces = {
                    self._format_number(sku.get("face_value") or "")
                    for sku in self.list_product_skus(item_id, product)
                }
                stores_differ = self._multi_sku_store_scope(
                    item_id, product,
                ).get("stores_differ")
                if amount_followup and (
                    not stores_differ or amount_followup not in real_faces
                ):
                    scoped_amount = amount_followup
                requested_price = self._requested_price_amount(message)
                if requested_price and requested_price not in real_faces:
                    scoped_amount = requested_price
            if scoped_amount:
                scoped_plan = self._store_aware_amount_plan_reply(
                    item_id, product, scoped_amount, store_context,
                )
                if scoped_plan:
                    return scoped_plan
            redemption_plan = self.amount_inquiry_plan_reply(
                product, plan_message, available_options=plan_options,
            )
            if redemption_plan:
                return redemption_plan

        voucher_sku = self.voucher_sku_lookup_reply(product, message)
        if voucher_sku:
            return voucher_sku

        direct_purchase = self.direct_coupon_purchase_reply(product, message)
        if direct_purchase:
            return {
                "reply": direct_purchase,
                "source": "当前商品真实SKU与在售状态",
                "decision": "allow",
                "kind": "sku_availability",
            }

        if any(word in message for word in (
            "多少代多少", "有哪些代金券", "有什么代金券", "代金券有哪些", "代金券有什么", "有哪些面额",
            "有什么面额", "卖哪些券", "有哪些券型", "有什么券型", "有什么券", "有哪些券", "还有什么券",
        )) or compact in {"代金券", "有代金券吗", "有没有代金券", "是代金券吗", "代金券有吗"}:
            return {
                "reply": self.coupon_catalog_reply(product, message),
                "source": "当前商品真实SKU列表",
                "decision": "allow",
                "kind": "coupon_catalog",
                "query_context_update": {"last_sku_catalog": True},
            }

        value_confirmation = self.voucher_value_confirmation_reply(product, message)
        if value_confirmation:
            value_match = re.search(
                r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?\s*"
                r"(?:(?:拍下|下单|购买)(?:后)?(?:就|可|可以)?\s*)?(?:直接\s*)?"
                r"(?:可?抵(?:用|扣)?|代)\s*(\d+(?:\.\d+)?)",
                message,
            )
            context_update = None
            if value_match:
                options = self.extract_product_options(product)
                day_type = self._requested_day_type(
                    message, default_today=self._has_explicit_day_options(options),
                )
                context_update = {"price_filters": {
                    "target_amount": self._format_number(value_match.group(2)),
                    "day_type": day_type, "intent": "price",
                    "updated_at": datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
                }}
            return {
                "reply": value_confirmation,
                "source": "当前商品真实售价、面额与发券组成",
                "decision": "allow",
                "kind": "voucher_value",
                **({"query_context_update": context_update} if context_update else {}),
            }

        discount = self.discount_reply(item_id, product, message)
        if discount:
            return {
                "reply": discount,
                "source": "当前商品真实售价与面额计算",
                "decision": "allow",
                "kind": "discount",
            }

        combination = self.benefit_combination_reply(product, message)
        if combination:
            return {
                "reply": combination,
                "source": "当前商品与优惠组合使用规则",
                "decision": "allow",
                "kind": "benefit_combination",
            }

        usage_scope = self.usage_subject_reply(product, message)
        if usage_scope:
            return usage_scope

        stacking = self.stacking_reply(product, message)
        if stacking:
            return {
                "reply": stacking,
                "source": "当前生效商品知识中的叠加规则",
                "decision": "review" if "转人工核实" in stacking else "allow",
                "kind": "stacking",
            }

        date_use = self.date_availability_reply(product, message)
        if date_use:
            return date_use

        day_use = self.day_availability_reply(product, message)
        if day_use:
            return day_use

        audience_price = self.audience_price_reply(product, message, store_context)
        if audience_price:
            return audience_price

        conditional_sale = self.conditional_sale_reply(product, message, store_context)
        if conditional_sale:
            return conditional_sale

        quantity_price = self.quantity_price_reply(product, message)
        if quantity_price:
            return {
                "reply": quantity_price,
                "source": "当前生效商品知识中的单份规格价格",
                "decision": "allow",
                "kind": "price",
            }

        selected_key = str((store_context or {}).get("selected_sku_key") or "")
        if selected_key and compact in {
            "多少钱", "多钱", "多少", "价格", "价格呢", "什么价", "什么价格",
            "怎么卖", "售价多少", "当前多少钱", "现在多少钱",
        }:
            selected = next((
                sku for sku in self.list_product_skus(item_id, product)
                if str(sku.get("sku_key") or "") == selected_key
            ), None)
            if selected:
                label = self._sku_public_label(selected)
                reply = (
                    f"{label}。" if selected.get("sale_price") else
                    f"{selected.get('sku_name') or '该规格'}的售价尚未同步，请先在后台补充售价后再报价。"
                )
                return {
                    "reply": reply, "source": "当前会话已选SKU的真实售价",
                    "decision": "allow", "kind": "price",
                    "query_context_update": {
                        "selected_sku_key": selected_key,
                        "selected_sku_name": str(selected.get("sku_name") or ""),
                    },
                }

        named_sku_price = self.named_sku_price_reply(product, message)
        if named_sku_price:
            return {
                "reply": named_sku_price,
                "source": "当前商品真实SKU价格",
                "decision": "review" if "需要人工核实" in named_sku_price else "allow",
                "kind": "price",
            }

        price_terms = (
            "多少钱", "多钱", "什么价格", "价格多少", "售价", "怎么卖", "几块钱", "几元",
            "今天多少钱", "当前价格", "价钱", "价格", "多少代",
        )
        generic_price_question = compact in {
            "多少钱", "多钱", "多少", "价格", "价格呢", "什么价", "什么价格",
            "怎么卖", "售价多少", "当前多少钱", "现在多少钱",
        }
        shorthand_amount_question = bool(re.fullmatch(
            r"\s*\d+(?:\.\d+)?\s*(?:元|块)?\s*(?:的)?\s*(?:多少钱|多钱|多少|什么价|价格)\s*[？?]?\s*",
            message,
        ))
        if any(word in message for word in price_terms) or generic_price_question or shorthand_amount_question:
            price_reply = self.price_reply(product, message)
            return {
                "reply": price_reply,
                "source": "当前生效商品知识中的实际规格价格",
                "decision": "review" if "需要人工核实" in price_reply else "allow",
                "kind": "price",
            }

        if any(word in message for word in ("外卖", "配送", "送到", "送货")):
            knowledge = "\n".join([
                str(product.get("raw_text") or ""),
                str(product.get("ai_summary") or ""),
            ])
            if re.search(r"(?:不支持|不可|不能|无法)[^。；\n]{0,8}(?:外卖|配送)", knowledge):
                reply = "当前商品资料明确不支持外卖配送。"
            elif re.search(r"(?:支持|可以|可)[^。；\n]{0,6}(?:外卖|配送)|(?:外卖|配送)[^。；\n]{0,6}(?:支持|可以|可)", knowledge):
                reply = "当前商品资料确认支持外卖配送。"
            elif "外带" in knowledge:
                reply = "商品资料仅确认支持到店外带，是否支持外卖配送暂未确认。"
            else:
                reply = "当前商品资料没有明确说明是否支持外卖配送，暂时无法准确确认。"
            return {
                "reply": reply,
                "source": "严格区分外带与外卖",
                "decision": "allow",
                "kind": "delivery_method",
            }

        store_negative = self.store_negative_confirmation_reply(
            item_id, product, message, store_context
        )
        if store_negative:
            return store_negative

        refund_policy_question = bool(
            re.search(r"退款规则|退换(?:货)?政策|怎么退款|如何退款|怎么申请退款|如何申请退款", message)
            or compact in {"可以退吗", "能退吗", "能退款吗", "支持退款吗", "可以退款吗"}
        )
        refund_directive = bool(re.search(
            r"给我退|帮我退|我要退|想退款|退了吧|退掉|申请退款|不想用了|不用了",
            message,
        ))
        dispute_words = (
            "退款被拒", "退不了", "不到账", "金额不对", "少退", "扣错", "平台介入",
            "投诉", "赔偿", "补偿", "立刻退", "马上退", "必须退", "现在退",
        )
        quality_expiry = any(word in message for word in ("收到就过期", "发来已过期", "刚收到就过期"))
        personal_expiry = any(word in message for word in ("忘记使用", "忘了用", "没来得及用", "没去用", "过期了"))
        redeemed_evidence = bool(re.search(r"已经核销|已核销|核销成功|已经使用|已经用了|用掉了", message))

        if refund_status_question:
            order_status = str((order_context or {}).get("status") or "").strip()
            if any(word in order_status for word in ("退款", "退货", "售后", "纠纷")):
                reply = (
                    f"当前订单状态显示“{order_status}”。退款进度和到账时间请以订单售后页面为准；"
                    "如页面长时间没有更新，可以把页面显示的具体状态发给我进一步核实。"
                )
                source = "当前会话已确认的订单售后状态"
            else:
                reply = (
                    "目前无法从当前会话确认这笔订单的退款进度。请先查看订单售后页面；"
                    "如果页面显示异常，可以把具体状态用文字发给我进一步核实。"
                )
                source = "缺少可核验的退款订单状态"
            return {
                "reply": reply,
                "source": source,
                "decision": "allow",
                "kind": "refund_status",
            }

        if quality_expiry:
            if not refund_request:
                return {
                    "reply": "请先核对券页面显示的有效期，并保留脱敏后的有效期提示；若收到时已经过期，请把页面提示文字发来核实。",
                    "source": "卡券有效期异常先排查，买家尚未提出退款",
                    "decision": "allow", "kind": "coupon_troubleshooting",
                }
            return {
                "reply": (
                    "如果卡券收到时就已过期，请先保留券码页面和有效期提示，并在订单中申请“仅退款”。"
                    "该情况需要人工核实，通常会在申请后的72小时内处理。"
                ),
                "source": "卡券有效期异常需人工核实",
                "decision": "review",
                "kind": "expiry_quality",
            }

        # An actual code/redemption failure is aftersales evidence by itself.
        # A bare “不能用吗” is intentionally excluded and remains available to
        # the existing store/time intent resolvers above.
        if actual_failure and not refund_request:
            return {
                "reply": (
                    "请先确认选择的是订单对应门店和规格，并重新打开券码后再尝试核销。"
                    "如仍提示无效或核销失败，请保留脱敏后的页面提示和门店反馈，我再按卡券异常继续核实。"
                ),
                "source": "卡券核销异常先排查，买家尚未提出退款",
                "decision": "allow", "kind": "coupon_troubleshooting",
            }

        if actual_failure and refund_request:
            guidance = policy_sentences(
                ("质量", "异常", "仅退款", "72", "人工"),
                "请保留脱敏后的页面提示或门店反馈，并按当前商品退款政策在订单售后页面操作。",
            )
            policy = policy_text()
            reply = (policy.rstrip() + "\n\n" if policy else "") + "根据您描述，属于卡券原因退款。" + guidance
            return {
                "reply": reply,
                "source": "卡券质量退款需人工核实",
                "decision": "review",
                "kind": "refund_quality",
            }

        if any(word in message for word in dispute_words):
            return {
                "reply": (
                    "这个退款情况需要结合订单和售后页面进一步核实。已经为您记录并转交人工，"
                    "通常会在72小时内核实处理，请以订单页面结果为准。"
                ),
                "source": "退款争议需人工核实",
                "decision": "review",
                "kind": "refund_dispute",
            }

        if refund_request or personal_reason:
            if payment_state == "unpaid":
                return {
                    "reply": "当前订单尚未付款，可以直接取消订单或不再付款，不需要发起售后退款。",
                    "source": "当前会话订单状态为未付款",
                    "decision": "allow",
                    "kind": "unpaid_cancel",
                }

            if redeemed_evidence:
                return {
                    "reply": (
                        "卡券成功核销后通常无法直接取消或退款。若属于误核销、未实际消费或门店操作异常，"
                        "请把具体情况和页面提示发给我，确认后再按对应方式处理。"
                    ),
                    "source": "已核销卡券需先核实异常原因",
                    "decision": "allow",
                    "kind": "refund_redeemed",
                }

            if personal_expiry:
                return {
                    "reply": policy_sentences(
                        ("过期", "退款", "补发", "理解"),
                        "若因未及时使用导致卡券已经过期，通常无法办理退款或补发，敬请理解。"
                        "如页面信息与商品说明不一致，可以提交具体提示进一步核实。",
                    ),
                    "source": "购买当天使用规则",
                    "decision": "deny",
                    "kind": "same_day_use",
                }

            if refund_policy_question:
                reply = policy_text() or (
                    "如果卡券尚未核销且仍在有效期内，可以按当前商品规则申请退款；"
                    "具体退款金额和审核结果请以订单退款页面为准。"
                )
                return {
                    "reply": reply,
                    "source": "当前商品已配置的退款政策",
                    "decision": "allow",
                    "kind": "refund_process",
                }

            if payment_state == "unknown" and (refund_directive or personal_reason):
                prompt = (
                    "为了准确判断处理方式，请先确认这笔订单是否已经付款；如果已经付款且尚未核销，"
                    "再说明是暂时不用、购买选择有误，还是现场无法核销，我再按当前商品规则说明下一步。"
                )
                return {
                    "reply": prompt,
                    "source": "退款请求缺少订单付款状态和原因",
                    "decision": "allow",
                    "kind": "aftersale_clarify",
                    "query_context_update": self._aftersale_context(
                        "payment_and_reason", prompt, payment_state,
                    ),
                }

            if personal_reason:
                policy = policy_text()
                guidance = (
                    "根据您描述，属于买家原因退款。请按上述政策在当前订单售后页面选择相符原因并提交；"
                    "退款条件、金额和审核结果均以已配置政策及订单页面为准。"
                )
                # Prefer the actual refund percentage over a fee percentage.  A
                # policy such as “扣除5%手续费，退款金额为实付金额95%” contains
                # both numbers and must calculate with 95%, not the first 5%.
                rate_match = re.search(
                    r"退款金额[^。；\n%]{0,30}?(\d+(?:\.\d+)?)\s*%", policy,
                ) or re.search(
                    r"(?:退回|可退|按)[^。；\n%]{0,20}?(\d+(?:\.\d+)?)\s*%", policy,
                )
                fee_match = re.search(
                    r"(?:扣除|收取)[^。；\n%]{0,20}(\d+(?:\.\d+)?)\s*%[^。；\n]{0,10}(?:手续费|服务费)",
                    policy,
                )
                if actual_paid_amount not in (None, ""):
                    try:
                        paid_amount = Decimal(str(actual_paid_amount))
                    except InvalidOperation:
                        paid_amount = Decimal("0")
                else:
                    paid_amount = self._paid_amount_from_text(message) or Decimal("0")
                if (rate_match or fee_match) and paid_amount > 0:
                    if rate_match:
                        rate_percent = Decimal(rate_match.group(1))
                    else:
                        rate_percent = Decimal("100") - Decimal(fee_match.group(1))
                    rate = rate_percent / Decimal("100")
                    calculated = (paid_amount * rate).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP,
                    )
                    guidance += (
                        f"按政策中的{rate_percent:g}%计算，订单实付{paid_amount:.2f}元"
                        f"对应{calculated:.2f}元。"
                    )
                return {
                    "reply": (policy.rstrip() + "\n\n" if policy else "") + guidance,
                    "source": "已确认的非卡券质量退款流程",
                    "decision": "allow",
                    "kind": "refund_process",
                }

            if payment_state == "paid":
                prompt = "请问退款原因是暂时不用了，还是到店后券码无法核销？两种情况的处理方式不同。"
                policy = policy_text()
                return {
                    "reply": (policy.rstrip() + "\n\n" if policy else "") + prompt,
                    "source": "已付款退款请求缺少退款原因",
                    "decision": "allow",
                    "kind": "aftersale_clarify",
                    "query_context_update": self._aftersale_context(
                        "refund_reason", prompt, payment_state,
                    ),
                }

            reply = policy_text() or (
                "如果卡券尚未核销且仍在有效期内，可以按当前商品规则申请退款；"
                "具体退款金额和审核结果请以订单退款页面为准。"
            )
            return {
                "reply": reply,
                "source": "当前商品已配置的退款政策",
                "decision": "allow",
                "kind": "refund_process",
            }

        if personal_expiry:
            return {
                "reply": policy_sentences(
                    ("当天", "过期", "退款", "补发", "理解"),
                    "若因未及时使用导致卡券已经过期，通常无法办理退款或补发，敬请理解。"
                    "如页面信息与商品说明不一致，可以提交具体提示进一步核实。",
                ),
                "source": "购买当天使用规则",
                "decision": "deny",
                "kind": "same_day_use",
            }
        same_day_confirmation = bool(re.search(
            r"(?:现在|今天).{0,10}(?:买|购买|拍|下单).{0,10}(?:就|马上|当天).{0,5}(?:用|使用)|"
            r"(?:买|购买|拍|下单)(?:了|后)?(?:是不是|是否|就)?(?:要|得|需要|必须)?"
            r"(?:马上|当天|现在)?(?:用|使用)|"
            r"(?:是不是|是否|必须|需要).{0,5}当天.{0,3}(?:用|使用)",
            message,
        ))
        if same_day_confirmation:
            effective_policy = policy_text()
            same_day_required = bool(re.search(
                r"当天[^。；\n]{0,10}(?:购买|使用)|(?:购买|使用)[^。；\n]{0,10}当天",
                effective_policy,
            ))
            if same_day_required:
                return {
                    "reply": (
                        "是的，这款券需要在购买当天使用。\n\n"
                        "您可以确定到店时间后再下单，这样可以避免卡券提前过期。\n\n"
                        "如果当天未使用，卡券过期后将无法退款或补发，还请您理解。"
                    ),
                    "source": "购买当天使用规则",
                    "decision": "allow",
                    "kind": "same_day_use",
                }
        stockpile_question = any(word in message for word in (
            "囤货", "囤券", "囤一年", "长期囤", "先买着", "提前买", "先买以后用",
        ))
        if stockpile_question:
            effective_policy = policy_text()
            same_day_required = bool(re.search(
                r"当天[^。；\n]{0,10}(?:购买|使用)|(?:购买|使用)[^。；\n]{0,10}当天",
                effective_policy,
            ))
            if not same_day_required:
                known_validity = policy_sentences(("有效期", "使用日期", "购买后"), "")
                reply = known_validity or (
                    "当前商品资料暂未明确是否适合提前购买。为避免过期，请确认到店使用当天再购买。"
                )
                return {
                    "reply": reply,
                    "source": "当前商品的单独有效期政策",
                    "decision": "allow",
                    "kind": "same_day_use",
                }
            return {
                "reply": (
                    "不建议提前囤券哦。这款券需要当天购买、当天使用，"
                    "建议您确定到店当天再下单，避免因未及时使用造成过期。"
                ),
                "source": "购买当天使用规则",
                "decision": "allow",
                "kind": "same_day_use",
            }
        delayed_use_question = bool(re.search(
            r"今天买[^。！？\n]{0,8}明天用|现在买[^。！？\n]{0,8}(?:明天|后天|改天|以后)用|"
            r"过[一二两三四五六七八九十\d]+天(?:再)?用|隔天(?:再)?用|改天(?:再)?用|以后(?:再)?用",
            message,
        ))
        if delayed_use_question:
            effective_policy = policy_text()
            same_day_required = bool(re.search(
                r"当天[^。；\n]{0,10}(?:购买|使用)|(?:购买|使用)[^。；\n]{0,10}当天",
                effective_policy,
            ))
            if not same_day_required:
                known_validity = policy_sentences(("有效期", "使用日期", "购买后"), "")
                reply = known_validity or (
                    "当前商品资料暂未明确购买后能否隔天使用。为避免过期，请确认到店使用当天再购买。"
                )
                return {
                    "reply": reply,
                    "source": "当前商品的单独有效期政策",
                    "decision": "allow",
                    "kind": "same_day_use",
                }
            return {
                "reply": (
                    "这款券需要当天购买、当天使用哦，暂不支持购买后过两天再用。"
                    "建议您确定到店当天再下单，使用会更稳妥。"
                ),
                "source": "购买当天使用规则",
                "decision": "allow",
                "kind": "same_day_use",
            }
        if any(word in message for word in ("明天能用", "明天可以用", "明天可用", "明天用")):
            tomorrow = datetime.now(CHINA_TZ).date() + timedelta(days=1)
            date_label = f"{tomorrow.year}年{tomorrow.month}月{tomorrow.day}日"
            knowledge = "\n".join([
                str(product.get("raw_text") or ""),
                str(product.get("ai_summary") or ""),
            ])
            unavailable = bool(re.search(
                rf"{re.escape(date_label)}[^。；\n]{{0,16}}(?:不可用|不能用)", knowledge
            ))
            weekday_only = "工作日" in knowledge and not any(
                word in knowledge for word in ("全周", "周末可用", "节假日可用", "节假日通用")
            )
            if unavailable or (weekday_only and tomorrow.weekday() >= 5):
                reply = f"明天是{date_label}，根据当前商品说明，该券明天不可用。"
            else:
                reply = "明天可用，但是请当天买、当天使用。"
            return {
                "reply": reply,
                "source": "北京时间与当前商品知识",
                "decision": "allow",
                "kind": "tomorrow_use",
            }
        purchase_entry = self.purchase_entry_reply(product, message)
        if purchase_entry:
            return {
                "reply": purchase_entry,
                "source": "当前商品页面与真实在售规格",
                "decision": "deny" if "无法购买" in purchase_entry or "没有" in purchase_entry else "allow",
                "kind": "purchase_flow",
            }

        if any(word in message for word in (
            "有效期", "什么时候过期", "改天能用", "以后能用", "隔天能用", "长期有效",
        )):
            effective_policy = policy_text()
            same_day_required = bool(re.search(
                r"当天[^。；\n]{0,10}(?:购买|使用)|(?:购买|使用)[^。；\n]{0,10}当天",
                effective_policy,
            ))
            if not same_day_required:
                known_validity = policy_sentences(("有效期", "使用日期", "购买后"), "")
                return {
                    "reply": known_validity or (
                        "当前商品资料暂未明确有效期。为避免过期，请确认到店使用当天再购买。"
                    ),
                    "source": "当前商品的单独有效期政策",
                    "decision": "allow",
                    "kind": "same_day_use",
                }
            return {
                "reply": (
                    "这款券需要当天购买、当天使用哦。建议您确定到店当天再下单，"
                    "避免提前购买后未及时使用。"
                ),
                "source": "当前商品适用的发货与退款政策",
                "decision": "allow",
                "kind": "same_day_use",
            }

        coupon_type = str(product.get("coupon_type") or "").strip()
        coupon_label = self.coupon_type_display(product)
        if any(word in message for word in ("转赠", "转送", "送给别人", "给别人用")):
            if coupon_type in {"mixed", "promotion"}:
                delivery = self.coupon_usage_instructions(product, message)
                return {
                    "reply": (
                        "当前商品知识暂未说明该规格是否支持转赠，请告诉我具体规格或面额后确认。"
                        + (f"\n{delivery}" if delivery else "")
                    ),
                    "source": "商品知识中的SKU级发券信息",
                    "decision": "allow",
                    "kind": "transfer_usage",
                }
            return {
                "reply": "不支持转赠哦，需要购买人本人下单并使用。\n\n付款后系统会自动发送领取信息，使用时打开美团券码，到适用门店向工作人员出示券码核销即可。",
                "source": "卡券转赠与核销规则",
                "decision": "allow",
                "kind": "transfer_usage",
            }
        platform_words = (
            "什么券", "发的什么券", "哪个平台", "什么平台", "美团还是抖音",
            "美团券吗", "抖音券吗", "是美团", "是抖音", "哪种券", "什么卡券",
            "卡券类型", "发什么卡", "什么渠道", "哪个渠道", "发券渠道", "发卡渠道",
            "发券平台", "发卡平台",
        )
        if any(word in message for word in platform_words):
            if coupon_type in {"mixed", "promotion"}:
                delivery = self.coupon_usage_instructions(product, message)
                return {
                    "reply": (
                        "根据当前商品知识，不同规格的发券渠道如下：\n" + delivery
                        if delivery else
                        "当前商品知识暂未标明具体发券渠道，请告诉我具体规格或面额后再确认。"
                    ),
                    "source": "商品知识中的SKU级发券信息",
                    "decision": "allow",
                    "kind": "coupon_type",
                }
            if not coupon_label:
                return {
                    "reply": "当前商品资料暂未设置卡券类型，暂时无法准确确认。",
                    "source": "卡券类型未配置",
                    "decision": "allow",
                    "kind": "coupon_type",
                }
            wrong_platform = ""
            if "美团" in message and product.get("coupon_type") != "meituan":
                wrong_platform = "，不是美团券"
            elif "抖音" in message and product.get("coupon_type") != "douyin":
                wrong_platform = "，不是抖音券"
            return {
                "reply": f"当前商品发放的是{coupon_label}{wrong_platform}。",
                "source": "当前商品人工设置的卡券类型",
                "decision": "allow",
                "kind": "coupon_type",
            }

        if any(word in message for word in ("链接失效", "显示错误", "页面报错", "还是打不开", "依然打不开", "复制到浏览器也打不开")):
            return {
                "reply": "该链接需要人工核实，我已为您转人工处理，请稍等。",
                "source": "券码链接二次失败",
                "decision": "review",
                "kind": "code_link_escalation",
            }
        if any(word in message for word in ("链接打不开", "点不开", "无法打开", "页面打不开", "链接没反应", "打不开")):
            return {
                "reply": "如果券码链接无法直接打开，请长按复制链接，粘贴到浏览器地址栏打开查看。",
                "source": "券码链接固定回复",
                "decision": "allow",
                "kind": "code_link",
            }
        if any(word in message for word in (
            "刷新券码", "换码", "补发券码", "重新发码", "重新发链接",
            "重新发一下", "重新发一份", "重发一下", "再发一下", "需要重新发",
            "重新发一个", "重新发过", "再发一个", "重新发送", "再次发送",
        )):
            return {
                "reply": "券码刷新、换码或补发需要人工授权处理，已经为您记录并转交人工，我们会在72小时内核实处理。",
                "source": "券码操作需要人工授权",
                "decision": "review",
                "kind": "code_operation_review",
            }
        if any(word in message for word in (
            "刚刚那个撤销了", "刚才那个撤销了", "上一个撤销了",
            "刚刚那个取消了", "刚才那个取消了", "上一个取消了",
        )):
            return {
                "reply": "撤销后的重新处理需要人工核实，已经为您记录并转交人工，我们会在72小时内处理。",
                "source": "撤销后的重新处理需要人工授权",
                "decision": "review",
                "kind": "code_operation_review",
            }
        if "刷新" in message:
            return {
                "reply": "请问是券码链接无法打开，还是券码显示异常呢？请把具体情况用文字告诉我，我为您进一步处理。",
                "source": "刷新请求语义确认",
                "decision": "allow",
                "kind": "refresh_clarification",
            }
        if any(word in message for word in (
            "码在哪里", "码在哪", "券码在哪里", "券码在哪", "怎么查看券码", "没看到券码",
            "券码没发", "没收到码", "没收到券码", "兑换码在哪里", "兑换码在哪", "没有链接",
        )):
            return {
                "reply": "请向上滑动聊天记录，找到券码消息并点击其中的链接查看。如果没有看到链接，请回复“没有链接”，我再为您核实。",
                "source": "券码位置固定回复",
                "decision": "allow",
                "kind": "code_location",
            }

        anomaly_words = ("发错码", "核销失败", "券码无效", "码无效", "无法核销")
        if any(word in message for word in anomaly_words):
            return {
                "reply": (
                    "券码异常或无法核销需要人工核实，已经为您记录并转交人工，"
                    "我们会在72小时内处理。请保留相关页面提示。"
                ),
                "source": "券码异常需要人工核实",
                "decision": "review",
                "kind": "code_operation_review",
            }
        else:
            send_words = ("怎么发", "如何发货", "发什么", "自动发货吗", "券码怎么收到", "怎么收到")
            use_words = ("怎么用", "如何使用", "怎么核销", "到店怎么操作", "如何核销")
            asks_send = any(word in message for word in send_words)
            asks_use = any(word in message for word in use_words)
            if asks_send or asks_use:
                reply = self.coupon_usage_instructions(product, message)
                if not reply:
                    reply = "商品知识暂未标明该规格的发券、领取或核销方式，请说明具体规格或面额后再确认。"
                return {
                    "reply": reply,
                    "source": "商品发码与核销知识" if coupon_type in {"mixed", "promotion"} else "发码与核销固定回复",
                    "decision": "allow",
                    "kind": "delivery_usage",
                }

        if any(word in message for word in (
            "去店里再买", "到店再买", "店里再买", "现场再买", "现场买吗",
            "去店里买", "到店买吗", "怎么买", "如何购买",
        )):
            if coupon_type == "promotion":
                return self.promotion_purchase_reply(product)
            if coupon_type == "mixed":
                delivery = self.coupon_usage_instructions(product, message)
                return {
                    "reply": (
                        "直接在当前商品页面选择所需规格拍下并付款。"
                        + (f"\n{delivery}" if delivery else "具体发券和领取方式以所选规格的商品知识为准。")
                    ),
                    "source": "商品知识中的SKU级购买与发券信息",
                    "decision": "allow",
                    "kind": "purchase_flow",
                }
            return {
                "reply": "不用到店后再买哦，直接在当前商品页面拍下并付款，系统会自动发送电子券；到店后出示券码核销即可。请确认当天需要使用后再购买。",
                "source": "购买、发货与核销流程",
                "decision": "allow",
                "kind": "purchase_flow",
            }

        # Product-option and attribute questions outrank store search. Store
        # lookup must never be triggered merely because a number appears in an
        # address or ordinary text resembles a branch name.
        attribute = self.product_attribute_reply(product, message)
        if attribute:
            return attribute

        option_availability = self.sku_availability_reply(product, message)
        if option_availability:
            return {
                "reply": option_availability,
                "source": "当前商品真实规格列表",
                "decision": "allow",
                "kind": "sku_availability",
                **({"query_context_update": {"last_sku_catalog": True}}
                   if re.search(r"代金券|优惠券|券型|面额", message) else {}),
            }

        explicit_area_list_query = bool(
            any(self._admin_key(city) in self._admin_key(message) for city in KNOWN_CITY_NAMES)
            and re.search(r"(?:有|都)?(?:哪家|那家|哪些|哪几家|几家|所有).{0,4}(?:门店|店)?", message)
        )
        referential_store = not explicit_area_list_query and any(
            word in message for word in ("这家店", "这个店", "该店", "那里", "刚才那家", "那家店")
        )
        # A buyer commonly sends the store name first and follows with only
        # “可以用吗/能用吗”.  When this conversation already has a store result,
        # treat these short confirmations as referring to that result.
        trusted_store_context = bool(store_context) and bool(store_context.get("verified", True))
        short_store_confirmation = trusted_store_context and bool(
            re.fullmatch(
                r"(?:这边|这里|那里|它|这个|该店)?(?:可以|能|可)(?:在这)?用(?:吗|嘛|不|么)*[？?。！!]*",
                compact,
            )
        )
        referential_store = referential_store or short_store_confirmation
        if referential_store and trusted_store_context:
            matches = list(store_context.get("matches") or [])
            if matches:
                if len(matches) == 1:
                    match = matches[0]
                    label = "".join(filter(None, [match.get("city", ""), match.get("branch", "")]))
                    reply = f"可以，{label or '刚才查询的门店'}可用。"
                else:
                    reply = self.format_store_matches(
                        matches, str(store_context.get("query") or "刚才查询的门店"), message
                    )
                return {
                    "reply": reply,
                    "source": "当前会话最近一次门店查询",
                    "decision": "allow",
                    "kind": "stores",
                    "store_matches": matches,
                    "store_query": str(store_context.get("query") or ""),
                }
        # Location cleanup is deliberately scoped to this store-intent path.
        # Other intent handlers continue to receive the untouched buyer text.
        query = extract_store_query(
            message, product=product, product_brand=self.extract_brand(product)
        )
        requested_store_query = query
        inherited_day = str((store_context or {}).get("pending_day_type") or "")
        sku_message = message
        if (
            inherited_day in {"weekday", "weekend", "holiday"}
            and not self._requested_day_type(message)
            and is_meaningful_store_query(query)
        ):
            day_word = {
                "weekday": "工作日", "weekend": "周末", "holiday": "节假日",
            }[inherited_day]
            sku_message = f"{day_word}{message}"
        pending_mode = str((store_context or {}).get("pending_store_query_mode") or "")
        pending_query = str((store_context or {}).get("pending_store_query") or "").strip()
        if pending_mode == "generic_landmark" and pending_query and is_meaningful_store_query(query):
            query = query + pending_query
        sku_scope = self._multi_sku_store_scope(item_id, product)
        skus = sku_scope.get("skus") or []
        sku_stores_differ = bool(sku_scope.get("stores_differ"))
        matched_skus = self.match_message_skus(item_id, sku_message, product)
        selected_sku = matched_skus[0] if len(matched_skus) == 1 else None
        if not selected_sku and store_context:
            prior_key = str(store_context.get("selected_sku_key") or "")
            selected_sku = next((sku for sku in skus if sku["sku_key"] == prior_key), None)
        selected_key = selected_sku["sku_key"] if selected_sku else ""
        if sku_stores_differ:
            multi_sku_store = self.resolve_multi_sku_store_query(
                item_id, product, sku_message, store_context,
            )
            if multi_sku_store:
                return multi_sku_store
        scope_list_ids = list(sku_scope.get("list_ids") or [])
        uniform_list_ids = (
            scope_list_ids
            if scope_list_ids and not sku_stores_differ and not selected_key else None
        )
        raw_store_result = (
            self.search_store(
                item_id,
                query if pending_mode == "generic_landmark" else message,
                sku_key=selected_key,
                list_ids_override=uniform_list_ids,
            )
            if is_meaningful_store_query(
                query if pending_mode == "generic_landmark" else message
            ) else {}
        )
        if (
            raw_store_result.get("resolved_query")
            and normalize_text(raw_store_result.get("resolved_query")) != normalize_text(message)
        ):
            store_result = raw_store_result
        elif is_meaningful_store_query(query):
            store_result = self.search_store(
                item_id, query, sku_key=selected_key,
                list_ids_override=uniform_list_ids,
            )
        else:
            store_result = {"status": "missing_query", "matches": []}
        query = str(store_result.get("resolved_query") or query).strip()
        if store_result.get("status") != "available":
            query = str(store_result.get("specific_query") or query).strip()
        if self.is_explicit_store_query(message, query, store_result):
            if not is_meaningful_store_query(query):
                return {
                    "reply": "请发送具体城市、商圈或门店名称，我帮您查询可用门店。",
                    "source": "门店关键词不完整",
                    "decision": "allow",
                    "kind": "stores_clarify",
                }
            clarification = self._store_search_clarification(
                query, store_result, [selected_sku] if selected_sku else [],
            )
            if clarification:
                return clarification
            if sku_stores_differ and not selected_sku:
                supported = self.reverse_store_sku_matches(item_id, query, product)
                if supported:
                    labels = "、".join(self._sku_public_label(sku) for sku in supported)
                    return {
                        "reply": f"根据“{query}”查询，可使用的商品规格有：{labels}。请按对应规格拍下。",
                        "source": "门店反向匹配当前商品真实规格和售价",
                        "decision": "allow", "kind": "stores_sku_recommendation",
                        "store_matches": [match for sku in supported for match in sku["matches"]],
                        "store_query": query,
                    }
                configured = [sku for sku in skus if sku.get("configuration_ready")]
                if configured:
                    return {
                        "reply": f"暂未在任何已配置规格的可用门店中查询到{query}。该门店可能不适用，您也可以换一个门店名称查询。",
                        "source": "所有已配置规格的门店表均未匹配",
                        "decision": "deny", "kind": "stores", "store_matches": [],
                        "store_query": query, "store_status": "unavailable",
                    }
                return {
                    "reply": "当前各商品规格都还没有配置可用门店资料，暂时无法准确推荐，请等待人工核实。",
                    "source": "规格门店资料均未配置",
                    "decision": "review", "kind": "stores",
                }
            if store_result.get("status") == "area_fallback":
                return {
                    "reply": self.format_store_area_fallback(
                        store_result, query, message,
                    ),
                    "source": "具体门店未匹配，返回所提市区的全部可用门店",
                    "decision": "allow", "kind": "stores",
                    "store_matches": list(store_result.get("matches") or []),
                    "store_query": query, "store_status": "area_fallback",
                    "query_context_update": ({
                        "selected_sku_key": selected_sku["sku_key"],
                        "selected_sku_name": selected_sku["sku_name"],
                    } if selected_sku else {}),
                }
            if store_result["status"] == "available":
                prefix = f"{self._sku_public_label(selected_sku)}：" if selected_sku else ""
                # When store applicability differs and the buyer did not name a
                # specification, reverse-match the store instead of presenting
                # a product-wide claim.
                if sku_stores_differ and not selected_sku:
                    supported = self.reverse_store_sku_matches(item_id, query, product)
                    if supported:
                        labels = "、".join(self._sku_public_label(sku) for sku in supported)
                        return {
                            "reply": f"根据“{query}”查询，可使用的商品规格有：{labels}。请按对应规格拍下。",
                            "source": "门店反向匹配当前商品真实规格和售价",
                            "decision": "allow", "kind": "stores_sku_recommendation",
                            "store_matches": [match for sku in supported for match in sku["matches"]],
                            "store_query": query,
                        }
                return {
                    "reply": prefix + self.format_store_matches(
                        store_result["matches"],
                        requested_store_query
                        if (
                            is_meaningful_store_query(requested_store_query)
                            and re.search(r"(?:区|县|旗)", requested_store_query)
                        ) else query,
                        message,
                    ),
                    "source": "当前商品绑定门店表",
                    "decision": "allow",
                    "kind": "stores",
                    "store_matches": store_result["matches"],
                    "store_query": query,
                    "store_status": "available",
                    "query_context_update": ({
                        "selected_sku_key": selected_sku["sku_key"],
                        "selected_sku_name": selected_sku["sku_name"],
                    } if selected_sku else {}),
                }
            if store_result["status"] == "unavailable":
                if sku_stores_differ and not selected_sku:
                    supported = self.reverse_store_sku_matches(item_id, query, product)
                    if supported:
                        labels = "、".join(self._sku_public_label(sku) for sku in supported)
                        return {
                            "reply": f"根据“{query}”查询，可使用的商品规格有：{labels}。请按对应规格拍下。",
                            "source": "门店反向匹配当前商品真实规格和售价",
                            "decision": "allow", "kind": "stores_sku_recommendation",
                            "store_query": query,
                        }
                    return {
                        "reply": f"暂未在任何已配置规格的可用门店中查询到{query}。该门店可能不适用，您也可以换一个门店名称查询。",
                        "source": "所有已配置规格的门店表均未匹配",
                        "decision": "deny", "kind": "stores",
                    }
                prefix = f"{self._sku_public_label(selected_sku)}的" if selected_sku else ""
                return {
                    "reply": prefix + self.format_store_unavailable(
                        query,
                        area_only=bool(
                            (store_result.get("province") or store_result.get("city"))
                            and not store_result.get("search_term")
                        ),
                    ),
                    "source": "当前商品绑定门店表",
                    "decision": "deny",
                    "kind": "stores",
                    "store_matches": [], "store_query": query, "store_status": "unavailable",
                }
            if store_result["status"] == "sku_store_unconfigured":
                return {
                    "reply": f"{self._sku_public_label(selected_sku or {})}暂未配置适用门店资料，无法准确确认该门店是否可用，请等待人工核实。",
                    "source": "当前规格门店资料未配置",
                    "decision": "review", "kind": "stores",
                }
            if store_result["status"] == "no_store_list":
                return {
                    "reply": "当前商品暂未配置可用门店资料，暂时无法准确确认该门店是否可用。",
                    "source": "门店表缺失",
                    "decision": "allow",
                    "kind": "stores",
                }

        time_words = ("现在", "当前", "中午", "午餐", "晚上", "晚餐", "周末", "工作日", "这个时间")
        availability_words = ("能用", "可用", "可以用", "适合", "推荐")
        if any(word in message for word in time_words) and any(word in message for word in availability_words):
            result = self.evaluate_time(item_id)
            if result["status"] == "blocked":
                rule = result["rule"] or {}
                reply = (
                    f"现在是{result['weekday_label']}{result['meal_period']}时段，"
                    f"根据当前商品知识，该券当前不可使用。"
                )
                if rule.get("reply"):
                    reply += str(rule["reply"]).strip()
                if rule.get("next_hint"):
                    reply += " " + rule["next_hint"]
                return {"reply": reply, "source": f"当前商品知识：{rule.get('label', '')}", "decision": "deny", "kind": "time"}
            if result["status"] == "allowed":
                rule = result["rule"] or {}
                return {
                    "reply": f"现在是{result['weekday_label']}{result['meal_period']}时段，根据当前商品说明，该券当前可以使用。",
                    "source": f"当前商品知识：{rule.get('label', '')}",
                    "decision": "allow",
                    "kind": "time",
                }
            return {
                "reply": (
                    "当前商品资料暂未明确具体使用时段，暂时无法准确确认现在是否可用。"
                    "请告诉我想查询工作日、周末、午市还是晚市，我再按商品资料为您核对。"
                ),
                "source": "商品资料未提供明确使用时间",
                "decision": "allow",
                "kind": "time",
            }
        return None

    def add_event(self, event_type: str, message: str, payload: Optional[Dict] = None):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO v2_events(event_type,message,payload,created_at) VALUES(?,?,?,?)",
                (event_type, message, json.dumps(payload or {}, ensure_ascii=False), self._now()),
            )

    def list_events(self, limit: int = 100) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM v2_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_refund_order(self, payload: Dict) -> Dict:
        now = self._now()
        order_id = str(payload.get("order_id") or "").strip()
        scope_id = str(payload.get("scope_id") or "").strip()
        paid_amount = str(payload.get("paid_amount") or "").strip()
        suggested_amount = ""
        if paid_amount:
            try:
                suggested_amount = str(
                    (Decimal(paid_amount) * Decimal("0.95")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
            except InvalidOperation:
                suggested_amount = ""
        refund_type = str(payload.get("refund_type") or "待人工判断").strip()
        deadline = str(payload.get("deadline_at") or "").strip()
        if not deadline:
            deadline = (datetime.now() + timedelta(hours=72)).isoformat(timespec="seconds")
        with self._connect() as conn:
            row = None
            if order_id:
                row = conn.execute(
                    "SELECT id FROM refund_orders WHERE order_id=? ORDER BY id DESC LIMIT 1",
                    (order_id,),
                ).fetchone()
            if not row and scope_id:
                row = conn.execute(
                    """SELECT id FROM refund_orders WHERE scope_id=?
                       AND status NOT IN ('已退款','已拒绝','已关闭','已处理')
                       ORDER BY id DESC LIMIT 1""",
                    (scope_id,),
                ).fetchone()
            values = (
                order_id, scope_id, str(payload.get("chat_id") or ""),
                str(payload.get("user_id") or ""), str(payload.get("user_name") or ""),
                str(payload.get("item_id") or ""), str(payload.get("product_title") or ""),
                paid_amount, suggested_amount, refund_type,
                str(payload.get("reason") or "").strip(),
                str(payload.get("status") or "待核实").strip(),
                str(payload.get("source") or "买家消息").strip(),
                str(payload.get("order_url") or "").strip(),
                str(payload.get("applied_at") or now), deadline, now,
            )
            if row:
                conn.execute(
                    """UPDATE refund_orders SET order_id=?,scope_id=?,chat_id=?,user_id=?,
                       user_name=?,item_id=?,product_title=?,paid_amount=?,suggested_amount=?,
                       refund_type=?,reason=?,status=?,source=?,order_url=?,applied_at=?,deadline_at=?,updated_at=?
                       WHERE id=?""",
                    values + (int(row["id"]),),
                )
                refund_id = int(row["id"])
            else:
                cur = conn.execute(
                    """INSERT INTO refund_orders(
                       order_id,scope_id,chat_id,user_id,user_name,item_id,product_title,
                       paid_amount,suggested_amount,refund_type,reason,status,source,order_url,
                       applied_at,deadline_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
                refund_id = int(cur.lastrowid)
        return self.get_refund_order(refund_id)

    def get_refund_order(self, refund_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM refund_orders WHERE id=?", (int(refund_id),)
            ).fetchone()
        return dict(row) if row else None

    def list_refund_orders(self, limit: int = 300) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM refund_orders
                   WHERE source<>'买家退款咨询'
                   ORDER BY updated_at DESC LIMIT ?""", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_refund_order_status(self, refund_id: int, status: str) -> Dict:
        allowed = {"待核实", "处理中", "已退款", "已拒绝", "已关闭", "已处理"}
        status = str(status or "").strip()
        if status not in allowed:
            raise ValueError("退款订单状态无效")
        with self._connect() as conn:
            conn.execute(
                "UPDATE refund_orders SET status=?,updated_at=? WHERE id=?",
                (status, self._now(), int(refund_id)),
            )
        record = self.get_refund_order(refund_id)
        if not record:
            raise ValueError("退款订单不存在")
        return record

    def dashboard(self, products=None, stores=None, refund_orders=None) -> Dict:
        products = self.list_v2_products() if products is None else products
        stores = self.list_store_lists() if stores is None else stores
        audits = self.count_audits()
        refund_orders = self.list_refund_orders() if refund_orders is None else refund_orders
        return {
            "products": len(products),
            "enabled_products": sum(1 for item in products if item["enabled"]),
            "store_lists": len(stores),
            "stores": sum(item["store_count"] for item in stores),
            "audits": audits,
            "refunds": {
                "total": len(refund_orders),
                "pending": sum(
                    1 for item in refund_orders
                    if item.get("status") in {"待核实", "处理中"}
                ),
            },
            "events": self.list_events(8),
        }


def extract_store_query(message: str, product: Optional[Dict] = None,
                        product_brand: str = "") -> str:
    text = str(message or "").strip()
    # Tolerate common key-repeat typos without widening fuzzy store matching.
    text = re.sub(r"能{2,}(?=(?:使用|用))", "能", text)
    # Product titles are frequently followed by a parenthesized branch.  That
    # bracket is a reliable entity boundary; use it before deleting intent
    # words from the rest of the sentence.
    bracket_locations = [
        part.strip(" ，,。；;：:")
        for part in re.findall(r"[（(]([^（）()]{2,40})[）)]", text)
        if any(word.lower() in part.lower() for word in STORE_LANDMARK_WORDS)
    ]
    if bracket_locations:
        return bracket_locations[-1]
    if re.search(r"不是|不要|别查|不查", text):
        correction = re.search(r"(?:而是|改成|[，,；;]\s*(?:是|查|要))\s*(.+)$", text)
        if correction:
            text = correction.group(1)
    # A buyer may put an independent purchase-timing confirmation directly
    # before a branch without punctuation, for example “吃完再买对吧 丹竹头店
    # 可以用吗”.  Treat the confirmation word as an entity boundary so that
    # the first question can never become part of the store search key.
    text = re.sub(
        r"^.*?(?:对吧|是吧|没错吧|对不对)[，,；;\s]*"
        r"(?=(?:[\u4e00-\u9fffA-Za-z0-9]{2,40})(?:总店|旗舰店|分店|门店|店|"
        r"商场|广场|购物中心|万达|万象城|万象汇|天虹))",
        "", text,
    )
    text = re.sub(
        r"^(?:吃完|吃了|用餐后|消费后|结账前|买单前)"
        r"[^。！？\n]{0,10}(?:再|才)?(?:买|拍|购买|下单)(?:对吧|是吧)?"
        r"[，,；;\s]*",
        "", text,
    )
    text = re.sub(
        r"^(?:老板|亲|您好|你好|哈喽|那个|这个|请问(?:一下|下)?|问一下|我在|"
        r"我这边(?:在|是)?|我的地址(?:在|是)?|定位(?:在|是)?|我想问|想问下|想咨询|"
        r"麻烦(?:问下|帮忙)?|想问(?:一下|下)?|帮我(?:查下|查一下)?)"
        r"\s*[，,：:~-]*",
        "", text,
    )
    for phrase in (
        "适用门店相关问题", "适用门店问题", "门店相关问题", "门店问题",
        "有什么适用门店", "有哪些适用门店", "哪些适用门店",
        "都有哪些可以使用", "都有哪些可以用", "有哪些可以使用", "有哪些可以用",
        "哪些门店可以使用", "哪些门店可以用", "哪些店可以使用", "哪些店可以用",
        "哪些门店能使用", "哪些门店能用", "哪些店能使用", "哪些店能用",
        "哪里可以使用", "哪里可以用", "哪里能使用", "哪里能用",
        "有哪几家门店", "有哪几家店", "有哪几家", "有什么门店", "有什么店",
        "有哪家门店", "有哪家店", "有那家门店", "有那家店",
        "有那些门店", "有那些店", "有哪些门店", "有哪些店",
        "这个券", "这张券", "该券", "能不能使用", "能不能用", "可不可以使用", "可不可以用",
        "不能使用吗", "不能用吗", "不可以使用吗", "不可以用吗", "不可使用吗", "不可用吗",
        "可以使用嘛", "可以使用吗", "可以用嘛", "能用嘛", "可用嘛",
        "也可以使用", "也可以用", "也能用", "也可用", "也可以",
        "可以用吗", "可以吗", "能用吗", "可用吗", "支持吗", "行吗",
        "可以用", "可以", "能用", "可用", "支持", "适用",
        "适用吗", "能不能用", "可以使用吗", "是否可用", "哪些门店", "哪个门店", "门店",
        "还有没有", "还有吗", "还有么", "还有嘛", "有没有", "没有", "有吗",
        "查一下", "帮我查", "请问", "附近", "吗", "嘛", "么", "呀", "呢", "吧", "？", "?",
        "适用门店问题", "门店问题", "适用问题", "相关问题", "问题", "查询", "咨询",
        "地址在哪里", "地址", "位置", "在哪里", "在哪儿", "在哪", "怎么走",
        "联系电话", "联系方式", "电话", "号码", "营业时间", "几点开门", "几点关门",
        "几点打烊", "开门", "关门", "打烊", "营业",
        "多少钱", "多钱", "什么价格", "价格多少", "价钱", "售价", "什么价", "怎么卖",
        "今天什么优惠", "今日什么优惠", "现在什么优惠", "当前什么优惠",
        "有什么优惠", "有啥优惠", "什么优惠", "优惠活动", "活动", "优惠", "折扣",
        "现在马上", "现在", "马上", "立即", "立刻",
        "这个点", "那个点", "这个地方", "那个地方", "这个位置", "那个位置",
        "这里", "这边", "当地", "那边",
    ):
        text = text.replace(phrase, " ")
    text = re.sub(r"[~～。！!，,、：:]+", " ", text)
    text = re.sub(r"(?:都)?(?:有哪些|有哪几家|哪些|哪里|哪儿|有什么)\s*$", " ", text)
    text = re.sub(r"\b(?:该|这个|那个|这里|那里|不|使用|您好|你好)\b", " ", text)
    text = re.sub(r"(?:这个|那个|这里|这边|当地|那边)\s*$", " ", text)
    text = re.sub(r"(?:^|\s)有\s*$|有\s*$", " ", text)
    # Strip Latin brand/product tokens only when they are known to belong to
    # the current product.  This fixes glued queries such as
    # “南昌万象城need” without deleting legitimate location abbreviations
    # such as IFS when they are not product-brand noise.
    if isinstance(product, dict):
        product_text = " ".join((
            str(product.get("title") or ""),
            str(product.get("coupon_type_custom") or ""),
        ))
        product_latin_tokens = {
            token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,30}", product_text)
            if token.lower() not in {"ifs", "mixc", "k11"}
        }
        for token in sorted(product_latin_tokens, key=len, reverse=True):
            text = re.sub(re.escape(token), " ", text, flags=re.I)
        # Category words from the current product title are not branch names.
        # Removing only grounded categories keeps queries such as
        # “石家庄聚宝源涮肉东尚店” focused on 石家庄 + 东尚店.
        for category in (
            "椰子鸡", "涮肉", "羊肉火锅", "火锅", "活鱼烤鱼", "烤鱼",
            "韩式烤肉", "日式烤肉", "烤肉", "烧肉", "烧烤",
            "韩国料理", "日本料理", "日式料理", "料理", "牛排",
            "自助餐", "自助", "餐饮", "美食",
        ):
            if category in product_text:
                text = text.replace(category, " ")
    # The deterministic product parser may also know a Chinese merchant brand.
    # Remove that exact, product-grounded literal only; never guess arbitrary
    # Chinese words as brands because they may be part of a real mall/branch.
    product_brand = str(product_brand or "").strip()
    brand_aliases = {product_brand} if len(product_brand) >= 2 else set()
    brand_aliases.update(
        part for part in re.split(r"[·•・|丨/\\\s]+", product_brand)
        if len(part) >= 2
    )
    for brand_alias in sorted(brand_aliases, key=len, reverse=True):
        text = re.sub(re.escape(brand_alias), " ", text, flags=re.I)
    # Prices, quantities, time/audience conditions and coupon words are not
    # part of a province/city/county/town/street/mall/branch search key.
    text = re.sub(
        r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十单双俩仨]+)\s*"
        r"(?:个)?(?:元|块|人|位|张|份|套)",
        " ", text,
    )
    text = re.sub(
        r"本周末|周末|工作日|平日|节假日|法定假日|今天|今晚|明天|明早|明中午|明晚|后天|"
        r"早餐|中午|午餐|晚上|晚餐|"
        r"成人|大人|儿童|小孩|学生|老人|女士|"
        r"代金券|现金券|抵扣券|套餐券|团购券|电子券|美团券|卡券|券",
        " ", text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    # A final “的” is a grammatical scope marker in queries such as
    # “荆州的/武汉市的”, never part of the city or branch search key.
    text = re.sub(r"的$", "", text).strip()
    return text


def is_meaningful_store_query(query: str) -> bool:
    normalized = normalize_text(query)
    if len(normalized) < 2:
        return False
    meaningless = {
        "该", "这个", "那个", "这里", "那里", "门店", "该门店", "不", "不可使用",
        "不可用", "可以", "能用", "您好", "你好", "具体", "城市", "哪个城市",
        "问题", "门店问题", "适用门店问题", "适用问题", "相关问题", "查询", "咨询",
        "适用门店", "可用门店", "店铺", "哪些门店", "哪些店", "哪里能用",
    }
    return normalized not in {normalize_text(value) for value in meaningless}
