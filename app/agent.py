"""ResearchFlow · Agent 核心（阶段 1）

这就是那个"Agent Loop"—— 所有 Agent 框架剥到底，都是这个循环：

    想一想（模型决策）
        ↓
    做一做（调用工具）
        ↓
    看看结果（写回上下文）
        ↓
    再想一想 …… 直到模型说"我答完了"或撞到限制

本项目**自己实现**这个循环，而不是套 LangChain/LangGraph。
理由（面试会问）：
    套框架你只能讲"我调用了 xxx"；自己实现你能讲
    "超预算了怎么收尾""工具失败要不要重试""上下文太长怎么截断"——
    这些才是工程能力。

三个必须有的护栏（没有它们就是玩具）：
    1. max_steps   最多想几步 —— 防止模型无限循环烧钱
    2. token 预算  总共能用多少 token —— 防止成本失控
    3. 超时        单步最长多久 —— 防止一个卡住的请求拖死整个 Worker
"""

from __future__ import annotations

import json

from . import db, llm, tools
from .config import settings

SYSTEM_PROMPT = """你是一个严谨的研究助手。你的任务是围绕用户给出的问题做调研，并产出一份有依据的研究报告。

工作要求：
1. 先思考需要查什么，然后调用工具查资料。
2. 优先读取一手资料（官方文档、项目仓库），不要只依赖搜索结果摘要。
3. 对同一主题，尽量看多个来源，注意它们说法是否一致。
4. 资料够了就停下来写报告，不要为了多查而多查。

写报告的要求：
- 用 Markdown 格式。
- 结构：概览 / 关键发现 / 对比或分析 / 参考来源。
- 只写你从工具结果里真正看到的内容。没有依据的推测必须明确标注"推测"。
- 如果资料不足以下结论，就直说资料不足，不要编。"""


