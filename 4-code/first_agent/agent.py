import json
import os
import logging
import time
from dotenv import load_dotenv
from openai import OpenAI
from tools.get_weather import get_weather
from tools.plan_trip import plan_trip
from schema import tools_schema


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

load_dotenv()

provider = os.environ["AI_PROVIDER"].lower()

client = OpenAI(
    api_key=os.environ["AI_API_KEY"],
    base_url=os.environ["AI_BASE_URL"],
)
model = os.environ["AI_MODEL_NAME"]
pricing_period = os.getenv("DEEPSEEK_PRICING_PERIOD", "idle").lower()

DEEPSEEK_PRICING = {
    "flash": {
        "cached_input": {"idle": 0.02, "peak": 0.04},
        "uncached_input": {"idle": 1.0, "peak": 2.0},
        "output": {"idle": 4.0, "peak": 8.0},
    },
    "pro": {
        "cached_input": {"idle": 0.15, "peak": 0.30},
        "uncached_input": {"idle": 4.5, "peak": 9.0},
        "output": {"idle": 13.5, "peak": 27.0},
    },
}
MAX_AGENT_LOOPS = 10


class AgentTracer:
    """记录一次用户请求内的模型调用链。"""

    def __init__(self):
        self.loops = []
        self.total_tokens = 0
        self.total_time = 0.0
        self.tool_calls_count = 0

    def record_loop(self, loop_num: int, response, loop_time: float, tool_names: list[str]):
        usage = response.usage
        loop_info = {
            "loop": loop_num,
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "total_tokens": usage.total_tokens if usage else 0,
            "time_seconds": round(loop_time, 2),
            "tool_calls": tool_names,
            "has_tool_call": bool(tool_names),
            "usage": usage,
        }
        self.loops.append(loop_info)
        self.total_tokens += loop_info["total_tokens"]
        self.total_time += loop_time
        self.tool_calls_count += len(tool_names)

    def print_report(self):
        """输出本次用户请求的调用链报告。"""
        print("\n" + "=" * 50)
        print("📊 Agent 调用链分析报告")
        print("=" * 50)
        print(f"总循环次数: {len(self.loops)}")
        print(f"总工具调用: {self.tool_calls_count} 次")
        print(f"总 token 消耗: {self.total_tokens}")
        print(f"总耗时: {round(self.total_time, 2)}s")
        print("\n--- 每次循环详情 ---")

        for loop in self.loops:
            role = "🔧 工具调用" if loop["has_tool_call"] else "💬 最终回答"
            print(f"  循环 {loop['loop']}: {role}")
            print(
                f"    tokens: {loop['total_tokens']} "
                f"(prompt: {loop['prompt_tokens']}, "
                f"completion: {loop['completion_tokens']})"
            )
            print(f"    延迟: {loop['time_seconds']}s")
            if loop["tool_calls"]:
                print(f"    工具: {', '.join(loop['tool_calls'])}")

        output_tokens = sum(loop["completion_tokens"] for loop in self.loops)
        model_tier = "pro" if "pro" in model.lower() else "flash"
        pricing = DEEPSEEK_PRICING[model_tier]
        period = pricing_period if pricing_period in {"idle", "peak"} else "idle"
        cached_input_tokens = sum(
            getattr(loop["usage"], "prompt_cache_hit_tokens", 0)
            for loop in self.loops
            if loop.get("usage")
        )
        uncached_input_tokens = sum(
            getattr(loop["usage"], "prompt_cache_miss_tokens", 0)
            for loop in self.loops
            if loop.get("usage")
        )
        if not cached_input_tokens and not uncached_input_tokens:
            uncached_input_tokens = sum(
                loop["prompt_tokens"] for loop in self.loops
            )

        estimated_cost = (
            cached_input_tokens * pricing["cached_input"][period]
            + uncached_input_tokens * pricing["uncached_input"][period]
            + output_tokens * pricing["output"][period]
        ) / 1_000_000
        print(
            f"💰 预估成本（DeepSeek {'V4-Pro' if model_tier == 'pro' else 'V4.1-Flash'}，"
            f"{period}）: ¥{estimated_cost:.6f}"
        )
        print("=" * 50)


