import { useEffect, useMemo, useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Boxes,
  FlaskConical,
  Github,
  KeyRound,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  ScrollText,
  Search,
  Settings,
  Users,
} from "lucide-react";
import { apiGet, apiPost } from "../../lib/api";
import type { Account, AdminModel, ApiKey } from "../../lib/types";
import { cn } from "../../lib/cn";
import { applyLocale } from "../../lib/locale";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../ui/dialog";
import { Input } from "../ui/input";
import { SegmentedControl } from "../ui/segmented";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

const NAV_ITEMS = [
  { path: "/dashboard", label: "仪表盘", icon: LayoutDashboard },
  { path: "/accounts", label: "账号", icon: Users },
  { path: "/models", label: "模型", icon: Boxes },
  { path: "/keys", label: "密钥", icon: KeyRound },
  { path: "/logs", label: "日志", icon: ScrollText },
  { path: "/settings", label: "设置", icon: Settings },
  { path: "/playground", label: "测试台", icon: FlaskConical },
] as const;

const REPO_URL = "https://github.com/taylorpelisse/Trae-mouth";
const FEEDBACK_URL = `${REPO_URL}/issues/new`;

function useScrollProgress() {
  const [progress, setProgress] = useState(0);
  useEffect(() => {
    const update = () => {
      const max = document.documentElement.scrollHeight - window.innerHeight;
      setProgress(max > 0 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0);
    };
    update();
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, []);
  return progress;
}

function TopBar({ onSearch }: { onSearch: () => void }) {
  const navigate = useNavigate();
  const [locale, setLocale] = useState<"zh-CN" | "zh-TW">(() => {
    try {
      return localStorage.getItem("trae.locale") === "zh-TW" ? "zh-TW" : "zh-CN";
    } catch {
      return "zh-CN";
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem("trae.locale", locale);
    } catch {
      // ignore storage failures
    }
    applyLocale(locale);
  }, [locale]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        onSearch();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onSearch]);

  return (
    <header className="sticky top-0 z-30 border-b border-border bg-canvas/85 backdrop-blur">
      <div className="mx-auto flex h-14 w-full max-w-content items-center justify-between gap-4 px-6">
        <div className="flex items-center gap-2.5">
          <img src={`${import.meta.env.BASE_URL}logo.svg`} alt="" className="h-7 w-7 shrink-0 rounded-[9px]" />
          <span className="text-[15px] font-semibold text-ink">Trae Mouth</span>
          <div className="ml-1 flex items-center gap-0.5">
            <Tooltip>
              <TooltipTrigger asChild>
                <a
                  href={REPO_URL}
                  target="_blank"
                  rel="noreferrer"
                  aria-label="项目仓库"
                  className="flex h-8 w-8 items-center justify-center rounded-control text-muted transition-colors hover:bg-canvas hover:text-ink"
                >
                  <Github className="h-4 w-4" />
                </a>
              </TooltipTrigger>
              <TooltipContent>项目仓库</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <a
                  href={FEEDBACK_URL}
                  target="_blank"
                  rel="noreferrer"
                  aria-label="反馈"
                  className="flex h-8 w-8 items-center justify-center rounded-control text-muted transition-colors hover:bg-canvas hover:text-ink"
                >
                  <MessageSquare className="h-4 w-4" />
                </a>
              </TooltipTrigger>
              <TooltipContent>反馈</TooltipContent>
            </Tooltip>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onSearch}
            aria-label="全局搜索"
            className="hidden h-9 w-56 select-none items-center gap-2 rounded-field border border-border bg-card px-3 text-13 text-muted transition-colors hover:border-ink/20 md:flex"
          >
            <Search className="h-3.5 w-3.5" />
            <span className="flex-1 text-left">全局搜索…</span>
            <kbd className="flex items-center gap-0.5 rounded-keycap border border-border bg-canvas px-1.5 py-0.5 text-[10px] text-muted">
              Ctrl K
            </kbd>
          </button>
          <Button
            variant="outline"
            size="iconSm"
            aria-label="全局搜索"
            onClick={onSearch}
            className="md:hidden"
          >
            <Search className="h-4 w-4" />
          </Button>
          <SegmentedControl
            ariaLabel="界面语言"
            className="hidden sm:inline-flex"
            size="sm"
            value={locale}
            onChange={setLocale}
            options={[
              { value: "zh-CN", label: "简体" },
              { value: "zh-TW", label: "繁體" },
            ]}
          />
          <Tooltip>
            <TooltipTrigger asChild>
              <span>
                <SegmentedControl
                  ariaLabel="地区切换"
                  size="sm"
                  value="cn"
                  onChange={() => undefined}
                  options={[
                    { value: "cn", label: "国内" },
                    { value: "intl", label: "国际", disabled: true },
                  ]}
                />
              </span>
            </TooltipTrigger>
            <TooltipContent>国际版将在后续接入</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="iconSm"
                aria-label="退出登录"
                onClick={async () => {
                  try {
                    await apiPost("/logout", {});
                  } finally {
                    navigate("/login", { replace: true });
                  }
                }}
              >
                <LogOut className="h-4 w-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>退出登录</TooltipContent>
          </Tooltip>
        </div>
      </div>
    </header>
  );
}

