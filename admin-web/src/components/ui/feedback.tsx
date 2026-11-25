import type { ReactNode } from "react";

export function EmptyState({
  title,
  description,
  action,
  icon,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-card border border-dashed border-border bg-card px-6 py-12 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-chip bg-canvas text-muted">
        {icon ?? <span className="text-lg">·</span>}
      </div>
      <div>
        <p className="text-13 font-medium text-ink">{title}</p>
        {description ? (
          <p className="mx-auto mt-1 max-w-md text-xs text-muted">{description}</p>
        ) : null}
      </div>
      {action}
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-8 flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 className="text-28 font-semibold leading-8 text-ink">{title}</h1>
        {description ? (
          <p className="mt-1 text-13 text-muted">{description}</p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      ) : null}
    </div>
  );
}

export function StatCard({
  label,
  value,
  hint,
  loading,
  tone,
  icon,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  loading?: boolean;
  tone?: "default" | "success" | "warning" | "danger" | "accent" | "info";
  icon?: ReactNode;
}) {
  const toneClass =
    tone === "success"
      ? "text-success"
      : tone === "warning"
        ? "text-warning"
        : tone === "danger"
          ? "text-danger"
          : tone === "accent"
            ? "text-accent"
            : tone === "info"
              ? "text-blue-600"
              : "text-ink";
  return (
    <div className="rounded-card border border-border bg-card p-[18px] shadow-soft">
      <div className="flex items-start justify-between gap-2">
        <p className="text-13 text-muted">{label}</p>
        {icon ? <span className="text-muted">{icon}</span> : null}
      </div>
      {loading ? (
        <div className="mt-3 h-8 w-24 animate-pulse rounded-control bg-border/60" />
      ) : (
        <p className={`mt-2 text-28 font-medium tabular-nums ${toneClass}`}>
          {value}
        </p>
      )}
      {hint ? <p className="mt-1 text-xs text-muted">{hint}</p> : null}
    </div>
  );
}
