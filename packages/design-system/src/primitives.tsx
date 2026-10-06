"use client";

import { cva, type VariantProps } from "class-variance-authority";
import { type ClassValue, clsx } from "clsx";
import {
  Checkbox as RCheckbox,
  ContextMenu as RContextMenu,
  Dialog as RDialog,
  DropdownMenu as RMenu,
  Popover as RPopover,
  Switch as RSwitch,
  Tooltip as RTooltip,
} from "radix-ui";
import * as React from "react";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/* ------------------------------------------------------------------------------------------------
 * Button
 * ----------------------------------------------------------------------------------------------*/

export const buttonVariants = cva(
  "inline-flex shrink-0 select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-sm font-medium transition-[background-color,color,box-shadow,opacity] duration-[var(--dur-1)] focus-visible:outline-none focus-visible:shadow-focus disabled:pointer-events-none disabled:opacity-45 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-contrast hover:bg-accent-strong",
        secondary: "bg-surface-2 text-fg shadow-[inset_0_0_0_1px_var(--border-strong)] hover:bg-surface-3",
        ghost: "text-fg-2 hover:bg-surface-2 hover:text-fg",
        subtle: "bg-accent-soft text-accent-strong hover:bg-accent-soft/80",
        danger: "bg-danger-soft text-danger shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--danger)_30%,transparent)] hover:bg-danger/20",
        link: "h-auto px-0 text-accent-strong underline-offset-2 hover:underline",
      },
      size: {
        xs: "h-6 px-2 text-meta [&_svg]:size-3.5",
        sm: "h-7 px-2.5 text-body [&_svg]:size-3.5",
        md: "h-8 px-3 text-body [&_svg]:size-4",
      },
    },
    defaultVariants: { variant: "secondary", size: "sm" },
  },
);

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button({ className, variant, size, type = "button", ...props }, ref) {
  return <button ref={ref} type={type} className={cn(buttonVariants({ variant, size }), className)} {...props} />;
});

export const IconButton = React.forwardRef<HTMLButtonElement, ButtonProps & { label: string }>(function IconButton(
  { className, variant = "ghost", size = "sm", label, children, ...props },
  ref,
) {
  const dim = size === "xs" ? "size-6" : size === "md" ? "size-8" : "size-7";
  return (
    <button ref={ref} type="button" aria-label={label} className={cn(buttonVariants({ variant, size }), dim, "px-0", className)} {...props}>
      {children}
    </button>
  );
});

/* ------------------------------------------------------------------------------------------------
 * Kbd, Spinner, Skeleton, Separator
 * ----------------------------------------------------------------------------------------------*/

export function Kbd({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <kbd
      className={cn(
        "inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-xs border border-line-strong bg-surface-2 px-1 font-sans text-micro font-medium text-fg-3",
        className,
      )}
    >
      {children}
    </kbd>
  );
}