function BottomDock() {
  const location = useLocation();
  return (
    <nav
      aria-label="主导航"
      className="fixed inset-x-0 bottom-0 z-40 flex justify-center px-4 pb-5"
    >
      <div className="flex max-w-full items-center gap-1 overflow-x-auto scrollbar-none rounded-card border border-border bg-card/95 p-1.5 shadow-dock backdrop-blur">
        {NAV_ITEMS.map((item) => {
          const active =
            location.pathname === item.path ||
            location.pathname.startsWith(`${item.path}/`);
          const Icon = item.icon;
          return (
            <Link
              key={item.path}
              to={item.path}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex shrink-0 items-center gap-1.5 rounded-field px-3.5 py-2 text-13 font-medium transition-colors",
                active
                  ? "bg-accent text-white"
                  : "text-muted hover:bg-canvas hover:text-ink",
              )}
            >
              <Icon className="h-4 w-4" aria-hidden />
              <span className="whitespace-nowrap">{item.label}</span>
            </Link>
          );
        })}
      </div>
    </nav>
  );
}

function SearchDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const [term, setTerm] = useState("");
  const accountsQuery = useQuery({
    queryKey: ["admin", "accounts"],
    queryFn: () => apiGet<{ accounts: Account[] }>("/accounts"),
    enabled: open,
    staleTime: 60_000,
  });
  const modelsQuery = useQuery({
    queryKey: ["admin", "models"],
    queryFn: () => apiGet<{ models: AdminModel[] }>("/models"),
    enabled: open,
    staleTime: 60_000,
  });
  const keysQuery = useQuery({
    queryKey: ["admin", "keys"],
    queryFn: () => apiGet<{ keys: ApiKey[] }>("/keys"),
    enabled: open,
    staleTime: 60_000,
  });
  const query = term.trim().toLowerCase();
  const settings = [
    { label: "设置 / 行为开关", path: "/settings", keywords: "设置 行为开关 全局 ide 原生工具" },
    { label: "设置 / 并发与限流", path: "/settings", keywords: "并发 冷却 限流" },
    { label: "测试台", path: "/playground", keywords: "测试 调试 playground" },
  ];
  const groups = [
    {
      title: "账号",
      items: (accountsQuery.data?.accounts ?? [])
        .filter((item) =>
          [item.username, item.label, item.user_id, item.id]
            .join(" ")
            .toLowerCase()
            .includes(query),
        )
        .slice(0, 5)
        .map((item) => ({
          id: item.id,
          label: item.username || item.label || item.id,
          hint: item.plan || item.user_id,
          path: "/accounts",
        })),
    },
    {
      title: "模型",
      items: (modelsQuery.data?.models ?? [])
        .filter((item) =>
          [item.id, item.name, item.series].join(" ").toLowerCase().includes(query),
        )
        .slice(0, 5)
        .map((item) => ({
          id: item.id,
          label: item.id,
          hint: item.name,
          path: "/models",
        })),
    },
    {
      title: "密钥",
      items: (keysQuery.data?.keys ?? [])
        .filter((item) =>
          [item.name, item.note, item.prefix].join(" ").toLowerCase().includes(query),
        )
        .slice(0, 5)
        .map((item) => ({
          id: item.id,
          label: item.name || item.prefix,
          hint: item.prefix,
          path: "/keys",
        })),
    },
    {
      title: "设置项",
      items: settings
        .filter((item) => item.keywords.toLowerCase().includes(query))
        .map((item) => ({
          id: item.path + item.label,
          label: item.label,
          hint: "打开设置",
          path: item.path,
        })),
    },
  ].filter((group) => group.items.length > 0);
  const loading =
    accountsQuery.isLoading || modelsQuery.isLoading || keysQuery.isLoading;

  function openResult(path: string) {
    onOpenChange(false);
    setTerm("");
    navigate(path);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[75vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>全局搜索</DialogTitle>
          <DialogDescription>搜索账号、模型、密钥与设置项</DialogDescription>
        </DialogHeader>
        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <Input
            autoFocus
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            placeholder="输入关键词…"
            aria-label="全局搜索关键词"
            className="pl-9"
          />
        </div>
        {!query ? (
          <p className="rounded-tile bg-canvas px-3 py-2.5 text-xs text-muted">
            输入账号名称、UID、模型 ID、密钥名称或设置项。
          </p>
        ) : loading ? (
          <p className="rounded-tile bg-canvas px-3 py-2.5 text-xs text-muted">
            正在搜索…
          </p>
        ) : groups.length === 0 ? (
          <p className="rounded-tile bg-canvas px-3 py-2.5 text-xs text-muted">
            没有匹配结果。
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            {groups.map((group) => (
              <div key={group.title}>
                <p className="mb-1 text-xs text-muted">{group.title}</p>
                <div className="flex flex-col">
                  {group.items.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => openResult(item.path)}
                      className="flex items-center justify-between gap-3 rounded-chip px-2 py-1.5 text-left text-13 text-ink hover:bg-canvas"
                    >
                      <span className="min-w-0 truncate">{item.label}</span>
                      <span className="shrink-0 truncate text-xs text-muted">
                        {item.hint}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function AppShell() {
  const [searchOpen, setSearchOpen] = useState(false);
  const progress = useScrollProgress();
  const openSearch = useMemo(() => () => setSearchOpen(true), []);

  return (
    <div className="flex min-h-screen flex-col">
      <TopBar onSearch={openSearch} />
      <div
        aria-hidden
        className="pointer-events-none fixed inset-x-0 top-14 z-30 h-0.5 origin-left bg-accent"
        style={{ transform: `scaleX(${progress})`, transition: "transform 80ms linear" }}
      />
      <main className="mx-auto w-full max-w-content flex-1 px-6 pb-32 pt-10">
        <Outlet />
      </main>
      <BottomDock />
      <SearchDialog open={searchOpen} onOpenChange={setSearchOpen} />
    </div>
  );
}
