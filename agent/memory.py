"""
Agent 的记忆，分两层：

1. 短期记忆 Conversation：同一段对话里的前几轮问答。
   - 最近 KEEP_RECENT 轮原样保留（问题 + 回答，不含中间的工具调用细节，省 token）
   - 更早的轮次压缩成一段摘要；对话再长，上下文也不会无限增长
   - 例：先问「GMV 最高的品牌是哪个」，再问「那它的平均成交价呢」，Agent 知道「它」指谁

2. 长期记忆 LongTermMemory：用户明确要求「记住」的偏好，存到 memory/notes.json，
   重启程序后依然有效。例：「记住：以后金额用万为单位」。
   Agent 通过 save_memory 工具写入；评测时不加载，保证评测结果可复现。
"""
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES_PATH = ROOT / "memory" / "notes.json"

KEEP_RECENT = 3          # 最近几轮原样保留
MAX_NOTES = 20           # 长期记忆最多保存几条


# ---------------------------------------------------------------------------
# 短期记忆：多轮对话
# ---------------------------------------------------------------------------
class Conversation:
    def __init__(self):
        self.turns: list[dict] = []   # [{"question": ..., "answer": ...}]
        self.summary = ""             # 更早轮次的压缩摘要

    def add(self, question: str, answer: str):
        self.turns.append({"question": question, "answer": answer or ""})
        if len(self.turns) > KEEP_RECENT:
            self._compress()

    def _compress(self):
        """把超出 KEEP_RECENT 的旧轮次交给模型压缩成摘要。"""
        from agent.llm import MODEL, client   # 放在函数里导入，避免循环导入

        old, self.turns = self.turns[:-KEEP_RECENT], self.turns[-KEEP_RECENT:]
        text = "\n\n".join(f"问：{t['question']}\n答：{t['answer']}" for t in old)
        prompt = (
            "把下面的数据分析对话压缩成要点摘要，供后续对话参考。"
            "保留：用户问过什么、关键结论和数字、使用的口径和筛选条件。200 字以内，不要添加原文没有的内容。\n\n"
            + (f"已有摘要：{self.summary}\n\n" if self.summary else "") + text
        )
        try:
            resp = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": prompt}])
            self.summary = resp.choices[0].message.content.strip()
        except Exception:
            # 压缩失败时退而求其次：只保留旧问题列表，不让对话中断
            self.summary += "\n" + "；".join(t["question"] for t in old)

    def messages(self) -> list[dict]:
        """转换成可以放进 messages 列表的历史消息（放在 System Prompt 之后、新问题之前）。"""
        msgs = []
        if self.summary:
            msgs += [{"role": "user", "content": f"【更早对话的摘要】\n{self.summary}"},
                     {"role": "assistant", "content": "好的，我会参考这些背景。"}]
        for t in self.turns:
            msgs += [{"role": "user", "content": t["question"]},
                     {"role": "assistant", "content": t["answer"]}]
        return msgs

    def reset(self):
        self.turns, self.summary = [], ""


# ---------------------------------------------------------------------------
# 长期记忆：用户偏好
# ---------------------------------------------------------------------------
def load_notes() -> list[dict]:
    if not NOTES_PATH.exists():
        return []
    try:
        return json.loads(NOTES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_note(note: str) -> str:
    """save_memory 工具的实现：保存一条用户偏好，返回给模型的确认文字。"""
    note = note.strip()
    if not note:
        return "错误：记忆内容为空"
    notes = load_notes()
    if any(n["note"] == note for n in notes):
        return f"这条已经记住了：{note}"
    notes.append({"note": note, "saved_at": time.strftime("%Y-%m-%d %H:%M")})
    notes = notes[-MAX_NOTES:]
    NOTES_PATH.parent.mkdir(exist_ok=True)
    NOTES_PATH.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"已记住：{note}（共 {len(notes)} 条长期记忆）"


def clear_notes():
    if NOTES_PATH.exists():
        NOTES_PATH.unlink()


def render_notes() -> str:
    """长期记忆 → 拼进 System Prompt 的一段文字。没有记忆时返回空字符串。"""
    notes = load_notes()
    if not notes:
        return ""
    lines = "\n".join(f"- {n['note']}" for n in notes)
    return ("\n\n# 长期记忆（用户之前要求记住的偏好）\n"
            "回答时遵守这些偏好；如果和数据口径规则冲突，以数据口径规则为准，并向用户说明。\n" + lines)
