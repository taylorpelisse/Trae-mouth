from __future__ import annotations

import copy
import json
import logging
import os
import re
import uuid
from typing import Any, AsyncIterator, Mapping, Optional

import httpx

from ..accounts.models import DeviceProfile
from ..config import Config
from .catalog import ide_headers

logger = logging.getLogger(__name__)
IMAGE_TOKENS = 1600

TOOL_START = "<trae_mouth_tool_call>"
TOOL_END = "</trae_mouth_tool_call>"
LEGACY_CALL_PREFIX = "[client tool call]"



def _parse_legacy_client_call(line: str) -> Optional[dict[str, Any]]:
    text = line.strip()
    if not text.startswith(LEGACY_CALL_PREFIX):
        return None
    match = re.match(
        r"^\[client tool call\]\s+id=(\S*)\s+name=(\S+)\s+arguments=(.*)$",
        text,
    )
    if not match:
        return None
    name = match.group(2).strip()
    if not name:
        return None
    raw_arguments = match.group(3).strip()
    try:
        arguments = json.loads(raw_arguments)
    except ValueError:
        return None
    if not isinstance(arguments, Mapping):
        arguments = {"value": arguments}
    return {"name": name, "arguments": dict(arguments)}


def _split_legacy_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    lines = text.splitlines(keepends=True)
    clean: list[str] = []
    calls: list[dict[str, Any]] = []
    for line in lines:
        call = _parse_legacy_client_call(line)
        if call is not None:
            calls.append(call)
            continue
        clean.append(line)
    return "".join(clean), calls


_DSML = r"[|｜]\s*[|｜]?\s*DSML\s*[|｜]\s*[|｜]?"
# One DSML tag, opening or closing: < | | DSML | | parameter name="x" string="true">
_DSML_TAG_RE = re.compile(
    rf"<(?P<close>/)?\s*(?:{_DSML})\s*"
    r"(?P<kind>tool_calls|function_calls|calls|invoke|parameter)\b(?P<attrs>[^>]*)>",
    re.S | re.I,
)
_DSML_HINT_RE = re.compile("dsml", re.I)
_DSML_KINDS = {
    "calls": "calls",
    "tool_calls": "calls",
    "function_calls": "calls",
    "invoke": "invoke",
    "parameter": "parameter",
}
_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</(?:\w+:)?tool_call>", re.S | re.I
)
_DOUBAO_CALL_RE = re.compile(
    r"<\|FunctionCallBegin\|>\s*(.*?)\s*<\|FunctionCallEnd\|>", re.S | re.I
)
# 模型自发使用的 XML 调用格式，三种写法都要认：
#   <function=Read>...</function>          （gfw/自造标记常见）
#   <function=Read> / function=Read>       （漏写尖括号）
#   <invoke name="Read">...</function>     （DSML 混写）
_XML_FUNC_OPEN_RE = re.compile(
    r"<?\s*function\s*=\s*\"?(?P<name1>[^\"\s<>]+)\"?\s*(?=>|<)"
    r"|<invoke\s+name\s*=\s*\"(?P<name2>[^\"]+)\"[^>]*>",
    re.I,
)
#   <parameter=path>v</parameter>          （自造标记）
#   <parameter name="path">v</parameter>   （DSML 风格）
_XML_PARAM_RE = re.compile(
    r"<parameter\s*=\s*\"?(?P<key1>[^\"\s<>]+)\"?\s*[^>]*>(?P<val1>.*?)</parameter>"
    r"|<parameter\s+name\s*=\s*\"(?P<key2>[^\"]+)\"[^>]*>(?P<val2>.*?)</parameter>",
    re.S | re.I,
)
# 裸写法：<tool_call>read_file<path>v</parameter></function>
# 既没有 function= 也没有 <parameter=，只能按 </function> 分段解析。
_BARE_PARAM_RE = re.compile(
    r"<(?!tool_call\b|function\b|parameter\b|arg_value\b|function_calls\b)"
    r"(?P<key>[A-Za-z_][\w.\-]*)\s*>(?P<val>.*?)</parameter>",
    re.S | re.I,
)
_BARE_NAME_RE = re.compile(
    r"\s*(?:<tool_call>\s*)?(?P<name>[A-Za-z_][\w.\-]*)", re.I
)
# 最退化的写法：read_file  path=/tmp/a.txt 或 read_file(path="/tmp/a.txt")
_BARE_KV_RE = re.compile(
    r"(?P<key>[A-Za-z_][\w.\-]*)\s*=\s*"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s,;<>\"')\]]+)"
)
# glm-4.6 在 agent 通道的写法：
#   <tool_call><name>run_command</name><args><command>echo hi</command></args></tool_call>
# 参数用同名标签成对出现，<args> 有时省略。
_TOOL_NAME_TAG_RE = re.compile(r"<name>\s*(?P<name>[^<>]+?)\s*</name>", re.I)
_ARGS_TAG_RE = re.compile(r"<args>\s*(?P<body>.*?)\s*</args>", re.I | re.S)
_TAG_PARAM_RE = re.compile(
    r"<(?P<key>[A-Za-z_][\w.\-]*)>\s*(?P<val>.*?)\s*</(?P=key)>", re.S
)
# DSML blocks are located by _scan_dsml; these only flag the JSON-style markers.
_NATIVE_OPEN_RES = (
    re.compile(r"<tool_call>", re.I),
    re.compile(r"<\|FunctionCallBegin\|>", re.I),
)


def _native_value(value: str, string_attr: str) -> Any:
    if "true" in string_attr.lower():
        return value
    stripped = value.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except ValueError:
        return value


def _native_call_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, Mapping):
        return dict(raw_arguments)
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except ValueError:
            return {"value": raw_arguments}
        if isinstance(parsed, Mapping):
            return dict(parsed)
        return {"value": parsed}
    return {"value": raw_arguments}


def _dsml_attr(attrs: str, name: str) -> str:
    match = re.search(rf"\b{name}\s*=\s*[\"']([^\"']*)[\"']", attrs, re.I)
    return match.group(1).strip() if match else ""


def _dsml_argument(invoke: dict[str, Any], name: str, value: Any) -> None:
    invoke["count"] += 1
    if name not in {"arguments", "parameters"}:
        invoke["arguments"][name] = value
    elif isinstance(value, Mapping):
        invoke["arguments"].update(value)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, Mapping):
            invoke["arguments"].update(parsed)
        else:
            invoke["arguments"][name] = value
    else:
        invoke["arguments"][name] = value


