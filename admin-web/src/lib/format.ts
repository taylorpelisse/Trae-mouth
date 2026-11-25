export function asNumber(value: unknown, fallback = 0): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function formatNumber(value: unknown): string {
  const number = asNumber(value);
  if (Math.abs(number) >= 100_000_000) {
    return (number / 100_000_000).toFixed(1).replace(/\.0$/, "") + "亿";
  }
  if (Math.abs(number) >= 10_000) {
    return (number / 10_000).toFixed(1).replace(/\.0$/, "") + "万";
  }
  return number.toLocaleString("zh-CN");
}

export function formatFullNumber(value: unknown): string {
  return asNumber(value).toLocaleString("zh-CN", {
    maximumFractionDigits: 2,
  });
}

export function formatContext(value: unknown): string {
  const number = asNumber(value);
  if (!number) return "0";
  if (number >= 1_000_000) {
    return `${(number / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (number >= 1000) {
    return `${Math.round(number / 1000)}K`;
  }
  return number.toLocaleString("zh-CN");
}

export function formatMillion(value: unknown): string {
  const number = asNumber(value);
  if (!number) return "0M";
  return `${(number / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M`;
}

export function formatTokenCount(value: unknown): string {
  const number = asNumber(value);
  if (Math.abs(number) >= 1_000_000) {
    return `${(number / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M`;
  }
  return number.toLocaleString("zh-CN");
}

export function formatDuration(ms: unknown): string {
  const value = asNumber(ms);
  if (!value) return "—";
  if (value < 1000) return `${Math.round(value)}ms`;
  return `${(value / 1000).toFixed(2)}s`;
}

export function formatTime(seconds: unknown): string {
  const value = asNumber(seconds);
  if (!value) return "—";
  const date = new Date(value * 1000);
  return date.toLocaleString("zh-CN", {
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function daysUntil(seconds: unknown): number | null {
  const value = asNumber(seconds);
  if (!value) return null;
  return Math.ceil((value * 1000 - Date.now()) / 86_400_000);
}

export function formatRelative(seconds: unknown): string {
  const value = asNumber(seconds);
  if (!value) return "—";
  const diff = Date.now() - value * 1000;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return `${Math.floor(diff / 86_400_000)} 天前`;
}

export function formatDateTimeInput(value: unknown): string {
  const number = asNumber(value);
  if (!number) return "";
  const date = new Date(number * 1000);
  const pad = (input: number) => String(input).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
    date.getDate(),
  )}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function fromDateTimeInput(value: string): number | null {
  if (!value) return null;
  const time = new Date(value).getTime();
  return Number.isFinite(time) ? Math.floor(time / 1000) : null;
}

export function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  document.execCommand("copy");
  area.remove();
  return Promise.resolve();
}
