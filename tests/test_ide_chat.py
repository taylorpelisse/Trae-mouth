from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.accounts.models import build_device
from app.upstream import ide_chat


def test_text_accumulator_handles_delta_and_cumulative() -> None:
    acc = ide_chat.TextAccumulator()
    assert acc.add("The") == "The"
    assert acc.add(" user") == " user"
    assert acc.text == "The user"
    assert acc.add("The user wants") == " wants"
    assert acc.add("The user") == ""
    assert acc.text == "The user wants"


def test_text_accumulator_delta_keeps_repeated_chunks() -> None:
    acc = ide_chat.TextAccumulator()
    assert acc.add_delta("a") == "a"
    assert acc.add_delta("a") == "a"
    assert acc.text == "aa"
    assert acc.add_delta("bc") == "bc"
    assert acc.text == "aabc"


def test_tool_block_filter_exposes_incomplete_payload() -> None:
    flt = ide_chat.ToolBlockFilter()
    visible, calls = flt.feed(
        'before<trae_mouth_tool_call>{"name":"get_weather","arguments":{"city":"Bei'
    )
    assert visible == "before"
    assert calls == []
    assert flt.incomplete.startswith('{"name":"get_weather"')

    visible, calls = flt.feed(
        'before<trae_mouth_tool_call>{"name":"get_weather","arguments":{"city":"Beijing"}}'
        "</trae_mouth_tool_call>"
    )
    assert visible == "before"
    assert calls == [{"name": "get_weather", "arguments": {"city": "Beijing"}}]
    assert flt.incomplete == ""


def test_tool_block_filter_recovers_legacy_text_call() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "I will read it now.\n"
        '[client tool call] id=call_1 name=read_file arguments={"path":"/tmp/a.txt"}\n'
    )
    visible, calls = flt.feed(text)
    assert visible == "I will read it now.\n"
    assert calls == [{"name": "read_file", "arguments": {"path": "/tmp/a.txt"}}]


def test_tool_block_filter_recovers_native_dsml_call() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "<｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="read_big_file">\n'
        '<｜｜DSML｜｜ parameter name="arguments" string="false">{}</｜｜DSML｜｜ parameter>\n'
        "</｜｜DSML｜｜ invoke>\n"
        "</｜｜DSML｜｜ calls>"
    )
    visible, calls = flt.feed(text)
    assert visible == ""
    assert calls == [{"name": "read_big_file", "arguments": {}}]


def test_tool_block_filter_recovers_dsml_tool_calls_tag() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "<||DSML||tool_calls>\n"
        '<||DSML||invoke name="Read">\n'
        '<||DSML||parameter name="file_path" string="true">a.py</||DSML||parameter>\n'
        "</||DSML||invoke>\n"
        "</||DSML||tool_calls>"
    )
    visible, calls = flt.feed(text)
    assert visible == ""
    assert calls == [{"name": "Read", "arguments": {"file_path": "a.py"}}]


def test_tool_block_filter_recovers_spaced_dsml_marker() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "< | | DSML | | tool_calls>\n"
        '< | | DSML | | invoke name="Read">\n'
        '< | | DSML | | parameter name="file_path" string="true">b.py</ | | DSML | | parameter>\n'
        "</ | | DSML | | invoke>\n"
        "</ | | DSML | | tool_calls>"
    )
    visible, calls = flt.feed(text)
    assert visible == ""
    assert calls == [{"name": "Read", "arguments": {"file_path": "b.py"}}]


def test_tool_block_filter_holds_partial_dsml_marker() -> None:
    flt = ide_chat.ToolBlockFilter()
    visible, calls = flt.feed("先看一下 <||DSML||tool_")
    assert visible == "先看一下 "
    assert calls == []

    visible, calls = flt.feed(
        "先看一下 <||DSML||tool_calls>"
        '<||DSML||invoke name="Read">'
        '<||DSML||parameter name="file_path" string="true">c.py</||DSML||parameter>'
        "</||DSML||invoke></||DSML||tool_calls>"
    )
    assert visible == "先看一下 "
    assert calls == [{"name": "Read", "arguments": {"file_path": "c.py"}}]


def test_tool_block_filter_holds_fullwidth_dsml_marker_split_across_chunks() -> None:
    # DeepSeek 实际输出的是全角竖线，标记前半截经常单独成一个数据块
    flt = ide_chat.ToolBlockFilter()
    visible, calls = flt.feed("我来看一下 <｜｜DSML｜｜")
    assert visible == "我来看一下 "
    assert calls == []

    visible, calls = flt.feed(
        "我来看一下 <｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="Bash">'
        '<｜｜DSML｜｜ parameter name="arguments" string="false">'
        '{"command":"dir"}</｜｜DSML｜｜ parameter>\n'
        "</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>"
    )
    assert visible == "我来看一下 "
    assert calls == [{"name": "Bash", "arguments": {"command": "dir"}}]