def _scan_dsml(
    text: str, *, final: bool = False
) -> tuple[str, list[dict[str, Any]], str, bool]:
    """Split ``text`` into prose, parsed calls and a trailing unclosed block.

    Tags are matched by nesting depth: a closing tag only ends the element of
    the same kind opened most recently.  A DSML sample quoted inside an
    argument value therefore stays part of that value instead of cutting the
    call short.

    The last element flags a call whose argument was still open when the text
    ended, which is only known once ``final`` is set.
    """

    prose: list[str] = []
    calls: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    cursor = 0
    block_start = 0
    block_calls = 0
    opened = False
    tags = list(_DSML_TAG_RE.finditer(text)) if _DSML_HINT_RE.search(text) else []

    def topmost(kind: str) -> int:
        return next(
            (
                index
                for index in range(len(stack) - 1, -1, -1)
                if stack[index]["kind"] == kind
            ),
            -1,
        )

    def close(index: int, end: int) -> None:
        """Close ``stack[index]`` and everything opened inside it, at ``end``."""
        while len(stack) > index:
            entry = stack.pop()
            quoted = bool(stack) and stack[-1]["kind"] == "parameter"
            if entry["kind"] == "parameter":
                owner = next(
                    (item for item in reversed(stack) if item["kind"] == "invoke"),
                    None,
                )
                if entry["name"] and owner is not None and not quoted:
                    _dsml_argument(
                        owner,
                        entry["name"],
                        _native_value(text[entry["start"] : end], entry["attrs"]),
                    )
            elif entry["kind"] == "invoke" and entry["name"] and not quoted:
                calls.append({"name": entry["name"], "arguments": entry["arguments"]})

    for index, tag in enumerate(tags):
        kind = _DSML_KINDS[tag.group("kind").lower()]
        attrs = tag.group("attrs")
        if tag.group("close") is None:
            if not stack:
                if kind != "calls":
                    continue  # a bare tag outside a block is plain text
                opened = True
                block_start, block_calls = tag.start(), len(calls)
                prose.append(text[cursor:block_start])
            if kind == "invoke":
                stack.append(
                    {
                        "kind": kind,
                        "name": _dsml_attr(attrs, "name"),
                        "arguments": {},
                        "count": 0,
                    }
                )
            elif kind == "parameter":
                stack.append(
                    {
                        "kind": kind,
                        "name": _dsml_attr(attrs, "name"),
                        "start": tag.end(),
                        "attrs": attrs,
                    }
                )
            else:
                stack.append({"kind": kind})
            continue
        target = topmost(kind)
        if target < 0:
            # A leftover structural tag (a doubled close, a wrapper closed
            # after its last invoke) is dropped when only whitespace follows
            # the block.
            if opened and not stack and not text[cursor : tag.start()].strip():
                cursor = tag.end()
            continue
        parameter = topmost("parameter")
        if (
            parameter >= 0
            and target <= parameter
            and (parameter == 0 or stack[parameter - 1]["kind"] == "invoke")
        ):
            # The tag would end a call argument.  Only trust it when the next
            # tag is adjacent or closes the block: a closing tag quoted inside
            # the value is followed by more of that value.
            following = tags[index + 1] if index + 1 < len(tags) else None
            if following is None:
                if not final:
                    continue
            elif text[tag.end() : following.start()].strip() and not (
                following.group("close")
                and _DSML_KINDS[following.group("kind").lower()] in {"invoke", "calls"}
            ):
                continue
        close(target, tag.start())
        if not stack:
            cursor = tag.end()

    if not stack:
        prose.append(text[cursor:])
        return "".join(prose), calls, "", False
    tail = text[block_start:]
    if not final:
        return "".join(prose), calls, tail, False
    if topmost("parameter") >= 0:
        return "".join(prose), calls, tail, True
    if len(calls) == block_calls and not any(
        entry["kind"] == "invoke" and entry["name"] and entry["count"]
        for entry in stack
    ):
        # A wrapper tag quoted in prose, not a call.
        prose.append(tail)
        return "".join(prose), calls, "", False
    for entry in stack:
        if entry["kind"] == "invoke" and entry["name"] and entry["count"]:
            calls.append({"name": entry["name"], "arguments": entry["arguments"]})
    return "".join(prose), calls, "", False


# 模型对同一件事的字段名不统一：名字有 name / tool_name / tool_call_name，
# 参数有 arguments / input / args / parameters。解析器原先各认一部分，只写
# 其中一种的模型就会被某一层静默吃掉，这里统一成同一套取值规则。
_CALL_NAME_FIELDS = ("name", "tool_name", "tool_call_name")
_CALL_ARGUMENT_FIELDS = ("arguments", "input", "args", "parameters")


def _call_field_name(item: Mapping[str, Any], function: Mapping[str, Any]) -> str:
    for source in (function, item):
        if not isinstance(source, Mapping):
            continue
        for key in _CALL_NAME_FIELDS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _call_field_arguments(item: Mapping[str, Any], function: Mapping[str, Any]) -> Any:
    for source in (function, item):
        if not isinstance(source, Mapping):
            continue
        for key in _CALL_ARGUMENT_FIELDS:
            if key in source and source[key] is not None:
                return source[key]
    return {}


def _call_arguments_mapping(arguments: Any) -> Optional[dict[str, Any]]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if isinstance(arguments, Mapping):
        return dict(arguments)
    return None


_FENCED_TAIL_RE = re.compile(
    r"```[A-Za-z0-9_+.\-]*[ \t]*\r?\n?(?P<body>.*?)```[ \t\r\n]*\Z", re.S
)


