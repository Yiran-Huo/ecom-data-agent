"""记忆的测试（不需要数据库，也不调用真实模型）。"""
import agent.llm
import agent.memory as memory
from agent.memory import Conversation


class _FakeClient:
    """假的模型客户端：压缩摘要时返回固定文字。"""
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                msg = type("M", (), {"content": "摘要：用户问过 GMV 最高的品牌（apple）"})
                return type("R", (), {"choices": [type("C", (), {"message": msg})]})


def test_conversation_keeps_recent_turns_as_messages():
    c = Conversation()
    c.add("GMV 最高的品牌是哪个？", "apple")
    msgs = c.messages()
    assert msgs[0] == {"role": "user", "content": "GMV 最高的品牌是哪个？"}
    assert msgs[1] == {"role": "assistant", "content": "apple"}


def test_conversation_compresses_old_turns(monkeypatch):
    monkeypatch.setattr(agent.llm, "client", _FakeClient)
    c = Conversation()
    for i in range(memory.KEEP_RECENT + 2):
        c.add(f"问题 {i}", f"回答 {i}")
    assert len(c.turns) == memory.KEEP_RECENT
    assert "摘要" in c.summary
    assert "更早对话的摘要" in c.messages()[0]["content"]


def test_long_term_notes_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "NOTES_PATH", tmp_path / "notes.json")
    assert memory.render_notes() == ""
    assert "已记住" in memory.save_note("金额用万为单位")
    assert "已经记住" in memory.save_note("金额用万为单位")   # 重复的不再保存
    assert "金额用万为单位" in memory.render_notes()
    memory.clear_notes()
    assert memory.load_notes() == []