@pytest.mark.parametrize(
    "opening",
    [
        "<｜｜DSML｜｜ calls>",
        "<||DSML||tool_calls>",
        "<|DSML|calls>",
        "< | | DSML | | calls>",
    ],
)
def test_tool_block_filter_holds_every_split_of_the_opening_tag(opening) -> None:
    # 开头标记在任意位置被数据块切开，都不能漏出记号
    body = (
        '<｜｜DSML｜｜ invoke name="Read">'
        '<｜｜DSML｜｜ parameter name="file_path" string="true">d.py'
        "</｜｜DSML｜｜ parameter></｜｜DSML｜｜ invoke>"
    )
    for cut in range(1, len(opening)):
        flt = ide_chat.ToolBlockFilter()
        visible, calls = flt.feed("前缀 " + opening[:cut])
        assert visible == "前缀 ", f"{opening!r} split at {cut} leaked {visible!r}"
        assert calls == []

        visible, calls = flt.feed("前缀 " + opening + body + "</｜｜DSML｜｜ calls>")
        assert visible == "前缀 "
        assert calls == [{"name": "Read", "arguments": {"file_path": "d.py"}}]


def test_tool_block_filter_recovers_native_tool_call_json() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "checking\n"
        '<tool_call>{"name":"get_weather","arguments":{"city":"Beijing"}}</tool_call>'
    )
    visible, calls = flt.feed(text)
    assert visible == "checking\n"
    assert calls == [{"name": "get_weather", "arguments": {"city": "Beijing"}}]


def test_tool_block_filter_can_disable_tool_parsing() -> None:
    flt = ide_chat.ToolBlockFilter(allow_tool_calls=False)
    text = '<trae_mouth_tool_call>{"name":"x","arguments":{}}</trae_mouth_tool_call>'
    visible, calls = flt.feed(text)
    assert visible == text
    assert calls == []


def test_tool_result_failed_detects_json_error() -> None:
    message = {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "run",
        "content": '{"status":"failed","message":"boom"}',
    }
    converted = ide_chat.convert_messages([message])
    text = converted[0]["content"][0]["text"]
    assert "status=failed" in text


def test_convert_messages_preserves_assistant_reasoning() -> None:
    converted = ide_chat.convert_messages(
        [
            {
                "role": "assistant",
                "content": "done",
                "reasoning_content": "the reasoning chain",
            }
        ]
    )
    assert converted[0]["reasoning_content"] == "the reasoning chain"
    assert converted[0]["reasoning"] == "the reasoning chain"


def test_parse_xml_tool_calls() -> None:
    block = (
        "<function=Read><parameter=file_path>/tmp/a.py</parameter>"
        "<parameter=offset>60</parameter><parameter=limit>11</parameter></function>"
    )
    calls = ide_chat._parse_xml_tool_calls(block)
    assert calls == [
        {
            "name": "Read",
            "arguments": {"file_path": "/tmp/a.py", "offset": "60", "limit": "11"},
        }
    ]


def test_parse_xml_tool_calls_multiple_functions() -> None:
    block = (
        "<function=Read><parameter=file_path>a.py</parameter></function>\n"
        "<function=Grep><parameter=pattern>foo</parameter></function>"
    )
    calls = ide_chat._parse_xml_tool_calls(block)
    assert [call["name"] for call in calls] == ["Read", "Grep"]
    assert calls[1]["arguments"] == {"pattern": "foo"}


def test_tool_call_block_parses_xml_body() -> None:
    """<tool_call> 包裹的 XML 调用也要能解析出来。"""
    wrapped = (
        "<tool_call><function=Write><parameter=file_path>/tmp/a.txt</parameter>"
        "<parameter=content>hello</parameter></function></tool_call>"
    )
    visible, calls, incomplete, truncated = ide_chat._extract_native_tool_calls(
        wrapped, final=True
    )
    assert visible.strip() == ""
    assert calls == [
        {
            "name": "Write",
            "arguments": {"file_path": "/tmp/a.txt", "content": "hello"},
        }
    ]
    assert incomplete == ""
    assert truncated is False


def test_tool_call_block_tolerates_namespaced_close() -> None:
    """Doubao 会写成 </seed:tool_call>，闭合标签允许命名空间前缀。"""
    wrapped = (
        "<tool_call><function=read_file><parameter=path>/tmp/a.txt</parameter>"
        "</function></seed:tool_call>"
    )
    visible, calls, incomplete, truncated = ide_chat._extract_native_tool_calls(
        wrapped, final=True
    )
    assert visible.strip() == ""
    assert calls == [{"name": "read_file", "arguments": {"path": "/tmp/a.txt"}}]
    assert incomplete == ""
    assert truncated is False


def test_tool_call_block_tolerates_missing_angle() -> None:
    """glm-5 会漏写成 <tool_call>function=read_file>...。"""
    wrapped = (
        "<tool_call>function=read_file><parameter=path>/tmp/a.txt</parameter>"
        "</function></tool_call>"
    )
    visible, calls, incomplete, truncated = ide_chat._extract_native_tool_calls(
        wrapped, final=True
    )
    assert visible.strip() == ""
    assert calls == [{"name": "read_file", "arguments": {"path": "/tmp/a.txt"}}]
    assert incomplete == ""
    assert truncated is False


def test_tool_call_block_without_closing_tag() -> None:
    """glm 系列经常只写 <tool_call> 开头，闭合标签整段漏掉。"""
    wrapped = (
        "<tool_call><function=read_file><parameter=path>/tmp/a.txt</parameter>"
        "</function>"
    )
    visible, calls, incomplete, truncated = ide_chat._extract_native_tool_calls(
        wrapped, final=True
    )
    assert visible.strip() == ""
    assert calls == [{"name": "read_file", "arguments": {"path": "/tmp/a.txt"}}]
    assert incomplete == ""
    assert truncated is False


