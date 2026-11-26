import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiDelete, apiGet, apiPatch, apiPost } from "../lib/api";
import type {
  AdminSettings,
  ConcurrencyInfo,
  InstanceInfo,
  MaintenanceJob,
  MaintenanceStatus,
  UpdateInfo,
  UpdateJob,
  UpdateStatus,
} from "../lib/types";
import { formatTime } from "../lib/format";
import { Progress } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { SectionCard } from "../components/ui/card";
import { PageHeader } from "../components/ui/feedback";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { SegmentedControl } from "../components/ui/segmented";
import { Skeleton } from "../components/ui/skeleton";
import { Switch } from "../components/ui/switch";

type SettingsResponse = AdminSettings & {
  settings?: Record<string, unknown>;
  raw?: Record<string, unknown>;
};

const SETTINGS_TABS = [
  { value: "general", label: "通用" },
  { value: "account", label: "账号" },
  { value: "system", label: "系统" },
  { value: "security", label: "安全" },
] as const;

type SettingsTab = (typeof SETTINGS_TABS)[number]["value"];

function readSettingsTab(): SettingsTab {
  const hash = window.location.hash.replace("#", "");
  return SETTINGS_TABS.some((item) => item.value === hash)
    ? (hash as SettingsTab)
    : "general";
}

const BOOLEAN_KEYS = [
  "reasoning_as_content",
  "strict_tool_args",
  "tool_duplicate_warning",
  "global_ide",
  "prewarm_catalogs",
  "native_tools",
] as const;

// 重启期间服务不可达属于正常现象，超过这个时长才判定失败
const UPDATE_OFFLINE_LIMIT = 10 * 60_000;
// 重启后前端文件已整体替换，用它记下结果，刷新后再提示
const UPDATE_DONE_KEY = "trae-mouth:update-done";

function isActiveJob(job: UpdateJob | null) {
  return (
    job?.status === "downloading" ||
    job?.status === "installing" ||
    job?.status === "restarting"
  );
}

