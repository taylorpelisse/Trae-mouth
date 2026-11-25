import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowUpRight,
  Clock3,
  Coins,
  Cpu,
  TriangleAlert,
  Users,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { apiGet } from "../lib/api";
import type { Account, CreditSummary, DashboardMetrics } from "../lib/types";
import { formatDuration, formatMillion, formatNumber } from "../lib/format";
import { Button } from "../components/ui/button";
import { SectionCard } from "../components/ui/card";
import { EmptyState, PageHeader, StatCard } from "../components/ui/feedback";
import { Skeleton } from "../components/ui/skeleton";
import { SegmentedControl } from "../components/ui/segmented";

const RANGE_OPTIONS = [
  { value: "week", label: "本周" },
  { value: "month", label: "本月" },
] as const;

function healthQuery() {
  return {
    queryKey: ["healthz"],
    queryFn: async () => {
      const response = await fetch("/healthz");
      if (!response.ok) throw new Error("healthz failed");
      return (await response.json()) as {
        ok: boolean;
        version: string;
        accounts: number;
        active: number;
        mode?: string;
      };
    },
    refetchInterval: 30_000,
    staleTime: 10_000,
  };
}

function FlowDiagram() {
  return (
    <div className="relative flex h-28 items-center overflow-hidden rounded-tile bg-canvas">
      <style>{`
        @keyframes tm-beam {
          0% { left: 0%; opacity: 0; }
          10% { opacity: 1; }
          45% { left: 50%; opacity: 1; }
          55% { left: 50%; opacity: .35; }
          90% { left: 100%; opacity: 1; }
          100% { left: 100%; opacity: 0; }
        }
      `}</style>
      <div className="relative z-10 grid w-full grid-cols-3 items-center gap-2 px-4 text-center">
        <div>
          <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-control border border-border bg-card text-muted">
            <Cpu className="h-5 w-5" />
          </div>
          <p className="mt-2 text-xs text-muted">客户端</p>
        </div>
        <div>
          <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-control border border-accent/30 bg-card">
            <img src={`${import.meta.env.BASE_URL}logo.svg`} alt="" className="h-6 w-6 rounded-[7px]" />
          </div>
          <p className="mt-2 text-xs font-medium text-ink">Trae Mouth</p>
        </div>
        <div>
          <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-control border border-border bg-card text-muted">
            <ArrowUpRight className="h-5 w-5" />
          </div>
          <p className="mt-2 text-xs text-muted">Trae 上游</p>
        </div>
      </div>
      <div
        className="pointer-events-none absolute top-[44px] z-0 h-px bg-border"
        style={{
          left: "calc(16px + (100% - 48px) / 6)",
          right: "calc(16px + (100% - 48px) / 6)",
        }}
      >
        <span
          className="absolute -top-1 h-2 w-2 -translate-x-1/2 rounded-full bg-accent shadow-[0_0_10px_3px_rgba(21,128,61,0.35)] motion-reduce:hidden"
          style={{ animation: "tm-beam 5s cubic-bezier(0.65,0,0.35,1) infinite" }}
        />
      </div>
    </div>
  );
}