def _schema_matches(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> bool:
    """参数键与工具 schema 完全吻合：无未知键、必填项齐备。"""

    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        return False
    if any(key not in properties for key in arguments):
        return False
    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if key not in arguments:
                return False
    return True


def recover_fenced_json_calls(
    text: str, tool_schemas: Optional[Mapping[str, Mapping[str, Any]]] = None
) -> tuple[str, list[dict[str, Any]], bool]:
    """把模型写在 ```json 围栏里的裸参数对象还原成工具调用。

    有些模型不写桥接标记，直接把一次调用的参数对象放进围栏。对象里没有
    工具名，所以只按本次声明的 schema 反推：参数键唯一匹配一个工具才还原；
    匹配不到就原样当正文，匹配到多个则标记 ``suspicious``，交给调用方决定
    不能静默结束这一轮。

    返回 ``(clean_text, calls, suspicious)``。
    """

    if not text or not tool_schemas:
        return text, [], False
    match = _FENCED_TAIL_RE.search(text)
    if not match:
        return text, [], False
    body = match.group("body").strip()
    if not body:
        return text, [], False
    try:
        data = json.loads(body)
    except ValueError:
        return text, [], False
    items = data if isinstance(data, list) else [data]
    if not items or not all(isinstance(item, Mapping) for item in items):
        return text, [], False
    calls: list[dict[str, Any]] = []
    ambiguous = False
    for item in items:
        function = item.get("function") if isinstance(item.get("function"), Mapping) else {}
        name = _call_field_name(item, function)
        if name:
            arguments = _call_arguments_mapping(_call_field_arguments(item, function))
            if arguments is None or name not in tool_schemas:
                # 写了名字但参数坏了、或名字不在本次声明里：这是调用尝试，
                # 不是普通正文，不能静默当成功。
                return text, [], True
            calls.append({"name": name, "arguments": arguments})
            continue
        candidates = [
            key
            for key, schema in tool_schemas.items()
            if isinstance(schema, Mapping) and _schema_matches(item, schema)
        ]
        if len(candidates) == 1:
            calls.append({"name": candidates[0], "arguments": dict(item)})
        elif candidates:
            ambiguous = True
        else:
            return text, [], False
    if ambiguous or not calls:
        return text, [], True
    return text[: match.start()].rstrip(), calls, False


def _parse_json_tool_calls(block: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(block)
    except ValueError:
        start = block.find("{")
        end = block.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(block[start : end + 1])
        except ValueError:
            return []
    items = data.get("tool_calls") if isinstance(data, Mapping) else data
    if not isinstance(items, list):
        items = [data]
    calls: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function") if isinstance(item.get("function"), Mapping) else item
        name = _call_field_name(item, function)
        if not name:
            continue
        raw_arguments = _call_field_arguments(item, function)
        calls.append({"name": name, "arguments": _native_call_arguments(raw_arguments)})
    return calls


def _parse_name_args_calls(block: str) -> list[dict[str, Any]]:
    """解析 ``<name>x</name>`` + ``<args>`` 的成对标签写法。

    glm-4.6 在 agent 通道会输出 ``<tool_call><name>run_command</name>
    <args><command>echo hi</command></args></tool_call>``。参数块没闭合、
    或参数块里解析不出任何键值时不返回调用——宁可当作截断，也不伪造一次
    参数缺失的调用。
    """

    name_match = _TOOL_NAME_TAG_RE.search(block)
    if not name_match:
        return []
    name = name_match.group("name").strip()
    if not name:
        return []
    rest = block[name_match.end() :]
    args_match = _ARGS_TAG_RE.search(rest)
    if "<args" in rest.lower() and args_match is None:
        return []
    body = args_match.group("body") if args_match else rest
    arguments: dict[str, Any] = {}
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return []
        if isinstance(parsed, Mapping):
            arguments = dict(parsed)
    if not arguments:
        for param in _XML_PARAM_RE.finditer(body):
            key = (param.group("key1") or param.group("key2") or "").strip()
            if not key:
                continue
            value = (
                param.group("val1")
                if param.group("val1") is not None
                else param.group("val2")
            )
            arguments[key] = (value or "").strip("\n")
    if not arguments:
        for match in _TAG_PARAM_RE.finditer(body):
            arguments[match.group("key")] = match.group("val").strip("\n")
    if not arguments and args_match is not None:
        return []
    return [{"name": name, "arguments": arguments}]


def _parse_xml_tool_calls(block: str) -> list[dict[str, Any]]:
    """解析 ``<function=X><parameter=Y>v</parameter></function>`` 形式的调用。

    模型对这套 XML 写法比对自定义 JSON 标记熟得多，长上下文里遵循率明显更高。
    同一段里可以有多个 function，参数值保留原样字符串交给下游 schema 归一化。
    """

    calls: list[dict[str, Any]] = []
    # glm-5.1 会把 DSML 标记和裸 <parameter=x> 混着写，先把 DSML 前缀抹成
    # 普通尖括号，后面的 XML 规则就能统一处理。
    block = re.sub(rf"<\s*(?:{_DSML})\s*", "<", block)
    openings = list(_XML_FUNC_OPEN_RE.finditer(block))
    for index, match in enumerate(openings):
        name = (match.group("name1") or match.group("name2") or "").strip()
        if not name:
            continue
        end = openings[index + 1].start() if index + 1 < len(openings) else len(block)
        body = block[match.end() : end]
        arguments: dict[str, Any] = {}
        for param in _XML_PARAM_RE.finditer(body):
            key = (param.group("key1") or param.group("key2") or "").strip()
            if not key:
                continue
            value = (
                param.group("val1")
                if param.group("val1") is not None
                else param.group("val2")
            )
            arguments[key] = (value or "").strip("\n")
        calls.append({"name": name, "arguments": arguments})
    if calls:
        return calls
    named = _parse_name_args_calls(block)
    if named:
        return named
    # glm-5.3 的裸写法五花八门：
    #   <tool_call>read_file<path>v</parameter></function>
    #   <tool_call>read_file><parameter=path>v</parameter></function>
    #   <tool_call>read_file(path="v")
    #   <tool_call>read_file  path=v</arg_value>
    # 没有 function=/invoke 可依，按 </function> 分段后依次尝试三种参数写法。
    bare_kv = _BARE_KV_RE if "<tool_call" in block.lower() else None
    for segment in re.split(r"</function\s*>", block, flags=re.I):
        name_match = _BARE_NAME_RE.match(segment)
        if not name_match:
            continue
        arguments: dict[str, Any] = {}
        for param in _XML_PARAM_RE.finditer(segment):
            key = (param.group("key1") or param.group("key2") or "").strip()
            if not key:
                continue
            value = (
                param.group("val1")
                if param.group("val1") is not None
                else param.group("val2")
            )
            arguments.setdefault(key, (value or "").strip("\n"))
        for key, value in _BARE_PARAM_RE.findall(segment):
            arguments.setdefault(key.strip(), value.strip("\n"))
        if not arguments and bare_kv is not None:
            for key, value in bare_kv.findall(segment):
                arguments.setdefault(key.strip(), value.strip("\"'"))
        if not arguments:
            continue
        calls.append(
            {
                "name": name_match.group("name").strip(),
                "arguments": arguments,
            }
        )
    return calls


def _call_attempt_complete(text: str) -> bool:
    """判断一段调用标记是否写到收尾（避免把写到一半的参数当合法调用）。"""

    lowered = text.lower()
    if "</function" in lowered or "</invoke" in lowered:
        return True
    if "</parameter" in lowered:
        return lowered.count("<parameter") <= lowered.count("</parameter>")
    return False


def _bare_style_only(text: str) -> bool:
    """只有裸写法（没有 <parameter / <function）时，截断无从判断。"""

    lowered = text.lower()
    return not any(
        marker in lowered
        for marker in ("<parameter", "function=", "<invoke")
    )


def _salvage_native_xml(text: str) -> list[dict[str, Any]]:
    """收走漏在正文里的 XML 调用；写到一半的参数一律不收。"""

    if _call_attempt_complete(text):
        return _parse_xml_tool_calls(text)
    if "<tool_call" in text.lower() and _bare_style_only(text):
        # 退化写法没有参数开标签，分不清截断；只接受解析出参数的调用。
        return [call for call in _parse_xml_tool_calls(text) if call["arguments"]]
    return []


def _looks_like_call_attempt(text: str) -> bool:
    """整段是不是一次（可能是截断的）工具调用，而不是正文里引用的标记。"""

    lowered = text.lower()
    if _call_attempt_complete(text):
        return True
    if "<function=" in lowered or "<invoke" in lowered:
        return True
    match = re.search(r"<tool_call>\s*", lowered)
    if not match:
        return _DSML_HINT_RE.search(text) is not None
    return bool(re.match(r"[<A-Za-z_]", lowered[match.end() :]))


def detect_tool_format(text: str) -> str:
    """粗分模型实际使用的调用写法，用于统计哪些解析分支还在被使用。"""

    lowered = text.lower()
    if "trae_mouth_tool_call" in lowered:
        return "legacy_marker"
    if _DSML_HINT_RE.search(text):
        return "dsml"
    if "<|functioncallbegin|>" in lowered:
        return "doubao_json"
    if _XML_FUNC_OPEN_RE.search(text):
        return "xml_function"
    if "<name>" in lowered and "<args" in lowered:
        return "name_args"
    if "<tool_call>" in lowered:
        return "tool_call_block"
    return "none"


def _extract_native_tool_calls(
    text: str, *, final: bool = False
) -> tuple[str, list[dict[str, Any]], str, bool]:
    """Recover model-native tool markup that leaked into assistant text.

    DeepSeek emits ``<｜｜DSML｜｜ calls>...``, Qwen/others emit
    ``<tool_call>{...}</tool_call>`` and Doubao emits
    ``<|FunctionCallBegin|>[...]<|FunctionCallEnd|>``.  Treat these as calls,
    not as user-visible text.

    ``final`` marks the end of the output: DSML blocks still open are then
    closed where that is safe, and the last element flags a call whose
    argument was cut off.
    """

    calls: list[dict[str, Any]] = []

    # DSML first, so a JSON-style marker quoted inside a DSML argument is
    # never mistaken for a call of its own.
    visible, dsml_calls, dsml_tail, truncated = _scan_dsml(text, final=final)
    calls.extend(dsml_calls)

    def replace_tool_call(match: re.Match[str]) -> str:
        body = match.group(1)
        parsed = _parse_json_tool_calls(body) or _parse_xml_tool_calls(body)
        calls.extend(parsed)
        return ""

    visible = _TOOL_CALL_RE.sub(replace_tool_call, visible)

    def replace_doubao(match: re.Match[str]) -> str:
        calls.extend(_parse_json_tool_calls(match.group(1)))
        return ""

    visible = _DOUBAO_CALL_RE.sub(replace_doubao, visible)

    if _DSML_HINT_RE.search(visible):
        # DSML 外壳里混裸 XML 的情况两头都可能出：整段都混（glm-4.7-auto），
        # 或只有后面一块混（deepseek 先给一块完整 DSML、再给一块裸参数）。
        # 后一种 DSML 扫描器会把裸参数块当“正文里引用的标记”放回 visible，
        # 这里统一用 XML 规则再解析一遍残段。
        marker = re.search(rf"<\s*(?:{_DSML})", visible, re.I)
        start = marker.start() if marker else 0
        hybrid = _salvage_native_xml(visible[start:])
        if hybrid:
            for call in hybrid:
                if call not in calls:
                    calls.append(call)
            visible = visible[:start]

    openings = [
        match.start()
        for pattern in _NATIVE_OPEN_RES
        for match in pattern.finditer(visible)
    ]
    if openings:
        start = min(openings)
        tail = visible[start:] + dsml_tail
        if final:
            # 大多数模型只写 <tool_call> 开头、忘了闭合标签，整段只能当作
            # 不完整尾巴留着。final 时按 XML 解析一遍，成功就收成正常调用，
            # 不然这些标记会原样漏给用户。
            xml_calls = _salvage_native_xml(tail)
            if xml_calls:
                calls.extend(xml_calls)
                return visible[:start], calls, "", truncated
        return visible[:start], calls, tail, truncated
    return visible, calls, dsml_tail, truncated


class UpstreamError(RuntimeError):
    def __init__(
        self, message: str, status_code: int = 502, *, account_fault: bool = True
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        # False when the model's own output is unusable: the account is fine
        # and must not be cooled down for it.
        self.account_fault = account_fault


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif part.get("type") in {"tool_result", "tool_use"}:
                    continue
                else:
                    parts.append(str(part.get("content") or ""))
        return "".join(parts)
    if isinstance(content, Mapping):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


def _message_reasoning(message: Mapping[str, Any]) -> str:
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    content = message.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, Mapping):
                continue
            block_type = str(part.get("type") or "").lower()
            if block_type not in {"thinking", "reasoning", "reasoning_content"}:
                continue
            text = part.get("thinking")
            if not isinstance(text, str):
                text = part.get("text")
            if not isinstance(text, str):
                text = part.get("content")
            if isinstance(text, str) and text:
                parts.append(text)
        return "".join(parts)
    return ""


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, Mapping):
        return [dict(content)]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, Mapping):
            continue
        block = dict(part)
        block_type = str(block.get("type") or "").lower()
        if block_type in {"text", "input_text"}:
            text = block.get("text")
            if not isinstance(text, str):
                text = str(block.get("content") or "")
            blocks.append({"type": "text", "text": text})
        elif block_type == "image_url":
            blocks.append(block)
        elif block_type == "image":
            # Keep Anthropic-style images as text-only placeholders for the
            # basic IDE path; OpenAI image_url blocks pass through unchanged.
            continue
        else:
            blocks.append(block)
    return blocks


