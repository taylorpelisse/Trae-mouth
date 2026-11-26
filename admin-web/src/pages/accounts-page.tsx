import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  ClipboardCopy,
  Download,
  Keyboard,
  LoaderCircle,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Search,
  Trash2,
  UserRound,
} from "lucide-react";
import { toast } from "sonner";
import { apiDelete, apiGet, apiPatch, apiPost } from "../lib/api";
import type { Account, CreditSummary } from "../lib/types";
import {
  copyText,
  daysUntil,
  formatNumber,
  formatTime,
} from "../lib/format";
import { cn } from "../lib/cn";
import { Badge, Progress } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  SheetContent,
} from "../components/ui/dialog";
import { EmptyState, PageHeader } from "../components/ui/feedback";
import { IconAction } from "../components/ui/icon-action";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
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

type AccountList = { ok: boolean; accounts: Account[] };
type ImportCode = {
  ok: boolean;
  code: string;
  expires_at: number;
  relay: string;
  bundle_url: string;
  oneclick_url: string;
  command: string;
};

const STATUS_OPTIONS = [
  { value: "all", label: "全部" },
  { value: "ok", label: "正常" },
  { value: "cooling", label: "冷却" },
  { value: "error", label: "异常" },
  { value: "attention", label: "注意" },
];

function accountHealth(account: Account) {
  if (account.status === "disabled") {
    return { key: "attention", label: "已停用", detail: "已停用", tone: "muted" as const };
  }
  if (account.status === "cooldown") {
    return {
      key: "cooling",
      label: "冷却中",
      detail: account.status_reason || "风控/配额冷却",
      tone: "warning" as const,
    };
  }
  if (
    account.status === "relogin_required" ||
    account.status === "invalid" ||
    account.last_error
  ) {
    return {
      key: "error",
      label: "异常",
      detail: account.last_error || account.status_reason || "需要重新登录",
      tone: "danger" as const,
    };
  }
  return { key: "ok", label: "正常", detail: "可用", tone: "success" as const };
}

function AccountValidity({ account }: { account: Account }) {
  const days = daysUntil(account.token_expires_at);
  if (days === null) {
    return <span className="text-muted">—</span>;
  }
  const expired = days <= 0;
  const warning = !expired && days <= 7;
  return (
    <div className="mx-auto w-28 text-center">
      <p className={expired ? "text-danger" : warning ? "text-warning" : "text-ink"}>
        {expired ? "已过期" : `剩余 ${days} 天`}
      </p>
      <Progress
        value={Math.min(100, Math.max(0, (days / 14) * 100))}
        className="mt-1"
        indicatorClassName={expired ? "bg-danger" : warning ? "bg-warning" : "bg-success"}
      />
    </div>
  );
}

