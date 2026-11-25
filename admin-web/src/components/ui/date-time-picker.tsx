import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "../../lib/cn";

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

function pad(value: number) {
  return String(value).padStart(2, "0");
}

function parseValue(value: string): Date | null {
  if (!value) return null;
  const [datePart, timePart = "00:00"] = value.split("T");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour, minute] = timePart.split(":").map(Number);
  if (!year || !month || !day) return null;
  return new Date(year, month - 1, day, hour || 0, minute || 0);
}

function toValue(date: Date, hour: string, minute: string) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
    date.getDate(),
  )}T${hour}:${minute}`;
}

function displayValue(value: string) {
  const date = parseValue(value);
  if (!date) return "";
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(
    date.getDate(),
  )} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function sameDay(left: Date, right: Date) {
  return (
    left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate()
  );
}

export function DateTimePicker({
  value,
  onChange,
  placeholder = "选择日期时间",
  id,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  id?: string;
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const parsed = parseValue(value);
  const [monthCursor, setMonthCursor] = useState(
    () => new Date((parsed ?? new Date()).getFullYear(), (parsed ?? new Date()).getMonth(), 1),
  );
  const [draftDate, setDraftDate] = useState<Date | null>(parsed);
  const [hour, setHour] = useState(parsed ? pad(parsed.getHours()) : "00");
  const [minute, setMinute] = useState(parsed ? pad(parsed.getMinutes()) : "00");

  useEffect(() => {
    if (!open) return;
    const current = parseValue(value);
    const base = current ?? new Date();
    setDraftDate(current);
    setHour(pad(base.getHours()));
    setMinute(pad(base.getMinutes()));
    setMonthCursor(new Date(base.getFullYear(), base.getMonth(), 1));
  }, [open, value]);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (triggerRef.current?.contains(target)) return;
      if (panelRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const update = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const width = 320;
      const height = panelRef.current?.offsetHeight || 360;
      let top = rect.bottom + 8;
      if (top + height > window.innerHeight - 12) {
        const above = rect.top - height - 8;
        top =
          above >= 12
            ? above
            : Math.max(12, window.innerHeight - height - 12);
      }
      setPosition({
        top,
        left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
      });
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open]);

  const days = useMemo(() => {
    const year = monthCursor.getFullYear();
    const month = monthCursor.getMonth();
    const first = new Date(year, month, 1);
    const offset = (first.getDay() + 6) % 7;
    return Array.from({ length: 42 }, (_, index) => {
      const date = new Date(year, month, 1 - offset + index);
      return { date, current: date.getMonth() === month };
    });
  }, [monthCursor]);

  const today = new Date();

  return (
    <>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        onClick={() => setOpen((current) => !current)}
        className={cn(
          "flex h-9 w-full items-center justify-between rounded-field border border-border bg-card px-3 text-left text-13 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
          open && "border-accent",
        )}
      >
        <span className={value ? "text-ink" : "text-muted"}>
          {displayValue(value) || placeholder}
        </span>
        <CalendarDays className="h-4 w-4 text-muted" />
      </button>
      {open
        ? createPortal(
            <div
              ref={panelRef}
              className="fixed z-[80] w-[320px] rounded-tile border border-border bg-card p-3 shadow-[0_18px_50px_-10px_rgba(20,40,30,0.38)]"
              style={{ top: position.top, left: position.left }}
            >
              <div className="mb-2 flex items-center justify-between">
                <button
                  type="button"
                  aria-label="上个月"
                  onClick={() =>
                    setMonthCursor(
                      new Date(
                        monthCursor.getFullYear(),
                        monthCursor.getMonth() - 1,
                        1,
                      ),
                    )
                  }
                  className="flex h-7 w-7 items-center justify-center rounded-control text-muted hover:bg-canvas hover:text-ink"
                >
                  <ChevronLeft className="h-4 w-4" />
                </button>
                <p className="text-13 font-medium text-ink">
                  {monthCursor.getFullYear()} 年 {monthCursor.getMonth() + 1} 月
                </p>
                <button
                  type="button"
                  aria-label="下个月"
                  onClick={() =>
                    setMonthCursor(
                      new Date(
                        monthCursor.getFullYear(),
                        monthCursor.getMonth() + 1,
                        1,
                      ),
                    )
                  }
                  className="flex h-7 w-7 items-center justify-center rounded-control text-muted hover:bg-canvas hover:text-ink"
                >
                  <ChevronRight className="h-4 w-4" />
                </button>
              </div>
              <div className="grid grid-cols-7 gap-0.5 text-center text-xs text-muted">
                {WEEKDAYS.map((day) => (
                  <span key={day} className="py-0.5">
                    {day}
                  </span>
                ))}
              </div>
              <div className="mt-1 grid grid-cols-7 gap-0.5">
                {days.map(({ date, current }) => {
                  const selected = draftDate ? sameDay(date, draftDate) : false;
                  const isToday = sameDay(date, today);
                  return (
                    <button
                      key={date.toISOString()}
                      type="button"
                      onClick={() => setDraftDate(date)}
                      className={cn(
                        "flex h-7 items-center justify-center rounded-chip text-xs transition-colors",
                        !current && "text-muted/45",
                        selected
                          ? "bg-ink text-white"
                          : "text-ink hover:bg-canvas",
                        !selected && isToday && "border border-accent/40",
                      )}
                    >
                      {date.getDate()}
                    </button>
                  );
                })}
              </div>
              <div className="mt-2 flex items-center gap-2 border-t border-border pt-2">
                <select
                  aria-label="小时"
                  value={hour}
                  onChange={(event) => setHour(event.target.value)}
                  className="h-7 rounded-control border border-border bg-card px-2 text-13 text-ink focus-visible:outline-none"
                >
                  {Array.from({ length: 24 }, (_, index) => pad(index)).map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
                <span className="text-xs text-muted">时</span>
                <select
                  aria-label="分钟"
                  value={minute}
                  onChange={(event) => setMinute(event.target.value)}
                  className="h-7 rounded-control border border-border bg-card px-2 text-13 text-ink focus-visible:outline-none"
                >
                  {Array.from({ length: 60 }, (_, index) => pad(index)).map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
                <span className="text-xs text-muted">分</span>
              </div>
              <div className="mt-2 flex items-center justify-between">
                <div className="flex gap-2">
                  <button
                    type="button"
                    className="text-xs text-muted hover:text-ink"
                    onClick={() => {
                      onChange("");
                      setOpen(false);
                    }}
                  >
                    清除
                  </button>
                  <button
                    type="button"
                    className="text-xs text-muted hover:text-ink"
                    onClick={() => {
                      const now = new Date();
                      setDraftDate(now);
                      setHour(pad(now.getHours()));
                      setMinute(pad(now.getMinutes()));
                      setMonthCursor(new Date(now.getFullYear(), now.getMonth(), 1));
                    }}
                  >
                    今天
                  </button>
                </div>
                <button
                  type="button"
                  className="rounded-control bg-ink px-3 py-1 text-xs font-medium text-white hover:bg-ink/90"
                  onClick={() => {
                    if (!draftDate) return;
                    onChange(toValue(draftDate, hour, minute));
                    setOpen(false);
                  }}
                >
                  确定
                </button>
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