def _tool_definitions(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    result: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, Mapping):
            parameters = {}
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(function.get("description") or ""),
                    "parameters": dict(parameters),
                },
            }
        )
    return result


def tool_schema_map(tools: Any) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for definition in _tool_definitions(tools):
        function = definition.get("function") or definition
        name = str(function.get("name") or "")
        if not name:
            continue
        parameters = function.get("parameters")
        schemas[name] = dict(parameters) if isinstance(parameters, Mapping) else {}
    return schemas


def _coerce_tool_value(value: Any, schema: Mapping[str, Any], name: str) -> Any:
    expected = schema.get("type")
    if isinstance(expected, list):
        expected = next((item for item in expected if item != "null"), None)
    if expected == "integer":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
            return int(value.strip())
        raise ValueError(f"tool {name!r} field expects integer, got {type(value).__name__}")
    if expected == "number":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
        raise ValueError(f"tool {name!r} field expects number, got {type(value).__name__}")
    if expected == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in {
            "true",
            "false",
            "1",
            "0",
            "yes",
            "no",
        }:
            return value.strip().lower() in {"true", "1", "yes"}
        raise ValueError(f"tool {name!r} field expects boolean, got {value!r}")
    if expected == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return parsed
        raise ValueError(f"tool {name!r} field expects array")
    if expected == "object":
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, Mapping):
                return dict(parsed)
        raise ValueError(f"tool {name!r} field expects object")
    if expected == "string" and not isinstance(value, str):
        if isinstance(value, (int, float, bool)):
            return str(value)
        raise ValueError(f"tool {name!r} field expects string")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        if isinstance(value, str):
            for candidate in enum:
                if isinstance(candidate, str) and candidate.lower() == value.lower():
                    return candidate
    return value


