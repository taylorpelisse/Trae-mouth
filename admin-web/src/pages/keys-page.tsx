import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ClipboardCopy,
  KeyRound,
  Pencil,
  Plus,
  Power,
  TerminalSquare,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { apiDelete, apiGet, apiPatch, apiPost } from "../lib/api";
import type { ApiKey } from "../lib/types";
import {
  copyText,
  formatDateTimeInput,
  formatRelative,
  formatTime,
  fromDateTimeInput,
} from "../lib/format";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog";
import { EmptyState, PageHeader } from "../components/ui/feedback";
import { DateTimePicker } from "../components/ui/date-time-picker";
import { IconAction } from "../components/ui/icon-action";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { TableSkeleton } from "../components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

type KeyList = { ok: boolean; keys: ApiKey[] };
type CreatedKey = { ok: boolean; key: ApiKey; token: string };

function keyState(key: ApiKey) {
  if (key.disabled) return { label: "停用", variant: "muted" as const };
  if (key.expires_at && key.expires_at <= Date.now() / 1000) {
    return { label: "已过期", variant: "warning" as const };
  }
  if (key.quota_requests && key.used_requests >= key.quota_requests) {
    return { label: "请求配额用尽", variant: "warning" as const };
  }
  if (key.quota_tokens && key.used_tokens >= key.quota_tokens) {
    return { label: "Token 配额用尽", variant: "warning" as const };
  }
  return { label: "启用", variant: "success" as const };
}

function CopyLine({
  label,
  value,
  mono = true,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-center gap-2 rounded-tile border border-border bg-canvas px-3 py-2">
      <span className="w-40 shrink-0 text-xs text-muted">{label}</span>
      <code className={`min-w-0 flex-1 truncate text-xs ${mono ? "font-mono" : ""}`}>
        {value}
      </code>
      <IconAction
        label={`复制${label}`}
        onClick={() => copyText(value).then(() => toast.success("已复制"))}
      >
        <ClipboardCopy className="h-4 w-4" />
      </IconAction>
    </div>
  );
}