export function DashboardPage() {
  const [creditRange, setCreditRange] = useState<"week" | "month">("week");

  const metrics = useQuery({
    queryKey: ["admin", "metrics", "14d"],
    queryFn: () => apiGet<DashboardMetrics>("/metrics?range=14d", { silent: true }),
    refetchInterval: 30_000,
  });
  const accounts = useQuery({
    queryKey: ["admin", "accounts"],
    queryFn: () => apiGet<{ ok: boolean; accounts: Account[] }>("/accounts"),
    refetchInterval: 30_000,
  });
  const credits = useQuery({
    queryKey: ["admin", "credits"],
    queryFn: () => apiGet<CreditSummary>("/credits", { silent: true }),
    refetchInterval: 60_000,
  });
  const creditHistory = useQuery({
    queryKey: ["admin", "credits", "history", creditRange],
    queryFn: () =>
      apiGet<{ series: { date: string; income: number; expense: number }[]; totals: { income: number; expense: number; net: number } }>(
        `/credits/history?range=${creditRange}`,
        { silent: true },
      ),
    refetchInterval: 60_000,
  });
  const health = useQuery(healthQuery());

  const rows = accounts.data?.accounts ?? [];
  const enabled = rows.filter((row) => row.status !== "disabled").length;
  const cooling = rows.filter((row) => row.status === "cooldown").length;
  const abnormal = rows.filter(
    (row) =>
      row.status === "relogin_required" ||
      row.status === "invalid" ||
      Boolean(row.last_error),
  ).length;
  const today = metrics.data?.today;
  const trend = metrics.data?.trend ?? [];
  const usage = metrics.data?.usage;
  const creditData = creditHistory.data;
  const creditSeries = creditData?.series ?? [];
  const historyTotals = creditData?.totals;

  const healthRows = useMemo(
    () => [
      ["运行模式", health.data?.mode || "IDE"],
      ["账号总数", String(rows.length)],
      ["启用账号", String(enabled)],
      ["粘性会话", String(metrics.data?.upstream?.sticky_sessions ?? 0)],
      ["服务版本", health.data?.version || metrics.data?.version || "—"],
    ],
    [
      health.data,
      rows.length,
      enabled,
      metrics.data?.version,
      metrics.data?.upstream?.sticky_sessions,
    ],
  );

  return (
    <>
      <PageHeader
        title="仪表盘"
        description="账号健康与调用概览，30 秒自动刷新"
        actions={
          <Button variant="outline" asChild>
            <Link to="/accounts">
              <Users className="h-4 w-4" />
              管理账号
            </Link>
          </Button>
        }
      />

      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <StatCard
          label="剩余积分"
          loading={credits.isLoading}
          value={
            credits.data?.available === null || credits.data?.available === undefined
              ? "—"
              : formatNumber(credits.data.available)
          }
          hint={credits.data?.supported ? "Trae 账号余额合计" : "等待首次余额查询"}
          tone="success"
          icon={<Coins className="h-4 w-4" />}
        />
        <StatCard
          label="今日已用"
          loading={credits.isLoading}
          value={
            credits.data?.today_used === null || credits.data?.today_used === undefined
              ? "—"
              : formatNumber(credits.data.today_used)
          }
          hint="今日消耗积分"
          icon={<Activity className="h-4 w-4" />}
        />
        <StatCard
          label="今日 Token"
          loading={metrics.isLoading}
          value={formatMillion(today?.tokens ?? 0)}
          hint="prompt + completion"
          icon={<Cpu className="h-4 w-4" />}
        />
        <StatCard
          label="启用账号"
          loading={accounts.isLoading}
          value={formatNumber(enabled)}
          hint="未停用且未归档"
          tone="success"
          icon={<Users className="h-4 w-4" />}
        />
        <StatCard
          label="冷却账号"
          loading={accounts.isLoading}
          value={formatNumber(cooling)}
          hint="风控/配额冷却中"
          tone="info"
          icon={<Clock3 className="h-4 w-4" />}
        />
        <StatCard
          label="异常账号"
          loading={accounts.isLoading}
          value={formatNumber(abnormal)}
          hint="需重登或最近报错"
          tone="danger"
          icon={<TriangleAlert className="h-4 w-4" />}
        />
      </div>

      <div className="mb-4 grid gap-4 lg:grid-cols-3">
        <SectionCard
          className="flex h-full flex-col lg:col-span-2"
          contentClassName="flex flex-1 flex-col"
          title="近 14 天调用趋势"
          description="成功与失败调用数"
        >
          {metrics.isLoading ? (
            <Skeleton className="h-64 w-full rounded-tile" />
          ) : trend.length ? (
            <>
              <div className="h-64 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={trend} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                    <defs>
                      <linearGradient id="tm-success" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#15803D" stopOpacity={0.28} />
                        <stop offset="100%" stopColor="#15803D" stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#E3EBE6" vertical={false} />
                    <XAxis dataKey="day" tick={{ fontSize: 11, fill: "#74807A" }} tickLine={false} axisLine={false} />
                    <YAxis tick={{ fontSize: 11, fill: "#74807A" }} tickLine={false} axisLine={false} allowDecimals={false} />
                    <ChartTooltip
                      contentStyle={{
                        borderRadius: 16,
                        border: "1px solid #E3EBE6",
                        fontSize: 12,
                      }}
                    />
                    <Area
                      type="monotone"
                      dataKey="requests"
                      name="成功"
                      stroke="#15803D"
                      strokeWidth={2}
                      fill="url(#tm-success)"
                    />
                    <Line
                      type="monotone"
                      dataKey="errors"
                      name="失败"
                      stroke="#DC2626"
                      strokeWidth={1.5}
                      strokeDasharray="4 4"
                      dot={false}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-3 border-t border-border pt-3 text-xs text-muted md:grid-cols-4">
                <div>
                  总调用
                  <p className="mt-1 text-base font-medium tabular-nums text-ink">
                    {formatNumber(usage?.requests ?? 0)}
                  </p>
                </div>
                <div>
                  总错误
                  <p className="mt-1 text-base font-medium tabular-nums text-danger">
                    {formatNumber(usage?.errors ?? 0)}
                  </p>
                </div>
                <div>
                  Token
                  <p className="mt-1 text-base font-medium tabular-nums text-ink">
                    {formatMillion(usage?.total_tokens ?? 0)}
                  </p>
                </div>
                <div>
                  平均首字
                  <p className="mt-1 text-base font-medium tabular-nums text-ink">
                    {formatDuration(usage?.avg_first_token_ms ?? 0)}
                  </p>
                </div>
              </div>
            </>
          ) : (
            <EmptyState
              title="暂无统计数据"
              description="近 14 天暂无调用记录，产生流量后自动显示"
            />
          )}
        </SectionCard>

        <SectionCard
          className="flex h-full flex-col"
          contentClassName="flex flex-1 flex-col"
          title="上游状态"
          description="服务运行信息（GET /healthz）"
        >
          <FlowDiagram />
          <dl className="mt-4 flex flex-1 flex-col divide-y divide-border/70 text-13">
            {healthRows.map(([label, value]) => (
              <div
                key={label}
                className={`flex flex-1 justify-between gap-4 py-2.5 ${
                  label === "服务版本"
                    ? "items-end pb-1"
                    : "items-center"
                }`}
                title={
                  label === "粘性会话"
                    ? "近期绑定到固定账号的会话数（默认 7 天）"
                    : undefined
                }
              >
                <dt className="text-muted">{label}</dt>
                <dd className={label === "运行模式" ? "font-medium text-accent" : "text-ink"}>
                  {value}
                </dd>
              </div>
            ))}
          </dl>
        </SectionCard>
      </div>

      <SectionCard
        title="积分收支"
        description="收入来自签到/奖励记录，支出按每日积分消耗统计（Asia/Shanghai）"
        actions={
          <SegmentedControl
            ariaLabel="统计区间"
            size="sm"
            value={creditRange}
            onChange={setCreditRange}
            options={RANGE_OPTIONS}
          />
        }
      >
        {creditHistory.isLoading ? (
          <Skeleton className="h-56 w-full rounded-tile" />
        ) : creditSeries.length ? (
          <>
            <div className="mb-4 grid grid-cols-3 gap-3">
              {[
                ["收入", historyTotals?.income ?? 0, "text-success"],
                ["支出", historyTotals?.expense ?? 0, "text-danger"],
                ["净增", historyTotals?.net ?? 0, (historyTotals?.net ?? 0) >= 0 ? "text-success" : "text-danger"],
              ].map(([label, value, tone]) => (
                <div key={String(label)} className="rounded-tile bg-canvas p-3">
                  <p className="text-xs text-muted">{label}</p>
                  <p className={`mt-1 text-lg font-medium tabular-nums ${tone}`}>
                    {Number(value) >= 0 ? "+" : ""}
                    {formatNumber(value)}
                  </p>
                </div>
              ))}
            </div>
            <div className="h-56 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={creditSeries} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#E3EBE6" vertical={false} />
                  <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#74807A" }} tickLine={false} axisLine={false} />
                  <YAxis tick={{ fontSize: 11, fill: "#74807A" }} tickLine={false} axisLine={false} />
                  <ChartTooltip contentStyle={{ borderRadius: 16, border: "1px solid #E3EBE6", fontSize: 12 }} />
                  <Line type="monotone" dataKey="income" name="收入" stroke="#15803D" strokeWidth={2} dot={creditSeries.length <= 2} />
                  <Line type="monotone" dataKey="expense" name="支出" stroke="#DC2626" strokeWidth={2} dot={creditSeries.length <= 2} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </>
        ) : (
          <EmptyState
            title="暂无收支记录"
            description={
              credits.data?.supported
                ? "从本次部署起统计：签到收入写入 events，积分消耗按每日余额基线计算"
                : "收支接口不可用，请检查服务状态"
            }
          />
        )}
      </SectionCard>
    </>
  );
}