def test_tool_call_block_with_dsml_invoke_mix() -> None:
    """glm-5.1 会把 DSML 标记和裸 parameter 混着写。"""
    wrapped = (
        "<tool_call><｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="read_file">'
        "<parameter=path>/tmp/a.txt</parameter></function></tool_call>"
    )
    visible, calls, incomplete, truncated = ide_chat._extract_native_tool_calls(
        wrapped, final=True
    )
    assert "<tool_call>" not in visible
    assert [call["name"] for call in calls] == ["read_file"]
    assert calls[0]["arguments"] == {"path": "/tmp/a.txt"}


def test_tool_call_block_truncated_argument_stays_incomplete() -> None:
    """参数写到一半就断的，不能当成合法调用下发。"""
    wrapped = (
        "<tool_call><function=read_file><parameter=path>/tmp/a.txt</parameter>"
        "<parameter=content>hel"
    )
    visible, calls, incomplete, truncated = ide_chat._extract_native_tool_calls(
        wrapped, final=True
    )
    assert calls == []
    assert incomplete.startswith("<tool_call>")


def test_parse_xml_tool_calls_name_stops_at_angle() -> None:
    """glm-5.3 漏 < 时名字不能把后一个标签吃进去。"""
    block = "<function=read_file<parameter=path>/tmp/a.txt</parameter></function>"
    calls = ide_chat._parse_xml_tool_calls(block)
    assert calls == [{"name": "read_file", "arguments": {"path": "/tmp/a.txt"}}]


def test_dsml_block_with_bare_xml_arguments() -> None:
    """glm-4.7-auto 只写 DSML 开头，参数和收尾用裸 XML。"""
    block = (
        "<｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="read_file">'
        "<parameter=path>/tmp/a.txt</parameter></function>"
    )
    visible, calls, incomplete, truncated = ide_chat._extract_native_tool_calls(
        block, final=True
    )
    assert visible.strip() == ""
    assert calls == [{"name": "read_file", "arguments": {"path": "/tmp/a.txt"}}]
    assert incomplete == ""


def test_tool_call_block_without_closing_tag_multiple_calls() -> None:
    """漏了 </tool_call> 时，同一段里的多个调用也要全部解析。"""
    wrapped = (
        "<tool_call>\n"
        "<function=read_file><parameter=path>a.txt</parameter></function>\n"
        "<function=read_file><parameter=path>b.txt</parameter></function>"
    )
    visible, calls, incomplete, truncated = ide_chat._extract_native_tool_calls(
        wrapped, final=True
    )
    assert visible == ""
    assert calls == [
        {"name": "read_file", "arguments": {"path": "a.txt"}},
        {"name": "read_file", "arguments": {"path": "b.txt"}},
    ]
    assert incomplete == ""


def test_convert_messages_multiple_tool_calls_and_results() -> None:
    """一轮多个调用、多个结果：每个结果都要带上自己的 id/name/参数。"""
    messages = [
        {"role": "user", "content": "读 a.txt 和 b.txt"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                },
                {
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "AAA"},
        {"role": "tool", "tool_call_id": "call_b", "content": "BBB"},
    ]
    converted = ide_chat.convert_messages(messages)
    results = [
        item["content"][0]["text"]
        for item in converted
        if item["role"] == "user" and "Client tool result" in item["content"][0]["text"]
    ]
    assert len(results) == 2
    assert "id=call_a" in results[0] and "<parameter=path>a.txt</parameter>" in results[0]
    assert "AAA" in results[0]
    assert "id=call_b" in results[1] and "<parameter=path>b.txt</parameter>" in results[1]
    assert "BBB" in results[1]
    # 助手那轮的调用记录不回显成正文，避免模型照抄
    assert not any(
        "read_file" in (item.get("content") or [{}])[0].get("text", "")
        for item in converted
        if item["role"] == "assistant"
    )


def test_tool_call_bare_name_format() -> None:
    """glm-5.3 偶尔写成 <tool_call>read_file<path>v</parameter></function>。"""
    wrapped = "<tool_call>read_file<path>/tmp/a.txt</parameter></function>"
    visible, calls, incomplete, truncated = ide_chat._extract_native_tool_calls(
        wrapped, final=True
    )
    assert visible == ""
    assert calls == [{"name": "read_file", "arguments": {"path": "/tmp/a.txt"}}]
    assert incomplete == ""


def test_parse_xml_tool_calls_bare_multiple() -> None:
    block = (
        "read_file<path>a.txt</parameter></function>"
        "read_file<path>b.txt</parameter></function>"
    )
    calls = ide_chat._parse_xml_tool_calls(block)
    assert calls == [
        {"name": "read_file", "arguments": {"path": "a.txt"}},
        {"name": "read_file", "arguments": {"path": "b.txt"}},
    ]


def test_parse_xml_tool_calls_bare_requires_parameter() -> None:
    """只有标记、没有参数的散文不能当成调用。"""
    assert ide_chat._parse_xml_tool_calls("<tool_call>read_file 一下") == []


def test_tool_call_bare_variants() -> None:
    """glm-5.3 抓到过的几种裸写法都要收成正常调用，且不漏标记。"""
    samples = [
        "<tool_call>read_file<parameter=path>/tmp/a.txt</parameter></function></arg_value>",
        "<tool_call>read_file><parameter=path>/tmp/a.txt</parameter></function>",
        "<tool_call>read_file  path=/tmp/a.txt</arg_value>parameter</arg_value>>"
        "<function_calls>[:function_call_end]",
        '<tool_call>read_file(path="/tmp/a.txt")</arg_value>parameter>value</parameter>',
    ]
    for sample in samples:
        visible, calls, incomplete, _ = ide_chat._extract_native_tool_calls(
            sample, final=True
        )
        assert calls == [
            {"name": "read_file", "arguments": {"path": "/tmp/a.txt"}}
        ], sample
        assert visible == "", sample
        assert incomplete == "", sample