export function AccountsPage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [addOpen, setAddOpen] = useState(false);
  const [detail, setDetail] = useState<Account | null>(null);
  const [editing, setEditing] = useState<Account | null>(null);
  const [deleting, setDeleting] = useState<Account | null>(null);
  const [checkinOpen, setCheckinOpen] = useState(false);

  const accountsQuery = useQuery({
    queryKey: ["admin", "accounts"],
    queryFn: () => apiGet<AccountList>("/accounts"),
    refetchInterval: 30_000,
  });
  const creditsQuery = useQuery({
    queryKey: ["admin", "credits"],
    queryFn: () =>
      apiGet<
        CreditSummary & {
          accounts?: { account_id: string; balance: number; plan?: string }[];
        }
      >(
        "/credits",
        { silent: true },
      ),
    refetchInterval: 60_000,
  });
  const detailQuery = useQuery({
    queryKey: ["admin", "account-detail", detail?.id],
    queryFn: () =>
      apiGet<{
        ok: boolean;
        account: Account;
        device: Record<string, unknown> | null;
      }>(`/accounts/${detail?.id}`, { silent: true }),
    enabled: Boolean(detail?.id),
    staleTime: 10_000,
  });

  const accounts = accountsQuery.data?.accounts ?? [];
  const credits = new Map(
    (creditsQuery.data?.accounts ?? []).map((item) => [item.account_id, item.balance]),
  );
  const creditPlans = new Map(
    (creditsQuery.data?.accounts ?? [])
      .filter((item) => item.plan)
      .map((item) => [item.account_id, item.plan as string]),
  );
  const deviceInfo = (detailQuery.data?.device ?? {}) as Record<string, unknown>;
  const memoryMb = Math.round(Number(deviceInfo.memory || 0) / 1024 / 1024);
  const cpuText =
    [deviceInfo.cpu_brand, deviceInfo.cpu_model].filter(Boolean).join(" ") ||
    String(deviceInfo.cpu || "—");

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    return accounts.filter((account) => {
      if (term) {
        const haystack = [
          account.label,
          account.username,
          account.user_id,
          account.id,
        ]
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(term)) return false;
      }
      if (statusFilter !== "all" && accountHealth(account).key !== statusFilter) {
        return false;
      }
      return true;
    });
  }, [accounts, search, statusFilter]);

  const refreshCredits = useMutation({
    mutationFn: () => apiPost("/credits?refresh=1"),
    onSuccess: () => {
      toast.success("积分已刷新");
      queryClient.invalidateQueries({ queryKey: ["admin", "credits"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const refreshAll = useMutation({
    mutationFn: () => apiPost("/accounts/refresh-all"),
    onSuccess: () => {
      toast.success("刷新完成");
      queryClient.invalidateQueries({ queryKey: ["admin", "accounts"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const toggleStatus = useMutation({
    mutationFn: (account: Account) =>
      apiPatch(`/accounts/${account.id}`, {
        disabled: account.status !== "disabled",
      }),
    onSuccess: (_, account) => {
      toast.success(account.status === "disabled" ? "账号已启用" : "账号已停用");
      queryClient.invalidateQueries({ queryKey: ["admin", "accounts"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const refreshToken = useMutation({
    mutationFn: (account: Account) => apiPost(`/accounts/${account.id}/refresh`),
    onSuccess: () => {
      toast.success("令牌已刷新");
      queryClient.invalidateQueries({ queryKey: ["admin", "accounts"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const deleteAccount = useMutation({
    mutationFn: (account: Account) => apiDelete(`/accounts/${account.id}`),
    onSuccess: () => {
      toast.success("账号已删除");
      setDeleting(null);
      queryClient.invalidateQueries({ queryKey: ["admin", "accounts"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  return (
    <>
      <PageHeader
        title="账号"
        description="管理 Trae Mouth 使用的 Trae 账号，状态每 30 秒刷新"
        actions={
          <>
            <Button
              variant="outline"
              disabled={refreshCredits.isPending}
              onClick={() => refreshCredits.mutate()}
            >
              <RefreshCw className={cn("h-4 w-4", refreshCredits.isPending && "animate-spin")} />
              刷新积分
            </Button>
            <Button
              variant="outline"
              disabled={refreshAll.isPending}
              onClick={() => refreshAll.mutate()}
            >
              <RefreshCw className={cn("h-4 w-4", refreshAll.isPending && "animate-spin")} />
              刷新令牌
            </Button>
            <Button variant="outline" onClick={() => setCheckinOpen(true)}>
              <CheckCircle2 className="h-4 w-4" />
              每日签到
            </Button>
            <Button onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" />
              添加账号
            </Button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="relative w-64">
          <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索名称 / 备注 / UID"
            aria-label="搜索账号"
            className="pl-9"
          />
        </div>
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="w-36" aria-label="状态筛选">
            <SelectValue placeholder="状态" />
          </SelectTrigger>
          <SelectContent>
            {STATUS_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="ml-auto text-13 text-muted">共 {filtered.length} 个账号</p>
      </div>

      {accountsQuery.isLoading ? (
        <TableSkeleton columns={8} />
      ) : filtered.length === 0 ? (
        <EmptyState
          title={accounts.length ? "没有匹配的账号" : "暂无账号"}
          description={
            accounts.length
              ? "调整搜索词或状态筛选后重试。"
              : "通过「添加账号」创建登录码或手动导入 token。"
          }
          action={
            accounts.length ? undefined : (
              <Button onClick={() => setAddOpen(true)}>
                <Plus className="h-4 w-4" />
                添加账号
              </Button>
            )
          }
        />
      ) : (
        <div className="overflow-hidden rounded-card border border-border bg-card">
          <Table className="table-fixed">
            <TableHeader>
              <TableRow>
                {["名称", "权限", "备注", "UID", "状态", "积分余额", "有效期", "操作"].map(
                  (label) => (
                    <TableHead key={label} className="text-center">
                      {label}
                    </TableHead>
                  ),
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((account) => {
                const health = accountHealth(account);
                const plan = account.plan || creditPlans.get(account.id) || "";
                return (
                  <TableRow key={account.id}>
                    <TableCell className="text-center font-medium text-ink">
                      {account.username || account.label || "未命名"}
                    </TableCell>
                    <TableCell className="text-center">
                      {plan ? (
                        <Badge
                          variant={plan.toLowerCase() === "free" ? "muted" : "accent"}
                        >
                          {plan}
                        </Badge>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    <TableCell className="max-w-[120px] truncate text-center text-muted">
                      {account.note || "—"}
                    </TableCell>
                    <TableCell className="truncate text-center font-mono text-xs text-muted">
                      {account.user_id || account.id}
                    </TableCell>
                    <TableCell className="text-center">
                      <span
                        className="inline-flex items-center gap-1.5"
                        title={health.detail}
                      >
                        <span
                          className={cn(
                            "h-1.5 w-1.5 rounded-full",
                            health.tone === "success"
                              ? "bg-success"
                              : health.tone === "warning"
                                ? "bg-warning"
                                : health.tone === "danger"
                                  ? "bg-danger"
                                  : "bg-muted",
                          )}
                        />
                        {health.label}
                      </span>
                    </TableCell>
                    <TableCell className="text-center tabular-nums">
                      {credits.has(account.id)
                        ? formatNumber(credits.get(account.id))
                        : "—"}
                    </TableCell>
                    <TableCell>
                      <AccountValidity account={account} />
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center justify-center gap-1">
                        <IconAction
                          label="详情"
                          onClick={() => setDetail(account)}
                        >
                          <UserRound className="h-4 w-4" />
                        </IconAction>
                        <IconAction label="编辑备注" onClick={() => setEditing(account)}>
                          <Pencil className="h-4 w-4" />
                        </IconAction>
                        <IconAction
                          label={account.status === "disabled" ? "启用账号" : "停用账号"}
                          className={
                            account.status === "disabled"
                              ? "text-warning hover:text-warning"
                              : undefined
                          }
                          onClick={() => toggleStatus.mutate(account)}
                        >
                          <Power className="h-4 w-4" />
                        </IconAction>
                        <IconAction
                          label="刷新令牌"
                          disabled={!account.has_refresh_token || refreshToken.isPending}
                          onClick={() => refreshToken.mutate(account)}
                        >
                          <RefreshCw className="h-4 w-4" />
                        </IconAction>
                        <IconAction
                          label="删除账号"
                          className="text-danger hover:text-danger"
                          onClick={() => setDeleting(account)}
                        >
                          <Trash2 className="h-4 w-4" />
                        </IconAction>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      <AddAccountDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        accounts={accounts}
        onAdded={() => {
          queryClient.invalidateQueries({ queryKey: ["admin", "accounts"] });
          setAddOpen(false);
        }}
      />

      <Dialog open={Boolean(detail)} onOpenChange={(open) => !open && setDetail(null)}>
        <SheetContent>
          {detail ? (
            <>
              <DialogHeader>
                <DialogTitle>{detail.username || detail.label || "账号详情"}</DialogTitle>
                <DialogDescription>
                  仅展示公开字段；token 与设备私钥不下发到前端
                </DialogDescription>
              </DialogHeader>
              <dl className="mt-6 divide-y divide-border/70 text-13">
                {[
                  ["ID", detail.id],
                  ["名称", detail.username || "—"],
                  ["备注", detail.note || "—"],
                  ["UID", detail.user_id || "—"],
                  ["版本", detail.edition || "—"],
                  ["Host", detail.host || "—"],
                  ["Plan", detail.plan || "—"],
                  ["状态", accountHealth(detail).label],
                  ["并发限制", String(detail.concurrency_limit)],
                  ["token 有效期", formatTime(detail.token_expires_at)],
                  ["refresh 有效期", formatTime(detail.refresh_expires_at)],
                  ["最近使用", formatTime(detail.last_used_at)],
                  ["最近刷新", formatTime(detail.last_refreshed_at)],
                  ["refresh token", detail.has_refresh_token ? "已保存" : "缺失"],
                  ["最近错误", detail.last_error || "—"],
                ].map(([label, value]) => (
                  <div key={String(label)} className="flex items-start justify-between gap-4 py-2.5">
                    <dt className="shrink-0 text-muted">{label}</dt>
                    <dd className="break-all text-right text-ink">{value}</dd>
                  </div>
                ))}
              </dl>
              <div className="mt-6 border-t border-border pt-4">
                <h3 className="text-13 font-medium text-ink">模拟设备信息</h3>
                <dl className="mt-2 divide-y divide-border/70 text-13">
                  {[
                    ["设备名称", deviceInfo.device_name || "—"],
                    ["设备型号", deviceInfo.device_model || "—"],
                    ["设备厂商", deviceInfo.device_manufacturer || "—"],
                    ["设备 ID", deviceInfo.device_id || "—"],
                    ["Machine ID", deviceInfo.machine_id || "—"],
                    ["设备类型", deviceInfo.device_type || "—"],
                    ["平台", deviceInfo.platform || "—"],
                    ["平台码", deviceInfo.platform_code || "—"],
                    ["操作系统", deviceInfo.os_name || "—"],
                    ["OS 版本", deviceInfo.os_version || "—"],
                    ["内核", deviceInfo.os_kernel || "—"],
                    ["CPU", cpuText],
                    ["CPU 核心", String(deviceInfo.cpu_count || "—")],
                    [
                      "内存",
                      memoryMb ? `${(memoryMb / 1024).toFixed(0)} GB` : "—",
                    ],
                    [
                      "屏幕",
                      deviceInfo.screen_width && deviceInfo.screen_height
                        ? `${deviceInfo.screen_width} x ${deviceInfo.screen_height}`
                        : "—",
                    ],
                    ["语言", deviceInfo.language || "—"],
                    [
                      "IDE 版本",
                      deviceInfo.ide_version
                        ? `${deviceInfo.ide_version} / ${deviceInfo.ide_version_code || "—"}`
                        : "—",
                    ],
                    ["应用版本", deviceInfo.app_version || "—"],
                    ["设备公钥", deviceInfo.public_key ? "已生成" : "缺失"],
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
              </div>
            </>
          ) : null}
        </SheetContent>
      </Dialog>

      <EditAccountDialog
        account={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          queryClient.invalidateQueries({ queryKey: ["admin", "accounts"] });
        }}
      />

      <Dialog open={Boolean(deleting)} onOpenChange={(open) => !open && setDeleting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除账号</DialogTitle>
            <DialogDescription>
              确认删除「{deleting?.username || deleting?.label || deleting?.id}」？
              该操作不可撤销，账号的 token 与设备信息会一并移除。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(null)}>
              取消
            </Button>
            <Button
              variant="danger"
              disabled={deleteAccount.isPending}
              onClick={() => deleting && deleteAccount.mutate(deleting)}
            >
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <CheckinDialog
        open={checkinOpen}
        onOpenChange={setCheckinOpen}
        accounts={accounts}
      />
    </>
  );
}

function EditAccountDialog({
  account,
  onClose,
  onSaved,
}: {
  account: Account | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [note, setNote] = useState("");
  const [pending, setPending] = useState(false);

  useEffect(() => {
    setNote(account?.note ?? "");
  }, [account]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!account) return;
    setPending(true);
    try {
      await apiPatch(`/accounts/${account.id}`, { note: note.trim() });
      toast.success("备注已保存");
      onSaved();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={Boolean(account)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>编辑备注</DialogTitle>
          <DialogDescription>
            名称来自平台用户名，不可在此修改；备注仅保存在本地。
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={save} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="account-note">备注</Label>
            <Input
              id="account-note"
              maxLength={80}
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="仅本地保存"
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              取消
            </Button>
            <Button type="submit" disabled={pending}>
              {pending ? "保存中…" : "保存"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function AddAccountDialog({
  open,
  onOpenChange,
  accounts,
  onAdded,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  accounts: Account[];
  onAdded: () => void;
}) {
  const [mode, setMode] = useState<"assistant" | "manual">("assistant");
  const [assistantNonce, setAssistantNonce] = useState(0);

  function startAssistant() {
    setMode("assistant");
    setAssistantNonce((value) => value + 1);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>添加账号</DialogTitle>
          <DialogDescription>
            登录助手自动下载专属 bat，或手动输入 token
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap gap-2">
          <Button onClick={startAssistant}>
            <Download className="h-4 w-4" />
            登录助手
          </Button>
          <Button variant="outline" onClick={() => setMode("manual")}>
            <Keyboard className="h-4 w-4" />
            手动输入
          </Button>
        </div>
        {mode === "assistant" ? (
          <TicketPanel
            accounts={accounts}
            startNonce={assistantNonce}
            onDone={() => {
              onAdded();
              onOpenChange(false);
            }}
          />
        ) : (
          <ManualPanel
            onDone={() => {
              onAdded();
              onOpenChange(false);
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function TicketPanel({
  accounts,
  startNonce,
  onDone,
}: {
  accounts: Account[];
  startNonce: number;
  onDone: () => void;
}) {
  const [ticket, setTicket] = useState<ImportCode | null>(null);
  const [pending, setPending] = useState(false);
  const [watching, setWatching] = useState(false);
  const baseline = useRef<Set<string>>(new Set());
  const lastStarted = useRef(0);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!watching) return;
    const timer = window.setInterval(async () => {
      try {
        const data = await apiGet<AccountList>("/accounts", { silent: true });
        const added = data.accounts.find((account) => !baseline.current.has(account.id));
        if (added) {
          setWatching(false);
          toast.success(`账号已添加：${added.username || added.label || added.id}`);
          queryClient.invalidateQueries({ queryKey: ["admin", "accounts"] });
          onDone();
        }
      } catch {
        // keep waiting; surface errors only on explicit actions
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [watching, onDone, queryClient]);

  async function createTicket() {
    setPending(true);
    setWatching(false);
    setTicket(null);
    try {
      baseline.current = new Set(accounts.map((account) => account.id));
      const data = await apiPost<ImportCode>("/accounts/import/code", {});
      setTicket(data);
      setWatching(true);
      const link = document.createElement("a");
      link.href = data.oneclick_url;
      link.download = "trae-mouth-login.bat";
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      link.remove();
      toast.success("专属登录助手已下载，旧登录助手已作废");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "生成登录助手失败");
    } finally {
      setPending(false);
    }
  }

  useEffect(() => {
    if (!startNonce || lastStarted.current === startNonce) return;
    lastStarted.current = startNonce;
    void createTicket();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startNonce]);

  return (
    <div className="flex flex-col gap-4">
      {pending ? (
        <p className="flex items-center gap-1.5 text-xs text-muted">
          <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
          正在生成专属登录助手…
        </p>
      ) : ticket ? (
        <>
          <p className="text-xs text-muted">
            已下载专属登录助手；再次点击「登录助手」会生成新的 bat，并立即作废当前这个。
          </p>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-field border border-border bg-card px-2 py-1.5 text-xs">
              {ticket.code}
            </code>
            <IconAction
              label="复制登录码"
              onClick={() => {
                copyText(ticket.code).then(() => toast.success("已复制"));
              }}
            >
              <ClipboardCopy className="h-4 w-4" />
            </IconAction>
          </div>
          <p className="text-xs text-muted">
            有效期至 {formatTime(ticket.expires_at)}，一次性使用。
          </p>
          {watching ? (
            <p className="flex items-center gap-1.5 text-xs text-muted">
              <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
              等待登录助手回传账号…（完成后自动关闭）
            </p>
          ) : null}
        </>
      ) : (
        <p className="text-xs text-muted">
          点击「登录助手」会自动下载专属 bat，双击运行后完成登录。
        </p>
      )}
    </div>
  );
}

function ManualPanel({ onDone }: { onDone: () => void }) {
  const [form, setForm] = useState({
    user_id: "",
    token: "",
    refresh_token: "",
    host: "",
  });
  const [pending, setPending] = useState(false);

  function update(key: keyof typeof form, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!form.user_id.trim() || !form.token.trim()) {
      toast.error("UID 和 access_token 为必填项");
      return;
    }
    setPending(true);
    try {
      await apiPost("/accounts/import/manual", {
        user_id: form.user_id.trim(),
        token: form.token.trim(),
        refresh_token: form.refresh_token.trim() || undefined,
        host: form.host.trim() || undefined,
      });
      toast.success("账号已导入");
      onDone();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "导入失败");
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="flex flex-col gap-4" onSubmit={submit}>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="manual-user-id">UID user_id</Label>
        <Input
          id="manual-user-id"
          value={form.user_id}
          onChange={(event) => update("user_id", event.target.value)}
          placeholder="Trae 用户 ID"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="manual-token">access_token</Label>
        <Input
          id="manual-token"
          type="password"
          value={form.token}
          onChange={(event) => update("token", event.target.value)}
          placeholder="Cloud-IDE-JWT"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="manual-refresh">refresh_token（可选）</Label>
        <Input
          id="manual-refresh"
          type="password"
          value={form.refresh_token}
          onChange={(event) => update("refresh_token", event.target.value)}
          placeholder="用于自动续期"
        />
      </div>
      <details className="rounded-tile border border-border bg-canvas px-3 py-2 text-13">
        <summary className="cursor-pointer text-muted">设备信息（可选）</summary>
        <div className="mt-3 flex flex-col gap-1.5">
          <Label htmlFor="manual-host">Host</Label>
          <Input
            id="manual-host"
            value={form.host}
            onChange={(event) => update("host", event.target.value)}
            placeholder="https://api.trae.cn"
          />
        </div>
      </details>
      <Button type="submit" disabled={pending}>
        {pending ? (
          <>
            <LoaderCircle className="h-4 w-4 animate-spin" />
            导入中…
          </>
        ) : (
          "导入账号"
        )}
      </Button>
      <p className="text-xs text-muted">
        密钥与 token 仅用于本次提交，不会写入本地存储或日志。
      </p>
    </form>
  );
}

type CheckinResult = {
  status: string;
  detail?: string;
};

type CheckinResponse = {
  ok: boolean;
  already?: boolean;
  claimed?: boolean;
  risk?: boolean;
  code?: number | null;
  message?: string;
  rate_limited?: boolean;
  retry_in?: number;
};

function classifyCheckin(data: CheckinResponse): CheckinResult {
  if (data.already) {
    return { status: "今日已签" };
  }
  if (data.ok && data.claimed) {
    return { status: "签到成功" };
  }
  if (data.rate_limited || data.code === 9074) {
    return {
      status: "限流中",
      detail: `签到设备号被上游限流，${data.retry_in ?? 15} 分钟后可再试，届时会自动更换设备号`,
    };
  }
  return {
    status: "签到异常",
    detail: data.message || "签到失败",
  };
}

function checkinStatusClass(status: string): string {
  if (status === "签到成功") return "text-accent";
  if (status === "限流中" || status === "稍后重试") return "text-amber-600";
  if (status === "签到异常") return "text-red-600";
  return "text-muted";
}

function CheckinDialog({
  open,
  onOpenChange,
  accounts,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  accounts: Account[];
}) {
  const [state, setState] = useState<Record<string, CheckinResult>>({});
  const [running, setRunning] = useState(false);
  const [summary, setSummary] = useState("");

  async function start() {
    setRunning(true);
    setState({});
    setSummary("");
    const next: Record<string, CheckinResult> = {};
    for (const account of accounts) {
      setState((current) => ({
        ...current,
        [account.id]: { status: "签到中…" },
      }));
      try {
        const data = await apiPost<CheckinResponse>(
          "/activity/checkin/account",
          { account_id: account.id },
          { silent: true },
        );
        next[account.id] = classifyCheckin(data);
      } catch (error) {
        next[account.id] = {
          status: "签到异常",
          detail: error instanceof Error ? error.message : "签到失败",
        };
      }
      setState((current) => ({
        ...current,
        [account.id]: next[account.id],
      }));
    }
    setRunning(false);
    const success = Object.values(next).filter(
      (item) => item.status === "签到成功" || item.status === "今日已签",
    ).length;
    setSummary(`签到成功 ${success} / ${accounts.length} 个账号`);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>每日签到</DialogTitle>
          <DialogDescription>
            按账号逐个查询状态并补签，失败原因会显示在对应账号下。
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[240px] overflow-y-auto overscroll-contain rounded-tile border border-border bg-canvas/40">
          <div className="divide-y divide-border/70">
            {accounts.map((account) => {
              const result = state[account.id];
              return (
                <div
                  key={account.id}
                  className="flex items-start justify-between gap-3 px-3 py-2.5"
                >
                  <div className="min-w-0">
                    <p className="truncate text-13 text-ink">
                      {account.username || account.label || account.id}
                    </p>
                    {result?.detail ? (
                      <p
                        className="mt-0.5 truncate text-xs text-muted"
                        title={result.detail}
                      >
                        {result.detail}
                      </p>
                    ) : null}
                  </div>
                  <span
                    className={cn(
                      "shrink-0 text-13",
                      checkinStatusClass(result?.status || "待签到"),
                    )}
                  >
                    {result?.status || "待签到"}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
        <DialogFooter>
          {summary ? (
            <p className="mr-auto self-center text-13 text-muted">{summary}</p>
          ) : null}
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
          <Button onClick={start} disabled={running || accounts.length === 0}>
            {running ? "签到中…" : "开始签到"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