function updateButtonLabel(job: UpdateJob | null, pending: boolean) {
  // 进度放在卡片下方的进度条里，按钮只表明当前阶段
  if (job?.status === "downloading") return "下载中…";
  if (job?.status === "installing") return "安装中…";
  if (job?.status === "restarting") return "重启中…";
  return pending ? "准备中…" : "一键更新";
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, unknown>>({});
  const timers = useRef<Record<string, number>>({});
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordPending, setPasswordPending] = useState(false);
  const [job, setJob] = useState<UpdateJob | null>(null);
  const jobActive = isActiveJob(job);
  const [tab, setTab] = useState<SettingsTab>(readSettingsTab);

  // 分类写进 hash，刷新或从别处跳回来都能停在原来那一页
  useEffect(() => {
    const sync = () => setTab(readSettingsTab());
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  function changeTab(next: SettingsTab) {
    setTab(next);
    window.location.hash = next === "general" ? "" : next;
  }

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
  const maintenanceQuery = useQuery({
    queryKey: ["admin", "maintenance"],
    queryFn: () => apiGet<MaintenanceStatus>("/maintenance"),
    refetchInterval: 15_000,
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
        job?: UpdateJob;
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
      if (data.job) setJob(data.job);
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

  // 进度由服务端保存，进入页面时先同步一次：刷新或换设备都能接着看到
  useEffect(() => {
    let cancelled = false;
    const flash = window.localStorage.getItem(UPDATE_DONE_KEY);
    apiGet<UpdateStatus>("/update/status", { silent: true })
      .then((data) => {
        if (cancelled) return;
        if (data.job) setJob(data.job);
        if (data.job?.status === "done") {
          toast.success(`已更新到 ${data.job.target}`);
          void apiDelete("/update", { silent: true });
          setJob(null);
        } else if (flash) {
          toast.success(`已更新到 ${flash}`);
        }
        if (flash) window.localStorage.removeItem(UPDATE_DONE_KEY);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!jobActive) return;
    let cancelled = false;
    let inFlight = false;
    let offlineSince = 0;

    const timer = window.setInterval(async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const data = await apiGet<UpdateStatus>("/update/status", { silent: true });
        if (cancelled) return;
        offlineSince = 0;
        if (!data.job) {
          setJob(null);
          return;
        }
        if (data.job.status === "done") {
          window.localStorage.setItem(UPDATE_DONE_KEY, data.job.target);
          window.location.reload();
          return;
        }
        setJob(data.job);
        if (data.job.status === "failed") {
          toast.error(`更新失败：${data.job.error || "未知原因"}`);
        }
      } catch {
        if (cancelled) return;
        if (!offlineSince) offlineSince = Date.now();
        if (Date.now() - offlineSince > UPDATE_OFFLINE_LIMIT) {
          window.clearInterval(timer);
          setJob((current) =>
            current
              ? {
                  ...current,
                  status: "failed",
                  error: "服务长时间无响应，请检查 data/update/update.log",
                }
              : current,
          );
          toast.error("更新未完成，服务长时间无响应");
        } else {
          // 安装和重启期间服务不可达，按重启中展示
          setJob((current) =>
            current ? { ...current, status: "restarting" } : current,
          );
        }
      } finally {
        inFlight = false;
      }
    }, 1000);
    return () => {
      window.clearInterval(timer);
      cancelled = true;
    };
  }, [jobActive]);

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

  // 积分刷新时刻可以填多个，全部写合法了才保存
  function saveCreditTimes(value: string) {
    setValues((current) => ({ ...current, credit_refresh_times: value }));
    if (splitTimes(value) === null) return;
    saveMutation.mutate({ key: "credit_refresh_times", value });
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
  // 两段进度都换算成 0-100：下载阶段用后端百分比，进入安装后下载固定为 100
  const downloadPercent = !job
    ? 0
    : job.status === "downloading"
      ? Math.min(99, job.percent ?? 0)
      : 100;
  const installPercent = !job
    ? 0
    : job.status === "installing"
      ? Math.min(99, job.percent ?? 0)
      : job.status === "restarting" || job.status === "done"
        ? 100
        : 0;

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

      {/* 页头自带 32px 下边距，这里抵掉一半，让 Tab 上下间距与卡片间距一致（16px） */}
      <div className="-mt-4 mb-4">
        <SegmentedControl
          value={tab}
          onChange={changeTab}
          options={SETTINGS_TABS}
          ariaLabel="设置分类"
        />
      </div>

      {settingsQuery.isLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-40 w-full rounded-panel" />
          <Skeleton className="h-40 w-full rounded-panel" />
          <Skeleton className="h-40 w-full rounded-panel" />
        </div>
      ) : (
        <div className="space-y-4">
          {tab === "security" ? (
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
          ) : null}

          {tab === "account" ? (
          <>
          <SectionCard
            title="账号设置"
            description="针对全部账号的定时任务和批量刷新都按错峰方式执行：每次打乱顺序，账号之间留出间隔和随机抖动"
          >
            <div className="grid gap-3 md:grid-cols-2">
              <NumberField
                label="账号间隔（秒）"
                hint="每个账号之间至少隔多久"
                value={values.account_stagger_seconds}
                onChange={(value) => setField("account_stagger_seconds", value)}
              />
              <NumberField
                label="随机抖动上限（秒）"
                hint="在间隔之上再随机多等一段时间"
                value={values.account_jitter_seconds}
                onChange={(value) => setField("account_jitter_seconds", value)}
              />
              <TimeField
                label="令牌刷新时刻"
                hint="每天这时处理即将到期的令牌；留空则只保留每小时的兜底检查"
                value={values.token_refresh_time}
                onChange={(value) => setField("token_refresh_time", value)}
              />
              <NumberField
                label="令牌刷新提前量（小时）"
                hint="令牌剩余不足这么多时间才刷新，与真实客户端一致默认 24"
                value={values.token_refresh_margin_hours}
                onChange={(value) => setField("token_refresh_margin_hours", value)}
              />
              <TimeField
                label="账号保活时刻"
                hint="每天这时逐个账号确认状态；留空则关闭"
                value={values.keepalive_time}
                onChange={(value) => setField("keepalive_time", value)}
              />
              <TextField
                label="积分刷新时刻"
                hint="多个用逗号隔开，例如 08:30, 14:30, 20:30；留空则关闭"
                value={values.credit_refresh_times}
                onChange={saveCreditTimes}
              />
              <TimeField
                label="模型目录刷新时刻"
                hint="每天这时用一个账号拉一次目录，账号轮流使用；留空则关闭"
                value={values.catalog_refresh_time}
                onChange={(value) => setField("catalog_refresh_time", value)}
              />
            </div>
            <MaintenanceStatusLines status={maintenanceQuery.data} />
          </SectionCard>

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
          </>
          ) : null}

          {tab === "general" ? (
          <>
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
          </>
          ) : null}

          {tab === "system" ? (
          <>
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
                    disabled={checkUpdateMutation.isPending || jobActive}
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
                      className="min-w-[6.5rem]"
                      onClick={() => updateMutation.mutate()}
                      disabled={updateMutation.isPending || jobActive}
                    >
                      {updateButtonLabel(job, updateMutation.isPending)}
                    </Button>
                  ) : null}
                </div>
              </div>
            )}
            {jobActive ? (
              <div className="mt-4 space-y-3">
                <div>
                  <div className="mb-1 flex items-center justify-between text-xs">
                    <span className="text-muted">
                      下载{job?.detail && job.status === "downloading" ? ` · ${job.detail}` : ""}
                    </span>
                    <span className="font-mono text-ink">{downloadPercent}%</span>
                  </div>
                  <Progress value={downloadPercent} />
                </div>
                <div>
                  <div className="mb-1 flex items-center justify-between text-xs">
                    <span className="text-muted">
                      安装{job?.detail && job.status === "installing" ? ` · ${job.detail}` : ""}
                    </span>
                    <span className="font-mono text-ink">{installPercent}%</span>
                  </div>
                  <Progress value={installPercent} />
                </div>
                <p className="text-xs text-accent">
                  {job?.status === "restarting"
                    ? "更新已安装，服务重启后页面会自动刷新。"
                    : "正在更新，请勿关闭页面。"}
                </p>
              </div>
            ) : null}
            {hasUpdate && updateInfo?.notes ? (
              <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap rounded-tile bg-canvas p-3 text-xs text-muted">
                {updateInfo.notes}
              </pre>
            ) : null}
            {job?.status === "failed" ? (
              <p className="mt-3 text-xs text-danger">
                更新失败：{job.error || "未知原因"}
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
          </>
          ) : null}
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
  hint,
  value,
  onChange,
}: {
  label: string;
  hint?: string;
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
      {hint ? <p className="text-xs text-muted">{hint}</p> : null}
    </div>
  );
}

function TextField({
  label,
  hint,
  value,
  onChange,
}: {
  label: string;
  hint?: string;
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
      {hint ? <p className="text-xs text-muted">{hint}</p> : null}
    </div>
  );
}

function TimeField({
  label,
  hint,
  value,
  onChange,
}: {
  label: string;
  hint?: string;
  value: unknown;
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      <Input
        type="time"
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(event) => onChange(event.target.value)}
      />
      {hint ? <p className="text-xs text-muted">{hint}</p> : null}
    </div>
  );
}