def test_dsml_multi_block_mixed_arguments() -> None:
    """先完整 DSML 调用、后裸参数调用，两块都要收走。"""
    text = (
        "<｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="read_file">'
        '<｜｜DSML｜｜ parameter name="path" string="true">/tmp/b.txt'
        "</｜｜DSML｜｜ parameter></｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>\n"
        "<｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="read_file">'
        "<parameter=path>/tmp/a.txt</parameter></function></tool_call>"
    )
    visible, calls, incomplete, truncated = ide_chat._extract_native_tool_calls(
        text, final=True
    )
    assert sorted(call["arguments"]["path"] for call in calls) == [
        "/tmp/a.txt",
        "/tmp/b.txt",
    ]
    assert "<｜｜DSML" not in visible
    assert "<parameter" not in visible


def test_detect_tool_format() -> None:
    assert (
        ide_chat.detect_tool_format("<tool_call><function=Read><parameter=path>a</parameter>")
        == "xml_function"
    )
    assert (
        ide_chat.detect_tool_format('<tool_call>{"name":"Read","arguments":{}}</tool_call>')
        == "tool_call_block"
    )
    assert ide_chat.detect_tool_format("<||DSML||tool_calls>") == "dsml"
    assert (
        ide_chat.detect_tool_format("<|FunctionCallBegin|>[]<|FunctionCallEnd|>")
        == "doubao_json"
    )
    assert (
        ide_chat.detect_tool_format(
            "<trae_mouth_tool_call>{}</trae_mouth_tool_call>"
        )
        == "legacy_marker"
    )
    assert ide_chat.detect_tool_format("普通回答") == "none"


def test_normalize_tool_arguments_repairs_defaults_and_types() -> None:
    schema = {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "mode": {"type": "string", "enum": ["fast", "safe"]},
            "count": {"type": "integer", "default": 1},
        },
        "required": ["x"],
    }
    normalized = ide_chat.normalize_tool_arguments(
        "tool", {"x": "7", "mode": "FAST"}, schema
    )
    assert normalized == {"x": 7, "mode": "fast", "count": 1}
    assert ide_chat.normalize_tool_arguments("tool", {"x": True}, schema)["x"] == 1
    # 缺必填字段不再中断整轮请求，只补默认值后原样下发。
    assert ide_chat.normalize_tool_arguments("tool", {}, schema) == {"count": 1}
    # 类型修不了时保留原值，同样不报错。
    assert ide_chat.normalize_tool_arguments("tool", {"x": "abc"}, schema)["x"] == "abc"
    # 参数完全不是 JSON 时才按错误处理。
    with pytest.raises(ValueError):
        ide_chat.normalize_tool_arguments("tool", "{not json", schema)


def test_convert_messages_truncates_large_tool_result() -> None:
    content = "A" * 1000 + "NEEDLE" + "B" * 1000
    converted = ide_chat.convert_messages(
        [{"role": "tool", "tool_call_id": "call_1", "name": "read", "content": content}],
        max_tool_result_chars=500,
    )
    text = converted[0]["content"][0]["text"]
    assert "tool result truncated" in text
    assert len(text) < 900


def test_compact_history_keeps_system_and_recent() -> None:
    system = {"role": "system", "content": "system rules"}
    old_user = {"role": "user", "content": "old " * 100}
    old_assistant = {"role": "assistant", "content": "old answer " * 100}
    recent = {"role": "user", "content": "recent"}
    compacted = ide_chat._compact_history(
        [system, old_user, old_assistant, recent], max_chars=40, max_messages=0
    )
    assert compacted[0] is system
    assert compacted[-1] is recent
    assert old_user not in compacted
    assert old_assistant not in compacted


def test_compact_history_keeps_matching_tool_call() -> None:
    system = {"role": "system", "content": "system rules"}
    call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }
        ],
    }
    result = {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "read",
        "content": "result",
    }
    compacted = ide_chat._compact_history(
        [system, call, result], max_chars=1000, max_messages=1
    )
    assert [message["role"] for message in compacted] == [
        "system",
        "assistant",
        "tool",
    ]


def test_completed_tool_call_reminder_skips_failed_results() -> None:
    call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read", "arguments": '{"path":"a"}'},
            }
        ],
    }
    success = {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "read",
        "content": "ok",
    }
    failed = {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "read",
        "content": "Error: no such file",
    }
    reminder = ide_chat._completed_tool_call_reminder([call, success])
    assert "Completed client tool calls" in reminder
    assert "<function=read><parameter=path>a</parameter>" in reminder
    assert ide_chat._completed_tool_call_reminder([call, failed]) == ""


def test_large_tool_catalog_compacts_prompt(monkeypatch) -> None:
    monkeypatch.setenv("TRAE_MOUTH_MAX_TOOL_SCHEMA_CHARS", "2048")
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{index}",
                "description": "A very verbose tool description " * 20,
                "parameters": {
                    "type": "object",
                    "properties": {
                        f"field_{index}": {
                            "type": "string",
                            "description": "long field description " * 10,
                        }
                    },
                    "required": [f"field_{index}"],
                },
            },
        }
        for index in range(20)
    ]
    prompt = ide_chat.build_tool_prompt(tools)
    assert "compact input signatures" in prompt
    assert '"available_tools"' in prompt
    assert '"parameters"' in prompt
    assert '"required"' in prompt
    assert "<tool_call><function=" in prompt


