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

FIRST_REPLY_TEMPLATE_VERSION = 7


COUPON_TYPE_LABELS = {
    "meituan": "美团卡券",
    "douyin": "抖音卡券",
    "merchant_miniapp": "商家小程序券",
    "electronic_code": "普通电子券码",
    "purchase_order": "代买单",
    "other": "其他卡券",
}

COUPON_TYPE_DEFAULT_INSTRUCTIONS = {
    "meituan": "当前商品发放的是美团电子券，付款后发送领取信息，到店出示券码核销。",
    "douyin": "当前商品发放的是抖音电子券，付款后发送领取信息，到店出示券码核销。",
    "merchant_miniapp": "当前商品发放的是商家小程序电子券，付款后发送领取信息，到店出示券码核销。",
    "electronic_code": "付款后发电子券码，门店扫码核销。",
    "purchase_order": "当前商品为代买单，付款后按订单说明发送领取信息，请按领取页面提示到店核销。",
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
    "ifs", "mall", "地址", "位置", "在哪", "电话", "号码", "营业", "开门", "打烊", "使用嘛", "用嘛",
)

GENERIC_STORE_LANDMARKS = (
    "万达", "万象城", "万象汇", "壹方城", "壹方天地", "万科里", "天街",
    "银泰", "吾悦", "大悦城", "来福士", "太古里", "印象城", "海岸城",
    "奥特莱斯", "ifs", "mall",
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
    "business_hours": ("营业时间", "营业时段", "营业时间段", "开放时间", "服务时间"),
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
        normalized_product.update({"raw_text": summary, "structured": structured or {}})
        summary = self.build_knowledge_summary(normalized_product) or summary
        structured_text = json.dumps(structured or {}, ensure_ascii=False)
        effective = current["raw_text"] if current.get("manual_edited") else summary
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

    @staticmethod
    def _format_number(value: object) -> str:
        text = str(value or "").strip().replace("￥", "").replace("¥", "")
        text = re.sub(r"\s*元\s*$", "", text)
        try:
            number = float(text)
            return f"{number:.2f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return text

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
            r"(?:^|[^\u4e00-\u9fff])券(?:$|[^\u4e00-\u9fff])",
            evidence,
        ))
        invalid_name = bool(re.search(
            r"[=￥¥%]|地址|重量|斤|公斤|千克|克",
            name,
        ))
        if not voucher_evidence or invalid_name:
            return None
        composition = cls._normalize_composition(composition_value, face_value)
        max_stack = cls._format_number(cls._pick(record, (
            "最多叠加", "叠加上限", "最多使用张数", "最多使用", "max_stack", "max_count",
        )))
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
            **cls._option_availability(record),
        }

    @classmethod
    def _raw_product_options(cls, raw_text: str, title: str = "") -> List[Dict]:
        text = str(raw_text or "").replace("\\n", "\n")
        output = []
        voucher_context = bool(re.search(
            r"\d+(?:\.\d+)?\s*元?\s*(?:代金券|抵扣券|现金券|券)",
            "\n".join((str(title or ""), text)),
        ))
        pattern = re.compile(
            r"(?:^|[\n；;])[ \t]*(?:[①②③④⑤⑥⑦⑧⑨⑩]|\d{1,2}、[ \t]*|\d{1,2}\.[ \t]+)?[ \t]*"
            r"(?P<face>\d+(?:\.\d+)?)[ \t]*元?[ \t]*(?:代金券|券)?"
            r"[ \t]*[：:\"“”']+[ \t]*(?:售价|价格)?[ \t]*[¥￥]?[ \t]*(?P<price>\d+(?:\.\d+)?)[ \t]*元?"
            r"(?P<tail>[^\n；;]{0,100})",
            re.M,
        )
        for match in pattern.finditer(text):
            matched_text = match.group(0)
            if not voucher_context and not re.search(
                r"代金券|抵扣券|现金券|\d+(?:\.\d+)?\s*元券", matched_text
            ):
                continue
            face = cls._format_number(match.group("face"))
            tail = match.group("tail") or ""
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
                "name": f"{face}元代金券",
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
                "products", "product_options", "skus", "sku", "商品规格", "商品列表",
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
        # For automatically synced products the real SKU payload is the price
        # authority; listing prose is only a supplement. A human-edited product
        # keeps the manually saved text as the highest authority.
        sku_authoritative = bool(structured_options) and not bool(product.get("manual_edited"))
        output = list(structured_options if sku_authoritative else raw_options)
        seen = {
            (item.get("face_value", ""), item.get("applicable_time", ""), item.get("sale_price", ""))
            for item in output
        }
        supplements = raw_options if sku_authoritative else structured_options
        for option in supplements:
            key = (option.get("face_value", ""), option.get("applicable_time", ""), option.get("sale_price", ""))
            if key not in seen:
                same_sku = next((
                    item for item in output
                    if item.get("face_value", "") == option.get("face_value", "")
                    and (
                        not item.get("applicable_time")
                        or not option.get("applicable_time")
                        or item.get("applicable_time") == option.get("applicable_time")
                    )
                ), None)
                if same_sku:
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
        # Some Goofish listings expose quantity selectors as separate SKUs
        # (100券、100券×2、100券×3). They are one sellable denomination, not
        # three products. Keep the unit SKU and retain the largest stack count.
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
                    current_max = int(str(base.get("max_stack") or "0") or "0")
                    base["max_stack"] = str(max(current_max, quantity))
                    continue
            collapsed.append(option)
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
            r"怎么收|怎么买|怎么拍|一共|总共|合计|要付)",
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
            r"([一二两三四五六七八九十单双俩仨\d]+)\s*(?:个|口)?(?:人|位)?",
            text,
        )
        count = None
        if match and re.search(r"人|位|口|单人|双人|[俩仨]", match.group(0)):
            count = cls._chinese_count(match.group(1))
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
        patterns = {
            "adult": rf"({number})\s*(?:个|名|位)?(?:成人|大人)",
            "child": rf"({number})\s*(?:个|名|位)?(?:儿童|小孩|小朋友|宝宝|娃)",
            "student": rf"({number})\s*(?:个|名|位)?学生",
            "senior": rf"({number})\s*(?:个|名|位)?(?:老人|老年人|长者)",
            "female": rf"({number})\s*(?:个|名|位)?(?:女士|女生|女性|女宾)",
        }
        for kind, pattern in patterns.items():
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
        if re.search(r"儿童|小孩|小朋友|宝宝|娃|儿童票|儿童餐", text):
            result.append("child")
        if re.search(r"学生|学生票", text):
            result.append("student")
        if re.search(r"老人|老年|长者|老人票", text):
            result.append("senior")
        if re.search(r"女士|女生|女性|女宾|女士票", text):
            result.append("female")
        if re.search(r"成人|大人|成人票", text):
            result.append("adult")
        return list(dict.fromkeys(result))

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
        if re.search(r"晚餐|晚市|晚上|夜间|夜宵|晚饭", text):
            result.append("dinner")
        if re.search(r"下午茶|茶歇|午后茶", text):
            result.append("afternoon_tea")
        return list(dict.fromkeys(result))

    @classmethod
    def _normalize_sale_option(cls, record: Dict) -> Optional[Dict]:
        voucher = cls._normalize_product_option(record)
        if voucher:
            evidence = " ".join(str(voucher.get(key) or "") for key in ("name", "applicable_time"))
            return {
                **voucher,
                "option_type": "voucher",
                "people_counts": cls._people_counts(evidence),
                "day_types": cls._day_types(evidence),
                "meal_periods": cls._meal_periods(evidence),
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
            "day_types": cls._day_types(f"{name} {applicable_time} {day_value}"),
            "meal_periods": cls._meal_periods(f"{name} {applicable_time} {meal_value}"),
            "audience_types": cls._audience_types(evidence),
            **cls._option_availability(record),
        }

    @classmethod
    def extract_sale_options(cls, product: Dict) -> List[Dict]:
        """Return verifiable coupon and package choices for semantic filtering."""
        output = []
        for option in cls.extract_product_options(product):
            evidence = " ".join(str(option.get(key) or "") for key in ("name", "applicable_time"))
            output.append({
                **option,
                "option_type": "voucher",
                "people_counts": cls._people_counts(evidence),
                "day_types": cls._day_types(evidence),
                "meal_periods": cls._meal_periods(evidence),
                "audience_types": cls._audience_types(evidence),
            })

        structured = product.get("structured") or {}
        facts = structured.get("facts") if isinstance(structured, dict) else {}
        candidates = []
        for container in (structured, facts):
            if not isinstance(container, dict):
                continue
            for key in (
                "sale_options", "packages", "package_options", "套餐规格", "套餐列表",
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
                r"儿童|小孩|学生|老人|老年|长者|女士|女生|成人票|"
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
                "day_types": cls._day_types(evidence),
                "meal_periods": cls._meal_periods(evidence),
                "audience_types": cls._audience_types(evidence),
            })

        unique = []
        seen = set()
        for option in output:
            if not cls._is_sellable_option(option):
                continue
            key = (
                option.get("option_type"), normalize_text(option.get("name")),
                option.get("sale_price"), tuple(option.get("day_types") or []),
                tuple(option.get("meal_periods") or []),
            )
            if key in seen:
                continue
            seen.add(key)
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
        if "今天" in text or "今日" in text:
            day_types = ["weekend" if datetime.now(CHINA_TZ).weekday() >= 5 else "weekday"]
        elif "明天" in text or "明日" in text:
            tomorrow = datetime.now(CHINA_TZ) + timedelta(days=1)
            day_types = ["weekend" if tomorrow.weekday() >= 5 else "weekday"]
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
        if slots.get("day_type"):
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
        evidence = " ".join(str(option.get(key) or "") for key in ("name", "applicable_time"))
        return cls._day_types(evidence)

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
        if re.search(r"今天|今日", text):
            return now
        if re.search(r"明天|明日", text):
            return now + timedelta(days=1)
        if "后天" in text:
            return now + timedelta(days=2)
        match = re.search(
            r"(?<!\d)(?:(\d{4})[年./-])?(\d{1,2})[月./-](\d{1,2})(?:日|号)?(?!\d)",
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

    def date_availability_reply(self, product: Dict, message: str) -> Optional[Dict]:
        text = str(message or "")
        use_intent = bool(re.search(r"(?:可以|能|可)(?:使用|用)|能不能用|是否可用|用得了", text))
        target = self._query_date(text)
        holiday_name = next((name for name in ("中秋", "国庆", "春节", "元旦", "劳动节") if name in text), "")
        if not use_intent or (not target and not holiday_name):
            return None
        knowledge = "\n".join((str(product.get("raw_text") or ""), str(product.get("ai_summary") or "")))
        if holiday_name and re.search(
            rf"(?:{holiday_name}[^。；\n]{{0,20}}(?:不可用|不能用|不适用)|"
            rf"除[^。；\n]{{0,20}}{holiday_name}[^。；\n]{{0,20}}外[^。；\n]{{0,20}}(?:可用|能用|使用))",
            knowledge,
        ):
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
            prefix = "明天" if re.search(r"明天|明日", text) else f"{target.month}月{target.day}日"
            return {"reply": f"{prefix}可以使用哦，请在使用当天购买、当天使用。",
                    "source": "北京时间与当前商品日期规则", "decision": "allow", "kind": "date_use"}
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
        counts = self._audience_counts(text)
        explicit_types = self._audience_types(text)
        if (
            not counts and len(explicit_types) == 1
            and re.search(r"多少钱|多钱|价格|售价|票价|有吗|有没有|有货|能买|能拍", text)
        ):
            counts = {explicit_types[0]: 1}
        if not counts:
            return None
        knowledge = "\n".join((str(product.get("raw_text") or ""), str(product.get("ai_summary") or "")))
        identity_product = bool(re.search(
            r"自助|成人|儿童|小孩|学生|老人|老年|女士|女宾", knowledge
        ))
        if not identity_product:
            return None
        price_intent = bool(re.search(r"多少钱|多钱|价格|售价|怎么卖|几块|几元|收费|票价", text))
        availability_intent = bool(re.search(r"有吗|有没有|有货|能买|能拍|可以吗", text))
        explicit_mix = bool(re.search(
            r"\d+\s*(?:大|成人).*\d+\s*(?:小|儿童|小孩)|"
            r"[一二两三四五六七八九十]+\s*(?:大|成人).*"
            r"[一二两三四五六七八九十]+\s*(?:小|儿童|小孩)|一家三口",
            text,
        ))
        if not (price_intent or availability_intent or explicit_mix):
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
        slots = self._conditional_query_slots(text)
        previous = dict((query_context or {}).get("price_filters") or {})
        if not slots.get("day_type") and previous.get("day_type"):
            slots["day_type"] = previous["day_type"]
        if not slots.get("meal_period") and previous.get("meal_period"):
            slots["meal_period"] = previous["meal_period"]
        options = [
            item for item in self.extract_sale_options(product)
            if item.get("sale_price") and self._is_sellable_option(item)
        ]
        if not slots.get("day_type") and self._has_explicit_day_options(options):
            slots["day_type"] = self._requested_day_type(text, default_today=True)
        has_weekend = any("weekend" in (item.get("day_types") or []) for item in options)
        has_holiday = any("holiday" in (item.get("day_types") or []) for item in options)
        holiday_covers = slots.get("day_type") == "weekend" and has_holiday and not has_weekend
        options = [item for item in options if self._option_matches_time(
            item, slots.get("day_type", ""), slots.get("meal_period", ""), holiday_covers
        )]
        labels = {"adult": "成人", "child": "儿童", "student": "学生", "senior": "老人", "female": "女士"}
        lines, missing = [], []
        total = Decimal("0")

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
                return {
                    "reply": self._format_conditional_option(option, slots),
                    "source": "当前商品现有有货的精确人数套餐",
                    "decision": "allow", "kind": "audience_price",
                }
        for kind, count in counts.items():
            candidates = []
            for option in options:
                audiences = option.get("audience_types") or []
                if kind == "adult":
                    if any(value in audiences for value in ("child", "student", "senior", "female")):
                        continue
                elif kind not in audiences:
                    continue
                people = option.get("people_counts") or []
                exact = count in people
                single = 1 in people or (not people and kind != "adult")
                if exact or single:
                    score = (2 if exact else 1) + (1 if slots.get("meal_period") in (option.get("meal_periods") or []) else 0)
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
            if quantity == 1:
                lines.append(f"{option['name']}售价{self._format_number(subtotal)}元")
            else:
                lines.append(f"{count}位{labels[kind]}需要购买{quantity}张{option['name']}，共{self._format_number(subtotal)}元")
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
            return {"reply": reply + "。", "source": "当前商品不同身份真实票价",
                    "decision": "allow", "kind": "audience_price"}
        if lines and missing:
            names = "、".join(missing_labels[item] for item in missing)
            return {
                "reply": "，".join(lines) + "。" + (
                    f"您好，本店目前没有“{names}”这一有货规格，"
                    "暂时无法通过当前商品购买，您可以到店咨询。"
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
        if day and not cls._day_types(name):
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
            "purchase_quantity", "purchase_unit", "amount_kind",
        )
        has_direct_slot = any(direct_slots.get(key) not in (None, "") for key in slot_keys)
        referential = bool(re.search(r"(?:呢|那|这个|这种|这款)[？?。！!]*$", text))
        context_followup = bool(previous and (has_direct_slot or pending_filled) and len(normalize_text(text)) <= 16)
        if not (
            price_intent or availability_intent or pending_filled
            or bool(direct_slots.get("purchase_quantity"))
            or ((referential or context_followup) and previous)
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
            ((referential or context_followup) and not (price_intent or availability_intent))
            or only_new_amount or pending_filled
        )
        slots = {key: previous.get(key) for key in slot_keys} if inherited else {}
        slots.update({key: value for key, value in direct_slots.items() if value not in (None, "")})
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
        options = [option for option in self.extract_sale_options(product) if option.get("sale_price")]
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
            "price" if price_intent else "availability" if availability_intent
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

        # Prefer an exact people-count package. If none exists, a clearly
        # identified unrestricted single-person option can safely be multiplied
        # by the requested party size. Identity fares and explicit one-item
        # purchase limits are deliberately excluded.
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
            if slots.get("day_type"):
                specific = [
                    option for option in time_compatible
                    if slots["day_type"] in (option.get("day_types") or [])
                    or (holiday_covers_weekend and "holiday" in (option.get("day_types") or []))
                ]
                if specific:
                    time_compatible = specific
            if slots.get("meal_period"):
                specific = [
                    option for option in time_compatible
                    if slots["meal_period"] in (option.get("meal_periods") or [])
                ]
                if specific:
                    time_compatible = specific
            single_options = []
            for option in time_compatible:
                evidence = " ".join(str(option.get(key) or "") for key in ("name", "applicable_time"))
                if (
                    option.get("option_type") == "package"
                    and (option.get("people_counts") or []) == [1]
                    and not (option.get("audience_types") or [])
                    and not re.search(r"(?:每单|每人|限购|仅限购买|最多购买)\s*1\s*(?:份|张|套|个)", evidence)
                ):
                    single_options.append(option)
            if len(single_options) == 1:
                option = single_options[0]
                total = Decimal(str(option.get("sale_price"))) * int(people_count)
                name = str(option.get("name") or "单人商品").strip()
                return {
                    "reply": (
                        f"{people_count}人需要购买{people_count}份{name}，"
                        f"单价{self._format_number(option.get('sale_price'))}元，"
                        f"共{self._format_number(total)}元。"
                    ),
                    "source": "当前日期可用的单人商品规格与人数换算",
                    "decision": "allow", "kind": "price",
                    "query_context_update": context_update,
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
            if voucher_matches and candidates is voucher_matches:
                faces = sorted({
                    self._format_number(option.get("face_value")) for option in candidates
                    if option.get("face_value")
                }, key=lambda value: Decimal(value))
                if target_amount:
                    exact = [
                        option for option in candidates
                        if self._format_number(option.get("face_value")) == self._format_number(target_amount)
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
                option = min(candidates, key=lambda item: Decimal(str(item.get("sale_price"))))
                price = Decimal(str(option.get("sale_price"))) * quantity
                face = Decimal(str(option.get("face_value"))) * quantity
                contents = self._purchase_contents_label(option, quantity)
                reply = (
                    f"购买{contents}共{self._format_number(price)}元，"
                    f"可抵扣{self._format_number(face)}元。"
                )
                explicit_limit = str(option.get("max_stack") or "").strip()
                if explicit_limit and quantity > int(Decimal(explicit_limit)):
                    reply += f"当前同面额代金券每次最多使用{int(Decimal(explicit_limit))}张，不能一次全部使用。"
                return {
                    "reply": reply, "source": "当前商品真实代金券与购买数量",
                    "decision": "allow", "kind": "price",
                    "query_context_update": context_update,
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
                if self._format_number(option.get("face_value")) == self._format_number(target_amount)
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
                named_subject = re.sub(
                    r"^(?:请问)?(?:有没有|有无)\s*|\s*(?:有吗|有没有|有么|有嘛)[？?]?$",
                    "", text,
                ).strip(" ，,。.!！?？~～")
                reply = (
                    f"当前商品没有“{named_subject or subject}”这一规格。"
                    f"您好，本店目前没有“{named_subject or subject}”这一有货规格，"
                    "暂时无法通过当前商品购买，您可以到店咨询。"
                )
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

    @classmethod
    def coupon_usage_instructions(cls, product: Dict) -> str:
        custom = str(product.get("coupon_instructions") or "").strip()
        if custom:
            return custom
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
                if self._format_number(option.get("face_value")) in requested
            ]
            # Keep a targeted raw-text fallback for compact buyer questions such
            # as “200多少钱”. It also covers a denomination composed of multiple
            # smaller coupons (200面额发两张100券).
            if not matched:
                for value in requested:
                    matched.extend(
                        option for option in self._raw_product_options(
                            product.get("raw_text") or "", product.get("title") or ""
                        )
                        if self._format_number(option.get("face_value") or "") == value
                    )
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
        match = re.search(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?\s*"
            r"(?:(?:拍下|下单|购买)(?:后)?(?:就|可|可以)?\s*)?(?:直接\s*)?"
            r"(?:可?抵(?:用|扣)?|代)\s*"
            r"(\d+(?:\.\d+)?)\s*(?:元|块)?",
            str(message or ""),
        )
        if not match:
            lookup = re.search(
                r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:的券)?\s*"
                r"(?:是多少抵|能抵多少|抵多少|代多少)",
                str(message or ""),
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
                if self._format_number(option.get("face_value")) == requested
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
                face = self._format_number(by_price.get("face_value"))
                return day_prefix + f"当前售价{requested}元对应{face}元代金券，共可抵扣{face}元。"
            faces = sorted({
                self._format_number(option.get("face_value")) for option in options
                if option.get("face_value")
            }, key=Decimal)
            suffix = f"当前已确认面额为{'、'.join(value + '元' for value in faces)}。" if faces else ""
            return day_prefix + f"当前商品没有{requested}元这一已确认的代金券规格。{suffix}"
        paid = self._format_number(match.group(1))
        face = self._format_number(match.group(2))
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
            and self._format_number(option.get("face_value")) == face
        ), None)
        if exact:
            contents = self._purchase_contents_label(exact)
            return day_prefix + (
                f"是的，售价{paid}元，购买后发放{contents}，"
                f"共可抵扣{face}元。"
            )

        # A listed bundle may be represented in the active manual facts as one
        # atomic coupon plus an explicit stack cap (for example 54元×2 ->
        # 200元 face value). Derive only exact arithmetic within that cap.
        for option in options:
            try:
                unit_paid = Decimal(str(option.get("sale_price") or "0"))
                unit_face = Decimal(str(option.get("face_value") or "0"))
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
            return day_prefix + (
                f"是的，售价{paid}元，购买后发放{contents}，"
                f"共可抵扣{face}元。"
            )

        same_face = next((
            option for option in options
            if self._format_number(option.get("face_value")) == face
        ), None)
        if same_face:
            actual_price = self._format_number(same_face.get("sale_price"))
            contents = self._purchase_contents_label(same_face)
            return day_prefix + (
                f"不是，{face}元代金券当前售价{actual_price}元，"
                f"购买后发放{contents}。"
            )

        same_price = next((
            option for option in options
            if self._format_number(option.get("sale_price")) == paid
        ), None)
        if same_price:
            actual_face = self._format_number(same_price.get("face_value"))
            contents = self._purchase_contents_label(same_price)
            return day_prefix + (
                f"当前售价{paid}元对应{actual_face}元代金券，"
                f"购买后发放{contents}。"
            )
        return ""

    def discount_reply(self, product: Dict, message: str) -> str:
        if not re.search(r"多少折|几折|折扣(?:多少|是几|呢|吗)?", str(message or "")):
            return ""
        options = self.extract_product_options(product)
        lines = []
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
            lines.append(
                f"{self._format_number(face)}元代金券售价{self._format_number(price)}元，"
                f"约{self._format_number(discount)}折"
            )
        return "；".join(lines) + "。" if lines else ""

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
        if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:元)?(?:代金券|券)?", subject):
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

    def coupon_catalog_reply(self, product: Dict) -> str:
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
        return "当前可选代金券如下：\n" + "\n".join(
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
            return self.coupon_catalog_reply(product)

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
    def _purchase_contents_label(cls, option: Dict, quantity: int = 1) -> str:
        """Render the coupons actually delivered, not an internal option name."""
        composition = str(option.get("composition") or "")
        pairs = re.findall(r"(\d+(?:\.\d+)?)元券(\d+)张", composition)
        if pairs:
            face = cls._format_number(option.get("face_value") or "")
            if (
                quantity == 1
                and len(pairs) == 1
                and cls._format_number(pairs[0][0]) == face
                and int(pairs[0][1]) == 1
            ):
                return f"{face}元代金券"
            parts = [
                f"{int(count) * quantity}张{cls._format_number(amount)}元代金券"
                for amount, count in pairs
            ]
            return "、".join(parts)
        face = cls._format_number(option.get("face_value") or "")
        if not face:
            return "当前商品"
        return f"{face}元代金券" if quantity == 1 else f"{quantity}张{face}元代金券"

    @classmethod
    def _option_total_value(cls, option: Dict) -> str:
        pairs = re.findall(
            r"(\d+(?:\.\d+)?)元券(\d+)张", str(option.get("composition") or "")
        )
        if pairs:
            total = sum(Decimal(amount) * int(count) for amount, count in pairs)
            return cls._format_number(total)
        return cls._format_number(option.get("face_value") or "")

    @classmethod
    def _option_delivers_single_face(cls, option: Dict, amount: str) -> bool:
        pairs = re.findall(
            r"(\d+(?:\.\d+)?)元券(\d+)张", str(option.get("composition") or "")
        )
        if pairs:
            return (
                len(pairs) == 1 and int(pairs[0][1]) == 1
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
        delivered = sum(
            int(count) for _, count in re.findall(
                r"(\d+(?:\.\d+)?)元券(\d+)张", str(option.get("composition") or "")
            )
        )
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
            limit = re.search(
                r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张",
                knowledge,
            )
            # Same-face stacking is the only safe default when no mixed-face
            # rule exists. Two identical coupons covers the explicit business
            # case “only 100 available, buyer asks for 200”; anything beyond
            # this conservative fallback is called out as unconfirmed below.
            maximum = int(limit.group(1)) if limit else 2
        delivered = sum(
            int(count)
            for _, count in re.findall(
                r"(\d+(?:\.\d+)?)元券(\d+)张", str(option.get("composition") or "")
            )
        ) or 1
        return max(1, maximum // delivered)

    def consumption_plan_reply(
        self, product: Dict, target_value: object, missing_denomination: bool = False,
        available_options: Optional[List[Dict]] = None,
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
            try:
                face = Decimal(str(option.get("face_value") or "0"))
                price = Decimal(str(option.get("sale_price") or "0"))
            except InvalidOperation:
                continue
            if face == target and price > 0:
                exact.append((price, option))
        if exact:
            price, option = min(exact, key=lambda value: value[0])
            face = Decimal(str(option.get("face_value") or "0"))
            contents = self._purchase_contents_label(option, 1)
            return (
                f"{self._format_number(target)}元消费的话，可以购买"
                f"{contents}，售价{self._format_number(price)}元，"
                f"可抵扣{self._format_number(face)}元。"
            )

        candidates = []
        for option in options:
            try:
                face = Decimal(str(option.get("face_value") or "0"))
                price = Decimal(str(option.get("sale_price") or "0"))
            except (InvalidOperation, ValueError):
                continue
            maximum = self._option_stack_limit(product, option)
            if face <= 0 or price <= 0 or maximum <= 0:
                continue
            quantity = min(int(target // face), maximum)
            if quantity > 0:
                delivered_count = sum(
                    int(count)
                    for _, count in re.findall(
                        r"(\d+(?:\.\d+)?)元券(\d+)张", str(option.get("composition") or "")
                    )
                ) or 1
                candidates.append((
                    face * quantity, price * quantity, delivered_count * quantity,
                    quantity, face, price, maximum, option,
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
        covered, total_price, coupon_count, quantity, face, price, maximum, option = min(
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
        combine_intent = bool(re.search(
            r"一起用|同时用|一块用|一并使用|搭配使用|同时核销|一起抵扣|"
            r"叠加|叠券|叠优惠|混用|合并用|组合用|累计使用|还能用|还能叠|"
            r"能叠|可叠|再用|再叠|再减",
            text,
        ))
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
        package_words = r"套餐|团购|单品|菜品|餐品|店内套餐"
        if re.search(package_words, text) and (options or re.search(r"代金券|券", text)):
            knowledge = str(product.get("raw_text") or "")
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
            r"每次(?:最多)?用(?:几|多少)张|一桌(?:最多)?用(?:几|多少)张",
            text,
        ):
            return ""

        values = list(dict.fromkeys(re.findall(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:元)?(?:代金券|券)", text
        )))
        options = self.extract_product_options(product)
        selected = None
        for value in values:
            selected = next((
                option for option in options
                if self._format_number(option.get("face_value")) == self._format_number(value)
            ), None)
            if selected:
                break
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
        amount_cap = re.search(
            r"(?:一桌|每桌|单桌|每次)[^。；\n]{0,12}?"
            r"(?:最多)?(?:可)?(?:代|抵(?:用|扣)?|使用)?\s*(\d+(?:\.\d+)?)\s*元",
            knowledge,
        )
        maximum = 0
        total_cap = Decimal("0")
        if amount_cap and unit_value > 0:
            total_cap = Decimal(amount_cap.group(1))
            maximum = int(total_cap // unit_value)
        if maximum <= 0:
            try:
                maximum = int(Decimal(str(selected.get("max_stack") or "0")))
            except (InvalidOperation, ValueError):
                maximum = 0
        if maximum <= 0:
            count_cap = re.search(
                r"(?:最多(?:使用|叠加)?|上限(?:为)?|可叠加|限用|限)\s*(\d+)\s*张",
                knowledge,
            )
            maximum = int(count_cap.group(1)) if count_cap else 0
        if maximum <= 0:
            return ""
        if total_cap <= 0:
            total_cap = unit_value * maximum

        delivered = self._purchase_contents_label(selected)
        mixed = (
            "支持不同面额代金券一起使用"
            if re.search(
                r"(?:支持|可以|可)[^。；\n]{0,10}不同面额[^。；\n]{0,8}叠加|"
                r"不同面额[^。；\n]{0,10}(?:支持|可以|可)[^。；\n]{0,8}叠加|混合叠加",
                knowledge,
            )
            else "不同面额的券不能混用"
        )
        return (
            f"该商品购买后发放{delivered}，一桌一次最多使用{maximum}张，"
            f"共可抵扣{self._format_number(total_cap)}元；{mixed}。"
        )

    def stacking_reply(self, product: Dict, message: str) -> str:
        if not re.search(r"一起用|同时用|叠加|混用|合并用|一次(?:可以|能)?用|可以用几张|能用几张", message):
            return ""
        if not re.search(r"代金券|优惠券|券|面额|\d+(?:\.\d+)?|[一二两三四五六七八九十]\s*张|几张|多少张|叠加|混用", message):
            return ""
        quantity_reply = self.coupon_quantity_limit_reply(product, message)
        if quantity_reply:
            return quantity_reply
        knowledge = str(product.get("raw_text") or "")
        values = list(dict.fromkeys(re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*元", message)))
        if not values:
            values = list(dict.fromkeys(re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\s*张)", message)))
        different = len(values) >= 2 and len(set(values)) >= 2
        sku_limits = self._sku_stack_limits(self.extract_product_options(product))
        denies_mixed = bool(re.search(
            r"不同面额[^。；\n]{0,12}(?:不可|不能|不支持|禁止)[^。；\n]{0,8}(?:叠加|混用|一起)|"
            r"(?:不可|不能|不支持|禁止)[^。；\n]{0,8}不同面额",
            knowledge,
        ))
        allows_mixed = not denies_mixed and bool(re.search(
            r"(?:支持|可以|可)[^。；\n]{0,10}不同面额[^。；\n]{0,8}叠加|不同面额[^。；\n]{0,10}(?:可以|可|支持)[^。；\n]{0,8}叠加|混合叠加",
            knowledge,
        ))
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
            return f"支持不同面额代金券一起叠加使用{max_text}。"
        denomination = values[0] if values else ""
        maximum = sku_limits.get(denomination)
        if maximum or max_match:
            maximum = maximum or int(max_match.group(1))
            quantity_match = re.search(r"([一二两三四五六七八九十\d]+)\s*张", message)
            if denomination and quantity_match:
                chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                quantity = (int(quantity_match.group(1)) if quantity_match.group(1).isdigit()
                            else chinese.get(quantity_match.group(1), 0))
                if quantity > maximum:
                    return f"同面额代金券每次最多使用{maximum}张，超出的金额请在门店另行支付。"
                total = Decimal(denomination) * quantity
                return (
                    f"可以，{quantity}张{self._format_number(denomination)}元代金券"
                    f"合计可抵扣{self._format_number(total)}元；每次最多使用{maximum}张。"
                )
            return f"{denomination + '元代金券' if denomination else '同面额代金券'}可以叠加，每次最多使用{maximum}张；不同面额不能混用。"
        return "当前商品资料暂未明确说明每次可以使用几张，暂时无法准确确认叠加数量。"

    @classmethod
    def _use_time_text(cls, product: Dict) -> str:
        facts = (product.get("structured") or {}).get("facts") or {}
        raw_text = str(product.get("raw_text") or "")
        use_time = cls._first_fact(
            facts, ("使用时间", "可用时间", "营业时间", "使用日期", "time")
        )
        combined = raw_text + "\n" + use_time
        has_unavailable = bool(re.search(
            r"(?:\d{4}年)?\d{1,2}月\d{1,2}日?.{0,24}(?:不可用|不能用|不适用)|"
            r"(?:中秋|国庆|春节|元旦|劳动节|节假日).{0,20}(?:不可用|不能用|不适用)",
            combined,
        ))
        cleaned = use_time.rstrip("。；; ")
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

    def order_payment_notice(self, item_id: str = "") -> str:
        product = self.get_v2_product(item_id) if item_id else None
        if not product:
            return (
                "【发货提醒】\n付款后系统会自动发货，请确认商品信息和使用规则无误后再付款。\n"
                "【退换货政策】\n请确认当天需要使用后再购买；非卡券质量问题退款需扣除5%手续费；"
                "卡券质量问题请申请仅退款并等待人工核实。"
            )
        if not product.get("order_notice_enabled", 1):
            return ""
        brand = self.extract_brand(product)
        options = self.extract_product_options(product)
        product_lines = [self.format_product_option(option, brand) for option in options]
        if not product_lines:
            product_lines = [brand + "电子券" if brand else (product.get("title") or "当前商品")]
        rules = []
        use_time = self._use_time_text(product)
        if use_time:
            rules.append(use_time.rstrip("。") + "。")
        use_rule = self._use_rule_text(product)
        if use_rule:
            rules.append(use_rule.rstrip("。") + "。")
        policy = self.effective_aftersale_policy(item_id)
        if not policy:
            policy = (
                "请确认当天到店使用后再付款，卡券须当天购买、当天使用。"
                "未及时使用导致过期不退不补；非卡券质量问题退款需扣除5%手续费；"
                "卡券质量问题请申请仅退款，转人工在72小时内核实处理。"
            )
        policy = policy.replace("【购买与发货】", "")
        policy = policy.replace("【非卡券质量问题退款】", "非卡券质量问题退款：")
        policy = policy.replace("【卡券质量问题退款】", "卡券质量问题退款：")
        policy = policy.replace("【过期与收货】", "过期与收货：")
        policy = re.sub(r"【([^】]+)】\s*", r"\1：", policy)
        policy = re.sub(r"付款后(?:系统)?(?:会)?自动发货[，。；;]?", "", policy)
        policy = re.sub(r"\s*\n\s*", "；", policy).strip("； ")
        return "\n".join([
            "【商品信息】",
            *product_lines,
            "【使用规则】",
            *(rules or ["具体使用规则以当前商品知识库为准。"]),
            "【发货提醒】",
            "付款后系统会自动发货，请确认商品信息和使用规则无误后再付款。",
            "【退换货政策】",
            policy,
        ])

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
        # Never inherit an AI-expanded mixed-denomination claim unless the
        # authoritative user/page text says so explicitly.
        return "仅支持同面额代金券叠加。" if options else ""

    def build_knowledge_summary(self, product: Dict) -> str:
        facts = (product.get("structured") or {}).get("facts") or {}
        options = self.extract_product_options(product)
        lines = [self.format_product_option(option) for option in options]
        sections = []
        if lines:
            sections.append("【商品规格】\n" + "\n".join(lines))
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
        use_rule = self._first_fact(facts, ("使用规则", "使用条件", "核销规则", "限制", "usage", "conditions"))
        if use_rule:
            sections.append("【使用规则】\n" + use_rule.rstrip("。；; ") + "。")
        return "\n\n".join(sections)

    def build_first_reply_text(self, product: Dict) -> str:
        """Build a short editable reply only from authoritative product knowledge."""
        facts = (product.get("structured") or {}).get("facts") or {}
        raw_text = str(product.get("raw_text") or "")
        title = re.sub(r"【[^】]*】|\[[^\]]*\]", "", str(product.get("title") or "")).strip()
        title = re.sub(r"^(?:自动发货|全国|现货|秒发)[\s·|丨+-]*", "", title, flags=re.I).strip()

        brand = self.extract_brand(product)
        coupon_type = str(product.get("coupon_type") or "").strip()
        coupon_phrase = {
            "meituan": "美团电子券",
            "douyin": "抖音电子券",
            "merchant_miniapp": "商家小程序电子券",
            "electronic_code": "电子券码",
            "other": self.coupon_type_display(product) or "电子券码",
        }.get(coupon_type, "电子券码")
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
        sections.append("【发券方式】\n" + self.coupon_usage_instructions(product).rstrip("。；; ") + "。")
        sections.append("【提醒】\n请当天购买、当天使用，过期不退不补。")
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
            return [classify_store_header(value) for value in values]

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
            raise FileNotFoundError("未找到门店表格")
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
        options = self.extract_sale_options(product)
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
            rule = rules.get(key) or {}
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
                "sale_price": str(option.get("sale_price") or ""),
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
                "list_ids": own_ids, "effective_list_ids": effective_ids,
                "own_store_count": self._store_count_for_lists(own_ids),
                "effective_store_count": self._store_count_for_lists(effective_ids),
                "configuration_ready": mode != "custom" or bool(own_ids), "inherited": mode != "custom",
            })
        return output

    def set_sku_store_rule(self, item_id: str, sku_key: str, sku_name: str,
                           mode: str, list_ids: Optional[List[int]] = None) -> Dict:
        item_id, sku_key = str(item_id or "").strip(), str(sku_key or "").strip()
        mode = str(mode or "inherit").strip()
        if not item_id or not sku_key:
            raise ValueError("请选择商品规格")
        if mode not in {"inherit", "custom"}:
            raise ValueError("门店适用方式无效")
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
                """INSERT INTO product_sku_store_rules(item_id,sku_key,sku_name,mode,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(item_id,sku_key) DO UPDATE SET
                   sku_name=excluded.sku_name,mode=excluded.mode,status='active',updated_at=excluded.updated_at""",
                (item_id, sku_key, sku_name or valid_keys[sku_key]["sku_name"], mode, "active", now, now),
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
        filters_present = bool(
            requested_amount or slots.get("people_counts") or slots.get("day_types")
            or slots.get("meal_periods") or slots.get("audience_types")
        )
        if not filters_present:
            return []
        matched = []
        for sku in skus:
            option = by_key.get(sku["sku_key"], {})
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
                supported = set(sku.get(key) or [])
                if requested and supported and not requested & supported:
                    break
            else:
                matched.append(sku)
        return matched

    @staticmethod
    def _sku_public_label(sku: Dict) -> str:
        name = str(sku.get("sku_name") or "该规格").strip()
        price = str(sku.get("sale_price") or "").strip()
        return f"{name}（售价{price}元）" if price else name

    def reverse_store_sku_matches(self, item_id: str, query: str,
                                  product: Optional[Dict] = None) -> List[Dict]:
        """Return only configured current SKUs that can actually use the queried store."""
        product = product or self.get_v2_product(item_id) or {}
        output = []
        for sku in self.list_product_skus(item_id, product):
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
        for index, row in enumerate(matrix, start=1):
            store_name = cls._store_display_name(row.get("store") or {})
            supported = list(row.get("supported_skus") or [])
            supported_keys = {str(sku.get("sku_key") or "") for sku in supported}
            if selected_skus:
                usable = [sku for sku in selected_skus if str(sku.get("sku_key") or "") in supported_keys]
                unusable = [sku for sku in selected_skus if str(sku.get("sku_key") or "") not in supported_keys]
                direct = []
                if usable:
                    direct.append(cls._compact_sku_names(usable) + "可以使用")
                if unusable:
                    direct.append(cls._compact_sku_names(unusable) + "不适用")
                if unusable and supported:
                    direct.append("可用规格为" + cls._compact_sku_names(supported))
                line = f"【{store_name}】：{'；'.join(direct) or '暂无已确认的可用规格'}。"
            else:
                labels = "；".join(cls._sku_public_label(sku) for sku in supported)
                line = f"{index}. 【{store_name}】：{labels or '暂无已确认的可用规格'}。"
            unknown = [
                sku for sku in row.get("unknown_skus") or []
                if not selected_keys or str(sku.get("sku_key") or "") in selected_keys
            ]
            if unknown:
                line += " " + cls._compact_sku_names(unknown) + "的门店资料暂未配置。"
            lines.append(line)
        if selected_skus and len(lines) == 1:
            return lines[0]
        heading = (
            f"根据“{query}”查询结果："
            if selected_skus else f"根据“{query}”查询到以下可用门店及规格："
        )
        return "\n\n".join((heading, "\n".join(lines), "请按对应门店支持的规格拍下。"))

    @classmethod
    def _compact_sku_names(cls, skus: List[Dict]) -> str:
        """Render voucher faces compactly without dropping non-voucher SKU names."""
        faces = [
            cls._format_number(sku.get("face_value") or "")
            for sku in skus
        ]
        if skus and all(faces) and all((sku.get("option_type") or "voucher") == "voucher" for sku in skus):
            return "、".join(f"{face}元" for face in faces) + "代金券"
        return "、".join(str(sku.get("sku_name") or "当前规格") for sku in skus)

    def _store_search_clarification(
        self, query: str, result: Dict, selected_skus: Optional[List[Dict]] = None,
    ) -> Optional[Dict]:
        """Turn uncertain store matches into a confirmation, never an availability claim."""
        status = str(result.get("status") or "")
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
            "reply": prefix + self.format_store_reply(match, str(message or "")),
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
        reply_parts = []
        if not selected_skus and 1 < len(matrix) <= 3:
            sections = reply.split("\n\n")
            reply_parts = [sections[0], *sections[1].splitlines(), *sections[2:]]
        return {
            "reply": reply,
            "reply_parts": reply_parts,
            "source": "多规格商品级门店与规格对应关系",
            "decision": "allow", "kind": "stores_sku_recommendation",
            "store_matches": matches, "store_query": resolved_query,
            "store_status": "available", "store_sku_matrix": matrix,
            "query_context_update": ({
                "selected_sku_key": selected_skus[0]["sku_key"],
                "selected_sku_name": selected_skus[0].get("sku_name", ""),
            } if len(selected_skus) == 1 else {}),
        }

    def resolve_store_matrix_followup(
        self, item_id: str, product: Dict, message: str,
        store_context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Answer only strict follow-ups to a recent <=3-store SKU matrix."""
        context = store_context if isinstance(store_context, dict) else {}
        matrix = list(context.get("store_sku_matrix") or [])
        if not matrix:
            return None
        text = str(message or "").strip()
        compact = re.sub(r"[\s，,。.!！?？~～]+", "", text)
        if not compact:
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

        amount_followup = re.fullmatch(
            r"(?:那|这个|这张|该)?(\d+(?:\.\d+)?)\s*(?:元)?(?:的|代金券|优惠券|券)?"
            r"(?:呢|可以吗|能用吗|可用吗|行吗|怎么样|咋样)?",
            compact,
        )
        selected_skus = self._explicit_store_skus(item_id, text, product)
        if not selected_skus:
            bare_amount = amount_followup
            if bare_amount:
                amount = self._format_number(bare_amount.group(1))
                selected_skus = [
                    sku for sku in self.list_product_skus(item_id, product)
                    if self._format_number(sku.get("face_value") or "") == amount
                ]

        followup_words = bool(re.search(
            r"这家|这个店|该店|那家|刚才|上面|这些店|这几个|"
            r"第一(?:家|个)|第二(?:家|个)|第三(?:家|个)|第[123](?:家|个)|"
            r"哪些面额|什么面额|哪些规格|什么规格|买哪|可以用吗|能用吗|可用吗|的呢|怎么样|咋样",
            compact,
        ))
        if not followup_words and not selected_skus:
            return None

        # A new explicit location always overrides old context and is handled
        # by the ordinary store resolver below.
        new_query = extract_store_query(
            text, product=product, product_brand=self.extract_brand(product)
        )
        if ordinal is None and not amount_followup and is_meaningful_store_query(new_query) and not re.fullmatch(
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
            "购物中心", "商业广场", "门店", "分店", "旗舰店", "街道",
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
                    if len(street_key) >= 2 and street_key in query_key:
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
        district = last_hit("district")
        chars = list(query_key)
        for start, end in removable:
            for index in range(start, end):
                chars[index] = ""
        remainder = "".join(chars)
        return province, city, district, remainder

    def search_store(self, item_id: str, query: str, limit: Optional[int] = None,
                     sku_key: str = "", list_ids_override: Optional[List[int]] = None) -> Dict:
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
        original_scope = self._extract_store_scope(rows, query_norm)
        original_area = any(original_scope[:3])
        original_term = self._store_fuzzy_key(original_scope[3])
        exact_brand_area_query = original_area and any(
            original_term == self._store_fuzzy_key(row.get("brand"))
            for row in rows if row.get("brand")
        )
        # “宜家武汉/武汉宜家” already contains two reliable entities. Fuzzy
        # branch recovery must not rewrite it to an invented full branch name.
        resolved_query = "" if exact_brand_area_query else self._best_location_query(rows, query)
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

        # A province/city-only question must return every branch in that scope.
        if (province_scope or city_scope or district_scope) and not search_term:
            for item in rows:
                item["score"] = 1.1
                item["match_quality"] = "area"
            matches = rows
        else:
            query_key = self._store_fuzzy_key(search_term or query_norm)
            direct = []
            fuzzy = []
            phonetic = []
            for item in rows:
                store_name = str(item.get("branch") or item.get("brand") or "").strip()
                if self._looks_like_product_title(store_name):
                    continue
                branch_key = self._store_fuzzy_key(item.get("branch"))
                local_branch_key = self._local_branch_key(item, branch_key)
                combined_key = self._store_fuzzy_key("".join(str(item.get(key) or "") for key in (
                    "district", "branch",
                )))
                # Only an explicitly address-shaped query may match the address
                # column. This prevents “300有吗” from hitting a 300号 address.
                address_key = ""
                if re.search(r"(?:路|街|道|巷|号|大厦|中心)", search_term or query_norm):
                    address_key = self._store_fuzzy_key(item.get("address"))
                # Generic labels such as “广场店” can normalize to an empty
                # key.  Empty-string containment is always true in Python and
                # previously let an unrelated “任丘悦都汇店” match that row.
                branch_key = branch_key if len(branch_key) >= 2 else ""
                local_branch_key = local_branch_key if len(local_branch_key) >= 2 else ""
                combined_key = combined_key if len(combined_key) >= 2 else ""
                address_key = address_key if len(address_key) >= 2 else ""

                grounded_keys = [
                    self._store_fuzzy_key(item.get(field))
                    for field in ("brand", "province", "city", "district")
                ]
                unexplained = query_key
                for grounded_key in sorted(
                    (value for value in grounded_keys if len(value) >= 2),
                    key=len, reverse=True,
                ):
                    unexplained = unexplained.replace(grounded_key, "", 1)
                if branch_key:
                    unexplained = unexplained.replace(branch_key, "", 1)
                has_unverified_qualifier = bool(
                    not (province_scope or city_scope or district_scope)
                    and branch_key and branch_key in query_key
                    and len(unexplained) >= 2
                )

                if not query_key:
                    score = 0.0
                    quality = "none"
                elif branch_key and (
                    query_key == branch_key
                    or (local_branch_key and query_key == local_branch_key)
                ):
                    score = 1.1
                    quality = "exact"
                elif (
                    (branch_key and query_key in branch_key)
                    or (branch_key and branch_key in query_key and not has_unverified_qualifier)
                    or (
                        (province_scope or city_scope or district_scope)
                        and local_branch_key
                        and (query_key in local_branch_key or local_branch_key in query_key)
                    )
                ):
                    score = 1.02
                    quality = "contained"
                elif (
                    (combined_key and query_key in combined_key)
                    or (address_key and query_key in address_key)
                ):
                    score = 1.0
                    quality = "contained"
                else:
                    scores = [
                        SequenceMatcher(None, query_key, value).ratio()
                        for value in (branch_key, local_branch_key, combined_key) if value
                    ] or [0.0]
                    if address_key:
                        scores.append(SequenceMatcher(None, query_key, address_key).ratio())
                    score = max(scores)
                    if has_unverified_qualifier:
                        # Keep a plausible tail as a confirmation candidate,
                        # never as a positive availability match.
                        score = max(score, 0.8)
                    if (
                        (province_scope or city_scope or district_scope)
                        and self._within_one_edit(query_key, local_branch_key)
                    ):
                        score = max(score, 0.86)
                    quality = "fuzzy"
                    query_pinyin = self._pinyin_key(query_key)
                    phonetic_values = [branch_key, combined_key]
                    if province_scope or city_scope or district_scope:
                        phonetic_values.append(local_branch_key)
                    pinyin_keys = {
                        self._pinyin_key(value) for value in phonetic_values if value
                    }
                    pinyin_keys.discard("")
                    if len(query_pinyin) >= 6 and pinyin_keys:
                        if any(
                            query_pinyin == key or query_pinyin in key
                            or (
                                (province_scope or city_scope or district_scope)
                                and key in query_pinyin
                            )
                            for key in pinyin_keys
                        ):
                            score = max(score, 0.96)
                            quality = "phonetic"
                item["score"] = round(score, 3)
                item["match_quality"] = quality
                if quality in {"exact", "contained"}:
                    direct.append(item)
                elif quality == "phonetic":
                    phonetic.append(item)
                elif score >= 0.76:
                    fuzzy.append(item)
            matches = direct
            if not matches and phonetic:
                best = max(value["score"] for value in phonetic)
                matches = [value for value in phonetic if value["score"] >= best - 0.02]
            if not matches and fuzzy:
                best = max(value["score"] for value in fuzzy)
                matches = [value for value in fuzzy if value["score"] >= best - 0.05]
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
        scoped_query = "".join(filter(None, (
            district_scope or city_scope or province_scope,
            search_term,
        ))) or query
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
                "resolved_query": query, "specific_query": scoped_query,
                "candidate_count": len(matches),
            }
        if not matches:
            return {
                "status": "unavailable", "matches": [], "province": province_scope,
                "city": city_scope, "district": district_scope, "search_term": search_term,
                "resolved_query": query, "specific_query": scoped_query,
            }
        unique_high_phonetic = bool(
            len(matches) == 1
            and matches[0].get("match_quality") == "phonetic"
            and float(matches[0].get("score") or 0) >= 0.94
        )
        if (
            all(item.get("match_quality") in {"fuzzy", "phonetic"} for item in matches)
            and not unique_high_phonetic
        ):
            return {
                "status": "needs_confirmation", "matches": matches,
                "province": province_scope, "city": city_scope,
                "district": district_scope, "search_term": search_term,
                "sku_key": sku_key, "mode": mode, "resolved_query": query,
                "specific_query": scoped_query,
            }
        return {
            "status": "available", "matches": matches, "province": province_scope,
            "city": city_scope, "district": district_scope, "search_term": search_term,
            "sku_key": sku_key, "mode": mode,
            "resolved_query": query,
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
        query_area = V2Store._area_key(query_value)
        row_areas = {
            V2Store._area_key(match.get("city")) for match in matches if match.get("city")
        } | {
            V2Store._area_key(match.get("province")) for match in matches if match.get("province")
        }
        area_only = query_area in row_areas
        if (
            len(matches) == 1 and not area_only
            and any(word in str(message or "") for word in (
                "能用", "可以用", "可用", "适用", "支持", "能不能", "可不可以",
            ))
            and not any(word in str(message or "") for word in (
                "地址", "位置", "在哪", "怎么走", "电话", "号码", "营业",
            ))
        ):
            display = str(matches[0].get("branch") or matches[0].get("brand") or "该门店").strip()
            if matches[0].get("match_quality") == "phonetic":
                # Preserve the existing explicit confirmation wording for
                # homophone recovery; only exact/contained store replies shrink.
                return (
                    f"可以使用。根据“{query_value}”查询到可用门店：【{display}】。"
                    f"您说的“{query_value}”对应【{display}】，"
                    "该门店属于当前商品适用门店。"
                )
            return (
                f"可以使用。根据“{query_value}”查询到可用门店：【{display}】"
            )
        if (
            len(matches) == 1 and not area_only
            and matches[0].get("match_quality") == "phonetic"
        ):
            display = str(matches[0].get("branch") or matches[0].get("brand") or "该门店").strip()
            return f"根据“{query_value}”的同音匹配，查询到可用门店：【{display}】。"
        if area_only:
            if len(grouped) == 1:
                names = next(iter(grouped.values()))
                unique_names = list(dict.fromkeys(names))
                body = "\n".join(
                    f"{index}. 【{name}】" for index, name in enumerate(unique_names, start=1)
                )
            else:
                body = "\n".join(lines)
            return f"根据“{query_value}”查询到以下可用门店：\n\n{body}\n\n以上均为当前商品的适用门店。"
        displays = []
        for city, names in grouped.items():
            if len(grouped) == 1:
                displays.extend(names)
            else:
                displays.extend(f"{city}{name}" for name in names)
        return f"根据“{query_value}”查询到可用门店：{'、'.join(dict.fromkeys(displays))}。"

    @classmethod
    def format_store_unavailable(cls, query: str, area_only: bool = False) -> str:
        value = str(query or "该关键词").strip(" \t\r\n，,。！？!?~～") or "该关键词"
        area_key = cls._area_key(value)
        known_areas = {
            cls._area_key(item) for item in KNOWN_CITY_NAMES | KNOWN_PROVINCE_NAMES
        }
        if area_only or area_key in known_areas:
            return (
                f"当前适用门店资料中暂未查询到{value}。\n\n"
                "建议您核对城市或完整门店名称，也可以更换其他地区查询。"
            )
        commercial_words = ("万达", "万象城", "万科里", "天街", "银泰", "吾悦", "大悦城", "来福士", "太古里")
        if any(word in value for word in commercial_words) and not value.endswith(("店", "商场", "广场", "购物中心")):
            value += "店"
        return (
            f"当前适用门店资料中暂未查询到{value}。\n\n"
            "建议您核对完整门店名称，或更换其他门店查询。"
        )

    @classmethod
    def is_explicit_store_query(cls, message: str, query: str, result: Optional[Dict] = None) -> bool:
        """Recognize places and shopping-centre names before generic “有吗” rules."""
        raw_query = str(query or "").strip(" \t\r\n，,。！？!?~～")
        message_key = normalize_text(message)
        query_key = normalize_text(query)
        compact_message = re.sub(r"[\s，,。.!！?？~～]+", "", str(message or "")).lower()
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
        if re.fullmatch(r"\d+(?:\.\d+)?(?:元)?", query_key):
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
        known_areas = {
            cls._area_key(value) for value in KNOWN_CITY_NAMES | KNOWN_PROVINCE_NAMES
        }
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
        if not (redemption_now or instant_after_buy):
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
                if requested_audiences.intersection(item.get("audience_types") or [])
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
                "reply": (
                    "当前商品资料暂未明确具体使用时段，暂时无法准确确认现在是否可用。"
                    "请告诉我想查询工作日、周末、午市还是晚市，我再按商品资料为您核对。"
                ),
                "source": "缺少可核验的当前可用SKU",
                "decision": "allow", "kind": "time",
            }

        audience_groups = {
            audience for item in current_matches for audience in (item.get("audience_types") or [])
        }
        if len(audience_groups) > 1 and not self._audience_types(text):
            return {
                "reply": "当前有多个票种，请告诉我是成人、儿童、学生、老人还是女士使用，我再按有货SKU确认。",
                "source": "当前时段存在多个身份票种",
                "decision": "allow", "kind": "time_clarify",
            }

        names = "、".join(dict.fromkeys(str(item.get("name") or "当前规格") for item in current_matches))
        return {
            "reply": (
                f"{issuance}今天有适用的有货规格：{names}。"
                f"当前资料未配置精确营业时段，是否能在此刻核销还需以适用门店营业时间和商品不可用日期为准。"
            ),
            "source": "当前商品现有有货SKU、日期条件与发券规则",
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
        status = re.sub(r"\s+", "", str(context.get("status") or ""))
        if any(word in status for word in ("等待买家付款", "待付款", "未付款")):
            return "unpaid"
        if any(word in status for word in (
            "等待卖家发货", "待发货", "已付款", "已发货", "等待确认收货", "确认收货",
            "交易成功", "退款", "退货", "售后", "纠纷",
        )):
            return "paid"
        if actual_paid_amount not in (None, "") or cls._paid_amount_from_text(message) is not None:
            return "paid"
        text = re.sub(r"\s+", "", str(message or ""))
        if re.search(r"(?:还没|没有|未|尚未)(?:付钱|付款)|待付款", text):
            return "unpaid"
        if re.search(r"已经付款|已付款|付款了|付过款|钱已经付|买了|购买后|收到券|收到码|收到链接", text):
            return "paid"
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
            return {
                "reply": prefix + self.format_store_matches(result["matches"], query, text),
                "source": "门店否定问法按适用门店查询", "decision": "allow", "kind": "stores",
                "store_matches": result["matches"], "store_query": query,
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
            r"每次(?:最多)?用(?:几|多少)张|一桌(?:最多)?用(?:几|多少)张",
            text,
        )
        add_task("stacking", "使用张数", stacking_match)

        restriction_match = re.search(
            r"有什么限制(?:条件)?|有哪些限制(?:条件)?|使用限制|限制条件|使用条件(?:是什么|有哪些|呢|吗)?",
            text,
        )
        add_task("restrictions", "使用限制", restriction_match)

        purchase_match = re.search(
            r"直接(?:拍|买)|(?:可以|能|可不可以|能不能)(?:直接)?(?:拍|买|购买)",
            text,
        )
        purchase_reply = self.direct_coupon_purchase_reply(product, text) if purchase_match else ""
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
            r"门店|店铺|商场|商圈|购物中心|广场|万达|万象城|万象汇|"
            r"壹方城|壹方天地|万科里|天街|银泰|吾悦|大悦城|来福士|"
            r"太古里|印象城|奥特莱斯|IFS|MALL",
            text,
            re.I,
        )
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
            raw_probe = probe_store(text) if is_meaningful_store_query(text) else None
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
                sku_question_match = re.search(
                    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:元)?"
                    r"(?!\s*(?:年|月|日|号|点|个|人|位|张|桌|份))"
                    r"(?=[^。！？]{0,12}(?:可以|能|可)(?:使用|用))",
                    text,
                )
            sku_is_store_applicability = bool(
                multi_store_scope.get("stores_differ")
                and not re.search(r"有(?:没有|吗|么)|卖不卖|什么规格|哪些规格", text)
            )
            if (
                sku_question_match and not value_match and not purchase_reply
                and not sku_is_store_applicability
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
                r"(?:\d{4}[-/.年])?\d{1,2}[-/.月]\d{1,2}(?:日|号)?|"
                r"今天|明天|中秋|国庆|春节|元旦|劳动节",
                text,
            )
            if date_child and date_match:
                add_task("date", "使用日期", date_match, date_child)

            condition_match = re.search(
                r"周末|工作日|平日|节假日|早餐|中午|午餐|晚餐|晚市|"
                r"\d+\s*(?:人|位)|[一二两三四五六七八九十]+\s*(?:个)?(?:人|位)",
                text,
            )
            conditional_child = (
                self.conditional_sale_reply(product, text, {}) if condition_match else None
            )
            if conditional_child:
                add_task("conditions", "使用条件", condition_match, conditional_child)
            elif not date_child:
                day_child = self.day_availability_reply(product, text)
                if day_child and condition_match:
                    add_task("day", "使用日期", condition_match, day_child)

            price_match = re.search(
                r"多少钱|多钱|什么价格|价格多少|价钱|售价|什么价|啥价|怎么卖|几元",
                text,
            )
            if price_match and not conditional_child:
                price_reply = self.price_reply(product, text)
                if price_reply:
                    add_task("price", "商品价格", price_match, price_reply)

        if len(tasks) < 2:
            return None

        replies = []
        child_results = []
        for _, kind, label, payload in sorted(tasks, key=lambda value: value[0]):
            child = None
            if kind == "usage":
                reply = self.coupon_usage_instructions(product)
            elif kind == "voucher_value":
                reply = self.voucher_value_confirmation_reply(product, text) or (
                    "当前商品资料中暂未找到能确认的售价与面额对应关系。"
                )
            elif kind == "stacking":
                reply = self.stacking_reply(product, text)
            elif kind == "restrictions":
                reply = self.usage_restrictions_reply(product)
            elif kind == "purchase":
                reply = str(payload or "")
            elif kind in {"date", "day", "conditions"}:
                child = dict(payload or {})
                reply = str(child.get("reply") or "").strip()
            elif kind == "price":
                reply = str(payload or "")
            elif kind == "sku":
                reply = str(payload or "")
            else:
                store_payload = dict(payload or {})
                store_query_value = str(store_payload.get("query") or "").strip()
                sku_text = str(store_payload.get("sku_text") or "").strip()
                child = self.resolve_deterministic(
                    item_id, f"{sku_text}{store_query_value}可以用吗", actual_paid_amount,
                    store_context, order_context, _allow_multi=False,
                )
                reply = str((child or {}).get("reply") or "").strip()
                if not reply:
                    reply = f"暂时无法确认{store_query_value}是否属于当前商品的适用门店。"
            if child:
                child_results.append(child)
            replies.append((label, reply.rstrip(" \t\r\n")))

        result = {
            "reply": "\n\n".join(
                f"{index}. {label}：{reply}"
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
                kind for _, kind, _, _ in sorted(tasks, key=lambda value: value[0])
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
                return {
                    "reply": "抱歉，暂时不支持语音图片识别，请发文字交流。",
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
            if previous_payment_state in {"paid", "unpaid"}:
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

        if delivery_mismatch:
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

        # 代买单只自动处理首次回复和流程。金额、报价以及其他问题均静默转人工。
        if (
            str(product.get("coupon_type") or "").strip() == "purchase_order"
            and not aftersale_candidate
        ):
            purchase_price_words = (
                "多少钱", "价格", "报价", "售价", "价钱", "几元", "几块",
                "怎么卖", "便宜", "优惠", "折扣", "最低", "少点", "小刀",
                "怎么凑", "如何凑", "多少代", "要付多少", "付多少钱",
            )
            explicit_amount = bool(
                re.search(r"\d+(?:\.\d+)?\s*(?:元|块|折|%)", message)
                or re.search(
                    r"\d+(?:\.\d+)?\s*(?:元)?\s*(?:怎么拍|如何拍|怎么买|如何买|怎么凑|如何凑)",
                    message,
                )
            )
            if explicit_amount or any(word in message for word in purchase_price_words):
                return {
                    "reply": "",
                    "source": "代买单报价由人工回复",
                    "decision": "silent_review",
                    "kind": "purchase_order_price",
                }
            if any(word in message for word in (
                "怎么领取", "如何领取", "怎么领", "怎么核销", "如何核销",
                "怎么使用", "如何使用", "怎么用", "发什么", "如何发",
                "怎么拍", "如何拍", "怎么买", "如何买", "怎么付款", "如何付款",
                "付款流程", "购买流程", "怎么操作", "桌码发哪里", "发桌码",
            )):
                return {
                    "reply": self.purchase_order_public_instructions(product),
                    "source": "代买单付款、领取与核销说明",
                    "decision": "allow",
                    "kind": "purchase_order_usage",
                }
            return {
                "reply": "",
                "source": "代买单仅自动处理首次回复和付款核销流程",
                "decision": "silent",
                "kind": "purchase_order_other",
            }

        if _allow_multi and not aftersale_candidate:
            multi = self.resolve_multi_question(
                item_id, product, message, actual_paid_amount,
                store_context, order_context,
            )
            if multi:
                return multi

        # “202怎么拍/298怎么买”表示按实际消费额推荐，只使用真实SKU。
        amount_plan = re.fullmatch(
            r"\s*(?:(?:吃(?:了)?|消费(?:了)?|用了|一共|总共|结账|买单|账单)\s*)?"
            r"(\d+(?:\.\d+)?)\s*(?:元|块)?[\s，,。；;：:~-]*"
            r"(?:怎么拍|如何拍|咋拍|怎么买|如何买|咋买|怎么凑|如何凑|咋凑|怎么办|咋弄)\s*[？?]?\s*",
            message,
        )
        if amount_plan:
            # Only voucher products support amount-to-denomination arithmetic.
            # Package prices must continue to the semantic package resolver,
            # otherwise “消费200元怎么买” can invent a coupon-like plan.
            if self.extract_product_options(product):
                reply = self.consumption_plan_reply(product, amount_plan.group(1))
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

        candidate_followup = self.resolve_store_candidate_followup(
            item_id, product, message, store_context,
        )
        if candidate_followup:
            return candidate_followup

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

        # A strict reference to the previous <=3-store result must be handled
        # before product-wide catalog questions such as “第一家有哪些面额”.
        store_followup = self.resolve_store_matrix_followup(
            item_id, product, message, store_context,
        )
        if store_followup:
            return store_followup

        if any(word in message for word in (
            "多少代多少", "有哪些代金券", "有什么代金券", "代金券有哪些", "代金券有什么", "有哪些面额",
            "有什么面额", "卖哪些券", "有哪些券型", "有什么券型",
        )) or compact in {"代金券", "有代金券吗", "有没有代金券", "是代金券吗", "代金券有吗"}:
            return {
                "reply": self.coupon_catalog_reply(product),
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

        discount = self.discount_reply(product, message)
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
        if actual_failure and (refund_request or payment_state == "paid"):
            reply = policy_sentences(
                ("质量", "异常", "仅退款", "72", "人工"),
                "如遇券码无效或无法核销，请保留页面提示或门店反馈，并在订单中申请“仅退款”。"
                "提交后需要人工核实，通常会在申请后的72小时内处理。",
            )
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
                return {
                    "reply": self._personal_refund_reply(message, actual_paid_amount),
                    "source": "已确认的非卡券质量退款流程",
                    "decision": "allow",
                    "kind": "refund_process",
                }

            if payment_state == "paid":
                prompt = "请问退款原因是暂时不用了，还是到店后券码无法核销？两种情况的处理方式不同。"
                return {
                    "reply": prompt,
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

        coupon_label = self.coupon_type_display(product)
        if any(word in message for word in ("转赠", "转送", "送给别人", "给别人用")):
            return {
                "reply": "不支持转赠哦，需要购买人本人下单并使用。\n\n付款后系统会自动发送领取信息，使用时打开美团券码，到适用门店向工作人员出示券码核销即可。",
                "source": "卡券转赠与核销规则",
                "decision": "allow",
                "kind": "transfer_usage",
            }
        platform_words = (
            "什么券", "发的什么券", "哪个平台", "什么平台", "美团还是抖音",
            "美团券吗", "抖音券吗", "是美团", "是抖音", "哪种券",
        )
        if any(word in message for word in platform_words):
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
                reply = self.coupon_usage_instructions(product)
                return {
                    "reply": reply,
                    "source": "发码与核销固定回复",
                    "decision": "allow",
                    "kind": "delivery_usage",
                }

        if any(word in message for word in (
            "去店里再买", "到店再买", "店里再买", "现场再买", "现场买吗",
            "去店里买", "到店买吗", "怎么买", "如何购买",
        )):
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

        all_store_question = bool(re.search(
            r"(?:所有|全部|全国|每家|每个|任意).{0,5}(?:门店|店铺|店).{0,5}(?:通用|可用|能用|可以用)|"
            r"(?:门店|店铺|店).{0,5}(?:都|全部|所有).{0,4}(?:通用|可用|能用|可以用)|"
            r"(?:全国|全城)(?:门店|店铺|店)?(?:都|全部)?(?:通用|可用|能用|可以用)|"
            r"^(?:通用吗|都能用吗|都可以用吗|都可用吗)[？?。！!]*$",
            message,
        ))
        if all_store_question:
            if self._multi_sku_store_scope(item_id, product).get("stores_differ"):
                return {
                    "reply": "不同商品规格的适用门店可能不同。请发送要购买的规格名称、面额或人数，以及城市或门店名称，我按对应规格为您准确查询。",
                    "source": "当前商品不同规格绑定了不同门店表",
                    "decision": "allow", "kind": "stores_clarify",
                }
            title = str(product.get("title") or "")
            subject = "鱼酷烤鱼券" if "鱼酷" in title else "当前商品卡券"
            return {
                "reply": f"{subject}需在指定门店使用。\n\n具体可用门店，请发送城市或店面名称进行查询。",
                "source": "当前商品绑定的指定适用门店",
                "decision": "allow",
                "kind": "stores_scope",
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
        pending_mode = str((store_context or {}).get("pending_store_query_mode") or "")
        pending_query = str((store_context or {}).get("pending_store_query") or "").strip()
        if pending_mode == "generic_landmark" and pending_query and is_meaningful_store_query(query):
            query = query + pending_query
        sku_scope = self._multi_sku_store_scope(item_id, product)
        skus = sku_scope.get("skus") or []
        sku_stores_differ = bool(sku_scope.get("stores_differ"))
        matched_skus = self.match_message_skus(item_id, message, product)
        selected_sku = matched_skus[0] if len(matched_skus) == 1 else None
        if not selected_sku and store_context:
            prior_key = str(store_context.get("selected_sku_key") or "")
            selected_sku = next((sku for sku in skus if sku["sku_key"] == prior_key), None)
        selected_key = selected_sku["sku_key"] if selected_sku else ""
        if sku_stores_differ:
            multi_sku_store = self.resolve_multi_sku_store_query(
                item_id, product, message, store_context,
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
                    "reply": prefix + self.format_store_matches(store_result["matches"], query, message),
                    "source": "当前商品绑定门店表",
                    "decision": "allow",
                    "kind": "stores",
                    "store_matches": store_result["matches"],
                    "store_query": query,
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

    def dashboard(self) -> Dict:
        products = self.list_v2_products()
        stores = self.list_store_lists()
        audits = self.count_audits()
        refund_orders = self.list_refund_orders()
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
        "可以用吗", "可以吗", "能用吗", "可用吗", "支持吗", "行吗",
        "可以用", "可以", "能用", "可用", "支持", "适用",
        "适用吗", "能不能用", "可以使用吗", "是否可用", "哪些门店", "哪个门店", "门店",
        "有没有", "没有", "有吗", "查一下", "帮我查", "请问", "附近", "吗", "嘛", "么", "呀", "呢", "吧", "？", "?",
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
    # The deterministic product parser may also know a Chinese merchant brand.
    # Remove that exact, product-grounded literal only; never guess arbitrary
    # Chinese words as brands because they may be part of a real mall/branch.
    product_brand = str(product_brand or "").strip()
    if len(product_brand) >= 2:
        text = re.sub(re.escape(product_brand), " ", text, flags=re.I)
    # Prices, quantities, time/audience conditions and coupon words are not
    # part of a province/city/county/town/street/mall/branch search key.
    text = re.sub(
        r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十单双俩仨]+)\s*"
        r"(?:个)?(?:元|块|人|位|张|份|套)",
        " ", text,
    )
    text = re.sub(
        r"周末|工作日|平日|节假日|法定假日|今天|明天|中午|午餐|晚上|晚餐|"
        r"成人|大人|儿童|小孩|学生|老人|女士|"
        r"代金券|现金券|抵扣券|套餐券|团购券|电子券|美团券|卡券|券",
        " ", text,
    )
    text = re.sub(r"\s+", " ", text).strip()
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
