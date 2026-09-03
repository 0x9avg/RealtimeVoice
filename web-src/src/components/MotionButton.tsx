import { Loader2 } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { cn } from "../lib/cn";

// Ported verbatim from
// ../../humanoid-robot/web/dashboard/src/components/MotionButton.tsx.

interface Props {
  label: string;
  icon?: LucideIcon;
  onPress: () => void;
  disabled?: boolean;
  busy?: boolean;
  danger?: boolean;
}

/** Large touch-friendly command button. */
export function MotionButton({ label, icon: Icon, onPress, disabled, busy, danger }: Props) {
  return (
    <button
      type="button"
      onClick={onPress}
      disabled={disabled || busy}
      className={cn(
        "relative flex min-h-[72px] flex-col items-center justify-center gap-1.5 rounded-xl border px-3 py-2 text-base font-medium transition active:scale-95 disabled:opacity-40",
        danger
          ? "border-red-500/40 bg-red-500/10 text-red-200"
          : "border-border bg-background/60 hover:bg-accent",
      )}
    >
      {busy ? (
        <Loader2 className="h-5 w-5 animate-spin" />
      ) : (
        Icon && <Icon className="h-5 w-5 text-muted-foreground" />
      )}
      <span className="text-center leading-tight">{label}</span>
    </button>
  );
}