def test_tool_block_filter_extracts_and_holds_partial() -> None:
    flt = ide_chat.ToolBlockFilter()
    visible, calls = flt.feed("before<trae_mouth")
    assert visible == "before"
    assert calls == []
    full = (
        "before<trae_mouth_tool_call>"
        '{"name":"get_weather","arguments":{"city":"Beijing"}}'
        "</trae_mouth_tool_call>after"
    )
    visible, calls = flt.feed(full)
    assert visible == "beforeafter"
    assert calls == [{"name": "get_weather", "arguments": {"city": "Beijing"}}]
    visible, calls = flt.feed(full)
    assert visible == "beforeafter"
    assert calls == []


def test_convert_messages_builds_tool_prompt_and_history() -> None:
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city":"Beijing"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "get_weather",
            "content": "sunny",
        },
    ]
    converted = ide_chat.convert_messages(
        messages,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ],
    )
    assert converted[0]["role"] == "system"
    system_text = converted[0]["content"][0]["text"]
    assert "You are helpful." in system_text
    assert "<tool_call><function=" in system_text
    assert not any(
        "[client tool call]" in (item.get("content") or [{}])[0].get("text", "")
        for item in converted
        if item.get("role") == "assistant" and item.get("content")
    )
    tool_result = next(
        item
        for item in converted
        if item["role"] == "user" and "Client tool result" in item["content"][0]["text"]
    )
    assert "status=succeeded" in tool_result["content"][0]["text"]
    assert 'arguments={"city":"Beijing"}' not in tool_result["content"][0]["text"]
    assert "<parameter=city>Beijing</parameter>" in tool_result["content"][0]["text"]
    assert "sunny" in tool_result["content"][0]["text"]


def test_resolve_model_prefers_max_variant() -> None:
    catalog = {
        "groups": {
            "ide": {
                "models": [
                    {
                        "config_name": "glm-5.4",
                        "display_name": "GLM-5.4",
                        "provider_models": ["glm-5.4__dev", "glm-5.4__max"],
                        "functions": ["chat_v3"],
                        "config_source": 1,
                    }
                ]
            }
        }
    }
    normal = ide_chat.resolve_model(catalog, "glm-5.4")
    assert normal and normal["model_name"] == "glm-5.4__dev"
    assert normal["max_mode_applied"] is False
    maxed = ide_chat.resolve_model(catalog, "glm-5.4", prefer_max=True)
    assert maxed and maxed["model_name"] == "glm-5.4__max"
    assert maxed["max_mode_applied"] is True
    suffix = ide_chat.resolve_model(catalog, "glm-5.4:1m")
    assert suffix and suffix["model_name"] == "glm-5.4__max"
    assert ide_chat.resolve_model(catalog, "missing") is None


def test_resolve_model_exact_config_beats_display_alias() -> None:
    catalog = {
        "groups": {
            "ide": {
                "models": [
                    {
                        "config_name": "glm-5.2_advisor_doubao",
                        "display_name": "GLM-5.2",
                        "provider_models": ["glm-5.2__dev"],
                        "functions": ["chat_v3"],
                    },
                    {
                        "config_name": "glm-5.2",
                        "display_name": "GLM-5.2",
                        "provider_models": ["glm-5.2__dev"],
                        "functions": ["chat_v3"],
                    },
                ]
            }
        }
    }
    resolved = ide_chat.resolve_model(catalog, "glm-5.2")
    assert resolved["config_name"] == "glm-5.2"


def test_resolve_model_max_can_use_solo_when_ide_has_no_max() -> None:
    catalog = {
        "groups": {
            "ide": {
                "models": [
                    {
                        "config_name": "glm-5.4",
                        "display_name": "GLM-5.4",
                        "provider_models": ["glm-5.4__dev"],
                        "functions": ["chat_v3"],
                    }
                ]
            },
            "solo": {
                "models": [
                    {
                        "config_name": "glm-5.4",
                        "display_name": "GLM-5.4",
                        "provider_models": ["glm-5.4__dev", "glm-5.4__max"],
                        "functions": ["solo_agent"],
                    }
                ]
            },
        }
    }
    resolved = ide_chat.resolve_model(catalog, "glm-5.4", prefer_max=True)
    assert resolved["endpoint"] == "solo"
    assert resolved["model_name"] == "glm-5.4__max"
    assert resolved["raw_chat_function"] == "solo_agent"