def run_agent() -> str:
    """运行 Agent：感知→决策→行动→观察"""
    messages = [
        {"role": "system", "content": "你是wujue的个人助手，根据用户的问题，合理调用工具获取数据，最后组织自然语言回答。"},
        # {"role": "user", "content": user_message},
    ]

    print("🌤️ wujue小助手已启动，输入 'quit' 退出\n")

    while True:
        user_input = input("👤 ").strip()
        if user_input.lower() in ["quit", "exit", "退出"]:
            print("👋 再见！")
            break
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})
        tracer = AgentTracer()
        loop_num = 0

        # === Agent 循环 ===
        while True:
            if loop_num >= MAX_AGENT_LOOPS:
                print(f"⚠️ 已达到最大 Agent 循环次数（{MAX_AGENT_LOOPS}），本轮结束。")
                tracer.print_report()
                break

            loop_num += 1
            start_time = time.time()

            # 1. 感知 + 决策：AI 决定是否调用工具
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools_schema,
            )

            loop_time = time.time() - start_time
            msg = response.choices[0].message
            tool_names = [
                tool_call.function.name
                for tool_call in (msg.tool_calls or [])
            ]
            tracer.record_loop(loop_num, response, loop_time, tool_names)

            # 2. 判断：AI 没调用工具 → 任务完成，返回结果
            if not msg.tool_calls:
                # return msg.content
                print(f"🤖 {msg.content}\n")
                messages.append(msg.model_dump(exclude_none=True))
                tracer.print_report()
                break

            # 3. 行动：AI 要调工具 → 执行工具
            messages.append(msg.model_dump(exclude_none=True))  # 先把 AI 的消息加入历史

            for tool_call in msg.tool_calls:
                # 解析工具名和参数
                func_name = tool_call.function.name
                try:
                    func_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as exc:
                    logger.error(f"❌ 工具参数解析失败: {exc}")
                    result = {
                        "error": "工具参数格式错误，无法解析 JSON。",
                        "details": str(exc),
                    }
                else:
                    print(f"🔧 调用工具: {func_name}({func_args})")

                    # 执行工具
                    result = execute_tool_with_retry(func_name, func_args)
                print(f"📦 结果: {result}")

                # 4. 观察：把工具结果返回给 AI
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            # 循环回去 → AI 看到工具结果，决定下一步

def execute_tool_with_retry(func_name: str, func_args: dict, max_retries: int = 3) -> dict:
    """
    执行工具，带指数退避重试

    Args:
        func_name: 工具名
        func_args: 工具参数
        max_retries: 最大重试次数

    Returns:
        工具结果字典（成功或错误信息）
    """
    for attempt in range(max_retries + 1):
        try:
            if func_name == "get_weather":
                result = get_weather(**func_args)
            elif func_name == "plan_trip":
                result = plan_trip(**func_args)
            else:
                result = {"error": f"未知工具: {func_name}"}
            return result

        except TimeoutError as e:
            if attempt < max_retries:
                # 指数退避：1s → 2s → 4s
                wait_time = 2 ** attempt
                logger.warning(f"⚠️ 第 {attempt+1} 次重试，等待 {wait_time}s: {e}")
                time.sleep(wait_time)
            else:
                logger.error(f"❌ 超过最大重试次数 ({max_retries})")
                return {
                    "error": f"天气服务暂时不可用，已重试 {max_retries} 次。建议稍后再试。",
                    "retry_attempts": max_retries,
                }

        except json.JSONDecodeError as e:
            # 不可重试的错误 → 直接降级
            logger.error(f"❌ 参数解析失败（不可重试）: {e}")
            return {
                "error": f"参数格式错误，无法解析: {str(e)}",
                "raw_args": str(func_args),
            }

        except TypeError as e:
            # 参数类型错误 → 不可重试，告诉 AI
            logger.error(f"❌ 参数类型错误（不可重试）: {e}")
            return {
                "error": f"工具参数错误: {str(e)}",
                "expected_params": "city (str), date (str, optional)",
            }

        except Exception as e:
            # 未知异常 → 降级，不暴露内部细节
            logger.error(f"❌ 未知异常: {type(e).__name__}: {e}")
            return {
                "error": "工具执行遇到未知错误，请稍后再试或换个方式提问。",
            }

if __name__ == "__main__":
    run_agent()