export function Spinner({ className, size = 12 }: { className?: string; size?: number }) {
  return (
    <svg className={cn("animate-spin-slow text-fg-3", className)} width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden>
      <circle cx="8" cy="8" r="6.25" stroke="currentColor" strokeOpacity="0.25" strokeWidth="1.5" />
      <path d="M14.25 8A6.25 6.25 0 0 0 8 1.75" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse-soft rounded-xs bg-surface-3", className)} />;
}

export function Separator({ className, vertical }: { className?: string; vertical?: boolean }) {
  return <div role="separator" className={cn(vertical ? "h-4 w-px" : "h-px w-full", "bg-line", className)} />;
}

/* ------------------------------------------------------------------------------------------------
 * Badges & status
 * ----------------------------------------------------------------------------------------------*/

export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "info" | "muted";

const toneClass: Record<Tone, string> = {
  neutral: "bg-surface-3 text-fg-2",
  accent: "bg-accent-soft text-accent-strong",
  success: "bg-success-soft text-success",
  warning: "bg-warning-soft text-warning",
  danger: "bg-danger-soft text-danger",
  info: "bg-info-soft text-info",
  muted: "bg-transparent text-fg-3 shadow-[inset_0_0_0_1px_var(--border-strong)]",
};

const dotClass: Record<Tone, string> = {
  neutral: "bg-fg-3",
  accent: "bg-accent",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-info",
  muted: "bg-fg-3",
};

export function Badge({ tone = "neutral", dot, children, className, title }: { tone?: Tone; dot?: boolean; children: React.ReactNode; className?: string; title?: string }) {
  return (
    <span
      title={title}
      className={cn("inline-flex h-[18px] max-w-full items-center gap-1 truncate rounded-xs px-1.5 text-meta font-medium leading-none", toneClass[tone], className)}
    >
      {dot && <span className={cn("size-1.5 shrink-0 rounded-full", dotClass[tone])} />}
      {children}
    </span>
  );
}

export function StatusDot({ tone = "neutral", pulse, className }: { tone?: Tone; pulse?: boolean; className?: string }) {
  return <span className={cn("inline-block size-1.5 shrink-0 rounded-full", dotClass[tone], pulse && "animate-pulse-soft", className)} />;
}

export const EMAIL_STATUS_META: Record<string, { label: string; tone: Tone; hint: string }> = {
  SAFE: { label: "Safe", tone: "success", hint: "Mailbox accepted (or published on the official site); domain is not catch-all" },
  RISKY: { label: "Risky", tone: "warning", hint: "Likely valid (strong pattern) but not confirmed" },
  CATCH_ALL: { label: "Catch-all", tone: "warning", hint: "Domain accepts any address — cannot be verified" },
  UNKNOWN: { label: "Unknown", tone: "muted", hint: "Verification inconclusive" },
  INVALID: { label: "Invalid", tone: "danger", hint: "Rejected, no MX, or disposable" },
};

export function EmailStatusBadge({ status }: { status?: string | null }) {
  if (!status) return <span className="text-fg-3">—</span>;
  const meta = EMAIL_STATUS_META[status] ?? { label: status, tone: "neutral" as Tone, hint: "" };
  return (
    <Badge
      tone={meta.tone}
      dot
      title={meta.hint}
      className={status === "CATCH_ALL" ? "bg-transparent shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--warning)_40%,transparent)]" : undefined}
    >
      {meta.label}
    </Badge>
  );
}

/** Four-segment confidence meter (0–1). */
export function ConfidenceMeter({ value, className }: { value?: number | null; className?: string }) {
  if (value === null || value === undefined) return <span className="text-fg-3">—</span>;
  const pct = Math.round(value * 100);
  const filled = value >= 0.9 ? 4 : value >= 0.75 ? 3 : value >= 0.5 ? 2 : value > 0 ? 1 : 0;
  const tone = value >= 0.75 ? "bg-success" : value >= 0.5 ? "bg-warning" : "bg-danger";
  return (
    <span className={cn("inline-flex items-center gap-1.5", className)} title={`${pct}% confidence`}>
      <span className="inline-flex gap-[2px]">
        {[0, 1, 2, 3].map((i) => (
          <span key={i} className={cn("h-2.5 w-[3px] rounded-[1px]", i < filled ? tone : "bg-surface-3")} />
        ))}
      </span>
      <span className="tabular-nums text-fg-2">{pct}</span>
    </span>
  );
}

export function FreshnessBadge({ freshness }: { freshness?: string | null }) {
  if (!freshness || freshness === "unknown") return null;
  const tone: Tone = freshness === "fresh" ? "success" : freshness === "stale" ? "warning" : "muted";
  const label = freshness === "fresh" ? "Fresh" : freshness === "stale" ? "Stale" : freshness;
  return (
    <Badge tone={tone} className="h-4 px-1 text-micro">
      {label}
    </Badge>
  );
}

/* ------------------------------------------------------------------------------------------------
 * Inputs
 * ----------------------------------------------------------------------------------------------*/

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...props }, ref) {
  return (
    <input
      ref={ref}
      className={cn(
        "h-7 w-full rounded-sm bg-surface-2 px-2.5 text-body text-fg shadow-[inset_0_0_0_1px_var(--border-strong)] outline-none transition-shadow duration-[var(--dur-1)] placeholder:text-fg-3 focus:shadow-[inset_0_0_0_1px_var(--accent)]",
        className,
      )}
      {...props}
    />
  );
});

export const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea({ className, ...props }, ref) {
  return (
    <textarea
      ref={ref}
      className={cn(
        "w-full resize-none rounded-sm bg-surface-2 px-2.5 py-2 text-body text-fg shadow-[inset_0_0_0_1px_var(--border-strong)] outline-none placeholder:text-fg-3 focus:shadow-[inset_0_0_0_1px_var(--accent)]",
        className,
      )}
      {...props}
    />
  );
});