def test_build_body_uses_prompt_tools_not_native_tools() -> None:
    resolved = {
        "config_name": "glm-5.4",
        "model_name": "glm-5.4__max",
        "raw_chat_function": "chat_v3",
        "config_source": 1,
    }
    body = ide_chat.build_body(
        resolved,
        [{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "ping",
                    "description": "Ping",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        session_id="sid",
    )
    assert body["config_name"] == "glm-5.4"
    assert body["model_name"] == "glm-5.4__max"
    assert body["raw_chat_function"] == "chat_v3"
    assert body["stream"] is True
    assert "tools" not in body
    assert "<tool_call><function=" in body["messages"][0]["content"][0]["text"]


def test_map_usage() -> None:
    usage = ide_chat.map_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "reasoning_tokens": 5,
            "cache_read_input_tokens": 3,
        }
    )
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 20
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 5
    assert usage["prompt_tokens_details"]["cached_tokens"] == 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"prompt_tokens": 100, "cache_read_tokens": 40}, 40),
        ({"prompt_tokens": 100, "cache_read_token": 41}, 41),
        ({"prompt_tokens": 100, "prompt_cache_hit_tokens": 42}, 42),
        ({"prompt_tokens": 100, "cached_tokens": 43}, 43),
        ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 44}}, 44),
        ({"prompt_tokens": 100, "input_tokens_details": {"cached_tokens": 45}}, 45),
        (
            {
                "prompt_tokens": 100,
                "cache_read_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 46},
            },
            46,
        ),
        ({"prompt_tokens": 100, "cached_tokens": True}, 0),
        ({"prompt_tokens": 100}, 0),
    ],
)
def test_map_usage_recognizes_cached_aliases(raw, expected) -> None:
    # 上游各家的缓存字段名不统一，全部归一成 OpenAI 的
    # prompt_tokens_details.cached_tokens
    usage = ide_chat.map_usage(raw)
    if expected:
        assert usage["prompt_tokens_details"] == {"cached_tokens": expected}
    else:
        assert "prompt_tokens_details" not in usage


def test_stream_raw_converts_read_error_to_upstream_error() -> None:
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: output\ndata: {"response":"x"}\n\n'
            raise httpx.ReadError("connection dropped")

    class BrokenTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=BrokenStream(),
            )

    config = SimpleNamespace(
        model_gateway="https://example.invalid",
        ide_version="3.4.22",
        ide_version_code="20261118",
    )
    resolved = {
        "config_name": "test-model",
        "model_name": "test-model__max",
        "raw_chat_function": "chat_v3",
        "config_source": 1,
    }

    async def collect() -> list[dict]:
        events = []
        async for event in ide_chat.stream_raw(
            config,
            "token",
            "user-1",
            build_device(),
            resolved,
            {},
            transport=BrokenTransport(),
        ):
            events.append(event)
        return events

    with pytest.raises(ide_chat.UpstreamError):
        asyncio.run(collect())


def test_stream_raw_falls_back_to_v1() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/v2/llm_raw_chat"):
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'event: output\ndata: {"response":"OK"}\n\n'
                'event: done\ndata: {"finish_reason":"stop"}\n\n'
            ),
        )

    config = SimpleNamespace(
        model_gateway="https://example.invalid",
        ide_version="3.4.22",
        ide_version_code="20261118",
    )
    resolved = {
        "config_name": "test-model",
        "model_name": "test-model__dev",
        "raw_chat_function": "chat_v3",
        "config_source": 1,
    }

    async def collect() -> list[dict]:
        events = []
        async for event in ide_chat.stream_raw(
            config,
            "token",
            "user-1",
            build_device(),
            resolved,
            {},
            transport=httpx.MockTransport(handler),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    assert paths == [
        "/api/ide/v2/llm_raw_chat",
        "/api/ide/v1/llm_raw_chat",
    ]
    assert events[0]["data"]["response"] == "OK"
    assert events[-1]["event"] == "done"


def test_convert_messages_keeps_only_last_assistant_reasoning() -> None:
    converted = ide_chat.convert_messages(
        [
            {
                "role": "assistant",
                "content": "first",
                "reasoning_content": "old reasoning",
            },
            {"role": "user", "content": "go on"},
            {
                "role": "assistant",
                "content": "second",
                "reasoning_content": "new reasoning",
            },
        ]
    )
    assistants = [item for item in converted if item.get("role") == "assistant"]
    assert assistants[0].get("reasoning_content") is None
    assert assistants[1]["reasoning_content"] == "new reasoning"


def test_convert_messages_drops_reasoning_on_tool_call_turns() -> None:
    converted = ide_chat.convert_messages(
        [
            {
                "role": "assistant",
                "content": "I will check.",
                "reasoning_content": "need a tool",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
    )
    assistant = next(item for item in converted if item.get("role") == "assistant")
    assert assistant.get("reasoning_content") is None
    assert assistant.get("reasoning") is None


def test_completed_tool_reminder_is_attached_to_tail() -> None:
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "read",
            "content": "ok",
        },
    ]
    converted = ide_chat.convert_messages(
        messages,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    system_text = converted[0]["content"][0]["text"]
    assert "Completed client tool calls" not in system_text
    assert converted[-1]["role"] == "user"
    blocks = converted[-1]["content"]
    assert "Client tool result" in blocks[0]["text"]
    assert "Completed client tool calls" in blocks[-1]["text"]


def test_estimate_tokens_handles_cjk_and_latin() -> None:
    zh = "北京今天的天气很好适合出门散步" * 100
    en = "The quick brown fox jumps over the lazy dog. " * 100
    zh_tokens = ide_chat._estimate_tokens(zh)
    en_tokens = ide_chat._estimate_tokens(en)
    assert 0.45 < zh_tokens / len(zh) < 0.7
    assert 0.15 < en_tokens / len(en) < 0.3


def test_compact_history_respects_token_budget() -> None:
    messages = [{"role": "user", "content": "旧消息" * 60} for _ in range(8)]
    messages.append({"role": "user", "content": "最新问题"})
    kept = ide_chat._compact_history(
        messages, 0, 0, max_prompt_tokens=120, reserved_tokens=0
    )
    assert 1 <= len(kept) < len(messages)
    assert kept[-1]["content"] == "最新问题"


def test_tool_results_are_truncated_before_budgeting() -> None:
    messages = [
        {"role": "user", "content": "KEEP-ME " * 30},
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "X" * 200000,
        },
        {"role": "user", "content": "最近问题"},
    ]
    converted = ide_chat.convert_messages(
        messages,
        max_tool_result_chars=1000,
        max_prompt_tokens=1200,
    )
    assert any("KEEP-ME" in str(item.get("content")) for item in converted)


def test_compact_history_counts_images() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    messages = [
        {"role": "user", "content": [image, {"type": "text", "text": "图"}]}
        for _ in range(6)
    ]
    kept = ide_chat._compact_history(
        messages, 0, 0, max_prompt_tokens=2000, reserved_tokens=0
    )
    assert 1 <= len(kept) < len(messages)


def test_convert_messages_accepts_legacy_function_role() -> None:
    converted = ide_chat.convert_messages(
        [{"role": "function", "name": "search", "content": "search result"}]
    )
    assert converted[0]["role"] == "user"
    text = converted[0]["content"][0]["text"]
    assert "Client tool result" in text
    assert "name=search" in text
    assert "search result" in text


def test_tool_block_filter_keeps_dsml_sample_inside_argument() -> None:
    flt = ide_chat.ToolBlockFilter()
    sample = (
        '<||DSML||invoke name="Read">'
        '<||DSML||parameter name="file_path" string="true">a.py'
        "</||DSML||parameter></||DSML||invoke>"
    )
    text = (
        "< | | DSML | | tool_calls>\n"
        '< | | DSML | | invoke name="Edit">\n'
        "< | | DSML | | parameter name=\"file_path\" string=\"true\">"
        "tests/x.py</ | | DSML | | parameter>\n"
        "< | | DSML | | parameter name=\"old_string\" string=\"true\">"
        f"old = '{sample}'\n"
        "</ | | DSML | | parameter>\n"
        "< | | DSML | | parameter name=\"new_string\" string=\"true\">"
        "new = 1</ | | DSML | | parameter>\n"
        "</ | | DSML | | invoke>\n</ | | DSML | | tool_calls>"
    )
    visible, calls = flt.feed(text)
    assert visible == ""
    assert calls == [
        {
            "name": "Edit",
            "arguments": {
                "file_path": "tests/x.py",
                "old_string": f"old = '{sample}'\n",
                "new_string": "new = 1",
            },
        }
    ]


def test_tool_block_filter_ignores_lone_closing_tag_inside_argument() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "<||DSML||tool_calls>"
        '<||DSML||invoke name="Edit">'
        '<||DSML||parameter name="old_string" string="true">'
        'drop "</||DSML||parameter>" here\n</||DSML||parameter>'
        '<||DSML||parameter name="new_string" string="true">'
        "done</||DSML||parameter>"
        "</||DSML||invoke></||DSML||tool_calls>"
    )
    visible, calls = flt.feed(text)
    assert visible == ""
    assert calls == [
        {
            "name": "Edit",
            "arguments": {
                "old_string": 'drop "</||DSML||parameter>" here\n',
                "new_string": "done",
            },
        }
    ]


