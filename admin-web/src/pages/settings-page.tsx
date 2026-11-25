import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save } from "lucide-react";
import { toast } from "sonner";
import { apiGet, apiPatch, apiPost } from "../lib/api";
import type { AdminSettings, ConcurrencyInfo, InstanceInfo } from "../lib/types";
import { formatTime } from "../lib/format";
import { Button } from "../components/ui/button";
import { SectionCard } from "../components/ui/card";
import { PageHeader } from "../components/ui/feedback";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Skeleton } from "../components/ui/skeleton";
import { Switch } from "../components/ui/switch";

type SettingsResponse = AdminSettings & {
  settings?: Record<string, unknown>;
  raw?: Record<string, unknown>;
};

const BOOLEAN_KEYS = [
  "reasoning_as_content",
  "strict_tool_args",
  "tool_duplicate_warning",
  "global_ide",
  "native_tools",
] as const;

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [dirty, setDirty] = useState<Record<string, unknown>>({});
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordPending, setPasswordPending] = useState(false);

  const settingsQuery = useQuery({
    queryKey: ["admin", "settings"],
    queryFn: () => apiGet<SettingsResponse>("/settings"),
  });
  const concurrencyQuery = useQuery({
    queryKey: ["admin", "concurrency"],
    queryFn: () =>
      apiGet<
        Omit<ConcurrencyInfo, "accounts"> & {
          accounts: Record<string, { active: number; limit: number }>;
        }
      >("/concurrency"),
    refetchInterval: 15_000,
  });
  const instanceQuery = useQuery({
    queryKey: ["admin", "instance"],
    queryFn: () => apiGet<InstanceInfo>("/instance"),
  });
  const auditQuery = useQuery({
    queryKey: ["admin", "audit"],
    queryFn: () =>
      apiGet<{
        events: {
          id: number;
          username: string;
          action: string;
          detail: string;
          created_at: number;
        }[];
      }>("/audit?limit=30"),
  });

  const settings = useMemo(() => {
    const source = settingsQuery.data;
    return {
      ...(source?.raw ?? {}),
      ...(source?.settings ?? {}),
      ...(source ?? {}),
    } as Record<string, unknown>;
  }, [settingsQuery.data]);

  useEffect(() => {
    setValues(settings);
    setDirty({});
  }, [settings]);

  const saveMutation = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      apiPatch<{ ok: boolean; persisted: boolean; changed: string[] }>("/settings", {
        values: payload,
      }),
    onSuccess: (data) => {
      toast.success(data.persisted ? "已保存修改" : "已应用（仅本次进程生效）");
      setDirty({});
      queryClient.invalidateQueries({ queryKey: ["admin", "settings"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "concurrency"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "models"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  function setField(key: string, value: unknown) {
    setValues((current) => ({ ...current, [key]: value }));
    setDirty((current) => ({ ...current, [key]: value }));
  }

  async function changePassword(event: React.FormEvent) {
    event.preventDefault();
    if (newPassword.length < 8) {
      toast.error("新密码至少 8 位");
      return;
    }
    setPasswordPending(true);
    try {
      await apiPost("/password", {
        current_password: currentPassword,
        new_password: newPassword,
      });
      toast.success("管理员密码已更新");
      setCurrentPassword("");
      setNewPassword("");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "修改失败");
    } finally {
      setPasswordPending(false);
    }
  }

  const instance = instanceQuery.data ?? settingsQuery.data?.instance;
  const accounts = concurrencyQuery.data?.accounts ?? {};

  return (
    <>
      <PageHeader
        title="设置"
        description="并发、上游、安全与运行信息，保存后写入数据库"
        actions={
          <Button
            disabled={!Object.keys(dirty).length || saveMutation.isPending}
            onClick={() => saveMutation.mutate(dirty)}
          >
            <Save className="h-4 w-4" />
            {saveMutation.isPending ? "保存中…" : "保存修改"}
          </Button>
        }
      />

      {settingsQuery.isLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-40 w-full rounded-panel" />
          <Skeleton className="h-40 w-full rounded-panel" />
          <Skeleton className="h-40 w-full rounded-panel" />
        </div>
      ) : (
        <div className="space-y-4">
          <SectionCard
            title="管理员安全"
            description="修改管理员登录密码；更新后其他会话按现有策略继续有效"
          >
            <form onSubmit={changePassword} className="flex flex-wrap items-end gap-3">
              <div className="flex min-w-[180px] flex-1 flex-col gap-1.5">
                <Label htmlFor="current-password">当前密码</Label>
                <Input
                  id="current-password"
                  type="password"
                  autoComplete="current-password"
                  value={currentPassword}
                  onChange={(event) => setCurrentPassword(event.target.value)}
                  placeholder="请输入当前密码"
                />
              </div>
              <div className="flex min-w-[180px] flex-1 flex-col gap-1.5">
                <Label htmlFor="new-password">新密码（至少 8 位）</Label>
                <Input
                  id="new-password"
                  type="password"
                  autoComplete="new-password"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                  placeholder="留空则不修改"
                />
              </div>
              <Button type="submit" variant="outline" disabled={passwordPending || !newPassword}>
                {passwordPending ? "更新中…" : "更新密码"}
              </Button>
            </form>
          </SectionCard>

          <div className="grid gap-4 lg:grid-cols-2">
            <SectionCard
              title="并发与限流"
              description="单账号并发上限，即同一账号可同时处理的请求数"
            >
              <div className="grid grid-cols-2 gap-3">
                <NumberField
                  label="单账号并发数"
                  value={values.default_concurrency}
                  onChange={(value) => setField("default_concurrency", value)}
                />
                <NumberField
                  label="冷却时长（秒）"
                  value={values.account_cooldown}
                  onChange={(value) => setField("account_cooldown", value)}
                />
              </div>
            </SectionCard>

            <SectionCard
              title="上游配置"
              description="地址与超时修改后需重启服务生效"
            >
              <div className="grid gap-3">
                <TextField
                  label="上游地址"
                  value={values.model_gateway}
                  onChange={(value) => setField("model_gateway", value)}
                />
                <div className="grid grid-cols-2 gap-3">
                  <NumberField
                    label="刷新间隔（秒）"
                    value={values.refresh_interval}
                    onChange={(value) => setField("refresh_interval", value)}
                  />
                  <NumberField
                    label="模型目录缓存（秒）"
                    value={values.model_catalog_ttl}
                    onChange={(value) => setField("model_catalog_ttl", value)}
                  />
                </div>
              </div>
            </SectionCard>
          </div>

          <SectionCard
            title="行为开关"
            description="保存后影响新请求；原生工具为预留项"
          >
            <div className="grid gap-3 md:grid-cols-2">
              {BOOLEAN_KEYS.map((key) => (
                <ToggleRow
                  key={key}
                  label={BOOLEAN_LABELS[key]}
                  hint={BOOLEAN_HINTS[key]}
                  checked={Boolean(values[key])}
                  onChange={(checked) => setField(key, checked)}
                />
              ))}
            </div>
          </SectionCard>

          <SectionCard
            title="请求限制"
            description="限制单次 API 请求体大小"
          >
            <NumberField
              label="请求体上限（字节）"
              value={values.max_request_bytes}
              onChange={(value) => setField("max_request_bytes", value)}
            />
          </SectionCard>

          <SectionCard
            title="运行信息"
            description="并发状态与实例信息"
          >
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <InfoTile label="版本" value={instance?.version || String(settings.version ?? "—")} />
              <InfoTile label="账号数" value={instance?.accounts ?? "—"} />
              <InfoTile label="并发上限" value={concurrencyQuery.data?.default_limit ?? String(settings.default_concurrency ?? "—")} />
              <InfoTile label="PID" value={instance?.pid ?? "—"} />
              <InfoTile label="运行时长" value={instance?.uptime_seconds ? `${Math.floor(instance.uptime_seconds / 60)} 分钟` : "—"} />
              <InfoTile label="启动时间" value={formatTime(instance?.started_at)} />
              <InfoTile label="运行模式" value={instance?.mode || "—"} />
              <InfoTile label="数据库" value={instance?.database || "—"} />
            </div>
            <div className="mt-4 overflow-hidden rounded-tile border border-border">
              <div className="grid grid-cols-3 bg-canvas px-3 py-2 text-xs text-muted">
                <span>账号</span>
                <span className="text-center">占用</span>
                <span className="text-right">上限</span>
              </div>
              {Object.keys(accounts).length === 0 ? (
                <p className="px-3 py-4 text-center text-xs text-muted">
                  当前没有正在跟踪的账号并发。
                </p>
              ) : (
                Object.entries(accounts).map(([id, value]) => (
                  <div key={id} className="grid grid-cols-3 border-t border-border px-3 py-2 text-13">
                    <span className="truncate font-mono text-xs">{id}</span>
                    <span className="text-center">{value.active}</span>
                    <span className="text-right">{value.limit}</span>
                  </div>
                ))
              )}
            </div>
          </SectionCard>

          <SectionCard title="审计日志" description="最近的管理员操作">
            <div className="max-h-[280px] divide-y divide-border/70 overflow-y-auto text-13">
              {(auditQuery.data?.events ?? []).length === 0 ? (
                <p className="py-3 text-center text-xs text-muted">暂无审计记录</p>
              ) : (
                (auditQuery.data?.events ?? []).map((item) => (
                  <div key={item.id} className="flex items-center justify-between gap-3 py-2">
                    <div className="min-w-0">
                      <p className="text-ink">{item.action}</p>
                      <p className="truncate text-xs text-muted">{item.detail || "—"}</p>
                    </div>
                    <div className="shrink-0 text-right text-xs text-muted">
                      <p>{item.username || "admin"}</p>
                      <p>{formatTime(item.created_at)}</p>
                    </div>
                  </div>
                ))
              )}
            </div>
          </SectionCard>

          <SectionCard title="只读配置" description="以下字段需修改 .env 并重启服务">
            <details>
              <summary className="cursor-pointer text-13 text-muted">
                展开查看 {Object.keys(settings).length} 项
              </summary>
              <dl className="mt-3 divide-y divide-border/70 text-13">
                {Object.entries(settings)
                  .filter(([key]) => key !== "ok" && key !== "settings" && key !== "raw")
                  .map(([key, value]) => (
                    <div key={key} className="flex items-center justify-between gap-4 py-2">
                      <dt className="font-mono text-xs text-muted">{key}</dt>
                      <dd className="break-all text-right text-xs text-ink">
                        {typeof value === "object" ? JSON.stringify(value) : String(value)}
                      </dd>
                    </div>
                  ))}
              </dl>
            </details>
          </SectionCard>
        </div>
      )}
    </>
  );
}

const BOOLEAN_LABELS: Record<string, string> = {
  reasoning_as_content: "推理内容回退正文",
  strict_tool_args: "严格工具参数",
  tool_duplicate_warning: "重复工具调用提醒",
  global_ide: "全局 IDE",
  native_tools: "原生工具",
};

const BOOLEAN_HINTS: Record<string, string> = {
  reasoning_as_content: "仅返回推理时回退为正文",
  strict_tool_args: "按 schema 校验和修复工具参数",
  tool_duplicate_warning: "在上下文中提醒已完成的工具调用",
  global_ide: "开启后模型列表和请求只使用 IDE",
  native_tools: "预留实验项，当前不生效",
};

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: unknown;
  onChange: (value: number) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      <Input
        inputMode="numeric"
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(event) => onChange(Number(event.target.value.replace(/[^\d.-]/g, "")))}
      />
    </div>
  );
}

function TextField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: unknown;
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      <Input
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

function ToggleRow({
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

function InfoTile({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-tile bg-canvas p-3">
      <p className="text-xs text-muted">{label}</p>
      <p className="mt-1 truncate text-13 font-medium text-ink">{value}</p>
    </div>
  );
}
