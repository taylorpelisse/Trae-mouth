import * as React from "react";
import { cn } from "../../lib/cn";

export function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("h-4 animate-pulse rounded-control bg-border/60", className)}
      {...props}
    />
  );
}

export function TableSkeleton({
  columns = 6,
  rows = 6,
}: {
  columns?: number;
  rows?: number;
}) {
  return (
    <div className="rounded-card border border-border bg-card p-4">
      <div className="mb-3 grid gap-3" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        {Array.from({ length: columns }).map((_, index) => (
          <Skeleton key={index} className="h-3.5" />
        ))}
      </div>
      <div className="space-y-2">
        {Array.from({ length: rows }).map((_, row) => (
          <div
            key={row}
            className="grid items-center gap-3 border-t border-border/60 py-3"
            style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
          >
            {Array.from({ length: columns }).map((__, column) => (
              <Skeleton
                key={column}
                className={column === 0 ? "h-4 w-4/5" : "h-3.5 w-3/4"}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
