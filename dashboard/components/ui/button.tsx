import { cn } from "@/lib/utils";
import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "ghost";

const styles: Record<Variant, string> = {
  primary: "bg-accent text-accent-ink shadow-[0_0_0_1px_rgb(0_0_0/0.08),0_8px_20px_-10px_var(--accent)] hover:brightness-110",
  secondary: "border border-line bg-panel-2 text-ink hover:border-line-strong",
  ghost: "text-muted hover:bg-panel-2 hover:text-ink",
};

export function Button({
  variant = "secondary",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      className={cn(
        "inline-flex h-9 items-center justify-center gap-1.5 rounded-lg px-3.5 text-sm font-medium transition active:scale-[0.97]",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
        "disabled:cursor-not-allowed disabled:opacity-40 disabled:active:scale-100",
        styles[variant],
        className,
      )}
      {...props}
    />
  );
}