def normalize_tool_arguments(
    name: str,
    arguments: Any,
    schema: Any,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Repair common tool-argument mistakes before returning them to a client.

    模型侧的参数失误（缺必填字段、类型写错）只修复和记录，不升级成请求失败：
    一条写坏的工具调用交给下游客户端报错、让模型下一轮自己补全，比整轮 400
    中断对话有用得多。只有连 JSON 都解析不出来的参数仍然按错误处理。
    """

    if arguments is None:
        args: dict[str, Any] = {}
    elif isinstance(arguments, Mapping):
        args = dict(arguments)
    elif isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            if strict:
                raise ValueError(f"tool {name!r} arguments are not valid JSON")
            return {"value": arguments}
        if isinstance(parsed, Mapping):
            args = dict(parsed)
        else:
            if strict:
                raise ValueError(f"tool {name!r} arguments must be a JSON object")
            return {"value": parsed}
    else:
        if strict:
            raise ValueError(f"tool {name!r} arguments must be a JSON object")
        return {"value": arguments}

    if not isinstance(schema, Mapping):
        return args
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required = schema.get("required")
    required = [str(key) for key in required] if isinstance(required, list) else []

    for key, prop in properties.items():
        if not isinstance(prop, Mapping):
            continue
        if key not in args and "default" in prop:
            args[str(key)] = copy.deepcopy(prop["default"])

    for key, value in list(args.items()):
        prop = properties.get(key)
        if not isinstance(prop, Mapping) or value is None:
            continue
        try:
            args[key] = _coerce_tool_value(value, prop, name)
        except ValueError:
            # 类型对不上就按原值下发，由客户端和模型下一轮自行纠正。
            logger.warning(
                "tool %r field %r keeps raw value: %r", name, key, value
            )

    missing = [key for key in required if key not in args or args[key] is None]
    if missing:
        logger.warning(
            "tool %r missing required field(s): %s", name, ", ".join(missing)
        )
    return args


def _tool_schema_budget() -> int:
    try:
        value = int(os.environ.get("TRAE_MOUTH_MAX_TOOL_SCHEMA_CHARS", "48000"))
    except (TypeError, ValueError):
        value = 48000
    return max(2048, value)


def _compact_tool_signature(definition: Mapping[str, Any]) -> dict[str, Any]:
    function = definition.get("function") or definition
    parameters = function.get("parameters") or {}
    properties = parameters.get("properties") if isinstance(parameters, Mapping) else {}
    fields: dict[str, Any] = {}
    if isinstance(properties, Mapping):
        for name, hint in properties.items():
            if not isinstance(hint, Mapping):
                fields[str(name)] = {"type": "string"}
                continue
            compact: dict[str, Any] = {}
            value_type = hint.get("type")
            if isinstance(value_type, (str, list)):
                compact["type"] = value_type
            enum = hint.get("enum")
            if isinstance(enum, list) and len(enum) <= 20:
                compact["enum"] = enum
            if value_type == "array" and isinstance(hint.get("items"), Mapping):
                item_type = hint["items"].get("type")
                if isinstance(item_type, str):
                    compact["items"] = {"type": item_type}
            fields[str(name)] = compact or {"type": "string"}
    signature: dict[str, Any] = {
        "name": str(function.get("name") or ""),
        "description": str(function.get("description") or "")[:240],
        "parameters": {
            "type": "object",
            "properties": fields,
        },
    }
    required = parameters.get("required") if isinstance(parameters, Mapping) else None
    if isinstance(required, list):
        signature["parameters"]["required"] = [str(value) for value in required]
    return signature


def _tool_prompt_payload(definitions: list[dict[str, Any]]) -> tuple[str, bool, int]:
    full = json.dumps(
        definitions, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    budget = _tool_schema_budget()
    if len(full) <= budget:
        return full, False, 0

    names = []
    for definition in definitions:
        function = definition.get("function") or definition
        name = str(function.get("name") or "")
        if name:
            names.append(name)
    discovery = {
        "tool_search",
        "search_tools",
        "list_tools",
        "plugin_search",
        "environment",
        "get_environment",
    }
    execution = {
        "shell",
        "shell_exec",
        "exec",
        "exec_command",
        "download",
        "download_file",
        "write",
        "write_file",
        "file_write",
        "edit",
        "edit_file",
        "apply_patch",
        "read",
        "read_file",
    }

    def priority(signature: Mapping[str, Any]) -> int:
        name = str(signature.get("name") or "").lower()
        basename = name.rsplit("__", 1)[-1]
        candidates = {name, basename}
        if candidates & discovery:
            return 0
        if candidates & execution:
            return 1
        return 2

    signatures = [_compact_tool_signature(definition) for definition in definitions]
    signatures.sort(key=priority)
    payload: dict[str, Any] = {
        "available_tools": names,
        "signatures": [],
        "omitted_signatures": len(signatures),
    }
    for signature in signatures:
        candidate = dict(payload)
        candidate["signatures"] = [*payload["signatures"], signature]
        candidate["omitted_signatures"] = len(signatures) - len(candidate["signatures"])
        encoded = json.dumps(
            candidate, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        if len(encoded) > budget:
            continue
        payload = candidate
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return encoded, True, int(payload["omitted_signatures"])


def build_tool_prompt(
    tools: Any,
    tool_choice: Any = None,
    parallel_tool_calls: Any = None,
) -> str:
    definitions = _tool_definitions(tools)
    if not definitions or tool_choice == "none":
        return ""
    tool_payload, compacted, omitted = _tool_prompt_payload(definitions)
    if compacted:
        schema_label = (
            "Available tools and compact input signatures (JSON). "
            "The full schema catalog exceeded the prompt budget"
        )
        if omitted:
            schema_label += (
                f"; {omitted} signature(s) are name-only. "
                "Use an available tool_search/plugin discovery tool before guessing arguments"
            )
        schema_label += ":"
    else:
        schema_label = "Available tools (JSON):"
    lines = [
        "You can call client tools. They execute on the caller's machine and are not server functions.",
        schema_label,
        tool_payload,
        "To call one or more tools, output only blocks in this exact format and no prose:",
        "<tool_call><function=tool_name><parameter=arg_name>value</parameter></function></tool_call>",
        "Use an exact declared tool name and write one <parameter> tag per argument. String values are plain text; object and array values are written as JSON inside the tag.",
        "The client assigns call ids; do not invent ids.",
        "Keep argument values as plain content: never put tool-call tags or markup inside a value, and describe such markup in words instead.",
        "Wait for the client tool result before continuing. Never claim a client file operation succeeded without a matching tool result.",
        "Lines starting with 'Client tool history' or '[client tool call]' are records of calls that already ran. Never echo, copy, or describe them as a new call.",
        "If another tool is needed, emit another tool-call block now instead of writing text such as 'Now calling ...'.",
        "Do not repeat a tool call whose result is already present in the conversation.",
    ]
    if tool_choice == "required":
        lines.append("Tool choice is required: call an available client tool now.")
    elif isinstance(tool_choice, Mapping):
        selected = tool_choice.get("function", tool_choice)
        if isinstance(selected, Mapping) and selected.get("name"):
            lines.append(
                f"Tool choice requires the client tool named {selected['name']}."
            )
    if parallel_tool_calls is False:
        lines.append("Request at most one tool per turn.")
    return "\n".join(lines)


def _truncate_tool_result(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = max(1, int(limit * 0.6))
    tail = max(1, limit - head)
    note = (
        f"\n...[tool result truncated: {len(text)} chars total; "
        f"showing first {head} and last {tail}; "
        "request a narrower range or offset for the missing part]...\n"
    )
    return text[:head] + note + text[-tail:]


def _tool_result_failed(message: Mapping[str, Any], text: str) -> bool:
    if message.get("is_error") is True or message.get("success") is False:
        return True
    if str(message.get("status") or "").lower() in {"error", "failed", "failure"}:
        return True
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except ValueError:
            payload = None
        if isinstance(payload, Mapping):
            if (
                payload.get("error")
                or payload.get("is_error") is True
                or payload.get("success") is False
                or payload.get("ok") is False
            ):
                return True
            if str(payload.get("status") or "").lower() in {
                "error",
                "failed",
                "failure",
            }:
                return True
    lowered = stripped.lower()
    return lowered.startswith("error") or lowered.startswith("failed")


def _estimate_tokens(text: str) -> int:
    """Rough token estimate tuned to CJK-heavy prompts.

    Measured against the IDE gateway: ~1.89 Chinese chars/token and
    ~4.5 English chars/token.  The values below stay slightly conservative.
    """

    if not text:
        return 0
    cjk = 0
    for char in text:
        code = ord(char)
        if (
            0x3040 <= code <= 0x30FF
            or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF
            or 0xFF00 <= code <= 0xFFEF
        ):
            cjk += 1
    other = len(text) - cjk
    return int(cjk / 1.8 + other / 4.3) + 1


def _image_blocks(message: Mapping[str, Any]) -> int:
    content = message.get("content")
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for part in content
        if isinstance(part, Mapping)
        and str(part.get("type") or "").lower() == "image_url"
    )


def _history_message_size(
    message: Mapping[str, Any], count_reasoning: bool = False
) -> int:
    text = _content_text(message.get("content"))
    size = len(text)
    role = str(message.get("role") or "").lower()
    if role in {"tool", "function"}:
        size += 40
    if count_reasoning:
        size += len(_message_reasoning(message))
    size += _image_blocks(message) * 4000
    return size


def _history_message_tokens(
    message: Mapping[str, Any], count_reasoning: bool = False
) -> int:
    tokens = _estimate_tokens(_content_text(message.get("content"))) + 4
    role = str(message.get("role") or "").lower()
    if role in {"tool", "function"}:
        tokens += 32
    if count_reasoning:
        tokens += _estimate_tokens(_message_reasoning(message))
    tokens += _image_blocks(message) * IMAGE_TOKENS
    return tokens


def _prepare_history(
    messages: list[Mapping[str, Any]], max_tool_result_chars: int
) -> list[Mapping[str, Any]]:
    """Truncate tool results before the budget is measured."""

    if max_tool_result_chars <= 0:
        return list(messages)
    prepared: list[Mapping[str, Any]] = []
    for message in messages:
        if isinstance(message, Mapping) and str(
            message.get("role") or ""
        ).lower() in {"tool", "function"}:
            text = _content_text(message.get("content"))
            truncated = _truncate_tool_result(text, max_tool_result_chars)
            if truncated != text:
                copy_message = dict(message)
                copy_message["content"] = truncated
                prepared.append(copy_message)
                continue
        prepared.append(message)
    return prepared


def _render_arguments_xml(arguments: Any, limit: int = 2000) -> str:
    """把参数对象写成桥接格式的 ``<parameter=...>`` 串。

    历史里用裸 JSON 展示参数时，模型会把它当成"写法示例"整段抄进正文，
    于是出现 ```json 围栏里的裸参数对象。改用目标格式展示，既去掉了可抄
    的样本，也给模型提供了一份正确示例。
    """

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return ""
    if not isinstance(arguments, Mapping):
        return ""
    parts: list[str] = []
    for key, value in arguments.items():
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        )
        if len(text) > limit:
            text = text[:limit] + f"…[{len(text)} chars]"
        text = text.replace("</parameter>", "<\\/parameter>")
        parts.append(f"<parameter={key}>{text}</parameter>")
    return "".join(parts)


def _render_call_xml(name: str, arguments: Any) -> str:
    rendered = _render_arguments_xml(arguments, limit=800)
    return f"<tool_call><function={name}>{rendered}</function></tool_call>"


def _completed_tool_call_reminder(messages: list[Mapping[str, Any]]) -> str:
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    results: dict[str, bool] = {}
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "")
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            for call in message["tool_calls"]:
                if not isinstance(call, Mapping):
                    continue
                call_id = str(
                    call.get("id")
                    or call.get("tool_call_id")
                    or call.get("toolCallId")
                    or ""
                )
                if not call_id:
                    continue
                function = (
                    call.get("function")
                    if isinstance(call.get("function"), Mapping)
                    else call
                )
                name = str(function.get("name") or call.get("name") or "")
                if not name:
                    continue
                arguments = function.get(
                    "arguments", function.get("input", call.get("arguments", {}))
                )
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        pass
                if not isinstance(arguments, Mapping):
                    arguments = {"value": arguments}
                calls[call_id] = {
                    "name": name,
                    "arguments": dict(arguments),
                }
                order.append(call_id)
        elif role == "tool":
            call_id = str(
                message.get("tool_call_id")
                or message.get("toolCallId")
                or ""
            )
            if call_id:
                results[call_id] = _tool_result_failed(
                    message, _content_text(message.get("content"))
                )

    completed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call_id in order:
        call = calls.get(call_id)
        if call is None or call_id not in results:
            continue
        # A failed result may legitimately be retried; only warn about success.
        if results[call_id]:
            continue
        key = json.dumps(call, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        completed.append(call)

    if not completed:
        return ""
    selected: list[str] = []
    total = 0
    for call in completed[:20]:
        rendered = _render_call_xml(
            str(call.get("name") or ""), call.get("arguments")
        )
        if selected and total + len(rendered) > 4000:
            break
        selected.append(rendered)
        total += len(rendered)
    omitted = len(completed) - len(selected)
    text = (
        "Completed client tool calls (already returned; do not repeat unless "
        "you need a fresh value):\n" + "\n".join(selected)
    )
    if omitted:
        text += f"\n({omitted} more completed call(s) omitted)"
    return text


# 历史预算里留给头部（对话开头）的比例。头部不动，上游前缀缓存才有稳定
# 的匹配起点；剩下的大头给最近的对话。
HEAD_BUDGET_RATIO = 0.25
# 预算小于这个量级时不做头部保留：那样只会挤掉更重要的近期对话。
HEAD_MIN_CHARS = 8192
HEAD_MIN_TOKENS = 4096


def _compact_history(
    messages: list[Mapping[str, Any]],
    max_chars: int,
    max_messages: int,
    max_prompt_tokens: int = 0,
    reserved_tokens: int = 0,
) -> list[Mapping[str, Any]]:
    """Keep system constraints plus a bounded recent history window.

    Token budgets follow the selected model's context window; the character
    budget stays as an upper bound.  Tool results are paired with their
    assistant call when the window starts inside a tool exchange, so the model
    never sees a dangling result.
    """

    if max_chars <= 0 and max_messages <= 0 and max_prompt_tokens <= 0:
        return list(messages)

    entries: list[tuple[int, Mapping[str, Any]]] = []
    for index, message in enumerate(messages):
        if isinstance(message, Mapping):
            entries.append((index, message))

    systems: list[tuple[int, Mapping[str, Any]]] = []
    non_system: list[tuple[int, Mapping[str, Any]]] = []
    seen_system: set[tuple[str, str]] = set()
    reasoning_index = -1
    for index, message in entries:
        role = str(message.get("role") or "user")
        if role in {"system", "developer"}:
            key = (role, _content_text(message.get("content")))
            if key in seen_system:
                continue
            seen_system.add(key)
            systems.append((index, message))
        else:
            non_system.append((index, message))
        if role == "assistant":
            reasoning_index = index
    if reasoning_index >= 0:
        last_assistant = messages[reasoning_index]
        calls = last_assistant.get("tool_calls")
        if isinstance(calls, list) and calls:
            # Reasoning is not sent together with tool calls.
            reasoning_index = -1

    if max_messages > 0:
        candidates = non_system[-max_messages:]
    else:
        candidates = non_system

    system_chars = sum(
        _history_message_size(message, index == reasoning_index)
        for index, message in systems
    )
    system_tokens = sum(
        _history_message_tokens(message, index == reasoning_index)
        for index, message in systems
    )
    remaining_chars = max_chars - system_chars if max_chars > 0 else -1
    remaining_tokens = (
        max_prompt_tokens - reserved_tokens - system_tokens
        if max_prompt_tokens > 0
        else -1
    )
    if max_prompt_tokens > 0 and remaining_tokens < 0:
        logger.warning(
            "system prompt alone exceeds token budget: %d + %d > %d",
            system_tokens,
            reserved_tokens,
            max_prompt_tokens,
        )

    # 头部固定：先按预算留住开头的若干条，剩下的预算再给尾部。
    #
    # 只从尾部反向累加时，历史一长窗口起点就逐轮前移，第一条用户消息跟着
    # 变化，整个前缀每轮都不同，上游前缀缓存只剩系统提示词能命中。头部
    # 留住不动，前缀就稳定，缓存才有意义。
    kept_head: list[tuple[int, Mapping[str, Any]]] = []
    head_chars = 0
    head_tokens = 0
    # 预算太小的时候不保留头部：挤掉的近期对话比省下的缓存更值钱
    head_char_limit = (
        int(remaining_chars * HEAD_BUDGET_RATIO)
        if remaining_chars >= HEAD_MIN_CHARS
        else 0
    )
    head_token_limit = (
        int(remaining_tokens * HEAD_BUDGET_RATIO)
        if remaining_tokens >= HEAD_MIN_TOKENS
        else 0
    )
    if candidates and (head_char_limit > 0 or head_token_limit > 0):
        for index, message in candidates:
            count_reasoning = index == reasoning_index
            size = _history_message_size(message, count_reasoning)
            tokens = _history_message_tokens(message, count_reasoning)
            if kept_head and (
                (head_char_limit > 0 and head_chars + size > head_char_limit)
                or (head_token_limit > 0 and head_tokens + tokens > head_token_limit)
            ):
                break
            kept_head.append((index, message))
            head_chars += size
            head_tokens += tokens

    kept_reversed: list[tuple[int, Mapping[str, Any]]] = []
    used_chars = head_chars
    used_tokens = head_tokens
    for index, message in reversed(candidates[len(kept_head) :]):
        count_reasoning = index == reasoning_index
        size = _history_message_size(message, count_reasoning)
        tokens = _history_message_tokens(message, count_reasoning)
        if kept_reversed and (
            (max_chars > 0 and used_chars + size > remaining_chars)
            or (max_prompt_tokens > 0 and used_tokens + tokens > remaining_tokens)
        ):
            break
        kept_reversed.append((index, message))
        used_chars += size
        used_tokens += tokens
    kept = kept_head + list(reversed(kept_reversed))

    # If the window starts with a tool result, include the assistant call
    # immediately before it; if that call is unavailable, drop the orphan.
    while kept and str(kept[0][1].get("role") or "") == "tool":
        position = next(
            (
                offset
                for offset, (index, _message) in enumerate(non_system)
                if index == kept[0][0]
            ),
            -1,
        )
        if position > 0:
            previous = non_system[position - 1][1]
            if (
                str(previous.get("role") or "") == "assistant"
                and isinstance(previous.get("tool_calls"), list)
            ):
                kept.insert(0, non_system[position - 1])
                break
        kept.pop(0)

    if not kept and non_system:
        kept = [non_system[-1]]

    if len(kept) < len(candidates):
        logger.info(
            "history trimmed: kept %d/%d messages, ~%d history tokens",
            len(kept),
            len(candidates),
            used_tokens,
        )

    return [message for _index, message in systems] + [
        message for _index, message in kept
    ]


def convert_messages(
    messages: list[Mapping[str, Any]],
    tools: Any = None,
    tool_choice: Any = None,
    parallel_tool_calls: Any = None,
    max_tool_result_chars: int = 0,
    max_history_chars: int = 0,
    max_history_messages: int = 0,
    max_prompt_tokens: int = 0,
    include_completed_calls: bool = True,
) -> list[dict[str, Any]]:
    messages = _prepare_history(messages, max_tool_result_chars)
    tool_prompt = build_tool_prompt(tools, tool_choice, parallel_tool_calls)
    # Reserve the stable tool block plus room for the tail reminder.
    reserved_tokens = _estimate_tokens(tool_prompt) + (
        2048 if tools and tool_choice != "none" else 256
    )
    messages = _compact_history(
        messages,
        max_history_chars,
        max_history_messages,
        max_prompt_tokens=max_prompt_tokens,
        reserved_tokens=reserved_tokens,
    )
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    call_records: dict[str, dict[str, Any]] = {}
    last_assistant_index = -1
    for index, message in enumerate(messages):
        if (
            isinstance(message, Mapping)
            and str(message.get("role") or "").lower() == "assistant"
        ):
            last_assistant_index = index
    reminder = ""
    if include_completed_calls and tools and tool_choice != "none":
        reminder = _completed_tool_call_reminder(messages)
    format_note = ""
    if tool_prompt:
        # 格式约束放在尾部：长上下文里系统提示头部的写法要求会被稀释，
        # 而每轮紧贴输入的这一行更难被忽略。
        format_note = (
            "Tool call format reminder: to call a tool output exactly "
            "<tool_call><function=tool_name><parameter=arg_name>value</parameter>"
            "</function></tool_call> with no prose around it. Never write the "
            "arguments as a ```json block."
        )

    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "user").lower()
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _content_text(content)
            if text:
                system_parts.append(text)
                system_parts.append("")
            continue
        if role == "assistant":
            raw_calls = message.get("tool_calls")
            if isinstance(raw_calls, list):
                for call in raw_calls:
                    if not isinstance(call, Mapping):
                        continue
                    function = (
                        call.get("function")
                        if isinstance(call.get("function"), Mapping)
                        else call
                    )
                    call_id = str(
                        call.get("id")
                        or call.get("tool_call_id")
                        or call.get("toolCallId")
                        or ""
                    )
                    name = str(function.get("name") or call.get("name") or "")
                    arguments = function.get(
                        "arguments", function.get("input", call.get("arguments", {}))
                    )
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            pass
                    if call_id:
                        call_records[call_id] = {
                            "name": name,
                            "arguments": arguments,
                        }
            # The assistant tool-call history is intentionally not echoed as
            # text.  Models tend to copy a textual call record as their next
            # answer, which looks like a tool call but is returned as content.
            # Tool results below carry id/name and their arguments in the
            # bridged <parameter> form instead of raw JSON.
            text = _content_text(content)
            blocks = _content_blocks(text)
            has_calls = isinstance(raw_calls, list) and bool(raw_calls)
            # Upstream rejects assistant reasoning combined with tool_calls;
            # keep it only for the most recent plain assistant turn.
            reasoning = (
                _message_reasoning(message)
                if index == last_assistant_index and not has_calls
                else ""
            )
            # 只有正文的 assistant 回合才发给上游：content 为空、仅带 reasoning
            # 的消息会被上游判成非法参数（Empty content + reasoning）。
            if blocks:
                item: dict[str, Any] = {"role": "assistant", "content": blocks}
                if reasoning:
                    # DeepSeek/agent upstreams may validate or use reasoning on
                    # assistant history.  Attach both spellings, matching the
                    # client-side roundtrip formats.
                    item["reasoning"] = reasoning
                    item["reasoning_content"] = reasoning
                converted.append(item)
            continue
        if role in {"tool", "function"}:
            text = _truncate_tool_result(
                _content_text(content), max_tool_result_chars
            )
            call_id = str(
                message.get("tool_call_id")
                or message.get("toolCallId")
                or ("legacy" if role == "function" else "unknown")
            )
            record = call_records.get(call_id, {})
            name = str(message.get("name") or record.get("name") or "tool")
            status = "failed" if _tool_result_failed(message, text) else "succeeded"
            rendered_arguments = _render_arguments_xml(record.get("arguments"))
            header = (
                f"Client tool result (already returned; use this value) "
                f"id={call_id} name={name} status={status}:\n"
            )
            if rendered_arguments:
                header += f"arguments: {rendered_arguments}\n"
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": header + text,
                        }
                    ],
                }
            )
            continue
        if role == "user":
            blocks = _content_blocks(content)
            if blocks:
                converted.append({"role": "user", "content": blocks})

    if tool_prompt:
        system_parts.append(tool_prompt)
    if system_parts:
        system = "\n".join(system_parts).strip()
        if system:
            converted.insert(
                0, {"role": "system", "content": [{"type": "text", "text": system}]}
            )
    tail_note = "\n".join(part for part in (format_note, reminder) if part)
    if tail_note:
        _attach_tail_note(converted, tail_note)
    return converted


