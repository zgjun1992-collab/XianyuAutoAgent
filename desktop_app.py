import asyncio
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from dotenv import load_dotenv, set_key
from loguru import logger
from openai import OpenAI

from app_store import AppStore
from main import XianyuLive
from XianyuAgent import XianyuReplyBot


APP_TITLE = "闲鱼餐饮卡券 AI 客服"


class DesktopApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1180x780")
        self.root.minsize(980, 680)

        load_dotenv(override=True)
        self.store = AppStore()
        self.live = None
        self.worker = None
        self.events = queue.Queue()
        self.status_value = "stopped"

        self._configure_style()
        self._build_ui()
        self._load_config()
        self._load_policies()
        self._refresh_products()
        self._refresh_audits()

        logger.add(
            lambda message: self.events.put({"type": "log", "message": str(message).rstrip()}),
            level="INFO",
            format="{time:HH:mm:ss} | {level} | {message}",
        )
        self.root.after(300, self._poll_events)
        self.root.after(1500, self._periodic_refresh)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 8))
        style.configure("Treeview", rowheight=28, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=(18, 14))
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        self.header_status = ttk.Label(header, text="● 已停止", foreground="#777777", style="Status.TLabel")
        self.header_status.pack(side="right", padx=(12, 4))

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self.dashboard_tab = ttk.Frame(self.tabs, padding=16)
        self.config_tab = ttk.Frame(self.tabs, padding=16)
        self.products_tab = ttk.Frame(self.tabs, padding=12)
        self.review_tab = ttk.Frame(self.tabs, padding=12)
        self.policy_tab = ttk.Frame(self.tabs, padding=16)
        self.audit_tab = ttk.Frame(self.tabs, padding=12)

        self.tabs.add(self.dashboard_tab, text="运行面板")
        self.tabs.add(self.config_tab, text="账号与AI")
        self.tabs.add(self.products_tab, text="商品资料")
        self.tabs.add(self.review_tab, text="待审核回复")
        self.tabs.add(self.policy_tab, text="客服约束")
        self.tabs.add(self.audit_tab, text="审计记录")

        self._build_dashboard()
        self._build_config()
        self._build_products()
        self._build_review()
        self._build_policy()
        self._build_audit()

    def _build_dashboard(self):
        controls = ttk.LabelFrame(self.dashboard_tab, text="客服控制", padding=16)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="启动客服", style="Primary.TButton", command=self.start_service)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止客服", command=self.stop_service, state="disabled")
        self.stop_button.pack(side="left", padx=10)
        ttk.Label(controls, text="默认采用人工审核模式；只有通过规则检查的消息才能在自动模式下发送。", foreground="#555555").pack(side="left", padx=14)

        stats = ttk.Frame(self.dashboard_tab)
        stats.pack(fill="x", pady=16)
        self.stat_vars = {}
        for key, title, color in [
            ("pending", "待审核", "#d97706"),
            ("sent", "已发送", "#15803d"),
            ("rejected", "已驳回", "#b91c1c"),
        ]:
            card = ttk.LabelFrame(stats, text=title, padding=16)
            card.pack(side="left", fill="x", expand=True, padx=(0, 10))
            var = tk.StringVar(value="0")
            self.stat_vars[key] = var
            ttk.Label(card, textvariable=var, font=("Microsoft YaHei UI", 24, "bold"), foreground=color).pack()

        log_frame = ttk.LabelFrame(self.dashboard_tab, text="运行日志", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", font=("Consolas", 9), background="#111827", foreground="#e5e7eb")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _form_row(self, parent, row, label, variable, show=None, width=70):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=7)
        entry = ttk.Entry(parent, textvariable=variable, width=width, show=show)
        entry.grid(row=row, column=1, sticky="ew", pady=7)
        return entry

    def _build_config(self):
        frame = ttk.LabelFrame(self.config_tab, text="连接配置", padding=18)
        frame.pack(fill="x")
        frame.columnconfigure(1, weight=1)

        self.api_key_var = tk.StringVar()
        self.cookie_var = tk.StringVar()
        self.base_url_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self._form_row(frame, 0, "API Key", self.api_key_var, show="●")
        self.cookie_entry = self._form_row(frame, 1, "闲鱼 Cookie", self.cookie_var, show="●")
        self._form_row(frame, 2, "模型接口", self.base_url_var)
        self._form_row(frame, 3, "模型名称", self.model_var)

        self.reveal_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="显示敏感信息", variable=self.reveal_var, command=self._toggle_secrets).grid(row=4, column=1, sticky="w", pady=6)

        actions = ttk.Frame(frame)
        actions.grid(row=5, column=1, sticky="w", pady=(10, 0))
        ttk.Button(actions, text="保存配置", command=self.save_config).pack(side="left")
        ttk.Button(actions, text="测试AI连接", command=self.test_ai).pack(side="left", padx=8)
        ttk.Label(actions, text="敏感信息只保存在本机 .env，不会写入审计日志。", foreground="#666666").pack(side="left", padx=10)

    def _build_products(self):
        pane = ttk.Panedwindow(self.products_tab, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane, padding=6)
        right = ttk.Frame(pane, padding=12)
        pane.add(left, weight=2)
        pane.add(right, weight=3)

        self.product_tree = ttk.Treeview(left, columns=("item_id", "title", "enabled"), show="headings")
        self.product_tree.heading("item_id", text="闲鱼商品ID")
        self.product_tree.heading("title", text="本地商品名称")
        self.product_tree.heading("enabled", text="状态")
        self.product_tree.column("item_id", width=150)
        self.product_tree.column("title", width=220)
        self.product_tree.column("enabled", width=70, anchor="center")
        self.product_tree.pack(fill="both", expand=True)
        self.product_tree.bind("<<TreeviewSelect>>", self._select_product)
        ttk.Button(left, text="新建商品资料", command=self._new_product).pack(fill="x", pady=(8, 0))

        right.columnconfigure(1, weight=1)
        right.rowconfigure(3, weight=1)
        self.product_id_var = tk.StringVar()
        self.product_title_var = tk.StringVar()
        self.product_enabled_var = tk.BooleanVar(value=True)
        self._form_row(right, 0, "闲鱼商品ID", self.product_id_var, width=48)
        self._form_row(right, 1, "本地商品名称", self.product_title_var, width=48)
        ttk.Checkbutton(right, text="启用这份本地资料", variable=self.product_enabled_var).grid(row=2, column=1, sticky="w", pady=5)
        ttk.Label(right, text="商品专属资料（优先级高于闲鱼页面）").grid(row=3, column=0, sticky="nw", padx=(0, 12), pady=7)
        self.product_content = tk.Text(right, wrap="word", height=20, font=("Microsoft YaHei UI", 10))
        self.product_content.grid(row=3, column=1, sticky="nsew", pady=7)
        self.product_content.insert("1.0", self._product_template())
        actions = ttk.Frame(right)
        actions.grid(row=4, column=1, sticky="w", pady=8)
        ttk.Button(actions, text="保存商品资料", command=self._save_product).pack(side="left")
        ttk.Button(actions, text="删除", command=self._delete_product).pack(side="left", padx=8)

    @staticmethod
    def _product_template():
        return (
            "面额：\n售价：\n有效期：\n适用门店：\n排除门店：\n"
            "使用时段：\n是否预约：\n叠加规则：\n每单限用：\n"
            "核销方式：\n发码时间：\n退款规则：\n特别说明：\n"
        )

    def _build_review(self):
        pane = ttk.Panedwindow(self.review_tab, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane, padding=5)
        right = ttk.Frame(pane, padding=10)
        pane.add(left, weight=3)
        pane.add(right, weight=4)

        self.review_tree = ttk.Treeview(left, columns=("id", "time", "buyer", "item", "reason"), show="headings")
        for key, title, width in [
            ("id", "ID", 50), ("time", "时间", 125), ("buyer", "买家", 100),
            ("item", "商品ID", 115), ("reason", "原因", 190),
        ]:
            self.review_tree.heading(key, text=title)
            self.review_tree.column(key, width=width)
        self.review_tree.pack(fill="both", expand=True)
        self.review_tree.bind("<<TreeviewSelect>>", self._select_review)

        right.columnconfigure(0, weight=1)
        right.rowconfigure(7, weight=1)
        self.review_id_var = tk.StringVar()
        self.review_meta_var = tk.StringVar(value="请选择一条待审核回复")
        self.review_user_var = tk.StringVar()
        self.review_reason_var = tk.StringVar()
        ttk.Label(right, textvariable=self.review_meta_var, style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(right, text="买家消息").grid(row=1, column=0, sticky="w")
        self.review_user = tk.Text(right, height=4, wrap="word", state="disabled")
        self.review_user.grid(row=2, column=0, sticky="ew", pady=(3, 9))
        ttk.Label(right, text="拦截原因").grid(row=3, column=0, sticky="w")
        ttk.Label(right, textvariable=self.review_reason_var, foreground="#b45309", wraplength=520).grid(row=4, column=0, sticky="w", pady=(3, 9))
        ttk.Label(right, text="审核后回复（可编辑）").grid(row=5, column=0, sticky="w")
        self.review_reply = tk.Text(right, height=10, wrap="word", font=("Microsoft YaHei UI", 10))
        self.review_reply.grid(row=7, column=0, sticky="nsew", pady=(3, 9))
        buttons = ttk.Frame(right)
        buttons.grid(row=8, column=0, sticky="w")
        ttk.Button(buttons, text="批准并发送", command=self._approve_review).pack(side="left")
        ttk.Button(buttons, text="驳回并保持人工", command=self._reject_review).pack(side="left", padx=8)
        self.resume_after_send_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(buttons, text="发送后恢复该会话的AI回复", variable=self.resume_after_send_var).pack(side="left", padx=8)

    def _build_policy(self):
        top = ttk.LabelFrame(self.policy_tab, text="发送模式", padding=12)
        top.pack(fill="x")
        self.reply_mode_var = tk.StringVar(value="review")
        ttk.Radiobutton(top, text="人工审核（推荐）", value="review", variable=self.reply_mode_var).pack(side="left")
        ttk.Radiobutton(top, text="安全消息自动发送", value="auto", variable=self.reply_mode_var).pack(side="left", padx=20)

        fallbacks = ttk.LabelFrame(self.policy_tab, text="高风险安全回复", padding=12)
        fallbacks.pack(fill="x", pady=10)
        fallbacks.columnconfigure(1, weight=1)
        self.safe_fallback_var = tk.StringVar()
        self.price_fallback_var = tk.StringVar()
        self.refund_fallback_var = tk.StringVar()
        self._form_row(fallbacks, 0, "通用", self.safe_fallback_var, width=80)
        self._form_row(fallbacks, 1, "议价", self.price_fallback_var, width=80)
        self._form_row(fallbacks, 2, "退款", self.refund_fallback_var, width=80)

        lists = ttk.Frame(self.policy_tab)
        lists.pack(fill="both", expand=True)
        left = ttk.LabelFrame(lists, text="禁止出现在AI承诺中的短语（每行一个）", padding=8)
        right = ttk.LabelFrame(lists, text="触发人工审核的买家关键词（每行一个）", padding=8)
        left.pack(side="left", fill="both", expand=True, padx=(0, 5))
        right.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self.forbidden_text = tk.Text(left, wrap="word", height=12)
        self.risk_text = tk.Text(right, wrap="word", height=12)
        self.forbidden_text.pack(fill="both", expand=True)
        self.risk_text.pack(fill="both", expand=True)
        ttk.Button(self.policy_tab, text="保存客服约束", command=self._save_policies).pack(anchor="e", pady=(10, 0))

    def _build_audit(self):
        self.audit_tree = ttk.Treeview(
            self.audit_tab,
            columns=("id", "time", "status", "buyer", "item", "message", "reply"),
            show="headings",
        )
        columns = [
            ("id", "ID", 45), ("time", "时间", 130), ("status", "状态", 75),
            ("buyer", "买家", 90), ("item", "商品ID", 110),
            ("message", "买家消息", 260), ("reply", "最终/草稿回复", 330),
        ]
        for key, title, width in columns:
            self.audit_tree.heading(key, text=title)
            self.audit_tree.column(key, width=width)
        self.audit_tree.pack(fill="both", expand=True)
        ttk.Button(self.audit_tab, text="刷新", command=self._refresh_audits).pack(anchor="e", pady=8)

    def _load_config(self):
        self.api_key_var.set(os.getenv("API_KEY", ""))
        self.cookie_var.set(os.getenv("COOKIES_STR", ""))
        self.base_url_var.set(os.getenv("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
        self.model_var.set(os.getenv("MODEL_NAME", "qwen-max"))

    def _toggle_secrets(self):
        show = "" if self.reveal_var.get() else "●"
        for entry in [self.cookie_entry]:
            entry.configure(show=show)
        # API key entry is the first Entry child in the form.
        entries = [child for child in self.config_tab.winfo_children()[0].winfo_children() if isinstance(child, ttk.Entry)]
        if entries:
            entries[0].configure(show=show)

    def save_config(self, quiet=False):
        values = {
            "API_KEY": self.api_key_var.get().strip(),
            "COOKIES_STR": self.cookie_var.get().strip(),
            "MODEL_BASE_URL": self.base_url_var.get().strip(),
            "MODEL_NAME": self.model_var.get().strip(),
        }
        if not all(values.values()):
            if not quiet:
                messagebox.showwarning("配置不完整", "API Key、Cookie、模型接口和模型名称都不能为空。")
            return False
        env_path = os.path.join(os.getcwd(), ".env")
        if not os.path.exists(env_path):
            open(env_path, "a", encoding="utf-8").close()
        for key, value in values.items():
            set_key(env_path, key, value)
            os.environ[key] = value
        if not quiet:
            messagebox.showinfo("保存成功", "连接配置已保存到本机。")
        return True

    def test_ai(self):
        if not self.save_config(quiet=True):
            return

        def run_test():
            self.events.put({"type": "log", "message": "正在测试AI连接..."})
            try:
                client = OpenAI(api_key=self.api_key_var.get().strip(), base_url=self.base_url_var.get().strip())
                result = client.chat.completions.create(
                    model=self.model_var.get().strip(),
                    messages=[{"role": "user", "content": "只回复OK"}],
                    max_tokens=8,
                    temperature=0,
                    timeout=20,
                )
                self.events.put({"type": "dialog", "kind": "info", "title": "连接成功", "message": f"模型回复：{result.choices[0].message.content}"})
            except Exception as exc:
                self.events.put({"type": "dialog", "kind": "error", "title": "连接失败", "message": str(exc)})

        threading.Thread(target=run_test, daemon=True).start()

    def start_service(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.save_config(quiet=True):
            self.tabs.select(self.config_tab)
            messagebox.showwarning("先完成配置", "请先填写并保存账号与AI配置。")
            return
        self._save_policies(quiet=True)
        try:
            bot = XianyuReplyBot()
            self.live = XianyuLive(
                self.cookie_var.get().strip(), bot_instance=bot, app_store=self.store,
                event_callback=self.events.put, interactive=False,
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            return

        def run():
            try:
                asyncio.run(self.live.main())
            except Exception as exc:
                self.events.put({"type": "status", "value": "error", "message": str(exc)})

        self.worker = threading.Thread(target=run, name="xianyu-service", daemon=True)
        self.worker.start()
        self._set_status("starting")

    def stop_service(self):
        if self.live:
            self.live.stop()
        self._set_status("stopping")

    def _set_status(self, status, message=""):
        self.status_value = status
        mapping = {
            "stopped": ("● 已停止", "#777777"),
            "starting": ("● 正在启动", "#d97706"),
            "connected": ("● 已连接", "#15803d"),
            "reconnecting": ("● 正在重连", "#d97706"),
            "stopping": ("● 正在停止", "#d97706"),
            "error": ("● 连接异常", "#b91c1c"),
        }
        text, color = mapping.get(status, (f"● {status}", "#777777"))
        self.header_status.configure(text=text, foreground=color)
        active = status in {"starting", "connected", "reconnecting", "stopping"}
        self.start_button.configure(state="disabled" if active else "normal")
        self.stop_button.configure(state="normal" if active else "disabled")
        if message:
            self._append_log(message)

    def _append_log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                event_type = event.get("type")
                if event_type == "log":
                    self._append_log(event.get("message", ""))
                elif event_type == "status":
                    self._set_status(event.get("value", "error"), event.get("message", ""))
                elif event_type in {"pending_reply", "audit_updated"}:
                    self._refresh_audits()
                    if event_type == "pending_reply":
                        self.tabs.select(self.review_tab)
                elif event_type == "dialog":
                    getattr(messagebox, f"show{event['kind']}")(event["title"], event["message"])
        except queue.Empty:
            pass
        self.root.after(300, self._poll_events)

    def _periodic_refresh(self):
        self._refresh_audits()
        self.root.after(2500, self._periodic_refresh)

    def _load_policies(self):
        policies = self.store.get_policies()
        self.reply_mode_var.set(policies["reply_mode"])
        self.safe_fallback_var.set(policies["safe_fallback"])
        self.price_fallback_var.set(policies["price_fallback"])
        self.refund_fallback_var.set(policies["refund_fallback"])
        self.forbidden_text.delete("1.0", "end")
        self.forbidden_text.insert("1.0", "\n".join(policies["forbidden_phrases"]))
        self.risk_text.delete("1.0", "end")
        self.risk_text.insert("1.0", "\n".join(policies["risk_keywords"]))

    def _save_policies(self, quiet=False):
        values = {
            "reply_mode": self.reply_mode_var.get(),
            "safe_fallback": self.safe_fallback_var.get().strip(),
            "price_fallback": self.price_fallback_var.get().strip(),
            "refund_fallback": self.refund_fallback_var.get().strip(),
            "forbidden_phrases": [x.strip() for x in self.forbidden_text.get("1.0", "end").splitlines() if x.strip()],
            "risk_keywords": [x.strip() for x in self.risk_text.get("1.0", "end").splitlines() if x.strip()],
        }
        for key, value in values.items():
            self.store.set_setting(key, value)
        if not quiet:
            messagebox.showinfo("保存成功", "客服约束已保存并立即生效。")

    def _refresh_products(self):
        for iid in self.product_tree.get_children():
            self.product_tree.delete(iid)
        for product in self.store.list_products():
            self.product_tree.insert("", "end", iid=product["item_id"], values=(product["item_id"], product["title"], "启用" if product["enabled"] else "停用"))

    def _new_product(self):
        self.product_tree.selection_remove(self.product_tree.selection())
        self.product_id_var.set("")
        self.product_title_var.set("")
        self.product_enabled_var.set(True)
        self.product_content.delete("1.0", "end")
        self.product_content.insert("1.0", self._product_template())

    def _select_product(self, _event=None):
        selected = self.product_tree.selection()
        if not selected:
            return
        product = self.store.get_product(selected[0])
        if not product:
            return
        self.product_id_var.set(product["item_id"])
        self.product_title_var.set(product["title"])
        self.product_enabled_var.set(bool(product["enabled"]))
        self.product_content.delete("1.0", "end")
        self.product_content.insert("1.0", product["content"])

    def _save_product(self):
        try:
            self.store.save_product(
                self.product_id_var.get(), self.product_title_var.get(),
                self.product_content.get("1.0", "end").strip(), self.product_enabled_var.get(),
            )
            self._refresh_products()
            messagebox.showinfo("保存成功", "商品专属资料已保存。")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _delete_product(self):
        item_id = self.product_id_var.get().strip()
        if item_id and messagebox.askyesno("确认删除", f"确定删除商品 {item_id} 的本地资料吗？"):
            self.store.delete_product(item_id)
            self._new_product()
            self._refresh_products()

    def _refresh_audits(self):
        counts = self.store.count_audits()
        for key, var in self.stat_vars.items():
            var.set(str(counts.get(key, 0)))

        for iid in self.review_tree.get_children():
            self.review_tree.delete(iid)
        for row in self.store.list_audits(status="pending"):
            reasons = "、".join(__import__("json").loads(row["reasons"])) or "人工审核模式"
            self.review_tree.insert("", "end", iid=str(row["id"]), values=(row["id"], row["created_at"].replace("T", " "), row["user_name"], row["item_id"], reasons))

        for iid in self.audit_tree.get_children():
            self.audit_tree.delete(iid)
        status_text = {"pending": "待审核", "sent": "已发送", "rejected": "已驳回"}
        for row in self.store.list_audits():
            reply = row["final_reply"] or row["draft_reply"]
            self.audit_tree.insert("", "end", values=(row["id"], row["created_at"].replace("T", " "), status_text.get(row["status"], row["status"]), row["user_name"], row["item_id"], row["user_message"], reply))

    def _select_review(self, _event=None):
        selected = self.review_tree.selection()
        if not selected:
            return
        row = self.store.get_audit(int(selected[0]))
        if not row:
            return
        import json
        self.review_id_var.set(str(row["id"]))
        self.review_meta_var.set(f"回复 #{row['id']} · 商品 {row['item_id']} · 买家 {row['user_name']}")
        self.review_reason_var.set("；".join(json.loads(row["reasons"])) or "当前为人工审核模式")
        self.review_user.configure(state="normal")
        self.review_user.delete("1.0", "end")
        self.review_user.insert("1.0", row["user_message"])
        self.review_user.configure(state="disabled")
        self.review_reply.delete("1.0", "end")
        self.review_reply.insert("1.0", row["final_reply"] or row["draft_reply"])

    def _approve_review(self):
        if not self.review_id_var.get():
            messagebox.showwarning("未选择", "请先选择一条待审核回复。")
            return
        if not self.live or self.status_value != "connected":
            messagebox.showwarning("客服未连接", "请先启动客服并等待连接成功。")
            return
        try:
            resume_ai = self.resume_after_send_var.get()
            future = self.live.approve_reply(
                int(self.review_id_var.get()),
                self.review_reply.get("1.0", "end").strip(),
                resume_ai=resume_ai,
            )
            future.result(timeout=15)
            self._refresh_audits()
            self.review_id_var.set("")
            mode_text = "已恢复AI回复。" if resume_ai else "当前会话仍保持人工接管。"
            messagebox.showinfo("发送成功", "审核后的回复已经发送。" + mode_text)
        except Exception as exc:
            messagebox.showerror("发送失败", str(exc))

    def _reject_review(self):
        if not self.review_id_var.get():
            return
        self.store.update_audit(int(self.review_id_var.get()), "rejected", "")
        self.review_id_var.set("")
        self._refresh_audits()

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("退出软件", "退出会停止闲鱼客服，确定继续吗？"):
                return
            self.stop_service()
        self.root.destroy()


def main():
    app_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    os.chdir(app_dir)
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
