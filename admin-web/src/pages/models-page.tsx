import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Boxes, ClipboardCopy, RefreshCw, Search } from "lucide-react";
import { toast } from "sonner";
import { apiGet } from "../lib/api";
import type { AdminModel, MaintenanceStatus } from "../lib/types";
import { copyText, formatContext } from "../lib/format";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { EmptyState, PageHeader, StatCard } from "../components/ui/feedback";
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

type ModelList = { models: AdminModel[]; fetched_at: number; loading?: boolean };

const SERIES_ORDER = [
  "DeepSeek",
  "Doubao",
  "GLM",
  "MiMo",
  "Kimi",
  "Qwen",
  "Claude",
  "Gemini",
  "GPT",
  "Hunyuan",
  "ERNIE",
  "Seed",
  "其他",
  "自定义",
];

function seriesOf(model: AdminModel): string {
  const source = (model.series || model.id).toLowerCase();
  const rules: [RegExp, string][] = [
    [/deepseek/, "DeepSeek"],
    [/doubao|volc/, "Doubao"],
    [/glm/, "GLM"],
    [/mimo/, "MiMo"],
    [/kimi/, "Kimi"],
    [/qwen/, "Qwen"],
    [/claude/, "Claude"],
    [/gemini/, "Gemini"],
    [/gpt|o1|o3/, "GPT"],
    [/hunyuan/, "Hunyuan"],
    [/ernie/, "ERNIE"],
    [/seed/, "Seed"],
    [/custom|自定/, "自定义"],
  ];
  return rules.find(([regex]) => regex.test(source))?.[1] || model.series || "其他";
}

function contextOf(model: AdminModel): number {
  return Math.max(model.context_window ?? 0, model.max_context ?? 0);
}