class ResearchAgent:
    """一次研究任务的执行者。

    刻意做成"一个实例跑一次任务"：
    状态都在实例里，跑完就丢，避免多任务之间互相污染。
    """

    def __init__(self, run_id: str, question: str) -> None:
        self.run_id = run_id
        self.question = question
        self.messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        self.tokens_used = 0
        self.step_no = 0

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------

    def _trim_context(self) -> None:
        """上下文太长时裁掉中间的工具结果。

        保留：system + 用户问题 + 最近 6 条消息
        裁掉：中间的旧消息，换成一行提示

        为什么要做这件事？——真实场景里上下文会越滚越大，
        最后要么超出模型窗口报错，要么把 prompt 费用烧穿。
        "怎么裁"是个值得在面试里展开的设计题。
        """
        if len(self.messages) <= 12:
            return
        head = self.messages[:2]
        tail = self.messages[-6:]
        dropped = len(self.messages) - len(head) - len(tail)
        note = {"role": "user", "content": f"（为控制上下文长度，已省略中间 {dropped} 条较早的工具结果）"}
        self.messages = head + [note] + tail

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> str:
        """执行研究，返回最终报告。超出任何护栏都会提前收尾。"""
        schemas = tools.tool_schemas()

        for self.step_no in range(1, settings.max_steps + 1):
            self._trim_context()

            # ---- 1. 护栏：预算检查（在花钱之前检查，不是之后）----
            if self.tokens_used >= settings.max_tokens_budget:
                return self._forced_wrapup("token 预算已用完")

            # ---- 2. 想一想 ----
            step_id = db.add_step(
                run_id=self.run_id,
                step_no=self.step_no,
                kind="llm",
                name="decide",
                payload=f"第 {self.step_no} 步决策，已用 token {self.tokens_used}",
            )
            try:
                decision = llm.chat(self.messages, self.step_no, schemas)
            except llm.LLMError as exc:
                # 模型调用失败是**外部故障**，不能当成任务失败直接扔了。
                # 先记下来，然后尝试用已有信息收尾 —— 这是可靠性设计的一部分。
                db.finish_step(step_id, "failed", str(exc)[:500], 0)
                return self._degraded_report(f"模型调用失败：{exc}")
            db.finish_step(
                step_id,
                "succeeded",
                (decision["content"] or "")[:500]
                or f"请求了 {len(decision['tool_calls'])} 个工具",
                0,
            )

            usage = decision.get("usage") or {}
            step_tokens = int(usage.get("total_tokens") or 0)
            self.tokens_used += step_tokens
            if step_tokens:
                # 立刻落库，而不是等任务结束才写一次。
                # 这样即使 Worker 中途崩了，已经花掉的钱也是查得到的。
                db.add_run_tokens(self.run_id, step_tokens)

            # ---- 3. 模型说完了，就是最终答案 ----
            if not decision["tool_calls"]:
                return decision["content"] or self._degraded_report("模型没有给出内容")

            # ---- 4. 做一做：执行工具 ----
            self._append_assistant_with_tool_calls(decision)

            for tc in decision["tool_calls"]:
                self._execute_tool(tc)

        # ---- 5. 步数用完，强制收尾 ----
        return self._forced_wrapup(f"已达到最大步数 {settings.max_steps}")

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    def _append_assistant_with_tool_calls(self, decision: dict) -> None:
        """把模型的"我要调工具"记进对话历史，这是 OpenAI 协议的硬性要求：
        发 tool 结果之前，必须有一条带 tool_calls 的 assistant 消息。"""
        self.messages.append(
            {
                "role": "assistant",
                "content": decision["content"] or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                        },
                    }
                    for tc in decision["tool_calls"]
                ],
            }
        )

    def _execute_tool(self, tc: dict) -> None:
        name = tc["name"]
        args = tc["arguments"]

        step_id = db.add_step(
            run_id=self.run_id,
            step_no=self.step_no,
            kind="tool",
            name=name,
            payload=json.dumps(args, ensure_ascii=False)[:1000],
        )

        # 模型吐出坏 JSON 的情况：明确当失败处理，让模型重试
        if "__parse_error__" in args:
            result = {"ok": False, "error": f"参数不是合法 JSON：{args['__parse_error__']}"}
        else:
            result = tools.call_tool(name, args)

        output = json.dumps(result, ensure_ascii=False)
        db.finish_step(
            step_id,
            "succeeded" if result.get("ok") else "failed",
            output[:4000],
            0,
        )

        # 无论成功失败，都把结果回给模型 —— 让模型自己决定下一步
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": output[:8000],
            }
        )

    # ------------------------------------------------------------------
    # 收尾策略
    # ------------------------------------------------------------------

    def _forced_wrapup(self, reason: str) -> str:
        """撞到护栏时的收尾：不再调工具，只让它基于已有信息写报告。

        这是个重要的设计决策：
        "预算用完" 不等于 "任务失败"。已经有部分结果，就应该尽力交付。
        """
        self.messages.append(
            {
                "role": "user",
                "content": (
                    f"【系统提示】{reason}。请立刻停止调用工具，"
                    f"仅基于目前已经获得的信息写出研究报告。"
                    f"如果信息不足以回答，请在报告中明确说明哪些部分缺乏依据。"
                ),
            }
        )
        try:
            decision = llm.chat(self.messages, 999, None)  # 不传工具 = 强制它不能再调
            return decision["content"] or self._degraded_report(reason)
        except llm.LLMError as exc:
            return self._degraded_report(f"{reason}，且收尾调用也失败：{exc}")

    def _degraded_report(self, reason: str) -> str:
        """最坏情况：连模型都用不了，也要交出一份"诚实的部分结果"。

        为什么不直接抛异常？
        因为对用户来说，"跑了 3 步拿到一些线索 + 明确说明失败原因"
        比 "整个任务失败，什么都看不到" 有用得多。
        这也是后端可靠性里"优雅降级"的具体体现。
        """
        collected = [
            m["content"]
            for m in self.messages
            if m.get("role") == "tool" and m.get("content")
        ]
        lines = [
            "# 研究报告（降级交付）",
            "",
            f"**研究问题**：{self.question}",
            "",
            f"**中断原因**：{reason}",
            "",
            f"**已完成步骤**：{self.step_no} 步，消耗 token 约 {self.tokens_used}",
            "",
        ]
        if collected:
            lines.append("## 已收集到的资料片段")
            lines.append("")
            for i, c in enumerate(collected[-3:], start=1):
                lines.append(f"{i}. `{c[:400]}`")
            lines.append("")
        else:
            lines.append("## 已收集到的资料")
            lines.append("")
            lines.append("（无 —— 在获取任何资料前就中断了）")
            lines.append("")
        return "\n".join(lines)


def run_research(run_id: str, question: str) -> tuple[str | None, str | None]:
    """Worker 调这个函数。返回 (报告, 错误信息)，两者必有且仅有一个为 None。"""
    agent = ResearchAgent(run_id, question)
    try:
        report = agent.run()
        return report, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"