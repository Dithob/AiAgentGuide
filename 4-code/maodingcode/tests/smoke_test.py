"""冒烟测试：不联网、不需要 API Key，验证核心链路真的能跑通。

跑法（在 4-code/ 目录下）：
    python maodingcode/tests/smoke_test.py      或用
    python -m pytest maodingcode/tests/ -q

之所以能脱离真实模型测试，是因为 AgentLoop 依赖的是 Provider 抽象。
换一个"按剧本返回"的假 Provider，就能把 Loop 的收敛、工具执行、
权限拦截、上下文裁剪这些逻辑全部覆盖到。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# IMPORT_ROOT = maodingcode 包的父目录（即 4-code/）。
# 项目根与包目录已合并成同一层，所以这里必须上溯两层（parents[2]）而不是 parent.parent。
IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

from maodingcode.config import AppConfig, ProviderConfig, SecurityConfig  # noqa: E402
from maodingcode.core.context import ContextManager, default_system_sections  # noqa: E402
from maodingcode.core.loop import AgentLoop, StopReason  # noqa: E402
from maodingcode.core.memory import MemoryStore  # noqa: E402
from maodingcode.core.message import Message  # noqa: E402
from maodingcode.core.observability import Tracer  # noqa: E402
from maodingcode.provider.base import ChatResponse, Provider, ToolCallRequest, Usage  # noqa: E402
from maodingcode.security.policy import Policy, Verdict  # noqa: E402
from maodingcode.skills.loader import SkillLoader, parse_frontmatter  # noqa: E402
from maodingcode.tools.builtin import build_builtin_tools, read_file  # noqa: E402
from maodingcode.tools.registry import ToolRegistry  # noqa: E402


# --------------------------------------------------------------------- 测试替身
class ScriptedProvider(Provider):
    """按剧本依次返回响应，用来驱动 Loop。"""

    name = "scripted"

    def __init__(self, script: list[ChatResponse], stream: bool = False) -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.stream = stream  # 为 True 时模拟流式：把正文一次性当增量推出去

    def chat(self, messages, tools=None, on_delta=None) -> ChatResponse:
        self.calls.append(messages)
        if not self.script:
            return ChatResponse(content="（剧本用尽）")
        response = self.script.pop(0)
        if on_delta is not None and self.stream and response.content:
            on_delta(response.content)
        return response

    def supports_streaming(self) -> bool:
        return self.stream

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


def _pass(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    raise AssertionError(msg)


# --------------------------------------------------------------------- 用例
def test_tool_registry_and_read(tmp: Path) -> None:
    print("test_tool_registry_and_read")
    sample = tmp / "hello.py"
    sample.write_text("print('hi')\nprint('bye')\n", encoding="utf-8")

    registry = ToolRegistry(policy=Policy(config=SecurityConfig(workspace_root=tmp)))
    registry.register_all(build_builtin_tools(tmp))

    names = registry.names()
    for expected in ("read_file", "edit_file", "grep_files", "run_shell"):
        if expected not in names:
            _fail(f"内置工具 {expected} 未注册")
    _pass(f"注册了 {len(names)} 个内置工具")

    out = registry.dispatch("read_file", {"path": "hello.py"})
    if "print('hi')" not in out:
        _fail(f"read_file 输出异常：{out[:200]}")
    _pass("read_file 正确返回内容")


def test_edit_file_uniqueness(tmp: Path) -> None:
    print("test_edit_file_uniqueness")
    f = tmp / "dup.txt"
    f.write_text("aaa\naaa\nbbb\n", encoding="utf-8")
    registry = ToolRegistry(policy=Policy(config=SecurityConfig(workspace_root=tmp)))
    registry.register_all(build_builtin_tools(tmp))

    out = registry.dispatch("edit_file", {"path": "dup.txt", "old_string": "aaa", "new_string": "zzz"})
    if "不唯一" not in out:
        _fail(f"非唯一匹配应当被拒绝，实际：{out}")
    _pass("非唯一 old_string 被正确拒绝")

    out = registry.dispatch(
        "edit_file",
        {"path": "dup.txt", "old_string": "aaa", "new_string": "zzz", "replace_all": True},
    )
    if "zzz" not in f.read_text(encoding="utf-8"):
        _fail("replace_all 未生效")
    _pass("replace_all 生效")


def test_policy_blocks_dangerous_command(tmp: Path) -> None:
    print("test_policy_blocks_dangerous_command")
    policy = Policy(config=SecurityConfig(workspace_root=tmp, permission_mode="auto"))

    for cmd in ("rm -rf /", "git push --force origin main", "sudo rm -fr ~/data"):
        decision = policy.check("run_shell", {"command": cmd})
        if decision.verdict is not Verdict.DENY:
            _fail(f"危险命令未被拦截：{cmd} -> {decision.verdict}")
    _pass("auto 模式下危险命令仍被拒绝（黑名单优先于模式）")

    ok = policy.check("run_shell", {"command": "python -m pytest -q"})
    if ok.verdict is not Verdict.ALLOW:
        _fail(f"安全命令被误拦：{ok.reason}")
    _pass("常规测试命令正常放行")


def test_policy_readonly_mode(tmp: Path) -> None:
    print("test_policy_readonly_mode")
    # 注意：策略层是通过注册中心回填可逆性标记的，所以必须先注册工具。
    # 绕过注册直接 check("read_file") 会拿不到 read_only 标记，
    # 此时策略按 fail-closed 处理（当写操作看）——这是刻意的保守行为。
    policy = Policy(config=SecurityConfig(workspace_root=tmp, permission_mode="readonly"))
    registry = ToolRegistry(policy=policy)
    registry.register_all(build_builtin_tools(tmp))

    if policy.check("write_file", {"path": "a.txt"}).verdict is not Verdict.DENY:
        _fail("readonly 模式下写入应被拒绝")
    if policy.check("run_shell", {"command": "echo hi"}).verdict is not Verdict.DENY:
        _fail("readonly 模式下执行命令应被拒绝")
    if policy.check("read_file", {"path": "a.txt"}).verdict is not Verdict.ALLOW:
        _fail("readonly 模式下读取应放行")
    _pass("readonly 模式行为正确（读放行 / 写与命令拒绝）")


def test_path_escape_requires_confirm(tmp: Path) -> None:
    print("test_path_escape_requires_confirm")
    policy = Policy(config=SecurityConfig(workspace_root=tmp, permission_mode="suggest"))
    decision = policy.check("write_file", {"path": "../outside.txt"})
    if decision.verdict is not Verdict.ASK:
        _fail(f"工作区外写入应当需要确认，实际 {decision.verdict}")
    _pass("../ 逃逸路径被识别为工作区外")


def _build_history(ctx: ContextManager, rounds: int, tool_chars: int) -> None:
    for i in range(rounds):
        ctx.add(Message.user(f"第 {i} 个问题"))
        ctx.add(Message.tool_result(f"call_{i}", "grep_files", f"命中文件{i}：" + "x" * tool_chars))
        ctx.add(Message.assistant(f"第 {i} 个回答"))


def test_context_folding() -> None:
    print("test_context_folding")
    # 12 轮 × 2000 字符工具输出 ≈ 6000 token；预算 = 6000*0.75 ≈ 4497。
    # 只靠折叠就应该压回预算内，不应触发整轮丢弃。
    ctx = ContextManager(
        workspace_root=Path.cwd(),
        max_context_tokens=6_000,
        keep_recent_turns=1,
        max_tool_output_chars=10_000,
    )
    ctx.set_base_sections({"identity": "你是一个测试助手。"})
    _build_history(ctx, rounds=12, tool_chars=2_000)

    payload, stats = ctx.build()
    if stats.folded_messages == 0:
        _fail(f"超预算时应当折叠历史工具输出（history_tokens={stats.history_tokens}）")
    _pass(f"折叠了 {stats.folded_messages} 条工具输出")
    if stats.dropped_messages != 0:
        _fail(f"只靠折叠就够用，不应丢消息，实际丢了 {stats.dropped_messages} 条")
    _pass("仅折叠即可回到预算内，未丢消息")
    if stats.history_tokens > 4_497:
        _fail(f"折叠后仍超预算：{stats.history_tokens}")
    _pass(f"折叠后历史 ≈{stats.history_tokens} token")

    if not any("已折叠" in m.get("content", "") for m in payload):
        _fail("折叠占位符未出现在最终 payload 中")
    _pass("折叠占位符已进入 payload")

    # 最近 1 轮必须保持完整——模型要靠它接着干活
    tail = [m for m in payload if m.get("role") == "tool"][-1]
    if "已折叠" in tail["content"]:
        _fail("最近一轮的工具输出不应被折叠")
    _pass("最近一轮保持完整未被折叠")


def test_context_dropping() -> None:
    print("test_context_dropping")
    # 把 keep_recent_turns 设得比总轮数还大 -> 折叠被完全禁止，
    # 只能靠整轮丢弃来降预算。这条路径专门守住
    # "只丢一轮就不管了"这个 bug。
    ctx = ContextManager(
        workspace_root=Path.cwd(),
        max_context_tokens=6_000,
        keep_recent_turns=50,
        max_tool_output_chars=10_000,
    )
    ctx.set_base_sections({"identity": "你是一个测试助手。"})
    _build_history(ctx, rounds=12, tool_chars=2_000)

    payload, stats = ctx.build()
    if stats.folded_messages != 0:
        _fail("keep_recent_turns 覆盖全部轮次时不应折叠")
    _pass("折叠已被最近轮保护挡住")
    if stats.dropped_messages == 0:
        _fail(f"必须整轮丢弃才能装进预算，实际没丢（history_tokens={stats.history_tokens}）")
    _pass(f"丢弃了最早 {stats.dropped_messages} 条消息（{stats.dropped_messages // 3} 轮）")
    if stats.history_tokens > 4_497:
        _fail(
            f"丢弃后仍超预算：{stats.history_tokens}。"
            f"说明只丢了一轮就返回，没有持续丢弃直到装得下。"
        )
    _pass(f"丢弃后历史 ≈{stats.history_tokens} token，已进入预算")

    # 最后一轮的用户消息必须还在，否则模型看不到当前问题
    if not any(m.get("role") == "user" and "第 11 个问题" in m.get("content", "") for m in payload):
        _fail("最后一轮的用户消息被误删")
    _pass("最后一轮完整保留")
    # 被丢弃的轮次不能残留在 payload 里（否则说明 cut 算错了）
    if any("第 0 个问题" in m.get("content", "") for m in payload):
        _fail("最早一轮应已被丢弃，但仍出现在 payload 中")
    _pass("被丢弃的轮次未出现在 payload 中")


def test_loop_completes_with_tool_call(tmp: Path) -> None:
    print("test_loop_completes_with_tool_call")
    script = [
        ChatResponse(
            tool_calls=[
                ToolCallRequest(
                    id="c1",
                    name="write_file",
                    arguments={"path": "out/report.md", "content": "# 报告\n内容\n"},
                    raw_arguments='{"path": "out/report.md", "content": "# 报告\\n内容\\n"}',
                )
            ],
            usage=Usage(prompt_tokens=120, completion_tokens=30, total_tokens=150),
            finish_reason="tool_calls",
        ),
        ChatResponse(
            content="已生成 out/report.md。",
            usage=Usage(prompt_tokens=200, completion_tokens=12, total_tokens=212),
            finish_reason="stop",
        ),
    ]
    provider = ScriptedProvider(script)
    policy = Policy(config=SecurityConfig(workspace_root=tmp, permission_mode="auto"))
    registry = ToolRegistry(policy=policy)
    registry.register_all(build_builtin_tools(tmp))

    ctx = ContextManager(workspace_root=tmp, max_context_tokens=32_000)
    ctx.set_base_sections(default_system_sections(tmp))
    tracer = Tracer()

    events: list[str] = []
    loop = AgentLoop(
        provider=provider,
        context=ctx,
        registry=registry,
        policy=policy,
        tracer=tracer,
        max_steps=5,
        emit=lambda e: events.append(e.kind),
    )
    result = loop.run("帮我写一份报告")

    if result.reason is not StopReason.COMPLETED:
        _fail(f"应当正常收敛，实际 {result.reason}: {result.content}")
    target = tmp / "out" / "report.md"
    if not target.is_file():
        _fail("工具未真正写入文件")
    if "已生成" not in result.content:
        _fail("最终回答未透出")
    _pass(f"Loop 收敛，写入了 {target.relative_to(tmp)}")

    if result.steps != 2 or result.tool_calls_made != 1:
        _fail(f"步数/工具调用统计异常：{result.steps}/{result.tool_calls_made}")
    _pass("步数与工具调用计数正确")

    s = tracer.summary()
    if s["total_tokens"] != 362 or s["tool_calls"] != 1:
        _fail(f"Tracer 统计异常：{s}")
    _pass(f"Tracer 统计正确：{s['steps']} 步 / {s['total_tokens']} tokens")

    for kind in ("step_start", "model_response", "tool_start", "tool_result", "stopped"):
        if kind not in events:
            _fail(f"缺少事件 {kind}，实际 {events}")
    _pass("事件流完整")


def test_loop_stops_on_max_steps(tmp: Path) -> None:
    print("test_loop_stops_on_max_steps")
    # 永远请求工具调用，验证熔断与步数上限
    def always_tool() -> ChatResponse:
        return ChatResponse(
            tool_calls=[ToolCallRequest(id="c", name="list_dir", arguments={"path": "."})],
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    provider = ScriptedProvider([always_tool() for _ in range(20)])
    policy = Policy(config=SecurityConfig(workspace_root=tmp, permission_mode="auto"))
    registry = ToolRegistry(policy=policy)
    registry.register_all(build_builtin_tools(tmp))
    ctx = ContextManager(workspace_root=tmp, max_context_tokens=32_000)

    loop = AgentLoop(provider, ctx, registry, policy, Tracer(), max_steps=4)
    result = loop.run("无限循环")
    if result.reason not in {StopReason.MAX_STEPS, StopReason.FAILURE_LOOP}:
        _fail(f"应当被步数/熔断终止，实际 {result.reason}")
    _pass(f"循环被正确终止：{result.reason.value}")


def test_loop_rejects_unauthorized_write(tmp: Path) -> None:
    print("test_loop_rejects_unauthorized_write")
    script = [
        ChatResponse(
            tool_calls=[
                ToolCallRequest(
                    id="c1",
                    name="run_shell",
                    arguments={"command": "rm -rf /"},
                    raw_arguments='{"command": "rm -rf /"}',
                )
            ],
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
        ChatResponse(content="好的，我不执行这个命令。", usage=Usage(total_tokens=20)),
    ]
    provider = ScriptedProvider(script)
    policy = Policy(config=SecurityConfig(workspace_root=tmp, permission_mode="auto"))
    registry = ToolRegistry(policy=policy)
    registry.register_all(build_builtin_tools(tmp))
    ctx = ContextManager(workspace_root=tmp, max_context_tokens=32_000)

    loop = AgentLoop(provider, ctx, registry, policy, Tracer(), max_steps=3)
    result = loop.run("删掉根目录")

    if result.reason is not StopReason.COMPLETED:
        _fail("应当继续对话而非崩溃")
    tool_msgs = [m for m in ctx.messages if m.role == "tool"]
    if not tool_msgs or "权限拒绝" not in tool_msgs[0].content:
        _fail(f"拒绝结果未回灌给模型：{[m.content for m in tool_msgs]}")
    _pass("危险命令被拦截并把拒绝原因回灌给模型")


def test_invalid_tool_json_is_self_healing(tmp: Path) -> None:
    print("test_invalid_tool_json_is_self_healing")
    script = [
        ChatResponse(
            tool_calls=[
                ToolCallRequest(id="c1", name="read_file", arguments={}, raw_arguments="{bad json")
            ],
            usage=Usage(total_tokens=10),
        ),
        ChatResponse(content="我重新发起调用。", usage=Usage(total_tokens=10)),
    ]
    provider = ScriptedProvider(script)
    policy = Policy(config=SecurityConfig(workspace_root=tmp, permission_mode="auto"))
    registry = ToolRegistry(policy=policy)
    registry.register_all(build_builtin_tools(tmp))
    ctx = ContextManager(workspace_root=tmp, max_context_tokens=32_000)
    loop = AgentLoop(provider, ctx, registry, policy, Tracer(), max_steps=3)
    result = loop.run("读文件")

    if result.reason is not StopReason.COMPLETED:
        _fail("JSON 解析失败不应中断循环")
    tool_msg = [m for m in ctx.messages if m.role == "tool"][0]
    if "合法 JSON" not in tool_msg.content:
        _fail(f"未把 JSON 错误回灌：{tool_msg.content}")
    _pass("非法 JSON 被转成可自愈的提示回灌")


def test_tool_schema_index_mode(tmp: Path) -> None:
    print("test_tool_schema_index_mode")
    registry = ToolRegistry(policy=Policy(config=SecurityConfig(workspace_root=tmp)), index_mode_threshold=3)
    registry.register_all(build_builtin_tools(tmp))
    schemas = registry.schemas()
    names = [s["function"]["name"] for s in schemas]
    if names != ["list_tools", "call_tool"]:
        _fail(f"工具过多时应切换到索引模式，实际 {names}")
    _pass("超过阈值自动切换到索引模式")

    out = registry.dispatch("list_tools", {"keyword": "read"})
    if "read_file" not in out:
        _fail(f"list_tools 检索失败：{out[:200]}")
    _pass("list_tools 可按关键词检索")

    # 放一个真实文件，确保转发后确实执行到了真实的 list_dir 逻辑
    (tmp / "marker.txt").write_text("hi", encoding="utf-8")
    out = registry.dispatch("call_tool", {"tool": "list_dir", "arguments": {"path": "."}})
    if "marker.txt" not in out:
        _fail(f"call_tool 转发失败：{out[:300]}")
    _pass("call_tool 正确转发到真实工具")

    # 转发时参数类型不对，也要能被拦住而不是崩掉
    out = registry.dispatch("call_tool", {"tool": "list_dir", "arguments": "不是对象"})
    if "必须是对象" not in out:
        _fail(f"非法 arguments 未被拦截：{out[:200]}")
    _pass("call_tool 拦截非法 arguments")


def test_skills_loading(tmp: Path) -> None:
    print("test_skills_loading")
    skill_dir = tmp / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 演示用技能\n---\n\n# 步骤\n\n1. 先做 A\n2. 再做 B\n",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp, ("skills",))
    skills = loader.discover()
    if not skills or skills[0].name != "demo":
        _fail(f"技能未加载：{skills}")
    _pass("发现技能 demo")

    index = loader.index_for_prompt()
    if "demo" not in index or "先做 A" in index:
        _fail("索引里不应包含技能正文")
    _pass("系统提示只注入索引，不注入正文")

    rendered = loader.get("demo").render()
    if "先做 A" not in rendered:
        _fail("use_skill 应返回正文")
    _pass("use_skill 按需加载正文")

    meta, body = parse_frontmatter("---\nname: x\ndescription: y\n---\ncontent")
    if meta.get("name") != "x" or body.strip() != "content":
        _fail(f"frontmatter 解析错误：{meta} / {body!r}")
    _pass("frontmatter 解析正确")


def test_memory_roundtrip(tmp: Path) -> None:
    print("test_memory_roundtrip")
    store = MemoryStore(workspace_root=tmp)
    path = store.ensure_project_memory()
    if not path.is_file():
        _fail("MAODING.md 未创建")
    store.append_note("测试前必须先跑 lint", section="构建命令")
    text = store.load_project_memory()
    if "测试前必须先跑 lint" not in text:
        _fail("追加的记录未生效")
    _pass("项目约定可追加并读回")

    store.save_session("t1", [Message.user("你好"), Message.assistant("在的")])
    msgs = store.load_session("t1")
    if len(msgs) != 2 or msgs[1].content != "在的":
        _fail(f"会话恢复失败：{msgs}")
    _pass("会话可保存并恢复")


def test_provider_payload_shape() -> None:
    print("test_provider_payload_shape")
    from maodingcode.provider.openai_compat import OpenAICompatProvider

    p = OpenAICompatProvider(ProviderConfig(api_key="k", base_url="https://x/v1", model="m"))
    if p._endpoint != "https://x/v1/chat/completions":
        _fail(f"endpoint 拼接错误：{p._endpoint}")
    _pass("endpoint 拼接正确")

    resp = p._parse(
        {
            "choices": [
                {
                    "message": {"content": None, "tool_calls": [{"id": "1", "function": {"name": "f", "arguments": '{"a":1}'}}]},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11, "prompt_cache_hit_tokens": 2},
        }
    )
    if not resp.wants_tools or resp.tool_calls[0].arguments != {"a": 1}:
        _fail(f"tool_calls 解析错误：{resp}")
    if resp.usage.cached_tokens != 2:
        _fail("缓存 token 未解析")
    _pass("响应解析正确（含 tool_calls 与缓存 token）")

    broken = p._parse({"choices": [{"message": {"tool_calls": [{"id": "1", "function": {"name": "f", "arguments": "{oops"}}]}}]})
    if broken.tool_calls[0].raw_arguments != "{oops" or broken.tool_calls[0].arguments:
        _fail("非法 JSON 参数应保留原文且参数为空")
    _pass("非法参数 JSON 被保留为原文")


def test_config_loading(tmp: Path) -> None:
    print("test_config_loading")
    import os

    (tmp / "maodingcode.toml").write_text(
        "\n".join(
            [
                "[model]",
                'name = "my-model"',
                "context_window = 64000",
                "[agent]",
                "max_steps = 7",
                "[security]",
                'permission_mode = "readonly"',
                'command_denylist = ["terraform destroy"]',
            ]
        ),
        encoding="utf-8",
    )
    os.environ["AI_API_KEY"] = "test-key"
    os.environ["AI_BASE_URL"] = "https://example.com/v1"
    try:
        from maodingcode.config import load_config

        cfg = load_config(workspace=tmp)
    finally:
        os.environ.pop("AI_API_KEY", None)
        os.environ.pop("AI_BASE_URL", None)

    if cfg.provider.model != "my-model" or cfg.max_steps != 7:
        _fail(f"TOML 配置未生效：{cfg.provider.model}/{cfg.max_steps}")
    if cfg.provider.context_window != 64000:
        _fail("context_window 未生效")
    if "terraform destroy" not in cfg.security.command_denylist:
        _fail("自定义黑名单未生效")
    if cfg.security.workspace_root != tmp.resolve():
        _fail("工作区根目录解析错误")
    _pass("配置文件 + 环境变量合并正确")

    from maodingcode.config import dump_config

    dumped = dump_config(cfg)
    if "test-key" in dumped:
        _fail("配置导出未脱敏，密钥泄漏")
    _pass("配置导出已脱敏")


def test_overrides_do_not_clobber_env(tmp: Path) -> None:
    """回归：命令行没传 --api-key 时，不能把 .env / 环境变量里的密钥覆盖成 None。

    这是一个真实踩过的坑：早期 `_apply_overrides` 按"键在不在集合里"挑字段，
    于是未传参数对应的 None 也被塞进 replace()，把已经读到的密钥清空了。
    现象是"明明配了 .env 却报缺少 API Key"——极难排查。
    """
    print("test_overrides_do_not_clobber_env")
    import os

    from maodingcode.config import load_config

    os.environ["AI_API_KEY"] = "from-env-key"
    os.environ["AI_BASE_URL"] = "https://env.example.com/v1"
    os.environ["AI_MODEL_NAME"] = "env-model"
    try:
        # 模拟 CLI 的调用方式：所有未传的参数都是 None / 空
        cfg = load_config(
            workspace=tmp,
            overrides={
                "workspace": str(tmp),
                "model": None,
                "base_url": None,
                "api_key": None,
                "permission_mode": None,
                "max_steps": None,
                "debug": False,
                "stream": True,
            },
        )
    finally:
        for k in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL_NAME"):
            os.environ.pop(k, None)

    if cfg.provider.api_key != "from-env-key":
        _fail(f"密钥被 None 覆盖了：{cfg.provider.api_key!r}")
    if cfg.provider.base_url != "https://env.example.com/v1":
        _fail(f"base_url 被 None 覆盖了：{cfg.provider.base_url!r}")
    if cfg.provider.model != "env-model":
        _fail(f"model 被 None 覆盖了：{cfg.provider.model!r}")
    _pass("未传的命令行参数不会覆盖环境变量里的值")

    # 传了值就必须生效（不能矫枉过正）
    os.environ["AI_API_KEY"] = "from-env-key"
    try:
        cfg2 = load_config(
            workspace=tmp,
            overrides={"workspace": str(tmp), "api_key": "from-cli", "model": "cli-model"},
        )
    finally:
        os.environ.pop("AI_API_KEY", None)
    if cfg2.provider.api_key != "from-cli" or cfg2.provider.model != "cli-model":
        _fail(f"显式传入的参数未生效：{cfg2.provider.api_key!r}/{cfg2.provider.model!r}")
    _pass("显式传入的参数正常覆盖")

    # stream=False 是有效值，必须能覆盖（这里容易被真值判断吃掉）
    os.environ["AI_API_KEY"] = "k"
    try:
        cfg3 = load_config(workspace=tmp, overrides={"workspace": str(tmp), "stream": False})
    finally:
        os.environ.pop("AI_API_KEY", None)
    if cfg3.stream or cfg3.provider.stream:
        _fail("stream=False 未生效（被真值判断吃掉了）")
    _pass("stream=False 能正确覆盖（且已同步到 provider）")


def main() -> int:
    print(f"\nMaoDingCode 冒烟测试\n{'=' * 52}")
    cases = [
        test_tool_registry_and_read,
        test_edit_file_uniqueness,
        test_policy_blocks_dangerous_command,
        test_policy_readonly_mode,
        test_path_escape_requires_confirm,
        test_skills_loading,
        test_memory_roundtrip,
    ]
    no_temp = [test_context_folding, test_context_dropping, test_provider_payload_shape]

    passed = 0
    try:
        for fn in cases:
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d).resolve())
            passed += 1
        for fn in no_temp:
            fn()
            passed += 1

        for fn in (
            test_loop_completes_with_tool_call,
            test_loop_stops_on_max_steps,
            test_loop_rejects_unauthorized_write,
            test_invalid_tool_json_is_self_healing,
            test_tool_schema_index_mode,
            test_config_loading,
            test_overrides_do_not_clobber_env,
        ):
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d).resolve())
            passed += 1
    except AssertionError:
        print(f"\n失败。已通过 {passed} 个用例。")
        return 1

    print(f"{'=' * 52}\n全部 {passed} 个用例通过 ✅\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
