import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiGet, apiPatch, apiPost } from "../lib/api";
import type {
  AdminSettings,
  ConcurrencyInfo,
  InstanceInfo,
  UpdateInfo,
} from "../lib/types";
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
  "prewarm_catalogs",
  "native_tools",
] as const;

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, unknown>>({});
  const timers = useRef<Record<string, number>>({});
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordPending, setPasswordPending] = useState(false);
  const [updating, setUpdating] = useState(false);

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
  const updateQuery = useQuery({
    queryKey: ["admin", "update"],
    queryFn: () => apiGet<UpdateInfo>("/update", { silent: true }),
    refetchInterval: 3_600_000,
    staleTime: 3_600_000,
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
  }, [settings]);

  useEffect(
    () => () => {
      Object.values(timers.current).forEach((timer) => window.clearTimeout(timer));
    },
    [],
  );

  const saveMutation = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) =>
      apiPatch<{
        ok: boolean;
        persisted: boolean;
        changed: string[];
        settings?: Record<string, unknown>;
      }>("/settings", { values: { [key]: value } }),
    onSuccess: (data, variables) => {
      const actual = data.settings?.[variables.key];
      setValues((current) => {
        if (actual === undefined || current[variables.key] !== variables.value) {
          return current;
        }
        return { ...current, [variables.key]: actual };
      });
      queryClient.invalidateQueries({ queryKey: ["admin", "concurrency"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "models"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const updateMutation = useMutation({
    mutationFn: () =>
      apiPost<{
        ok: boolean;
        started?: boolean;
        latest?: string;
        error?: string;
      }>("/update/apply", {}),
    onSuccess: (data) => {
      if (!data.ok) {
        toast.error(
          data.error === "no_update"
            ? "当前已是最新版本"
            : data.error || "更新失败",
        );
        return;
      }
      setUpdating(true);
      toast.success("更新已开始，服务将自动重启");
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const checkUpdateMutation = useMutation({
    mutationFn: () => apiGet<UpdateInfo>("/update?force=1", { silent: true }),
    onSuccess: (data) => {
      queryClient.setQueryData(["admin", "update"], data);
      if (data.error) {
        toast.error(`检查失败：${data.error}`);
      } else if (data.available) {
        toast.success(`发现新版本 ${data.latest}`);
      } else {
        toast.success("已是最新版本");
      }
    },
    onError: (error: Error) => toast.error(error.message),
  });

  useEffect(() => {
    if (!updating) return;
    const latest = (updateQuery.data?.latest ?? "").replace(/^v/, "");
    const started = Date.now();
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch("/healthz", { cache: "no-store" });
        if (response.ok) {
          const data = await response.json();
          if (!latest || String(data.version ?? "") === latest) {
            window.location.reload();
            return;
          }
        }
      } catch {
        // The service is restarting.
      }
      if (Date.now() - started > 60_000) {
        window.clearInterval(timer);
        setUpdating(false);
        toast.error("更新未完成，请检查 data/update/update.log");
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [updating, updateQuery.data?.latest]);

  function setField(key: string, value: unknown) {
    setValues((current) => ({ ...current, [key]: value }));
    const currentTimer = timers.current[key];
    if (currentTimer) window.clearTimeout(currentTimer);
    if ((BOOLEAN_KEYS as readonly string[]).includes(key)) {
      delete timers.current[key];
      saveMutation.mutate({ key, value });
      return;
    }
    timers.current[key] = window.setTimeout(() => {
      delete timers.current[key];
      saveMutation.mutate({ key, value });
    }, 400);
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
  const totalConcurrency = Object.values(accounts).reduce(
    (sum, item) => sum + (item.limit ?? 0),
    0,
  );
  const updateInfo = updateQuery.data;
  const hasUpdate = Boolean(updateInfo?.available);

  return (
    <>
      <PageHeader
        title="设置"
        description="并发、上游、安全与运行信息，修改后自动保存"
        actions={
          <span className="text-xs text-muted">
            {saveMutation.isPending ? "保存中…" : "修改后自动保存"}
          </span>
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
              description="账号并发、冷却、失败换号与首字超时；换号次数最大为账号数 - 1"
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
                <NumberField
                  label="最多换号次数"
                  value={values.account_failover_limit}
                  onChange={(value) => setField("account_failover_limit", value)}
                />
                <NumberField
                  label="首字超时（秒）"
                  value={values.first_token_timeout}
                  onChange={(value) => setField("first_token_timeout", value)}
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
            description="限制请求体、工具输出和历史上下文大小"
          >
            <div className="grid grid-cols-2 gap-3">
              <NumberField
                label="请求体上限（字节）"
                value={values.max_request_bytes}
                onChange={(value) => setField("max_request_bytes", value)}
              />
              <NumberField
                label="工具结果最大字符"
                value={values.tool_result_max_chars}
                onChange={(value) => setField("tool_result_max_chars", value)}
              />
              <NumberField
                label="历史最大字符"
                value={values.history_max_chars}
                onChange={(value) => setField("history_max_chars", value)}
              />
              <NumberField
                label="历史最大消息数（0 = 不限）"
                value={values.history_max_messages}
                onChange={(value) => setField("history_max_messages", value)}
              />
            </div>
          </SectionCard>

          <SectionCard
            title="版本更新"
            description="从 GitHub Release 检查并安装更新"
          >
            {updateQuery.isLoading ? (
              <p className="text-13 text-muted">正在检查更新…</p>
            ) : (
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-13 text-ink">
                    当前版本{" "}
                    <span className="font-mono">
                      {updateInfo?.current || String(settings.version ?? "—")}
                    </span>
                    {hasUpdate ? (
                      <span className="text-muted">
                        {" → "}
                        <span className="font-mono text-accent">
                          {updateInfo?.latest}
                        </span>
                      </span>
                    ) : null}
                  </p>
                  <p className="mt-1 text-xs text-muted">
                    {updateInfo?.error
                      ? `检查失败：${updateInfo.error}`
                      : hasUpdate
                        ? "发现新版本"
                        : "已是最新版本"}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => checkUpdateMutation.mutate()}
                    disabled={checkUpdateMutation.isPending || updating}
                  >
                    {checkUpdateMutation.isPending ? "检查中…" : "检查更新"}
                  </Button>
                  {updateInfo?.release_url ? (
                    <Button variant="outline" size="sm" asChild>
                      <a
                        href={updateInfo.release_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Release
                      </a>
                    </Button>
                  ) : null}
                  {hasUpdate ? (
                    <Button
                      size="sm"
                      onClick={() => updateMutation.mutate()}
                      disabled={updateMutation.isPending || updating}
                    >
                      {updating
                        ? "更新中…"
                        : updateMutation.isPending
                          ? "下载中…"
                          : "一键更新"}
                    </Button>
                  ) : null}
                </div>
              </div>
            )}
            {hasUpdate && updateInfo?.notes ? (
              <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap rounded-tile bg-canvas p-3 text-xs text-muted">
                {updateInfo.notes}
              </pre>
            ) : null}
            {updating ? (
              <p className="mt-3 text-xs text-accent">
                更新已开始，服务重启后页面会自动刷新。
              </p>
            ) : null}
          </SectionCard>

          <SectionCard
            title="运行信息"
            description="并发状态与实例信息"
          >
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <InfoTile label="版本" value={instance?.version || String(settings.version ?? "—")} />
              <InfoTile label="账号数" value={instance?.accounts ?? "—"} />
              <InfoTile
                label="总并发上限"
                value={concurrencyQuery.isLoading ? "—" : totalConcurrency}
              />
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
  prewarm_catalogs: "模型目录预热",
};

const BOOLEAN_HINTS: Record<string, string> = {
  reasoning_as_content: "仅返回推理时回退为正文",
  strict_tool_args: "按 schema 校验和修复工具参数",
  tool_duplicate_warning: "在上下文中提醒已完成的工具调用",
  global_ide: "开启后模型列表和请求只使用 IDE",
  native_tools: "预留实验项，当前不生效",
  prewarm_catalogs: "启动时预加载模型目录，首次打开更快（下次启动生效）",
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