def _attach_tail_note(
    converted: list[dict[str, Any]], note: str
) -> None:
    """Attach a per-turn note to the tail so the system prefix stays stable."""

    if not note or not converted:
        return
    last = converted[-1]
    if str(last.get("role") or "") != "user":
        converted.append({"role": "user", "content": [{"type": "text", "text": note}]})
        return
    content = last.get("content")
    if isinstance(content, list):
        content.append({"type": "text", "text": note})
    elif isinstance(content, str) and content:
        last["content"] = [
            {"type": "text", "text": content},
            {"type": "text", "text": note},
        ]
    else:
        last["content"] = [{"type": "text", "text": note}]


WEB_AGENT_TYPES = {
    "remote": "solo_agent_remote",
    "work": "solo_work_remote",
    "design": "solo_design_remote",
}
CHANNEL_ALIASES = {
    "ide": "ide",
    "chat": "ide",
    "solo": "solo",
    "agent": "remote",
    "remote": "remote",
    "work": "work",
    "design": "design",
}


def parse_model_channel(model: str) -> tuple[str, str]:
    """Split an optional ``channel/model`` or ``channel:model`` prefix."""
    requested = str(model or "").strip()
    lowered = requested.lower()
    for separator in ("/", ":"):
        if separator not in lowered:
            continue
        prefix, rest = requested.split(separator, 1)
        channel = CHANNEL_ALIASES.get(prefix.strip().lower())
        if channel and rest.strip():
            return channel, rest.strip()
    return "", requested


