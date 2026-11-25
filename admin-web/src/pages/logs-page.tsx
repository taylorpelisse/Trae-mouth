import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Ellipsis, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { apiDelete, apiGet } from "../lib/api";
import type { AdminLog, ApiKey } from "../lib/types";
import {
  formatContext,
  formatDuration,
  formatNumber,
  formatTokenCount,
} from "../lib/format";
import { Button } from "../components/ui/button";
import {
  Dialog,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  SheetContent,
} from "../components/ui/dialog";
import { EmptyState, PageHeader } from "../components/ui/feedback";
import { IconAction } from "../components/ui/icon-action";
import { Input } from "../components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../components/ui/select";
import { TableSkeleton } from "../components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

type LogResponse = { items: AdminLog[]; total: number; limit: number; offset: number };
type KeyList = { ok: boolean; keys: ApiKey[] };
const PAGE_SIZES = [10, 20, 50, 100, 200] as const;

function logTime(seconds: number): string {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}年${pad(date.getMonth() + 1)}月${pad(date.getDate())}日 ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

export function LogsPage() {
  const queryClient = useQueryClient();
  const [range, setRange] = useState("1d");
  const [keyFilter, setKeyFilter] = useState("all");
  const [status, setStatus] = useState("all");
  const [model, setModel] = useState("");
  const [ip, setIp] = useState("");
  const [offset, setOffset] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [pendingClear, setPendingClear] = useState(false);
  const [manualRefreshing, setManualRefreshing] = useState(false);
  const [selectedLog, setSelectedLog] = useState<AdminLog | null>(null);

  const logsQuery = useQuery({
    queryKey: ["admin", "logs", range, keyFilter, status, model, ip, offset, pageSize],
    queryFn: () =>
      apiGet<LogResponse>(
        `/logs?range=${range}&limit=${pageSize}&offset=${offset}${
          keyFilter !== "all" ? `&key=${encodeURIComponent(keyFilter)}` : ""
        }${model ? `&model=${encodeURIComponent(model)}` : ""}${
          ip ? `&ip=${encodeURIComponent(ip)}` : ""
        }${
          status === "error"
            ? "&errors=true"
            : status === "success"
              ? "&success=true"
              : ""
        }`,
      ),
    refetchInterval: 500,
  });
  const keysQuery = useQuery({
    queryKey: ["admin", "keys"],
    queryFn: () => apiGet<KeyList>("/keys"),
    refetchInterval: 30_000,
  });
  const keyNames = useMemo(
    () => new Map((keysQuery.data?.keys ?? []).map((key) => [key.id, key.name])),
    [keysQuery.data?.keys],
  );

  const logs = logsQuery.data?.items ?? [];
  const total = logsQuery.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const page = Math.floor(offset / pageSize) + 1;

  async function refreshNow() {
    setManualRefreshing(true);
    try {
      await logsQuery.refetch();
    } finally {
      setManualRefreshing(false);
    }
  }

  async function clearLogs() {
    if (!window.confirm("清空全部请求日志？该操作不可撤销。")) return;
    setPendingClear(true);
    try {
      await apiDelete("/logs");
      toast.success("日志已清空");
      queryClient.invalidateQueries({ queryKey: ["admin", "logs"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "metrics"] });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清空失败");
    } finally {
      setPendingClear(false);
    }
  }

  return (
    <>
      <PageHeader
        title="日志"
        description="业务请求日志，0.5 秒自动刷新"
        actions={
          <>
            <Button
              variant="outline"
              disabled={manualRefreshing}
              onClick={refreshNow}
            >
              <RefreshCw
                className={`h-4 w-4 ${manualRefreshing ? "animate-spin" : ""}`}
              />
              刷新日志
            </Button>
            <Button variant="outline" disabled={pendingClear} onClick={clearLogs}>
              <Trash2 className="h-4 w-4" />
              {pendingClear ? "清空中…" : "清空日志"}
            </Button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Select value={range} onValueChange={(value) => { setRange(value); setOffset(0); }}>
          <SelectTrigger className="w-32" aria-label="时间范围">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="1d">近 1 天</SelectItem>
            <SelectItem value="7d">近 7 天</SelectItem>
            <SelectItem value="30d">近 30 天</SelectItem>
          </SelectContent>
        </Select>
        <Select value={keyFilter} onValueChange={(value) => { setKeyFilter(value); setOffset(0); }}>
          <SelectTrigger className="w-32" aria-label="密钥筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部密钥</SelectItem>
            {(keysQuery.data?.keys ?? []).map((key) => (
              <SelectItem key={key.id} value={key.id}>
                {key.name || key.prefix}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={status} onValueChange={(value) => { setStatus(value); setOffset(0); }}>
          <SelectTrigger className="w-32" aria-label="状态筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部状态</SelectItem>
            <SelectItem value="success">成功</SelectItem>
            <SelectItem value="error">失败</SelectItem>
          </SelectContent>
        </Select>
        <Input
          className="w-44"
          value={model}
          onChange={(event) => { setModel(event.target.value); setOffset(0); }}
          placeholder="模型"
          aria-label="按模型筛选"
        />
        <Input
          className="w-36"
          value={ip}
          onChange={(event) => { setIp(event.target.value); setOffset(0); }}
          placeholder="来源 IP"
          aria-label="按来源 IP 筛选"
        />
        <div className="ml-auto flex items-center gap-2 text-13 text-muted">
          <Select
            value={String(pageSize)}
            onValueChange={(value) => {
              setPageSize(Number(value));
              setOffset(0);
            }}
          >
            <SelectTrigger className="w-28" aria-label="每页条数">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PAGE_SIZES.map((size) => (
                <SelectItem key={size} value={String(size)}>
                  {size} 条/页
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span>共 {total} 条</span>
          <Button
            variant="outline"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - pageSize))}
          >
            上一页
          </Button>
          <span>
            第 {page} / {pageCount} 页
          </span>
          <Button
            variant="outline"
            disabled={offset + pageSize >= total}
            onClick={() => setOffset(offset + pageSize)}
          >
            下一页
          </Button>
        </div>
      </div>

      {logsQuery.isLoading ? (
        <TableSkeleton columns={12} rows={8} />
      ) : logs.length === 0 ? (
        <EmptyState title="暂无日志" description="当前筛选条件下没有日志记录。" />
      ) : (
        <div className="overflow-hidden rounded-card border border-border bg-card">
          <Table className="table-fixed [&_td]:py-2">
            <colgroup>
              {["12%", "9%", "8%", "7%", "20%", "7%", "7%", "6%", "6%", "5%", "8%", "5%"].map(
                (width, index) => (
                  <col key={index} style={{ width }} />
                ),
              )}
            </colgroup>
            <TableHeader>
              <TableRow>
                {[
                  "时间",
                  "密钥",
                  "IP",
                  "账号",
                  "模型",
                  "思考等级",
                  "上下文",
                  "首字",
                  "耗时",
                  "积分",
                  "输入/输出",
                  "更多",
                ].map((label) => (
                  <TableHead key={label} className="text-center">
                    {label}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {logs.map((log) => (
                <TableRow key={log.id}>
                  <TableCell className="text-center text-xs text-muted">{logTime(log.time)}</TableCell>
                  <TableCell className="truncate text-center text-xs">
                    {keyNames.get(log.key_id) || log.key_id || "本机"}
                  </TableCell>
                  <TableCell className="truncate text-center font-mono text-[11px] text-muted">
                    {log.ip || "—"}
                  </TableCell>
                  <TableCell className="truncate text-center text-xs">
                    {log.account_label || log.account_id || "—"}
                  </TableCell>
                  <TableCell className="truncate text-center text-xs">{log.model || "—"}</TableCell>
                  <TableCell className="text-center text-xs">{log.reasoning_effort || "—"}</TableCell>
                  <TableCell className="text-center text-xs">
                    {log.context_window ? formatContext(log.context_window) : "—"}
                  </TableCell>
                  <TableCell className="text-center text-xs">{formatDuration(log.first_token_ms)}</TableCell>
                  <TableCell className="text-center text-xs">{formatDuration(log.total_ms)}</TableCell>
                  <TableCell className="text-center text-xs">
                    {log.credits === null || log.credits === undefined
                      ? "—"
                      : formatNumber(log.credits)}
                  </TableCell>
                  <TableCell className="text-center text-xs">
                    {formatTokenCount(log.prompt_tokens)} /{" "}
                    {formatTokenCount(log.completion_tokens)}
                  </TableCell>
                  <TableCell className="text-center">
                    <IconAction label="更多" onClick={() => setSelectedLog(log)}>
                      <Ellipsis className="h-4 w-4" />
                    </IconAction>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Dialog
        open={Boolean(selectedLog)}
        onOpenChange={(open) => !open && setSelectedLog(null)}
      >
        <SheetContent>
          {selectedLog ? (
            <>
              <DialogHeader>
                <DialogTitle>请求详情</DialogTitle>
                <DialogDescription>{selectedLog.request_id}</DialogDescription>
              </DialogHeader>
              <dl className="mt-6 divide-y divide-border/70 text-13">
                {[
                  ["请求 ID", selectedLog.request_id || "—"],
                  ["时间", logTime(selectedLog.time)],
                  ["端点", selectedLog.endpoint || "—"],
                  ["密钥", keyNames.get(selectedLog.key_id) || selectedLog.key_id || "本机"],
                  ["来源 IP", selectedLog.ip || "—"],
                  ["账号", selectedLog.account_label || selectedLog.account_id || "—"],
                  ["模型", selectedLog.model || "—"],
                  ["状态", String(selectedLog.status || "—")],
                  ["思考等级", selectedLog.reasoning_effort || "—"],
                  [
                    "上下文",
                    selectedLog.context_window
                      ? formatContext(selectedLog.context_window)
                      : "—",
                  ],
                  ["首字", formatDuration(selectedLog.first_token_ms)],
                  ["总耗时", formatDuration(selectedLog.total_ms)],
                  ["输入 Token", formatTokenCount(selectedLog.prompt_tokens)],
                  ["输出 Token", formatTokenCount(selectedLog.completion_tokens)],
                  ["错误", selectedLog.error_code || "—"],
                ].map(([label, value]) => (
                  <div
                    key={String(label)}
                    className="flex items-start justify-between gap-4 py-2.5"
                  >
                    <dt className="shrink-0 text-muted">{String(label)}</dt>
                    <dd className="break-all text-right text-ink">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            </>
          ) : null}
        </SheetContent>
      </Dialog>
    </>
  );
}
