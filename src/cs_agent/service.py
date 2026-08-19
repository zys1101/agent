"""AI 客服回答生成：本地 Ollama 模型 + 规则化快捷建议。"""

from __future__ import annotations

import re

from quote_agent.llm import LLMUnavailableError, OllamaClient

from .config import CsAgentSettings

SYSTEM_PROMPT = """你是"松辰智能"官网的 AI 智能客服，代号"小辰"，负责解答访客关于平台与服务的常见问题。

关于平台你需要知道：
- 公司全称：松辰智能（天津），主营机械设计外包服务，包括整机与非标自动化设计、工装夹具与测试治具、零部件传动与产品结构、钣金与机柜结构、CAD 制图逆向与标准化、仿真分析与工程咨询等。
- 客户可以通过首页"AI 智能报价"快速获得参考报价，也可以发布需求到接单大厅，由工程师报价竞单。
- 需求流程：发布需求 → 工程师报价 → 客户确认 → 支付托管 → 工程师交付 → 验收结算。
- 工程师可申请入驻认证；订单相关资金走平台托管，工程师完成交付且客户验收后放款。
- 联系人工客服：电话 186-4912-6373；邮箱 sonsentech@foxmail.com；工作时间见官网悬浮客服（默认 9:00-18:00）。

回复要求：
1. 使用简体中文，语气友好、专业、简洁，一般控制在 300 字以内。
2. 回答与机械设计外包、报价、需求发布、工程师入驻、订单交易流程相关的问题；与平台无关或敏感的问题，礼貌引导回主题。
3. 涉及具体价格时，引导用户使用"AI 智能报价"或直接发布需求获取工程师报价，不要承诺最终价格。
4. 无法回答时，建议联系人工客服并提供电话与邮箱。"""

DEFAULT_SUGGESTIONS = [
    "如何发布设计需求？",
    "AI 智能报价怎么用？",
    "平台有哪些设计服务？",
    "如何入驻成为工程师？",
    "订单资金如何托管结算？",
    "怎么联系人工客服？",
]

SUGGESTION_POOL = [
    ("如何发布设计需求？", "发布"),
    ("AI 智能报价怎么用？", "报价"),
    ("平台有哪些设计服务？", "服务"),
    ("如何入驻成为工程师？", "入驻"),
    ("订单资金如何托管结算？", "托管"),
    ("怎么联系人工客服？", "人工"),
]


class CustomerService:
    def __init__(self, llm: OllamaClient, settings: CsAgentSettings):
        self.llm = llm
        self.settings = settings

    def reply(self, message: str, history: list[dict] | None = None) -> dict:
        """生成一条客服回复；Ollama 不可用时抛出 LLMUnavailableError，由 Worker 回传失败。"""
        text = self.llm.generate(self._build_prompt(message, history or []))
        reply = text.strip()
        if not reply:
            raise LLMUnavailableError("empty model response")
        return {"reply": reply, "suggestions": self.build_suggestions(reply)}

    def _build_prompt(self, message: str, history: list[dict]) -> str:
        lines = [f"【系统】\n{SYSTEM_PROMPT}", "\n【对话历史】"]
        for item in history[-self.settings.max_history :]:
            role = "用户" if item.get("role") == "user" else "客服"
            lines.append(f"{role}：{item.get('content', '')}")
        lines.append(f"\n用户：{message}")
        lines.append("\n请以客服身份直接回复用户，不要输出任何额外说明。")
        return "\n".join(lines)

    def build_suggestions(self, reply: str) -> list[str]:
        """根据回复内容挑选不重复的快捷追问（避免与回复语义重复）。"""
        used = [
            text for text, keyword in SUGGESTION_POOL if keyword not in reply
        ]
        if len(used) >= 3:
            return used[:4]
        return DEFAULT_SUGGESTIONS[:4]
