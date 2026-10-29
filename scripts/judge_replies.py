"""Score reply quality of an e2e client report with DeepSeek as judge.

Reads the JSON produced by scripts/real_e2e_client.py and scores every turn's
(user text, reply) pair on relevance and naturalness (1-5). Not part of the
default pytest suite.
"""

import argparse
import json
import os
import ssl
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).parent.parent / ".env.local"

JUDGE_PROMPT = """你是语音对话质量的评委。用户说：「{user}」
语音助手回复：「{reply}」
请从两个维度打分（1-5 整数）：
- relevance：回复对问题的相关性和信息量（答非所问=1，完全切题=5）
- naturalness：口语自然度，是否适合语音朗读（书面/机械=1，自然口语=5）
只输出 JSON：{{"relevance": N, "naturalness": N, "note": "一句简短理由"}}"""


def load_deepseek_key() -> str:
    key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY")
    if key:
        return key
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() in ("DEEPSEEK_API_KEY", "LLM_API_KEY") and value.strip():
                return value.strip().strip('"').strip("'")
    raise RuntimeError("DEEPSEEK_API_KEY not found in environment or .env.local")


def judge_turn(user_text: str, reply_text: str, api_key: str, model: str) -> dict:
    import certifi

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": JUDGE_PROMPT.format(
            user=user_text, reply=reply_text)}],
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        result = json.loads(response.read().decode("utf-8"))
    content = result["choices"][0]["message"]["content"]
    return json.loads(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="JSON report from real_e2e_client.py")
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "deepseek-flash"))
    args = parser.parse_args()

    report = json.loads(args.report.read_text())
    api_key = load_deepseek_key()
    scores = []
    for turn in report["metrics"]["turns"]:
        if not turn.get("user_text") or not turn.get("reply_text"):
            continue
        score = judge_turn(turn["user_text"], turn["reply_text"], api_key, args.model)
        scores.append({"user": turn["user_text"], "reply": turn["reply_text"][:60], **score})
    print(json.dumps(scores, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