def test_tool_block_filter_recovers_single_bar_dsml_call() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "<｜DSML｜function_calls>\n<｜DSML｜invoke name=\"Bash\">\n"
        '<｜DSML｜parameter name="command" string="true">echo hi</｜DSML｜parameter>\n'
        "</｜DSML｜invoke>\n</｜DSML｜function_calls>"
    )
    visible, calls = flt.feed(text)
    assert visible == ""
    assert calls == [{"name": "Bash", "arguments": {"command": "echo hi"}}]


def test_tool_block_filter_waits_before_trusting_closing_tag() -> None:
    flt = ide_chat.ToolBlockFilter()
    pieces = [
        "<||DSML||tool_calls><||DSML||invoke name=\"Edit\">",
        '<||DSML||parameter name="old_string" string="true">'
        'keep "</||DSML||parameter>"',
        " here</||DSML||parameter>",
        '<||DSML||parameter name="new_string" string="true">'
        "x</||DSML||parameter>",
        "</||DSML||invoke></||DSML||tool_calls>",
    ]
    text = ""
    for piece in pieces[:-1]:
        text += piece
        assert flt.feed(text) == ("", [])
    text += pieces[-1]
    visible, calls = flt.feed(text)
    assert visible == ""
    assert calls == [
        {
            "name": "Edit",
            "arguments": {
                "old_string": 'keep "</||DSML||parameter>" here',
                "new_string": "x",
            },
        }
    ]


def test_tool_block_filter_final_closes_unclosed_block_without_leak() -> None:
    flt = ide_chat.ToolBlockFilter()
    pieces = [
        "先看一下 ",
        "< | | DS",
        "ML | | tool_",
        "calls>\n< | | DSML | | invoke name=\"Bash\">",
        "< | | DSML | | parameter name=\"command\" string=\"true\">"
        "echo hi</ | | DSML | | parameter>",
        "\n</ | | DSML | | invoke>",
    ]
    text = ""
    for piece in pieces[:-1]:
        text += piece
        assert flt.feed(text) == ("先看一下 ", [])
    text += pieces[-1]
    visible, calls = flt.feed(text)
    assert visible == "先看一下 "
    assert calls == [{"name": "Bash", "arguments": {"command": "echo hi"}}]
    assert flt.feed(text, final=True) == ("先看一下 ", [])