def resolve_model(
    catalog: Mapping[str, Any],
    model: str,
    *,
    prefer_max: bool = False,
    channel: str = "",
) -> Optional[dict[str, Any]]:
    requested = str(model or "").strip()
    if not requested:
        return None
    prefixed, stripped = parse_model_channel(requested)
    if prefixed:
        channel = prefixed
        requested = stripped
    if requested.lower().endswith("__max"):
        prefer_max = True
        requested = requested[: -len("__max")]
    elif requested.lower().endswith((":1m", "-1m")):
        prefer_max = True
        requested = requested[:-3]
    lowered = requested.lower()
    # 目录快照可能还没拉过；没有目录时按“模型不存在”处理，不抛异常
    groups = (catalog or {}).get("groups") or {}
    channel = CHANNEL_ALIASES.get(str(channel or "").strip().lower(), "")
    group_order = (
        [channel]
        if channel
        else ["ide", "solo", "remote", "work", "design"]
    )
    candidates: list[tuple[str, Mapping[str, Any], list[str]]] = []
    for group_key in group_order:
        group = groups.get(group_key)
        if not isinstance(group, Mapping):
            continue
        entries = [
            entry for entry in group.get("models") or [] if isinstance(entry, Mapping)
        ]
        exact = [
            entry
            for entry in entries
            if str(entry.get("config_name") or "").lower() == lowered
        ]
        matches = exact or [
            entry
            for entry in entries
            if str(entry.get("display_name") or "").lower() == lowered
        ]
        for entry in matches:
            providers = [
                str(value) for value in entry.get("provider_models") or [] if value
            ]
            if providers:
                candidates.append((group_key, entry, providers))
            elif group_key in WEB_AGENT_TYPES:
                candidates.append(
                    (group_key, entry, [str(entry.get("config_name") or requested)])
                )

    def build(
        group_key: str, entry: Mapping[str, Any], providers: list[str], selected: str
    ) -> dict[str, Any]:
        functions = [str(value) for value in entry.get("functions") or []]
        function = functions[0] if functions else (
            "chat_v3" if group_key == "ide" else "solo_agent"
        )
        if group_key == "solo" and "solo_agent" in functions:
            function = "solo_agent"
        web = group_key in WEB_AGENT_TYPES
        agent_type = WEB_AGENT_TYPES.get(group_key, "")
        return {
            "config_name": str(entry.get("config_name") or requested),
            "model_name": selected,
            "raw_chat_function": agent_type or function,
            "config_source": int(entry.get("config_source") or 1),
            "endpoint": group_key,
            "channel": group_key,
            "agent_type": agent_type,
            "web": web,
            "max_mode_applied": (
                selected.lower().endswith("__max") if not web else False
            ),
            "entry": entry,
        }

    if prefer_max:
        for group_key, entry, providers in candidates:
            selected = next(
                (value for value in providers if value.lower().endswith("__max")),
                "",
            )
            if selected:
                return build(group_key, entry, providers, selected)
    for group_key, entry, providers in candidates:
        selected = next(
            (value for value in providers if value.lower().endswith("__dev")),
            providers[0],
        )
        return build(group_key, entry, providers, selected)
    return None


# 目录里的档位是展示名，llm_raw_chat 线上只认 low / high / max：
# light→low、extra_high→max，其余按原值发。
WIRE_EFFORTS = {
    "light": "low",
    "lite": "low",
    "minimal": "low",
    "extra_high": "max",
    "xhigh": "max",
    "ultra": "max",
}


def wire_effort(level: str) -> str:
    text = str(level or "").strip()
    return WIRE_EFFORTS.get(text.lower(), text)


def build_body(
    resolved: Mapping[str, Any],
    messages: list[Mapping[str, Any]],
    *,
    tools: Any = None,
    tool_choice: Any = None,
    parallel_tool_calls: Any = None,
    session_id: str = "",
    reasoning_effort: str = "",
    max_tool_result_chars: int = 0,
    max_history_chars: int = 0,
    max_history_messages: int = 0,
    max_prompt_tokens: int = 0,
    include_completed_calls: bool = True,
) -> dict[str, Any]:
    sid = session_id or str(uuid.uuid4())
    body: dict[str, Any] = {
        "config_name": resolved["config_name"],
        "model_name": resolved["model_name"],
        "conversation_id": sid,
        "session_id": sid,
        "stream": True,
        "raw_chat_function": resolved["raw_chat_function"],
        "config_source": int(resolved.get("config_source") or 1),
        "messages": convert_messages(
            messages,
            tools,
            tool_choice,
            parallel_tool_calls,
            max_tool_result_chars=max_tool_result_chars,
            max_history_chars=max_history_chars,
            max_history_messages=max_history_messages,
            max_prompt_tokens=max_prompt_tokens,
            include_completed_calls=include_completed_calls,
        ),
    }
    if reasoning_effort:
        body["reasoning_effort"] = wire_effort(reasoning_effort)
    return body


