import { useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { LoaderCircle, Play, Square, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { apiGet, API_BASE } from "../lib/api";
import type { Account, AdminModel, PlaygroundResult } from "../lib/types";
import { Button } from "../components/ui/button";
import { SectionCard } from "../components/ui/card";
import { PageHeader } from "../components/ui/feedback";
import { Label } from "../components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../components/ui/select";
import { Switch } from "../components/ui/switch";
import { Textarea } from "../components/ui/input";

type AccountList = { ok: boolean; accounts: Account[] };
type ModelList = { models: AdminModel[] };

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|; )tm_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export function PlaygroundPage() {
  const [accountId, setAccountId] = useState("auto");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("__default__");
  const [oneM, setOneM] = useState(true);
  const [streaming, setStreaming] = useState(true);
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  const [text, setText] = useState("");
  const [result, setResult] = useState<PlaygroundResult | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const accountsQuery = useQuery({
    queryKey: ["admin", "accounts"],
    queryFn: () => apiGet<AccountList>("/accounts"),
    refetchInterval: 30_000,
  });
  const modelsQuery = useQuery({
    queryKey: ["admin", "models"],
    queryFn: () => apiGet<ModelList>("/models"),
  });

  const selectedModel = useMemo(
    () => modelsQuery.data?.models.find((item) => item.id === model),
    [modelsQuery.data?.models, model],
  );
  const levels = selectedModel?.reasoning_levels ?? [];

  async function send() {
    if (!model) {
      toast.error("请选择模型");
      return;
    }
    if (!message.trim()) {
      toast.error("请输入消息");
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setPending(true);
    setText("");
    setResult(null);
    try {
      const response = await fetch(`${API_BASE}/playground/chat`, {
        method: "POST",
        credentials: "same-origin",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          "X-CSRF-Token": csrfToken(),
        },
        body: JSON.stringify({
          model,
          message: message.trim(),
          account_id: accountId === "auto" ? "" : accountId,
          one_m: oneM,
          reasoning_effort: effort === "__default__" ? "" : effort,
          stream: streaming,
        }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        throw new Error(data?.detail || data?.error?.message || `请求失败（HTTP ${response.status}）`);
      }
      if (!streaming) {
        const data = (await response.json()) as PlaygroundResult;
        setResult(data);
        setText(data.content || "");
        return;
      }
      if (!response.body) throw new Error("响应无数据流");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let accumulated = "";
      const calls: Record<number, { name?: string; arguments?: string }> = {};
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const raw of lines) {
          const line = raw.trim();
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload || payload === "[DONE]") continue;
          let event: unknown;
          try {
            event = JSON.parse(payload);
          } catch {
            continue;
          }
          if (!event || typeof event !== "object") continue;
          const data = event as Record<string, unknown>;
          if (data.type === "text" && typeof data.text === "string") {
            accumulated += data.text;
            setText(accumulated);
          } else if (data.type === "tool_call") {
            const index = Number(data.index ?? 0);
            const current = calls[index] ?? {};
            if (typeof data.name === "string") current.name = data.name;
            if (typeof data.arguments === "string") current.arguments = data.arguments;
            calls[index] = current;
          } else if (data.type === "error") {
            throw new Error(String(data.message || "上游返回错误"));
          } else if (data.type === "result" && data.result) {
            setResult(data.result as PlaygroundResult);
          } else if (data.type === "usage") {
            setResult((current) => ({
              content: current?.content ?? accumulated,
              reasoning: current?.reasoning ?? "",
              tool_calls: current?.tool_calls ?? [],
              usage: data as Record<string, unknown>,
            }));
          } else if (data.type === "done" && data.result) {
            setResult(data.result as PlaygroundResult);
          }
        }
      }
      if (Object.keys(calls).length) {
        setResult((current) => ({
          content: accumulated,
          reasoning: current?.reasoning ?? "",
          tool_calls: Object.values(calls),
          usage: current?.usage ?? null,
        }));
      }
    } catch (error) {
      if ((error as Error).name !== "AbortError") {
        toast.error(error instanceof Error ? error.message : "请求失败");
      }
    } finally {
      setPending(false);
      abortRef.current = null;
    }
  }

  function stop() {
    abortRef.current?.abort();
    setPending(false);
  }

  return (
    <>
      <PageHeader
        title="测试台"
        description="通过管理登录态验证聊天链路，无需业务 API Key"
        actions={
          pending ? (
            <Button variant="outline" onClick={stop}>
              <Square className="h-4 w-4" />
              停止
            </Button>
          ) : undefined
        }
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <SectionCard title="请求" description="可选账号、1M 与思考等级">
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label>账号</Label>
              <Select value={accountId} onValueChange={setAccountId}>
                <SelectTrigger>
                  <SelectValue placeholder="自动选择" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">自动选择</SelectItem>
                  {(accountsQuery.data?.accounts ?? []).map((account) => (
                    <SelectItem key={account.id} value={account.id}>
                      {account.label || account.username || account.id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>模型</Label>
              <Select
                value={model}
                onValueChange={(value) => {
                  setModel(value);
                  setEffort("__default__");
                }}
              >
                <SelectTrigger>
                  <SelectValue placeholder={modelsQuery.isLoading ? "加载模型…" : "选择模型"} />
                </SelectTrigger>
                <SelectContent>
                  {(modelsQuery.data?.models ?? []).map((item) => (
                    <SelectItem key={item.id} value={item.id}>
                      {item.id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>思考等级</Label>
              <Select value={effort} onValueChange={setEffort} disabled={levels.length === 0}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__default__">默认</SelectItem>
                  {levels.map((level) => (
                    <SelectItem key={level} value={level}>
                      {level}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <ToggleLine
              label="1M 上下文"
              hint="关闭后使用非 1M 配置"
              checked={oneM}
              onChange={setOneM}
            />
            <ToggleLine
              label="流式输出"
              hint="关闭后等待完整响应"
              checked={streaming}
              onChange={setStreaming}
            />
            <div className="flex flex-col gap-1.5">
              <Label>消息</Label>
              <Textarea
                rows={5}
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                placeholder="输入测试消息…"
              />
            </div>
            <div className="flex gap-2">
              <Button onClick={send} disabled={pending}>
                {pending ? (
                  <>
                    <LoaderCircle className="h-4 w-4 animate-spin" />
                    发送中…
                  </>
                ) : (
                  <>
                    <Play className="h-4 w-4" />
                    发送
                  </>
                )}
              </Button>
              <Button
                variant="outline"
                onClick={() => {
                  setText("");
                  setResult(null);
                }}
              >
                <Trash2 className="h-4 w-4" />
                清空输出
              </Button>
            </div>
          </div>
        </SectionCard>

        <SectionCard title="响应" description="正文与 tool_calls；推理内容已过滤">
          {pending && streaming ? (
            <pre className="min-h-[320px] whitespace-pre-wrap break-words rounded-tile bg-canvas p-4 text-13">
              {text || "…"}
            </pre>
          ) : result || text ? (
            <div className="flex flex-col gap-4">
              <div>
                <p className="mb-1.5 text-xs font-medium text-muted">正文</p>
                <pre className="min-h-[120px] whitespace-pre-wrap break-words rounded-tile bg-canvas p-4 text-13">
                  {result?.content || text || "（空）"}
                </pre>
              </div>
              {result?.tool_calls?.length ? (
                <div>
                  <p className="mb-1.5 text-xs font-medium text-muted">tool_calls</p>
                  <pre className="max-h-[320px] overflow-auto rounded-tile bg-canvas p-4 font-mono text-xs">
                    {JSON.stringify(result.tool_calls, null, 2)}
                  </pre>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="flex min-h-[320px] items-center justify-center rounded-tile border border-dashed border-border text-13 text-muted">
              发送一条消息查看响应
            </div>
          )}
        </SectionCard>
      </div>
    </>
  );
}

function ToggleLine({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-tile border border-border bg-canvas px-3 py-2.5">
      <div>
        <p className="text-13 text-ink">{label}</p>
        <p className="mt-0.5 text-xs text-muted">{hint}</p>
      </div>
      <Switch checked={checked} onCheckedChange={onChange} />
    </div>
  );
}
