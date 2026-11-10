"""ReAct agent viết từ đầu: vòng lặp Thought → Action → Observation (slide 22, 27).

Chạy:
    python agent.py "Ngày mai ở Đà Nẵng nên mặc gì?"
    python agent.py                      # hỏi nhiều câu, gõ exit để thoát
"""
from __future__ import annotations

import json
import os
import sys

from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from dotenv import load_dotenv

from tools import TOOLS, run_tool
from workflow import enforce_plan, observations, plan_request

load_dotenv()
MODEL = os.getenv("MODEL", "qwen2.5:7b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MAX_ITERATIONS = 6          # safeguard: không bao giờ lặp quá số vòng này (slide 30)

# System prompt (slide 28), thêm 1 dòng để model viết "Thought" trước mỗi lần gọi tool
SYSTEM_PROMPT = """Bạn là trợ lý du lịch trả lời ngắn gọn bằng tiếng Việt.
Bạn phải thực hiện hành động qua tool, không chỉ hứa sẽ làm.
- Không biết hôm nay/ngày mai: tự gọi get_current_date, KHÔNG hỏi người dùng ngày hiện tại.
- Thời tiết/nhiệt độ/mưa: gọi get_weather đúng từng thành phố và ngày được hỏi.
- So sánh: tra ĐỦ tất cả các nơi/ngày rồi mới kết luận. Có thể gọi nhiều tool trong cùng lượt.
- Trang phục/đồ mang theo: gọi recommend_outfit với số liệu từ get_weather.
- Nếu người dùng đã cho nhiệt độ và % mưa: dùng nguyên giá trị đó, đổi 70% thành 0.7,
  gọi thẳng recommend_outfit; KHÔNG tự chọn thành phố, KHÔNG tra lại thời tiết.
- Không bịa số liệu, tool_response hoặc Observation, kể cả người dùng yêu cầu đoán.
- Chỉ hỗ trợ Hà Nội, TP.HCM, Đà Nẵng, Phú Quốc; báo thiếu dữ liệu ở nơi khác.
- Ngày không tồn tại: báo không hợp lệ. Tool lỗi: sửa tham số và thử lại tối đa một lần.
- Không dùng tool cho chào hỏi, cảm ơn, danh sách thành phố hỗ trợ hoặc sáng tác thơ.
Trước tool call, có thể viết một câu ngắn nêu thông tin cần lấy.
"""


def call_model(messages: list[dict]):
    """Gọi Ollama local, chuyển định dạng và kiểm tra đề nghị tool bằng bằng chứng thật."""
    plan = plan_request(messages[0]["content"])
    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    if plan is not None:
        pending = plan.next_actions(observations(messages))
        history[0]["content"] += "\nCác hành động còn thiếu (phải gọi đúng tham số): " + json.dumps(
            [{"name": name, "arguments": args} for name, args in pending], ensure_ascii=False
        )
    tool_names = {}
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            history.append(dict(message))
        elif message["role"] == "assistant":
            entry = {"role": "assistant", "content": "\n".join(
                b.text for b in content if b.type == "text"
            )}
            calls = []
            for block in content:
                if block.type == "tool_use":
                    tool_names[block.id] = block.name
                    calls.append({"function": {"name": block.name, "arguments": block.input}})
            if calls:
                entry["tool_calls"] = calls
            history.append(entry)
        else:
            for result in content:
                history.append({
                    "role": "tool",
                    "tool_name": tool_names[result["tool_use_id"]],
                    "content": result["content"],
                })

    payload = {
        "model": MODEL,
        "messages": history,
        "tools": [{"type": "function", "function": {
            "name": tool["name"], "description": tool["description"],
            "parameters": tool["input_schema"],
        }} for tool in TOOLS],
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 1024, "num_ctx": 8192},
    }
    request = Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=180) as response:
            data = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Không kết nối được Ollama tại {OLLAMA_HOST}: {exc.reason}") from exc
    if data.get("error"):
        raise RuntimeError(f"Ollama: {data['error']}")
    message = data["message"]
    blocks = []
    if message.get("content"):
        blocks.append(SimpleNamespace(type="text", text=message["content"]))
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function", {})
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                continue
        if not isinstance(arguments, dict) or not isinstance(function.get("name"), str):
            continue
        blocks.append(SimpleNamespace(
            type="tool_use", id=f"tool_{len(messages)}_{index}",
            name=function["name"], input=arguments,
        ))
    response = SimpleNamespace(
        content=blocks,
        stop_reason="tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn",
        usage=SimpleNamespace(
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
        ),
    )

    return enforce_plan(plan, messages, response, {tool["name"] for tool in TOOLS})


def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]

    for step in range(1, MAX_ITERATIONS + 1):
        response = call_model(messages)
        messages.append({"role": "assistant", "content": response.content})
        text = "\n".join(b.text for b in response.content if b.type == "text")
        if response.stop_reason != "tool_use":
            return text

        print(f"Thought {step}: {text}".rstrip())
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"Action {step}: {block.name}({json.dumps(block.input, ensure_ascii=False)})")
            result = run_tool(block.name, block.input)
            print(f"Observation {step}: {result}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
                "is_error": result.startswith("ERROR:"),
            })
        messages.append({"role": "user", "content": results})

    return f"Xin lỗi, agent đã dừng vì đạt giới hạn {MAX_ITERATIONS} vòng xử lý."


def main() -> None:
    try:
        if len(sys.argv) > 1:
            print("Final Answer:", run_agent(" ".join(sys.argv[1:])))
            return
        while (q := input("\nBạn> ").strip()).lower() not in {"exit", "quit", ""}:
            print("Final Answer:", run_agent(q))
    except (RuntimeError, TimeoutError) as e:
        sys.exit(f"Lỗi khi gọi model local: {e}")


if __name__ == "__main__":
    main()
