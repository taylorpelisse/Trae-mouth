import { useRef } from "react";
import { cn } from "../../lib/cn";

export type SegmentOption<T extends string = string> = {
  value: T;
  label: string;
  disabled?: boolean;
};

export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  className,
  size = "default",
}: {
  value: T;
  onChange: (value: T) => void;
  options: readonly SegmentOption<T>[];
  ariaLabel?: string;
  className?: string;
  size?: "default" | "sm";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const enabled = options.filter((option) => !option.disabled);

  function move(direction: number) {
    const index = enabled.findIndex((option) => option.value === value);
    if (index < 0) return;
    const next = enabled[(index + direction + enabled.length) % enabled.length];
    if (next) onChange(next.value);
  }

  return (
    <div
      ref={ref}
      role="tablist"
      aria-label={ariaLabel}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          move(-1);
        }
        if (event.key === "ArrowRight") {
          event.preventDefault();
          move(1);
        }
      }}
      className={cn(
        "inline-flex items-center rounded-field border border-border bg-card p-1",
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={active}
            disabled={option.disabled}
            onClick={() => onChange(option.value)}
            className={cn(
              "rounded-[18px] px-3 font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40",
              size === "sm" ? "h-7 text-xs" : "h-8 text-13",
              active
                ? "bg-ink text-white"
                : "text-muted hover:bg-canvas hover:text-ink",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