export function Checkbox({
  checked,
  onCheckedChange,
  className,
  label,
  indeterminate,
}: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  className?: string;
  label?: string;
  indeterminate?: boolean;
}) {
  return (
    <RCheckbox.Root
      aria-label={label}
      checked={indeterminate ? "indeterminate" : checked}
      onCheckedChange={(v) => onCheckedChange(v === true)}
      onClick={(e) => e.stopPropagation()}
      className={cn(
        "grid size-3.5 shrink-0 place-items-center rounded-[4px] shadow-[inset_0_0_0_1px_var(--border-strong)] transition-colors duration-[var(--dur-1)] hover:shadow-[inset_0_0_0_1px_var(--text-muted)] focus-visible:outline-none focus-visible:shadow-focus data-[state=checked]:bg-accent data-[state=checked]:shadow-none data-[state=indeterminate]:bg-accent data-[state=indeterminate]:shadow-none",
        className,
      )}
    >
      <RCheckbox.Indicator className="text-accent-contrast">
        {indeterminate ? (
          <svg width="8" height="8" viewBox="0 0 8 8" aria-hidden>
            <path d="M1.5 4h5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        ) : (
          <svg width="9" height="9" viewBox="0 0 10 10" aria-hidden>
            <path d="M2 5.2 4.1 7.2 8 2.8" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
      </RCheckbox.Indicator>
    </RCheckbox.Root>
  );
}

export function Switch({ checked, onCheckedChange, label }: { checked: boolean; onCheckedChange: (v: boolean) => void; label?: string }) {
  return (
    <RSwitch.Root
      aria-label={label}
      checked={checked}
      onCheckedChange={onCheckedChange}
      className="relative h-4 w-7 shrink-0 rounded-full bg-surface-3 shadow-[inset_0_0_0_1px_var(--border-strong)] transition-colors duration-[var(--dur-2)] data-[state=checked]:bg-accent focus-visible:outline-none focus-visible:shadow-focus"
    >
      <RSwitch.Thumb className="block size-3 translate-x-0.5 rounded-full bg-fg transition-transform duration-[var(--dur-2)] data-[state=checked]:translate-x-[13px] data-[state=checked]:bg-accent-contrast" />
    </RSwitch.Root>
  );
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  className,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: React.ReactNode }[];
  className?: string;
}) {
  return (
    <div className={cn("inline-flex h-7 items-center gap-0.5 rounded-sm bg-surface-2 p-0.5 shadow-[inset_0_0_0_1px_var(--border-subtle)]", className)} role="radiogroup">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            "h-6 rounded-[4px] px-2 text-meta font-medium text-fg-3 transition-colors duration-[var(--dur-1)] hover:text-fg-2",
            value === o.value && "bg-surface-3 text-fg shadow-[0_1px_0_rgba(0,0,0,0.25)]",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function ProgressBar({ value, max = 100, className, tone = "accent" }: { value: number; max?: number; className?: string; tone?: "accent" | "success" }) {
  const pct = Math.max(0, Math.min(100, (value / Math.max(1, max)) * 100));
  return (
    <div className={cn("h-1 w-full overflow-hidden rounded-full bg-surface-3", className)} role="progressbar" aria-valuenow={value} aria-valuemax={max}>
      <div className={cn("h-full rounded-full transition-[width] duration-500 ease-out", tone === "success" ? "bg-success" : "bg-accent")} style={{ width: `${pct}%` }} />
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------
 * Tooltip
 * ----------------------------------------------------------------------------------------------*/

export const TooltipProvider = RTooltip.Provider;

export function Tip({
  content,
  children,
  side = "top",
  shortcut,
  delay,
}: {
  content: React.ReactNode;
  children: React.ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  shortcut?: string[];
  delay?: number;
}) {
  return (
    <RTooltip.Root delayDuration={delay}>
      <RTooltip.Trigger asChild>{children}</RTooltip.Trigger>
      <RTooltip.Portal>
        <RTooltip.Content side={side} sideOffset={6} className="z-50 flex animate-fade-in items-center gap-2 rounded-sm bg-surface-3 px-2 py-1 text-meta text-fg shadow-popover">
          {content}
          {shortcut && (
            <span className="flex gap-0.5">
              {shortcut.map((k) => (
                <Kbd key={k}>{k}</Kbd>
              ))}
            </span>
          )}
        </RTooltip.Content>
      </RTooltip.Portal>
    </RTooltip.Root>
  );
}

/* ------------------------------------------------------------------------------------------------
 * Menus (dropdown + context)
 * ----------------------------------------------------------------------------------------------*/

const menuContent = "z-50 min-w-[180px] animate-fade-in overflow-hidden rounded-md bg-surface-3 p-1 text-body text-fg shadow-popover";
const menuItem =
  "relative flex h-7 cursor-default select-none items-center gap-2 rounded-sm px-2 text-body outline-none data-[disabled]:pointer-events-none data-[disabled]:opacity-40 data-[highlighted]:bg-row-hover data-[highlighted]:bg-surface-2 [&_svg]:size-3.5 [&_svg]:text-fg-3";

export const Menu = RMenu.Root;
export const MenuTrigger = RMenu.Trigger;
export const MenuSub = RMenu.Sub;

export function MenuContent({
  children,
  align = "start",
  className,
  sideOffset = 4,
}: {
  children: React.ReactNode;
  align?: "start" | "end" | "center";
  className?: string;
  sideOffset?: number;
}) {
  return (
    <RMenu.Portal>
      <RMenu.Content align={align} sideOffset={sideOffset} className={cn(menuContent, className)}>
        {children}
      </RMenu.Content>
    </RMenu.Portal>
  );
}

export function MenuItem({
  children,
  onSelect,
  danger,
  shortcut,
  disabled,
  icon,
}: {
  children: React.ReactNode;
  onSelect?: () => void;
  danger?: boolean;
  shortcut?: string;
  disabled?: boolean;
  icon?: React.ReactNode;
}) {
  return (
    <RMenu.Item disabled={disabled} onSelect={onSelect} className={cn(menuItem, danger && "text-danger [&_svg]:text-danger")}>
      {icon}
      <span className="flex-1 truncate">{children}</span>
      {shortcut && <span className="text-meta text-fg-3">{shortcut}</span>}
    </RMenu.Item>
  );
}

export function MenuSubTrigger({ children, icon }: { children: React.ReactNode; icon?: React.ReactNode }) {
  return (
    <RMenu.SubTrigger className={cn(menuItem, "data-[state=open]:bg-surface-2")}>
      {icon}
      <span className="flex-1">{children}</span>
      <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden className="text-fg-3">
        <path d="m3.5 2 3 3-3 3" stroke="currentColor" strokeWidth="1.3" fill="none" />
      </svg>
    </RMenu.SubTrigger>
  );
}

export function MenuSubContent({ children }: { children: React.ReactNode }) {
  return (
    <RMenu.Portal>
      <RMenu.SubContent sideOffset={6} className={cn(menuContent, "max-h-80 overflow-y-auto")}>
        {children}
      </RMenu.SubContent>
    </RMenu.Portal>
  );
}

export function MenuLabel({ children }: { children: React.ReactNode }) {
  return <RMenu.Label className="px-2 pb-1 pt-1.5 text-micro uppercase tracking-wide text-fg-3">{children}</RMenu.Label>;
}

export function MenuSeparator() {
  return <RMenu.Separator className="-mx-1 my-1 h-px bg-line" />;
}

export function MenuCheckboxItem({ checked, onCheckedChange, children }: { checked: boolean; onCheckedChange: (v: boolean) => void; children: React.ReactNode }) {
  return (
    <RMenu.CheckboxItem checked={checked} onCheckedChange={onCheckedChange} onSelect={(e) => e.preventDefault()} className={cn(menuItem, "pl-7")}>
      <RMenu.ItemIndicator className="absolute left-2 text-accent">
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
          <path d="M2 5.2 4.1 7.2 8 2.8" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" />
        </svg>
      </RMenu.ItemIndicator>
      {children}
    </RMenu.CheckboxItem>
  );
}

export const ContextMenu = RContextMenu.Root;
export const ContextMenuTrigger = RContextMenu.Trigger;

export function ContextMenuContent({ children }: { children: React.ReactNode }) {
  return (
    <RContextMenu.Portal>
      <RContextMenu.Content className={menuContent}>{children}</RContextMenu.Content>
    </RContextMenu.Portal>
  );
}

export function ContextMenuItem({
  children,
  onSelect,
  danger,
  icon,
  shortcut,
}: {
  children: React.ReactNode;
  onSelect?: () => void;
  danger?: boolean;
  icon?: React.ReactNode;
  shortcut?: string;
}) {
  return (
    <RContextMenu.Item onSelect={onSelect} className={cn(menuItem, danger && "text-danger [&_svg]:text-danger")}>
      {icon}
      <span className="flex-1 truncate">{children}</span>
      {shortcut && <span className="text-meta text-fg-3">{shortcut}</span>}
    </RContextMenu.Item>
  );
}

export function ContextMenuSeparator() {
  return <RContextMenu.Separator className="-mx-1 my-1 h-px bg-line" />;
}

export function ContextMenuSub({ label, icon, children }: { label: React.ReactNode; icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <RContextMenu.Sub>
      <RContextMenu.SubTrigger className={cn(menuItem, "data-[state=open]:bg-surface-2")}>
        {icon}
        <span className="flex-1">{label}</span>
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden className="text-fg-3">
          <path d="m3.5 2 3 3-3 3" stroke="currentColor" strokeWidth="1.3" fill="none" />
        </svg>
      </RContextMenu.SubTrigger>
      <RContextMenu.Portal>
        <RContextMenu.SubContent sideOffset={6} className={cn(menuContent, "max-h-80 overflow-y-auto")}>
          {children}
        </RContextMenu.SubContent>
      </RContextMenu.Portal>
    </RContextMenu.Sub>
  );
}

/* ------------------------------------------------------------------------------------------------
 * Popover, Dialog, Sheet
 * ----------------------------------------------------------------------------------------------*/

export const Popover = RPopover.Root;
export const PopoverTrigger = RPopover.Trigger;
export const PopoverAnchor = RPopover.Anchor;

export function PopoverContent({
  children,
  className,
  align = "start",
  side = "bottom",
  sideOffset = 6,
}: {
  children: React.ReactNode;
  className?: string;
  align?: "start" | "center" | "end";
  side?: "top" | "bottom" | "left" | "right";
  sideOffset?: number;
}) {
  return (
    <RPopover.Portal>
      <RPopover.Content
        align={align}
        side={side}
        sideOffset={sideOffset}
        className={cn("z-50 animate-fade-in rounded-md bg-surface-3 p-2 text-body text-fg shadow-popover outline-none", className)}
      >
        {children}
      </RPopover.Content>
    </RPopover.Portal>
  );
}

export const Dialog = RDialog.Root;
export const DialogTrigger = RDialog.Trigger;
export const DialogClose = RDialog.Close;

export function DialogContent({
  children,
  title,
  description,
  className,
  width = 520,
}: {
  children: React.ReactNode;
  title: React.ReactNode;
  description?: React.ReactNode;
  className?: string;
  width?: number;
}) {
  return (
    <RDialog.Portal>
      <RDialog.Overlay className="fixed inset-0 z-50 bg-overlay backdrop-blur-[2px] data-[state=open]:animate-fade-in" />
      <RDialog.Content
        style={{ width: `min(${width}px, calc(100vw - 32px))` }}
        className={cn("fixed left-1/2 top-[12vh] z-50 -translate-x-1/2 rounded-lg bg-surface-1 shadow-dialog outline-none data-[state=open]:animate-fade-in", className)}
      >
        <div className="flex items-start justify-between gap-4 border-b border-line px-4 py-3">
          <div className="min-w-0">
            <RDialog.Title className="text-heading text-fg">{title}</RDialog.Title>
            {description ? (
              <RDialog.Description className="mt-0.5 text-meta text-fg-3">{description}</RDialog.Description>
            ) : (
              <RDialog.Description className="sr-only">{title}</RDialog.Description>
            )}
          </div>
          <RDialog.Close className="rounded-sm p-1 text-fg-3 hover:bg-surface-2 hover:text-fg" aria-label="Close">
            <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
              <path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            </svg>
          </RDialog.Close>
        </div>
        {children}
      </RDialog.Content>
    </RDialog.Portal>
  );
}
