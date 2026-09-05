import json
import re
from typing import List, Dict, Optional
import os
import sys
from openai import OpenAI
from loguru import logger
from app_store import DEFAULT_POLICIES, find_unauthorized_promises
from privacy_guard import redact_sensitive_text


class XianyuReplyBot:
    def __init__(self):
        # 初始化OpenAI客户端
        self.client = OpenAI(
            api_key=os.getenv("API_KEY"),
            base_url=os.getenv("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )
        self._init_system_prompts()
        self.global_system_prompt = ""
        self._init_agents()
        self.router = IntentRouter(self.agents['classify'])
        self.last_intent = None  # 记录最后一次意图


    def _init_agents(self):
        """初始化各领域Agent"""
        self.agents = {
            'classify':ClassifyAgent(self.client, self.classify_prompt, self._safe_filter, self.global_system_prompt),
            'price': PriceAgent(self.client, self.price_prompt, self._safe_filter, self.global_system_prompt),
            'tech': TechAgent(self.client, self.tech_prompt, self._safe_filter, self.global_system_prompt),
            'default': DefaultAgent(self.client, self.default_prompt, self._safe_filter, self.global_system_prompt),
        }

    def set_global_system_prompt(self, prompt: str):
        """Apply the UI-configured highest-priority prompt to every model call."""
        self.global_system_prompt = str(prompt or "").strip()
        for agent in getattr(self, "agents", {}).values():
            agent.global_system_prompt = self.global_system_prompt

    def _init_system_prompts(self):
        """初始化各Agent专用提示词，优先加载用户自定义文件，否则使用Example默认文件"""
        local_prompt_dir = os.path.join(os.getcwd(), "prompts")
        bundled_root = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        bundled_prompt_dir = os.path.join(bundled_root, "prompts")
        
        def load_prompt_content(name: str) -> str:
            """尝试加载提示词文件"""
            candidates = [
                os.path.join(local_prompt_dir, f"{name}.txt"),
                os.path.join(local_prompt_dir, f"{name}_example.txt"),
                os.path.join(bundled_prompt_dir, f"{name}.txt"),
                os.path.join(bundled_prompt_dir, f"{name}_example.txt"),
            ]
            file_path = next((path for path in candidates if os.path.exists(path)), "")
            if not file_path:
                raise FileNotFoundError(f"未找到{name}提示词或模板")

            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                logger.debug(f"已加载 {name} 提示词，路径: {file_path}, 长度: {len(content)} 字符")
                return content

        try:
            # 加载分类提示词
            self.classify_prompt = load_prompt_content("classify_prompt")
            # 加载价格提示词
            self.price_prompt = load_prompt_content("price_prompt")
            # 加载技术提示词
            self.tech_prompt = load_prompt_content("tech_prompt")
            # 加载默认提示词
            self.default_prompt = load_prompt_content("default_prompt")
            # 语义辅助层只拆分问题和抽取原文槽位，不直接生成业务答案。
            self.semantic_prompt = load_prompt_content("semantic_prompt")
                
            logger.info("成功加载所有提示词")
        except Exception as e:
            logger.error(f"加载提示词时出错: {e}")
            raise

    def _safe_filter(self, text: str) -> str:
        """安全过滤模块"""
        text = str(text or "").strip()
        blocked_phrases = ["微信", "QQ", "支付宝", "银行卡", "线下"]
        if any(p in text for p in blocked_phrases):
            return "请通过闲鱼平台沟通和交易。"
        if find_unauthorized_promises(text):
            return DEFAULT_POLICIES["manual_review_notice"]
        text = re.sub(r"(?i)sku", "商品规格", text)
        text = text.replace("知识库", "商品资料").replace("数据库", "资料")
        return text

    def format_history(self, context: List[Dict]) -> str:
        """只保留最近的有效对话，减少请求体和模型首字等待时间。"""
        max_messages = max(2, int(os.getenv("AI_CONTEXT_MESSAGES", "8")))
        max_chars = max(500, int(os.getenv("AI_CONTEXT_CHARS", "3500")))
        user_assistant_msgs = [
            msg for msg in context if msg.get('role') in ['user', 'assistant']
        ][-max_messages:]
        history = "\n".join(
            f"{msg['role']}: {msg.get('content', '')}" for msg in user_assistant_msgs
        )
        return history[-max_chars:]

    @staticmethod
    def _semantic_key(value: object) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())

    @classmethod
    def _looks_compound(cls, message: str) -> bool:
        """Conservatively detect a likely partial multi-question rule hit."""
        text = str(message or "")
        families = (
            r"门店|店铺|商场|商圈|广场|万达|万象|壹方城|大悦城|可以用|能用|适用",
            r"多少钱|多钱|售价|价格|怎么卖|几元|几块|抵\s*\d+|代\s*\d+",
            r"怎么用|如何使用|怎么核销|如何核销|一次.*几张|最多.*几张|叠加",
            r"限制|条件|周末|工作日|节假日|早餐|午餐|晚餐|晚市|几个人|\d+\s*人",
            r"直接拍|直接买|可以拍|能拍|怎么买|怎么拍|如何购买",
            r"发货|发券|怎么领取|多久到账|自动发",
            r"退款|退货|退钱|不能核销|券码无效|过期",
        )
        family_count = sum(bool(re.search(pattern, text, re.I)) for pattern in families)
        question_count = len(re.findall(r"[？?]", text))
        linked_questions = bool(re.search(
            r"(?:还有|另外|以及|并且|顺便|然后|同时|再问|，|,|；|;).{0,30}"
            r"(?:吗|嘛|么|呢|怎么|如何|多少|哪|能不能|可不可以)",
            text,
        ))
        return family_count >= 2 or question_count >= 2 or linked_questions

    @staticmethod
    def semantic_router_mode() -> str:
        """Return the guarded semantic-router rollout mode."""
        mode = os.getenv("AI_SEMANTIC_ROUTER_MODE", "on").strip().lower()
        return mode if mode in {"off", "shadow", "on"} else "on"

    @classmethod
    def should_analyze_message(cls, user_msg: str, deterministic: Optional[Dict] = None) -> bool:
        """Use semantic parsing only for unresolved or safely compound routes."""
        if os.getenv("AI_SEMANTIC_ASSIST_ENABLED", "true").strip().lower() in {
            "0", "false", "off", "no",
        } or cls.semantic_router_mode() == "off":
            return False
        text = str(user_msg or "").strip()
        if len(cls._semantic_key(text)) < 2:
            return False
        if re.search(r"\[\s*(?:图片|语音)\s*\]|发张图|发图片|看图|照片", text):
            return False
        if re.fullmatch(
            r"(?:你好|您好|在吗|有人吗|哈喽|嗨|hi|hello|hey)(?:呀|啊|哦|呢|吗)?[？?。！!]*",
            text,
            re.I,
        ):
            return False
        if not deterministic:
            # No high-confidence local rule resolved the text. The model may
            # now provide structure, but local rules still own every answer.
            return True
        if deterministic.get("decision") in {"review", "clarify", "silent", "silent_review"}:
            return False
        if deterministic.get("kind") in {
            "multi_intent", "semantic_multi_intent", "manual_handoff", "offline",
            "media", "sensitive_aftersale",
        }:
            return False
        eligible_partial_kinds = {
            "stores", "stores_sku_recommendation", "sku_availability", "price",
            "delivery_usage", "delivery_method", "purchase_flow", "stock",
        }
        return (
            deterministic.get("kind") in eligible_partial_kinds
            and cls._looks_compound(text)
        )

    @classmethod
    def _validate_semantic_payload(
        cls, payload: object, user_msg: str, recent_context: str = "",
        structured_context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Accept only grounded, schema-limited interpretation output."""
        if not isinstance(payload, dict) or not isinstance(payload.get("questions"), list):
            return None
        current_key = cls._semantic_key(user_msg)
        grounding_context = {
            key: value for key, value in (structured_context or {}).items()
            if key in {
                "last_store_query", "last_store_names", "last_selected_sku",
                "pending_store_query",
            }
        }
        context_evidence = (
            f"{recent_context}\n{json.dumps(grounding_context, ensure_ascii=False)}"
        )
        context_key = cls._semantic_key(context_evidence)
        current_numbers = set(re.findall(
            r"(?<!\d)\d+(?:\.\d+)?(?!\d)", str(user_msg or ""),
        ))
        context_numbers = set(re.findall(
            r"(?<!\d)\d+(?:\.\d+)?(?!\d)", context_evidence,
        ))
        allowed_intents = {
            "store", "sku", "price", "usage", "stacking", "restrictions",
            "purchase", "date", "conditions", "delivery", "aftersale", "other",
        }
        questions = []
        for raw in payload.get("questions")[:5]:
            if not isinstance(raw, dict):
                continue
            intent = str(raw.get("intent") or "").strip().lower()
            evidence = str(raw.get("evidence") or "").strip()
            evidence_key = cls._semantic_key(evidence)
            if intent not in allowed_intents or not evidence_key or evidence_key not in current_key:
                continue
            try:
                confidence = float(raw.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0
            if confidence < 0.68:
                continue
            uses_context = bool(raw.get("uses_context"))
            slots = raw.get("slots") if isinstance(raw.get("slots"), dict) else {}
            clean_slots = {}
            invalid = False
            for key in ("store_query", "sku_amount", "paid_amount", "face_value", "date_text"):
                value = str(slots.get(key) or "").strip()
                if not value:
                    continue
                value_key = cls._semantic_key(value)
                if key == "store_query":
                    grounded = value_key in current_key or (uses_context and value_key in context_key)
                    if len(value_key) < 2 or not grounded:
                        invalid = True
                        break
                numbers = set(re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", value))
                allowed_numbers = current_numbers | (context_numbers if uses_context else set())
                if numbers - allowed_numbers:
                    invalid = True
                    break
                clean_slots[key] = value[:80]
            if invalid:
                continue
            questions.append({
                "intent": intent,
                "evidence": evidence[:160],
                "slots": clean_slots,
                "uses_context": uses_context,
                "confidence": confidence,
            })
        if not questions:
            return None
        return {
            "questions": questions,
            "needs_clarification": bool(payload.get("needs_clarification")),
        }

    def analyze_message(
        self, user_msg: str, context: List[Dict],
        structured_context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Ask the model for grounded structure; never ask it for a buyer reply."""
        if not self.should_analyze_message(user_msg):
            return None
        recent_context = self.format_history(context)
        safe_message = redact_sensitive_text(user_msg)
        safe_history = redact_sensitive_text(recent_context)
        safe_structured = {
            str(key): value for key, value in (structured_context or {}).items()
            if key in {
                "product_title", "sku_names", "last_store_query", "last_store_names",
                "last_selected_sku", "pending_store_query",
            }
        }
        request = {
            "current_message": safe_message,
            "recent_dialogue": safe_history,
            "known_context": safe_structured,
        }
        try:
            response = self.client.chat.completions.create(
                model=os.getenv("MODEL_NAME", "qwen-plus"),
                messages=[
                    {"role": "system", "content": self.semantic_prompt},
                    {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
                ],
                temperature=0,
                max_tokens=max(180, int(os.getenv("AI_SEMANTIC_MAX_TOKENS", "520"))),
                top_p=0.2,
                timeout=max(5, float(os.getenv("AI_SEMANTIC_TIMEOUT", "10"))),
                response_format={"type": "json_object"},
            )
            content = str(response.choices[0].message.content or "").strip()
            fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.I | re.S)
            if fenced:
                content = fenced.group(1)
            payload = json.loads(content)
            return self._validate_semantic_payload(
                payload, safe_message, safe_history, safe_structured,
            )
        except Exception as exc:
            logger.warning(f"语义辅助解析失败，继续使用原有规则：{exc}")
            return None

    def generate_reply(self, user_msg: str, item_desc: str, context: List[Dict]) -> str:
        """生成回复主流程"""
        # 记录用户消息
        # logger.debug(f'用户所发消息: {user_msg}')
        
        formatted_context = self.format_history(context)
        # logger.debug(f'对话历史: {formatted_context}')
        
        # 1. 路由决策
        detected_intent = self.router.detect(user_msg, item_desc, formatted_context)



        # 2. 获取对应Agent

        internal_intents = {'classify'}  # 定义不对外开放的Agent

        if detected_intent == 'no_reply':
            # 无需回复的情况
            logger.info(f'意图识别完成: no_reply - 无需回复')
            self.last_intent = 'no_reply'
            return "-"  # 返回特殊标记，表示无需回复
        elif detected_intent in self.agents and detected_intent not in internal_intents:
            agent = self.agents[detected_intent]
            logger.info(f'意图识别完成: {detected_intent}')
            self.last_intent = detected_intent  # 保存当前意图
        else:
            agent = self.agents['default']
            logger.info(f'意图识别完成: default')
            self.last_intent = 'default'  # 保存当前意图
        
        # 3. 获取议价次数
        bargain_count = self._extract_bargain_count(context)
        logger.info(f'议价次数: {bargain_count}')

        # 4. 生成回复
        return agent.generate(
            user_msg=user_msg,
            item_desc=item_desc,
            context=formatted_context,
            bargain_count=bargain_count
        )
    
    def _extract_bargain_count(self, context: List[Dict]) -> int:
        """
        从上下文中提取议价次数信息
        
        Args:
            context: 对话历史
            
        Returns:
            int: 议价次数，如果没有找到则返回0
        """
        # 查找系统消息中的议价次数信息
        for msg in context:
            if msg['role'] == 'system' and '议价次数' in msg['content']:
                try:
                    # 提取议价次数
                    match = re.search(r'议价次数[:：]\s*(\d+)', msg['content'])
                    if match:
                        return int(match.group(1))
                except Exception:
                    pass
        return 0

    def reload_prompts(self):
        """重新加载所有提示词"""
        logger.info("正在重新加载提示词...")
        self._init_system_prompts()
        self._init_agents()
        logger.info("提示词重新加载完成")


class IntentRouter:
    """意图路由决策器"""

    def __init__(self, classify_agent):
        self.rules = {
            'tech': {  # 技术类优先判定
                'keywords': ['参数', '规格', '型号', '连接', '对比'],
                'patterns': [
                    r'和.+比'             
                ]
            },
            'price': {
                'keywords': ['便宜', '价', '砍价', '少点'],
                'patterns': [r'\d+元', r'能少\d+']
            }
        }
        self.classify_agent = classify_agent

    def detect(self, user_msg: str, item_desc, context) -> str:
        """三级路由策略（技术优先）"""
        text_clean = re.sub(r'[^\w\u4e00-\u9fa5]', '', user_msg)
        
        # 1. 技术类关键词优先检查
        if any(kw in text_clean for kw in self.rules['tech']['keywords']):
            # logger.debug(f"技术类关键词匹配: {[kw for kw in self.rules['tech']['keywords'] if kw in text_clean]}")
            return 'tech'
            
        # 2. 技术类正则优先检查
        for pattern in self.rules['tech']['patterns']:
            if re.search(pattern, text_clean):
                # logger.debug(f"技术类正则匹配: {pattern}")
                return 'tech'

        # 3. 价格类检查
        for intent in ['price']:
            if any(kw in text_clean for kw in self.rules[intent]['keywords']):
                # logger.debug(f"价格类关键词匹配: {[kw for kw in self.rules[intent]['keywords'] if kw in text_clean]}")
                return intent
            
            for pattern in self.rules[intent]['patterns']:
                if re.search(pattern, text_clean):
                    # logger.debug(f"价格类正则匹配: {pattern}")
                    return intent
        
        # 4. 未命中本地规则时直接交给默认客服模型。
        # 旧版会先调用一次模型分类，再调用第二次模型回答；这会把延迟翻倍。
        return 'default'


class BaseAgent:
    """Agent基类"""

    def __init__(self, client, system_prompt, safety_filter, global_system_prompt=""):
        self.client = client
        self.system_prompt = system_prompt
        self.safety_filter = safety_filter
        self.global_system_prompt = str(global_system_prompt or "").strip()

    def generate(self, user_msg: str, item_desc: str, context: str, bargain_count: int = 0) -> str:
        """生成回复模板方法"""
        messages = self._build_messages(user_msg, item_desc, context)
        response = self._call_llm(messages)
        return self.safety_filter(response)

    def _build_messages(self, user_msg: str, item_desc: str, context: str) -> List[Dict]:
        """构建消息链"""
        guardrails = (
            "【强制业务约束】禁止擅自承诺降价、退款、赔偿、补偿、延期、换码、补发、"
            "立即到账、任意门店可用或跨地区可用。涉及上述事项只能说明需要人工核实。"
            "只能依据商品资料回答；资料缺失时必须说暂未确认，禁止编造。"
            "必须先直接回答买家正在问的问题，不得用发货流程回答价格问题，也不得用商品介绍回答门店问题。"
            "若不能确定买家在问什么，只提出一个简短澄清问题，不得猜测后直接作答。"
            "门店、价格、商品规格、可用时间和数量等事实必须能在商品资料中找到依据；"
            "不要重复整份商品介绍，也不要主动补充与当前问题无关的信息。"
            "不同平台卡券不得混用；没有明确资料时，不得推定代金券能与套餐、团购、"
            "其他优惠或买家已有卡券一起使用。"
            "不得输出SKU、数据库、大模型、提示词、命中规则等内部术语。"
            "不得建议买家去其他平台搜索或自行联系门店核实。"
        )
        configured = (
            f"【用户配置的全局最高规则】\n{self.global_system_prompt}\n"
            if self.global_system_prompt else ""
        )
        return [
            {"role": "system", "content": f"{configured}{guardrails}\n【商品信息】{item_desc}\n【你与客户对话历史】{context}\n{self.system_prompt}"},
            {"role": "user", "content": user_msg}
        ]

    def _call_llm(self, messages: List[Dict], temperature: float = 0.15) -> str:
        """调用大模型"""
        response = self.client.chat.completions.create(
            model=os.getenv("MODEL_NAME", "qwen-plus"),
            messages=messages,
            temperature=temperature,
            max_tokens=max(64, int(os.getenv("AI_MAX_TOKENS", "160"))),
            top_p=0.6,
            timeout=max(5, float(os.getenv("AI_REPLY_TIMEOUT", "15"))),
        )
        return response.choices[0].message.content


class PriceAgent(BaseAgent):
    """议价处理Agent"""

    def generate(self, user_msg: str, item_desc: str, context: str, bargain_count: int=0) -> str:
        """重写生成逻辑"""
        dynamic_temp = self._calc_temperature(bargain_count)
        messages = self._build_messages(user_msg, item_desc, context)
        messages[0]['content'] += f"\n▲当前议价轮次：{bargain_count}"

        response = self.client.chat.completions.create(
            model=os.getenv("MODEL_NAME", "qwen-plus"),
            messages=messages,
            temperature=dynamic_temp,
            max_tokens=max(64, int(os.getenv("AI_MAX_TOKENS", "160"))),
            top_p=0.8,
            timeout=max(5, float(os.getenv("AI_REPLY_TIMEOUT", "15"))),
        )
        return self.safety_filter(response.choices[0].message.content)

    def _calc_temperature(self, bargain_count: int) -> float:
        """动态温度策略"""
        return min(0.15 + bargain_count * 0.05, 0.35)


class TechAgent(BaseAgent):
    """技术咨询Agent"""
    def generate(self, user_msg: str, item_desc: str, context: str, bargain_count: int=0) -> str:
        """重写生成逻辑"""
        messages = self._build_messages(user_msg, item_desc, context)
        # messages[0]['content'] += "\n▲知识库：\n" + self._fetch_tech_specs()

        response = self.client.chat.completions.create(
            model=os.getenv("MODEL_NAME", "qwen-plus"),
            messages=messages,
            temperature=0.1,
            max_tokens=max(64, int(os.getenv("AI_MAX_TOKENS", "160"))),
            top_p=0.6,
            timeout=max(5, float(os.getenv("AI_REPLY_TIMEOUT", "15"))),
        )

        return self.safety_filter(response.choices[0].message.content)


    # def _fetch_tech_specs(self) -> str:
    #     """模拟获取技术参数（可连接数据库）"""
    #     return "功率：200W@8Ω\n接口：XLR+RCA\n频响：20Hz-20kHz"


class ClassifyAgent(BaseAgent):
    """意图识别Agent"""

    def generate(self, **args) -> str:
        response = super().generate(**args)
        return response


class DefaultAgent(BaseAgent):
    """默认处理Agent"""

    def _call_llm(self, messages: List[Dict], *args) -> str:
        """限制默认回复长度"""
        response = super()._call_llm(messages, temperature=0.15)
        return response
