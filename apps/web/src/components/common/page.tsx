"use client";

import { cn, Skeleton } from "@scout/design-system";

/** Shared page chrome for non-table pages: compact header + scrollable body (spec: no dashboard clutter). */
export function Page({
  title,
  icon,
  actions,
  children,
  width = "wide",
}: {
  title: React.ReactNode;
  icon?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  width?: "wide" | "narrow";
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-4">
        {icon}
        <h1 className="title-gradient truncate text-title tracking-[-0.01em]">{title}</h1>
        <div className="ml-auto flex items-center gap-1.5">{actions}</div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className={cn("mx-auto w-full px-4 py-5", width === "narrow" ? "max-w-[760px]" : "max-w-[1180px]")}>{children}</div>
      </div>
    </div>
  );
}

export function Block({ title, aside, children, className }: { title?: React.ReactNode; aside?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("mb-6", className)}>
      {(title || aside) && (
        <div className="mb-2 flex items-center justify-between gap-2">
          {title && <h2 className="text-micro font-medium uppercase tracking-wide text-fg-3">{title}</h2>}
          {aside}
        </div>
      )}
      {children}
    </section>
  );
}

export function Panel({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("rounded-md bg-surface-1 shadow-[inset_0_0_0_1px_var(--border-subtle)]", className)}>{children}</div>;
}

export function Metric({ label, value, hint, tone }: { label: string; value: React.ReactNode; hint?: React.ReactNode; tone?: "success" | "warning" | "danger" }) {
  return (
    <div className="rounded-md bg-surface-1 px-3 py-2.5 shadow-[inset_0_0_0_1px_var(--border-subtle)]">
      <div className="text-meta text-fg-3">{label}</div>
      <div className={cn("tabular mt-0.5 text-title", tone === "success" ? "text-success" : tone === "warning" ? "text-warning" : tone === "danger" ? "text-danger" : "text-fg")}>
        {value}
      </div>
      {hint && <div className="mt-0.5 text-micro text-fg-3">{hint}</div>}
    </div>
  );
}

export function DataTable<T>({
  rows,
  columns,
  empty,
  loading,
  rowKey,
  onRowClick,
}: {
  rows: T[] | undefined;
  columns: { key: string; label: React.ReactNode; className?: string; render: (r: T) => React.ReactNode }[];
  empty?: React.ReactNode;
  loading?: boolean;
  rowKey: (r: T) => string;
  onRowClick?: (r: T) => void;
}) {
  return (
    <Panel className="overflow-x-auto">
      <table className="w-full text-table">
        <thead>
          <tr className="border-b border-line text-left text-meta text-fg-3">
            {columns.map((c) => (
              <th key={c.key} className={cn("h-8 whitespace-nowrap px-3 font-medium", c.className)}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading &&
            Array.from({ length: 5 }).map((_, i) => (
              <tr key={i} className="border-b border-line/60">
                {columns.map((c) => (
                  <td key={c.key} className="h-9 px-3">
                    <Skeleton className="h-2.5 w-2/3" />
                  </td>
                ))}
              </tr>
            ))}
          {!loading &&
            rows?.map((r) => (
              <tr
                key={rowKey(r)}
                onClick={onRowClick ? () => onRowClick(r) : undefined}
                className={cn("border-b border-line/60 last:border-0", onRowClick && "cursor-pointer hover:bg-row-hover")}
              >
                {columns.map((c) => (
                  <td key={c.key} className={cn("h-9 px-3", c.className)}>
                    {c.render(r)}
                  </td>
                ))}
              </tr>
            ))}
        </tbody>
      </table>
      {!loading && !rows?.length && <div className="px-3 py-8 text-center text-meta text-fg-3">{empty ?? "Nothing here yet"}</div>}
    </Panel>
  );
}
