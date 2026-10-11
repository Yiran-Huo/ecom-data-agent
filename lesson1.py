# ====== 这部分直接复制 ======
FAKE_DB = {"events": 1000000}   # 假数据库：假装 events 表有 100 万行

def count_rows(table):
    """工具：数某张表有多少行"""
    return FAKE_DB[table]

TOOLS = {"count_rows": count_rows}   # 工具名 → 函数

def fake_llm(messages):
    """假大模型：没看到数据就要求调工具，看到了就回答"""
    last = messages[-1]
    if last["role"] == "user":
        return {"type": "tool_call", "name": "count_rows", "args": {"table": "events"}}
    if last["role"] == "tool":
        return {"type": "answer", "content": f"events 表一共有 {last['content']} 行。"}

def run_agent(question):
    # 对话记录：第一条是用户的问题
    messages = [{"role": "user", "content": question}]

    # 最多重复 5 次，和 messages 有几条无关
    for step in range(5):
        # 每次都把「完整的对话」发给模型（模型没有记忆）
        reply = fake_llm(messages)

        # 情况一：模型直接回答 → 结束
        if reply["type"] == "answer":
            return reply["content"]

        # 情况二：模型要调用工具 → 程序替它执行
        elif reply["type"] == "tool_call":
            func = TOOLS[reply["name"]]           # 按名字取出函数
            result = func(**reply["args"])       # 相当于 count_rows(table="events")
            messages.append({"role": "tool", "content": result})  # 结果加回对话
            print(f"第 {step + 1} 步：调用 {reply['name']}，参数 {reply['args']}，结果 {result}")

    return "超过最大步数"