const TIME_PART = /^([01]\d|2[0-3]):[0-5]\d$/;

/** 拆出多个时刻；返回 null 表示还有没写完整的项。 */
function splitTimes(value: string): string[] | null {
  const parts = value
    .trim()
    .split(/[,，;；\s]+/)
    .filter(Boolean);
  return parts.every((part) => TIME_PART.test(part)) ? parts : null;
}

function describeClock(epoch: number): string {
  const date = new Date(epoch * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function describeNext(job: MaintenanceJob): string {
  if (job.running) return "进行中…";
  if (!job.slots.length) return "未设定";
  return job.next_run ? describeClock(job.next_run) : "今天已完成";
}

function describeResult(job: MaintenanceJob): {
  text: string;
  tone: "muted" | "danger";
} {
  const last = job.last;
  if (!last) return { text: "未运行", tone: "muted" };
  return {
    text: `${describeClock(last.finished)} · 成功 ${last.ok} · 失败 ${last.failed} · 跳过 ${last.skipped}`,
    tone: last.failed > 0 ? "danger" : "muted",
  };
}

function MaintenanceStatusLines({ status }: { status?: MaintenanceStatus }) {
  if (!status) {
    return <p className="mt-4 text-xs text-muted">正在读取账号维护状态…</p>;
  }
  const { jobs, safety, endpoints, invalid_times: invalid } = status;
  const errors = [
    invalid.token_refresh_time
      ? "令牌刷新时刻格式无效，任务已停用；请改成 HH:MM 或留空"
      : "",
    invalid.keepalive_time
      ? "账号保活时刻格式无效，任务已停用；请改成 HH:MM 或留空"
      : "",
    invalid.credit_refresh_times
      ? "积分刷新时刻格式无效，任务已停用；请写成 HH:MM，多个用逗号隔开"
      : "",
    invalid.catalog_refresh_time
      ? "模型目录刷新时刻格式无效，任务已停用；请改成 HH:MM 或留空"
      : "",
  ].filter(Boolean);
  const safetyJob: MaintenanceJob = {
    ...jobs.token,
    slots: ["hourly"],
    next_run: safety.next_run,
    running: false,
    last: safety.last,
  };
  const rows: { label: string; job: MaintenanceJob }[] = [
    { label: "令牌刷新", job: jobs.token },
    { label: "令牌兜底", job: safetyJob },
    { label: "账号保活", job: jobs.keepalive },
    { label: "积分刷新", job: jobs.credits },
    { label: "模型目录", job: jobs.catalogs },
  ];
  return (
    <div className="mt-4">
      {errors.length ? (
        <div className="mb-3 space-y-1">
          {errors.map((text) => (
            <p key={text} className="text-xs text-danger">
              {text}
            </p>
          ))}
        </div>
      ) : null}
      <div className="overflow-hidden rounded-tile border border-border">
        <div className="grid grid-cols-[5rem_5.5rem_1fr] gap-3 bg-canvas px-3 py-1.5 text-xs text-muted">
          <span>任务</span>
          <span>下次</span>
          <span className="text-right">上次结果</span>
        </div>
        {rows.map(({ label, job }) => {
          const result = describeResult(job);
          return (
            <div
              key={label}
              className="grid grid-cols-[5rem_5.5rem_1fr] items-center gap-3 border-t border-border px-3 py-1.5 text-xs"
            >
              <span className="text-ink">{label}</span>
              <span className="text-muted">{describeNext(job)}</span>
              <span
                className={
                  result.tone === "danger"
                    ? "text-right text-danger"
                    : "text-right text-muted"
                }
              >
                {result.text}
              </span>
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-xs text-muted">
        {status.catalog_account
          ? `模型目录下次由 ${status.catalog_account.label} 拉取；`
          : ""}
        令牌刷新接口：{endpoints.native} 原生 · {endpoints.legacy} 旧接口 ·{" "}
        {endpoints.unknown} 待判断
      </p>
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