def test_tool_block_filter_final_releases_quoted_wrapper_as_text() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = "格式是：< | | DSML | | tool_calls>\n后面仍是正文"
    assert flt.feed(text) == ("格式是：", [])
    assert flt.native_incomplete.startswith("< | | DSML | | tool_calls>")
    assert flt.feed(text, final=True) == (text, [])


def test_tool_block_filter_final_flags_cut_argument() -> None:
    flt = ide_chat.ToolBlockFilter()
    text = (
        "<||DSML||tool_calls><||DSML||invoke name=\"Edit\">"
        '<||DSML||parameter name="new_string" string="true">half'
    )
    assert flt.feed(text) == ("", [])
    assert flt.native_truncated is False
    assert flt.feed(text, final=True) == ("", [])
    assert flt.native_truncated is True


def test_recover_fenced_bare_json_call_maps_unique_schema() -> None:
    text = (
        "编译 v0.46:\n```json\n"
        '{"command":"gradle :app:assembleRelease","run_in_background":true}\n```'
    )
    schemas = {
        "RunCommand": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "run_in_background": {"type": "boolean"},
            },
            "required": ["command"],
        },
        "Read": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    }
    clean, calls, suspicious = ide_chat.recover_fenced_json_calls(text, schemas)
    assert clean == "编译 v0.46:"
    assert calls == [
        {
            "name": "RunCommand",
            "arguments": {
                "command": "gradle :app:assembleRelease",
                "run_in_background": True,
            },
        }
    ]
    assert suspicious is False


def test_recover_fenced_plain_json_answer_is_left_alone() -> None:
    text = '```json\n{"answer": 42}\n```'
    clean, calls, suspicious = ide_chat.recover_fenced_json_calls(
        text, {"Read": {"properties": {"file_path": {"type": "string"}}}}
    )
    assert clean == text
    assert calls == []
    assert suspicious is False


def test_recover_fenced_bare_json_flags_ambiguous_tools() -> None:
    text = '```json\n{"command": "ls"}\n```'
    schemas = {
        "Bash": {"properties": {"command": {"type": "string"}}, "required": ["command"]},
        "Shell": {"properties": {"command": {"type": "string"}}, "required": ["command"]},
    }
    clean, calls, suspicious = ide_chat.recover_fenced_json_calls(text, schemas)
    assert clean == text
    assert calls == []
    assert suspicious is True


def test_recover_fenced_named_call_uses_declared_tool() -> None:
    text = '```json\n{"name": "Read", "arguments": {"file_path": "a.py"}}\n```'
    clean, calls, suspicious = ide_chat.recover_fenced_json_calls(
        text, {"Read": {"properties": {"file_path": {"type": "string"}}}}
    )
    assert clean == ""
    assert calls == [{"name": "Read", "arguments": {"file_path": "a.py"}}]
    assert suspicious is False


def test_recover_fenced_undeclared_name_is_suspicious() -> None:
    text = '```json\n{"name": "Shell", "arguments": {"command": "ls"}}\n```'
    clean, calls, suspicious = ide_chat.recover_fenced_json_calls(
        text, {"Read": {"properties": {"file_path": {"type": "string"}}}}
    )
    assert clean == text
    assert calls == []
    assert suspicious is True


def test_tool_block_filter_parses_name_args_shape() -> None:
    """glm-4.6 agent 通道：<name>x</name><args><k>v</k></args>。"""

    text = (
        "<tool_call>\n<name>run_command</name>\n<args>\n"
        "<command>echo trae-native-tool-ok</command>\n</args>\n</tool_call>"
    )
    visible, calls = ide_chat.ToolBlockFilter().feed(text, final=True)
    assert visible == ""
    assert calls == [
        {"name": "run_command", "arguments": {"command": "echo trae-native-tool-ok"}}
    ]
    assert ide_chat.detect_tool_format(text) == "name_args"


def test_name_args_shape_without_args_tag_and_json_body() -> None:
    bare = '<tool_call><name>Read</name><file_path>a.py</file_path></tool_call>'
    visible, calls = ide_chat.ToolBlockFilter().feed(bare, final=True)
    assert visible == ""
    assert calls == [{"name": "Read", "arguments": {"file_path": "a.py"}}]

    as_json = '<tool_call><name>Read</name><args>{"file_path": "b.py"}</args></tool_call>'
    visible, calls = ide_chat.ToolBlockFilter().feed(as_json, final=True)
    assert visible == ""
    assert calls == [{"name": "Read", "arguments": {"file_path": "b.py"}}]


def test_name_args_truncated_is_not_a_call() -> None:
    text = "<tool_call><name>run_command</name><args><command>echo half"
    visible, calls = ide_chat.ToolBlockFilter().feed(text, final=True)
    assert visible == ""
    assert calls == []


def test_completed_call_reminder_uses_bridge_format() -> None:
    messages = [
        {"role": "user", "content": "读文件"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"a.txt"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    reminder = ide_chat._completed_tool_call_reminder(messages)
    assert (
        "<tool_call><function=read_file><parameter=path>a.txt</parameter>"
        "</function></tool_call>" in reminder
    )
    assert '"arguments"' not in reminder


def test_convert_messages_appends_tail_format_reminder() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]
    converted = ide_chat.convert_messages(
        [{"role": "user", "content": "读文件"}], tools
    )
    tail = converted[-1]["content"][-1]["text"]
    assert "Tool call format reminder" in tail
