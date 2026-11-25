import * as React from "react";
import { cn } from "../../lib/cn";

const fieldClass =
  "rounded-field border border-border bg-card text-13 text-ink placeholder:text-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50";

export const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn("flex h-9 w-full px-3 py-1", fieldClass, className)}
    {...props}
  />
));
Input.displayName = "Input";

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      "flex min-h-[80px] w-full resize-y px-3 py-2",
      fieldClass,
      className,
    )}
    {...props}
  />
));
Textarea.displayName = "Textarea";