export function KeysPage() {
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [accessOpen, setAccessOpen] = useState(false);
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const [editingNote, setEditingNote] = useState<ApiKey | null>(null);
  const [editingExpiry, setEditingExpiry] = useState<ApiKey | null>(null);
  const [deleting, setDeleting] = useState<ApiKey | null>(null);

  const keysQuery = useQuery({
    queryKey: ["admin", "keys"],
    queryFn: () => apiGet<KeyList>("/keys"),
    refetchInterval: 30_000,
  });
  const keys = keysQuery.data?.keys ?? [];

  const toggle = useMutation({
    mutationFn: (key: ApiKey) =>
      apiPatch(`/keys/${key.id}`, { disabled: !key.disabled }),
    onSuccess: (_, key) => {
      toast.success(key.disabled ? "密钥已启用" : "密钥已停用");
      queryClient.invalidateQueries({ queryKey: ["admin", "keys"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const remove = useMutation({
    mutationFn: (key: ApiKey) => apiDelete(`/keys/${key.id}`),
    onSuccess: () => {
      toast.success("密钥已删除");
      setDeleting(null);
      queryClient.invalidateQueries({ queryKey: ["admin", "keys"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  return (
    <>
      <PageHeader
        title="密钥"
        description="业务 API Key 的创建、停用与有效期管理"
        actions={
          <>
            <Button variant="outline" onClick={() => setAccessOpen(true)}>
              <TerminalSquare className="h-4 w-4" />
              接入入口
            </Button>
            <Button onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" />
              创建密钥
            </Button>
          </>
        }
      />

      {keysQuery.isLoading ? (
        <TableSkeleton columns={7} />
      ) : keys.length === 0 ? (
        <EmptyState
          icon={<KeyRound className="h-5 w-5" />}
          title="暂无密钥"
          description="创建第一个业务 API Key 以调用 /v1 接口。"
          action={
            <Button onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" />
              创建密钥
            </Button>
          }
        />
      ) : (
        <div className="overflow-hidden rounded-card border border-border bg-card">
          <Table className="table-fixed">
            <TableHeader>
              <TableRow>
                {["名称", "备注", "密钥", "状态", "有效期", "最近使用", "操作"].map(
                  (label) => (
                    <TableHead key={label} className="text-center">
                      {label}
                    </TableHead>
                  ),
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {keys.map((key) => {
                const state = keyState(key);
                return (
                  <TableRow key={key.id}>
                    <TableCell className="text-center font-medium text-ink">
                      {key.name || "未命名"}
                    </TableCell>
                    <TableCell className="max-w-[120px] truncate text-center text-muted">
                      {key.note || "—"}
                    </TableCell>
                    <TableCell className="text-center">
                      <button
                        type="button"
                        className="inline-flex items-center gap-1 font-mono text-xs text-muted hover:text-ink"
                        onClick={() =>
                          copyText(key.prefix).then(() =>
                            toast.success("已复制前缀（完整密钥仅在创建时显示）"),
                          )
                        }
                      >
                        {key.prefix}…
                        <ClipboardCopy className="h-3 w-3" />
                      </button>
                    </TableCell>
                    <TableCell className="text-center">
                      <Badge variant={state.variant}>{state.label}</Badge>
                    </TableCell>
                    <TableCell className="text-center">
                      {key.expires_at ? (
                        <span className={key.expires_at <= Date.now() / 1000 ? "text-danger" : ""}>
                          {formatTime(key.expires_at)}
                        </span>
                      ) : (
                        "永久"
                      )}
                    </TableCell>
                    <TableCell className="text-center text-muted">
                      {key.last_used_at ? formatRelative(key.last_used_at) : "—"}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center justify-center gap-1">
                        <IconAction label="编辑备注" onClick={() => setEditingNote(key)}>
                          <Pencil className="h-4 w-4" />
                        </IconAction>
                        <IconAction label="设置有效期" onClick={() => setEditingExpiry(key)}>
                          <KeyRound className="h-4 w-4" />
                        </IconAction>
                        <IconAction
                          label={key.disabled ? "启用密钥" : "停用密钥"}
                          onClick={() => toggle.mutate(key)}
                        >
                          <Power className="h-4 w-4" />
                        </IconAction>
                        <IconAction label="删除密钥" onClick={() => setDeleting(key)}>
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

      <CreateKeyDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={(token) => {
          setCreateOpen(false);
          setCreatedToken(token);
          queryClient.invalidateQueries({ queryKey: ["admin", "keys"] });
        }}
      />

      <Dialog open={Boolean(createdToken)} onOpenChange={(open) => !open && setCreatedToken(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>完整密钥（仅此一次）</DialogTitle>
            <DialogDescription>创建后完整密钥仅展示一次，请立即保存。</DialogDescription>
          </DialogHeader>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 break-all rounded-field border border-border bg-canvas px-3 py-2 font-mono text-xs">
              {createdToken}
            </code>
            <IconAction
              label="复制密钥"
              onClick={() =>
                createdToken &&
                copyText(createdToken).then(() => toast.success("已复制"))
              }
            >
              <ClipboardCopy className="h-4 w-4" />
            </IconAction>
          </div>
          <DialogFooter>
            <Button onClick={() => setCreatedToken(null)}>我已保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <NoteDialog
        keyRecord={editingNote}
        onClose={() => setEditingNote(null)}
        onSaved={() => {
          setEditingNote(null);
          queryClient.invalidateQueries({ queryKey: ["admin", "keys"] });
        }}
      />
      <ExpiryDialog
        keyRecord={editingExpiry}
        onClose={() => setEditingExpiry(null)}
        onSaved={() => {
          setEditingExpiry(null);
          queryClient.invalidateQueries({ queryKey: ["admin", "keys"] });
        }}
      />

      <Dialog open={Boolean(deleting)} onOpenChange={(open) => !open && setDeleting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除密钥</DialogTitle>
            <DialogDescription>
              确认删除「{deleting?.name}」（{deleting?.prefix}…）？
              使用该密钥的客户端将立即无法调用，且无法恢复。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(null)}>
              取消
            </Button>
            <Button
              variant="danger"
              disabled={remove.isPending}
              onClick={() => deleting && remove.mutate(deleting)}
            >
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AccessDialog open={accessOpen} onOpenChange={setAccessOpen} />
    </>
  );
}

function CreateKeyDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (token: string) => void;
}) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState("chat,models");
  const [allowedIps, setAllowedIps] = useState("");
  const [quotaRequests, setQuotaRequests] = useState("0");
  const [quotaTokens, setQuotaTokens] = useState("0");
  const [pending, setPending] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      toast.error("请输入密钥名称");
      return;
    }
    setPending(true);
    try {
      const data = await apiPost<CreatedKey>("/keys", {
        name: name.trim(),
        scopes: scopes.trim() || "chat,models",
        allowed_ips: allowedIps.trim(),
        quota_requests: Math.max(0, Number(quotaRequests) || 0),
        quota_tokens: Math.max(0, Number(quotaTokens) || 0),
      });
      toast.success("密钥已创建");
      setName("");
      setAllowedIps("");
      setQuotaRequests("0");
      setQuotaTokens("0");
      onCreated(data.token);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "创建失败");
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>创建密钥</DialogTitle>
          <DialogDescription>创建后完整密钥仅展示一次，请立即保存。</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="key-name">名称</Label>
            <Input
              id="key-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="如 本地开发"
            />
          </div>
          <details className="rounded-tile border border-border bg-canvas px-3 py-2 text-13">
            <summary className="cursor-pointer text-muted">高级设置</summary>
            <div className="mt-3 grid gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="key-scopes">Scopes</Label>
                <Input
                  id="key-scopes"
                  value={scopes}
                  onChange={(event) => setScopes(event.target.value)}
                  placeholder="chat,models"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="key-ips">IP 白名单</Label>
                <Input
                  id="key-ips"
                  value={allowedIps}
                  onChange={(event) => setAllowedIps(event.target.value)}
                  placeholder="逗号分隔 IP 或 CIDR，留空不限"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="key-quota-requests">请求配额</Label>
                  <Input
                    id="key-quota-requests"
                    inputMode="numeric"
                    value={quotaRequests}
                    onChange={(event) => setQuotaRequests(event.target.value)}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="key-quota-tokens">Token 配额</Label>
                  <Input
                    id="key-quota-tokens"
                    inputMode="numeric"
                    value={quotaTokens}
                    onChange={(event) => setQuotaTokens(event.target.value)}
                  />
                </div>
              </div>
              <p className="text-xs text-muted">配额填 0 表示不限制。</p>
            </div>
          </details>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button type="submit" disabled={pending}>
              {pending ? "创建中…" : "创建"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function NoteDialog({
  keyRecord,
  onClose,
  onSaved,
}: {
  keyRecord: ApiKey | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [note, setNote] = useState("");
  const [pending, setPending] = useState(false);
  useEffect(() => setNote(keyRecord?.note ?? ""), [keyRecord]);
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!keyRecord) return;
    setPending(true);
    try {
      await apiPatch(`/keys/${keyRecord.id}`, { note: note.trim() });
      toast.success("备注已保存");
      onSaved();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setPending(false);
    }
  }
  return (
    <Dialog open={Boolean(keyRecord)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>编辑备注</DialogTitle>
          <DialogDescription>备注只保存在本地，用于标识密钥用途。</DialogDescription>
        </DialogHeader>
        <form onSubmit={save} className="flex flex-col gap-4">
          <Input
            maxLength={120}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="如 本地脚本 / 某客户端"
          />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              取消
            </Button>
            <Button type="submit" disabled={pending}>
              保存
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ExpiryDialog({
  keyRecord,
  onClose,
  onSaved,
}: {
  keyRecord: ApiKey | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [value, setValue] = useState("");
  const [pending, setPending] = useState(false);
  useEffect(() => setValue(formatDateTimeInput(keyRecord?.expires_at)), [keyRecord]);

  async function save(expiresAt: number | null) {
    if (!keyRecord) return;
    setPending(true);
    try {
      await apiPatch(`/keys/${keyRecord.id}`, { expires_at: expiresAt });
      toast.success(expiresAt ? "有效期已更新" : "有效期已设为永久");
      onSaved();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={Boolean(keyRecord)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>设置有效期</DialogTitle>
          <DialogDescription>
            到期后密钥自动失效；留空或选择“设为永久”表示长期有效。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <DateTimePicker
            value={value}
            onChange={setValue}
            placeholder="选择到期时间"
          />
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              variant="outline"
              disabled={pending}
              onClick={() => save(null)}
            >
              设为永久
            </Button>
            <Button variant="outline" onClick={onClose}>
              取消
            </Button>
            <Button
              disabled={pending}
              onClick={() => save(fromDateTimeInput(value))}
            >
              保存
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function AccessDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const origin = window.location.origin;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>接入入口</DialogTitle>
          <DialogDescription>
            业务 API Key 在「密钥」页创建；以下地址使用同源服务。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-2">
          <CopyLine label="服务地址" value={origin} />
          <CopyLine label="鉴权" value="Authorization: Bearer <业务 API Key>" />
          <p className="text-xs text-muted">也支持 x-api-key 头。</p>
          <CopyLine label="OpenAI 兼容" value={`${origin}/v1`} />
          <CopyLine label="Chat Completions" value={`${origin}/v1/chat/completions`} />
          <CopyLine label="Models" value={`${origin}/v1/models`} />
          <CopyLine label="OPENAI_BASE_URL" value={`${origin}/v1`} />
          <CopyLine label="OPENAI_API_KEY" value="<业务 API Key>" />
        </div>
        <p className="text-xs text-muted">
          支持流式、工具调用与多轮会话；模型列表以 /v1/models 返回为准。
        </p>
      </DialogContent>
    </Dialog>
  );
}