export function ModelsPage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [series, setSeries] = useState("all");
  const [capability, setCapability] = useState("all");
  const [sort, setSort] = useState("name");
  const [refreshing, setRefreshing] = useState(false);
  const [progress, setProgress] = useState("");

  const modelsQuery = useQuery({
    queryKey: ["admin", "models"],
    queryFn: () => apiGet<ModelList>("/models"),
    staleTime: 0,
    refetchOnMount: "always",
    // 目录在后台按错峰间隔加载，定时重新读取直到齐全
    refetchInterval: (query) => (query.state.data?.loading ? 5_000 : 30_000),
  });
  const models = modelsQuery.data?.models ?? [];

  const seriesOptions = useMemo(() => {
    const found = new Set(models.map(seriesOf));
    return [...found].sort((left, right) => {
      const li = SERIES_ORDER.indexOf(left);
      const ri = SERIES_ORDER.indexOf(right);
      return (li < 0 ? 99 : li) - (ri < 0 ? 99 : ri);
    });
  }, [models]);

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    const rows = models.filter((model) => {
      if (term && !`${model.id} ${model.name}`.toLowerCase().includes(term)) {
        return false;
      }
      if (series !== "all" && seriesOf(model) !== series) return false;
      if (capability === "reasoning" && model.reasoning_levels.length === 0) return false;
      if (capability === "vision" && !model.multimodal) return false;
      if (capability === "tools" && !model.tools_supported) return false;
      if (capability === "1m" && !model.max_mode) return false;
      return true;
    });
    return rows.sort((left, right) => {
      if (sort === "context") return contextOf(right) - contextOf(left);
      if (sort === "output") return (right.max_output ?? 0) - (left.max_output ?? 0);
      return left.id.localeCompare(right.id);
    });
  }, [models, search, series, capability, sort]);

  const maxContext = models.reduce((max, model) => Math.max(max, contextOf(model)), 0);

  async function refresh() {
    setRefreshing(true);
    setProgress("");
    try {
      // 刷新在后台按错峰间隔逐个账号进行，这里等它完成后再取最新目录
      await apiGet<ModelList>("/models?refresh=1");
      for (let round = 0; round < 900; round += 1) {
        const status = await apiGet<MaintenanceStatus>("/maintenance", { silent: true });
        const job = status.jobs.catalogs;
        if (!job.running) break;
        setProgress(
          job.progress ? ` ${job.progress.done}/${job.progress.total}` : "",
        );
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
      }
      const data = await apiGet<ModelList>("/models");
      queryClient.setQueryData(["admin", "models"], data);
      toast.success("模型目录已刷新");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "刷新失败");
    } finally {
      setRefreshing(false);
      setProgress("");
    }
  }

  return (
    <>
      <PageHeader
        title="模型"
        description="模型目录与能力标记，数据来自 Trae Mouth 模型接口"
        actions={
          <Button variant="outline" onClick={refresh} disabled={refreshing}>
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
            {refreshing ? `刷新中${progress}` : "刷新模型"}
          </Button>
        }
      />

      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
        <StatCard label="模型数" value={models.length} loading={modelsQuery.isLoading} icon={<Boxes className="h-4 w-4" />} />
        <StatCard
          label="支持推理"
          value={models.filter((model) => model.reasoning_levels.length > 0).length}
          loading={modelsQuery.isLoading}
        />
        <StatCard
          label="多模态"
          value={models.filter((model) => model.multimodal).length}
          loading={modelsQuery.isLoading}
        />
        <StatCard
          label="1M / 大上下文"
          value={models.filter((model) => model.max_mode).length}
          loading={modelsQuery.isLoading}
        />
        <StatCard
          label="最大上下文"
          value={maxContext ? formatContext(maxContext) : "—"}
          loading={modelsQuery.isLoading}
        />
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="relative w-64">
          <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索模型 ID"
            className="pl-9"
          />
        </div>
        <Select value={series} onValueChange={setSeries}>
          <SelectTrigger className="w-44" aria-label="系列筛选">
            <SelectValue placeholder="全部系列" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部系列</SelectItem>
            {seriesOptions.map((item) => (
              <SelectItem key={item} value={item}>
                {item}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={capability} onValueChange={setCapability}>
          <SelectTrigger className="w-36" aria-label="能力筛选">
            <SelectValue placeholder="全部能力" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部能力</SelectItem>
            <SelectItem value="reasoning">推理</SelectItem>
            <SelectItem value="vision">视觉</SelectItem>
            <SelectItem value="tools">原生工具</SelectItem>
            <SelectItem value="1m">1M</SelectItem>
          </SelectContent>
        </Select>
        <Select value={sort} onValueChange={setSort}>
          <SelectTrigger className="w-32" aria-label="排序">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="name">按名称</SelectItem>
            <SelectItem value="context">按上下文</SelectItem>
            <SelectItem value="output">按最大输出</SelectItem>
          </SelectContent>
        </Select>
        <p className="ml-auto text-13 text-muted">共 {filtered.length} 个模型</p>
      </div>

      {modelsQuery.isLoading ? (
        <TableSkeleton columns={8} />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="暂无模型数据"
          description="模型目录为空，或当前筛选条件没有匹配项。"
        />
      ) : (
        <div className="overflow-hidden rounded-card border border-border bg-card">
          <Table className="table-fixed">
            <colgroup>
              {["20%", "10%", "10%", "10%", "20%", "10%", "10%", "10%"].map(
                (width, index) => (
                  <col key={index} style={{ width }} />
                ),
              )}
            </colgroup>
            <TableHeader>
              <TableRow>
                {["模型 ID", "上下文", "最大输出", "prompt 上限", "推理档位", "视觉", "原生工具", "1M"].map(
                  (label) => (
                    <TableHead key={label} className="text-center">
                      {label}
                    </TableHead>
                  ),
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((model) => {
                const context = contextOf(model);
                return (
                  <TableRow key={model.id}>
                    <TableCell className="font-mono text-xs">
                      <div className="flex items-center gap-1.5">
                        <span className="min-w-0 truncate" title={model.id}>
                          {model.id}
                        </span>
                        <button
                          type="button"
                          aria-label="复制模型名称"
                          className="shrink-0 text-muted hover:text-ink"
                          onClick={() =>
                            copyText(model.id).then(() => toast.success("已复制模型名称"))
                          }
                        >
                          <ClipboardCopy className="h-3.5 w-3.5" />
                        </button>
                      </div>
                      {model.name && model.name !== model.id ? (
                        <p className="mt-0.5 truncate text-[10px] text-muted">{model.name}</p>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-center">
                      {context ? formatContext(context) : "—"}
                    </TableCell>
                    <TableCell className="text-center">
                      {model.max_output ? formatContext(model.max_output) : "—"}
                    </TableCell>
                    <TableCell className="text-center">
                      {model.prompt_max_tokens ? formatContext(model.prompt_max_tokens) : "—"}
                    </TableCell>
                    <TableCell className="text-center">
                      {model.reasoning_levels.length ? (
                        <>
                          {model.reasoning_levels.join(" / ")}
                          {model.default_reasoning_level ? (
                            <span className="text-muted">（默认 {model.default_reasoning_level}）</span>
                          ) : null}
                        </>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    <TableCell className="text-center">
                      {model.multimodal ? <span className="text-accent">✓</span> : "—"}
                    </TableCell>
                    <TableCell className="text-center">
                      {model.tools_supported ? <span className="text-accent">✓</span> : "—"}
                    </TableCell>
                    <TableCell className="text-center">
                      {model.max_mode ? <Badge variant="accent">1M</Badge> : "—"}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </>
  );
}
