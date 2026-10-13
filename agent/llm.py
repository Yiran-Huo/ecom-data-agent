import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(api_key=os.getenv("LLM_API_KEY"), base_url=os.getenv("LLM_BASE_URL"))
MODEL = os.getenv("LLM_MODEL")

if __name__ == "__main__":
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "用一句话介绍你自己"}],
    )
    print(resp.choices[0].message.content)
    print(resp.usage)   # 这次调用用了多少 token，第 7 步要用