async def stream_raw(
    config: Config,
    token: str,
    user_id: str,
    device: DeviceProfile,
    resolved: Mapping[str, Any],
    body: Mapping[str, Any],
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> AsyncIterator[dict[str, Any]]:
    function = str(resolved["raw_chat_function"])
    headers = ide_headers(
        config,
        token,
        user_id,
        device,
        function,
    )
    headers["Accept"] = "text/event-stream"
    headers["Connection"] = "keep-alive"
    urls = [
        f"{config.model_gateway}/api/ide/v2/llm_raw_chat",
        f"{config.model_gateway}/api/ide/v1/llm_raw_chat",
    ]
    fallback_statuses = {404, 405, 410, 501}
    timeout = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)
    owned_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout, transport=transport)
    response: Optional[httpx.Response] = None
    try:
        for index, url in enumerate(urls):
            request = client.build_request(
                "POST", url, headers=headers, json=dict(body)
            )
            try:
                response = await client.send(request, stream=True)
            except (httpx.HTTPError, OSError) as exc:
                raise UpstreamError(
                    f"upstream request failed: {exc}", status_code=502
                ) from exc
            if response.status_code < 400:
                break
            detail = (await response.aread()).decode("utf-8", "replace")[:500]
            if (
                index == 0
                and response.status_code in fallback_statuses
            ):
                await response.aclose()
                response = None
                continue
            raise UpstreamError(
                f"upstream HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
            )
        event = ""
        try:
            assert response is not None
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    yield {"event": "done", "data": {}}
                    return
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                yield {"event": event or str(data.get("event") or ""), "data": data}
                event = ""
        except (httpx.HTTPError, OSError) as exc:
            raise UpstreamError(
                f"upstream stream interrupted: {exc}", status_code=502
            ) from exc
    finally:
        if response is not None:
            await response.aclose()
        if owned_client:
            await client.aclose()


class TextAccumulator:
    def __init__(self) -> None:
        self.text = ""

    def add(self, fragment: Any) -> str:
        if fragment is None:
            return ""
        fragment = str(fragment)
        if not fragment:
            return ""
        if fragment.startswith(self.text):
            delta = fragment[len(self.text) :]
            self.text = fragment
            return delta
        if self.text.startswith(fragment):
            return ""
        common = 0
        limit = min(len(self.text), len(fragment))
        while common < limit and self.text[common] == fragment[common]:
            common += 1
        delta = fragment[common:]
        self.text += delta
        return delta

    def add_delta(self, fragment: Any) -> str:
        """Append one upstream delta chunk verbatim.

        Trae's IDE v2 ``output.response`` / ``output.reasoning_content`` and
        the legacy ``text_delta`` / ``reasoning_delta`` events are incremental
        chunks.  Treating them as cumulative text drops repeated chunks such as
        ``"a" + "a"`` and can corrupt streamed tool-call JSON.
        """

        if fragment is None:
            return ""
        fragment = str(fragment)
        if not fragment:
            return ""
        self.text += fragment
        return fragment



def _common_prefix(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _partial_marker_suffix(text: str, marker: str) -> int:
    limit = min(len(text), len(marker) - 1)
    for size in range(limit, 0, -1):
        if text[-size:] == marker[:size]:
            return size
    return 0


_NATIVE_MARKERS = (
    "<||dsml||tool_calls>",
    "<||dsml||function_calls>",
    "<||dsml||calls>",
    "<tool_call>",
    "<|functioncallbegin|>",
)


def _partial_native_suffix(text: str) -> int:
    """Length of a trailing fragment that may become a native call marker."""

    index = text.rfind("<")
    if index < 0:
        return 0
    fragment = text[index:]
    # 全角竖线换成半角、单双竖线视为同一种写法，再与标记比对
    compact = re.sub(r"\s+", "", fragment).replace("｜", "|").lower()
    compact = re.sub(r"\|+", "||", compact)
    if not compact:
        return 0
    for marker in _NATIVE_MARKERS:
        if marker.startswith(compact):
            return len(fragment)
    return 0


def _parse_tool_block(block: str) -> list[dict[str, Any]]:
    block = block.strip()
    if not block:
        return []
    data: Any = None
    try:
        data = json.loads(block)
    except ValueError:
        start = block.find("{")
        end = block.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(block[start : end + 1])
            except ValueError:
                data = None
    if data is None:
        return []
    items = data.get("tool_calls") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = [data]
    calls: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function") if isinstance(item.get("function"), Mapping) else item
        name = _call_field_name(item, function)
        if not name:
            continue
        arguments = _call_field_arguments(item, function)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        if not isinstance(arguments, Mapping):
            arguments = {"value": arguments}
        calls.append({"name": name, "arguments": dict(arguments)})
    return calls


class ToolBlockFilter:
    def __init__(self, allow_tool_calls: bool = True) -> None:
        self._seen: set[str] = set()
        self.incomplete = ""
        self.native_incomplete = ""
        self.native_truncated = False
        self.allow_tool_calls = allow_tool_calls

    def feed(
        self, text: str, *, final: bool = False
    ) -> tuple[str, list[dict[str, Any]]]:
        """Return client-visible text and newly completed calls.

        ``final`` marks the end of the output: nothing is held back any more
        and an unfinished call is reported instead of waited for.
        """
        if not self.allow_tool_calls:
            self.incomplete = ""
            self.native_incomplete = ""
            self.native_truncated = False
            return text, []
        visible: list[str] = []
        calls: list[dict[str, Any]] = []
        position = 0
        self.incomplete = ""
        self.native_incomplete = ""
        while True:
            start = text.find(TOOL_START, position)
            if start < 0:
                tail = text[position:]
                hold = _partial_marker_suffix(tail, TOOL_START)
                if hold:
                    self.incomplete = tail[-hold:]
                    tail = tail[:-hold]
                visible.append(tail)
                break
            visible.append(text[position:start])
            end = text.find(TOOL_END, start + len(TOOL_START))
            if end < 0:
                # Keep the unclosed payload so finalize() can salvage a valid
                # JSON body or report a clear interruption instead of silently
                # returning an empty assistant turn.
                self.incomplete = text[start + len(TOOL_START) :]
                break
            block = text[start + len(TOOL_START) : end]
            calls.extend(_parse_tool_block(block))
            position = end + len(TOOL_END)
        visible_text, legacy_calls = _split_legacy_calls("".join(visible))
        calls.extend(legacy_calls)
        visible_text, native_calls, native_incomplete, truncated = (
            _extract_native_tool_calls(visible_text, final=final)
        )
        calls.extend(native_calls)
        self.native_incomplete = native_incomplete
        self.native_truncated = truncated
        hold = (
            0 if final or native_incomplete else _partial_native_suffix(visible_text)
        )
        if hold:
            visible_text = visible_text[:-hold]
        new_calls: list[dict[str, Any]] = []
        for call in calls:
            key = json.dumps(call, sort_keys=True, ensure_ascii=False)
            if key in self._seen:
                continue
            self._seen.add(key)
            new_calls.append(call)
        return visible_text, new_calls


def map_usage(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    usage = raw
    for _ in range(3):
        nested = None
        for key in ("usage", "token_usage", "tokenUsage", "metrics", "data"):
            value = usage.get(key)
            if isinstance(value, Mapping):
                nested = value
                break
        if not nested:
            break
        usage = nested
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
    total = usage.get("total_tokens", 0)
    try:
        prompt = int(prompt or 0)
        completion = int(completion or 0)
        total = int(total or prompt + completion)
    except (TypeError, ValueError):
        return None
    result = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    reasoning = usage.get("reasoning_tokens")
    if isinstance(reasoning, (int, float)) and reasoning > 0:
        result["completion_tokens_details"] = {"reasoning_tokens": int(reasoning)}
    cached = usage.get("cache_read_input_tokens")
    if isinstance(cached, (int, float)) and cached > 0:
        result["prompt_tokens_details"] = {"cached_tokens": int(cached)}
    return result
