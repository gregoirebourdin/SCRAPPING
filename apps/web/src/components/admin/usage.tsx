"use client";

import { ProgressBar } from "@scout/design-system";
import { useQuery } from "@tanstack/react-query";
import { ChartNoAxesColumn } from "lucide-react";
import Link from "next/link";

import { Block, DataTable, Metric, Page, Panel } from "@/components/common/page";
import { api } from "@/lib/api";
import { n, pct, usd } from "@/lib/format";
import { qk } from "@/lib/queries";

interface Usage {
  month_cost_usd: number;
  monthly_budget_usd: number | null;
  hard_budget_cap: boolean;
  qualified_this_month: number;
  cost_per_qualified: number | null;
  by_category: { category: string; cost_usd: number; quantity: number; tokens_in: number; tokens_out: number }[];
  by_model: { model: string; cost_usd: number; tokens_in: number; tokens_out: number; calls: number }[];
  by_campaign: { id: string; name: string; cost_usd: number; qualified: number; raw: number; yield: number | null; cost_per_qualified: number | null }[];
  daily: { day: string; cost_usd: number }[];
  totals: { unique_companies: number; unique_people: number; new_companies_this_month: number; new_people_this_month: number; safe_email_rate: number | null };
}

/** Cost tracking (spec §95–§96): spend vs budget, by category/model/campaign, daily trend. */
export function UsagePage() {
  const q = useQuery({ queryKey: qk.usage, queryFn: () => api<Usage>("usage"), refetchInterval: 60_000 });
  const u = q.data;
  const budget = u?.monthly_budget_usd ?? 0;
  const used = u?.month_cost_usd ?? 0;
  const maxDay = Math.max(0.0001, ...(u?.daily ?? []).map((d) => d.cost_usd));

  return (
    <Page title="Usage & cost" icon={<ChartNoAxesColumn className="size-4 text-fg-3" />}>
      <div className="mb-6 grid grid-cols-2 gap-2 md:grid-cols-4">
        <Metric
          label="Spend this month"
          value={usd(used)}
          tone={budget && used / budget > 0.9 ? "warning" : undefined}
          hint={
            budget ? (
              <span className="block">
                <ProgressBar value={used} max={budget} className="mb-1 mt-1" />
                of {usd(budget)} budget {u?.hard_budget_cap ? "· hard cap" : "· soft limit"} ·{" "}
                <Link href="/settings" className="hover:text-fg hover:underline">
                  edit
                </Link>
              </span>
            ) : (
              "No budget set"
            )
          }
        />
        <Metric label="Qualified this month" value={n(u?.qualified_this_month)} hint={u?.cost_per_qualified ? `${usd(u.cost_per_qualified, 3)} per qualified lead` : undefined} />
        <Metric label="Companies in registry" value={n(u?.totals.unique_companies)} hint={`+${n(u?.totals.new_companies_this_month)} this month`} />
        <Metric
          label="People in registry"
          value={n(u?.totals.unique_people)}
          hint={`+${n(u?.totals.new_people_this_month)} this month · ${pct(u?.totals.safe_email_rate)} safe emails`}
        />
      </div>

      <Block title="Daily spend (30 days)">
        <Panel className="px-3 pb-2 pt-4">
          <div className="flex h-28 items-end gap-[3px]">
            {(u?.daily ?? []).map((d) => (
              <div key={d.day} className="group relative flex-1">
                <div className="w-full rounded-t-[2px] bg-accent/70 transition-colors group-hover:bg-accent" style={{ height: `${Math.max(2, (d.cost_usd / maxDay) * 104)}px` }} />
                <div className="pointer-events-none absolute -top-7 left-1/2 z-10 hidden -translate-x-1/2 whitespace-nowrap rounded-xs bg-surface-3 px-1.5 py-0.5 text-micro text-fg shadow-popover group-hover:block">
                  {new Date(d.day).toLocaleDateString("en-US", { month: "short", day: "numeric" })} · {usd(d.cost_usd, 3)}
                </div>
              </div>
            ))}
            {!u?.daily?.length && <p className="w-full self-center text-center text-meta text-fg-3">No spend yet</p>}
          </div>
        </Panel>
      </Block>

      <div className="grid gap-4 lg:grid-cols-2">
        <Block title="By category">
          <DataTable
            rows={u?.by_category}
            loading={q.isLoading}
            rowKey={(r) => r.category}
            columns={[
              { key: "c", label: "Category", render: (r) => <span className="text-fg">{r.category.replace(/_/g, " ")}</span> },
              { key: "q", label: "Units", className: "text-right", render: (r) => <span className="tabular text-fg-2">{n(r.quantity)}</span> },
              { key: "t", label: "Tokens", className: "text-right", render: (r) => <span className="tabular text-fg-2">{n(r.tokens_in + r.tokens_out)}</span> },
              { key: "cost", label: "Cost", className: "text-right", render: (r) => <span className="tabular text-fg">{usd(r.cost_usd, 3)}</span> },
            ]}
          />
        </Block>
        <Block title="By model">
          <DataTable
            rows={u?.by_model}
            loading={q.isLoading}
            rowKey={(r) => r.model}
            columns={[
              { key: "m", label: "Model", render: (r) => <span className="font-mono text-[12px] text-fg">{r.model}</span> },
              { key: "calls", label: "Calls", className: "text-right", render: (r) => <span className="tabular text-fg-2">{n(r.calls)}</span> },
              {
                key: "t",
                label: "Tokens in / out",
                className: "text-right",
                render: (r) => (
                  <span className="tabular text-fg-2">
                    {n(r.tokens_in)} / {n(r.tokens_out)}
                  </span>
                ),
              },
              { key: "cost", label: "Cost", className: "text-right", render: (r) => <span className="tabular text-fg">{usd(r.cost_usd, 3)}</span> },
            ]}
          />
        </Block>
      </div>

      <Block title="By search">
        <DataTable
          rows={u?.by_campaign}
          loading={q.isLoading}
          rowKey={(r) => r.id}
          empty="No searches this month"
          columns={[
            {
              key: "n",
              label: "Search",
              render: (r) => (
                <Link href={`/campaigns/${r.id}`} className="text-fg hover:underline">
                  {r.name}
                </Link>
              ),
            },
            { key: "raw", label: "Discovered", className: "text-right", render: (r) => <span className="tabular text-fg-2">{n(r.raw)}</span> },
            { key: "q", label: "Qualified", className: "text-right", render: (r) => <span className="tabular text-fg">{n(r.qualified)}</span> },
            { key: "y", label: "Yield", className: "text-right", render: (r) => <span className="tabular text-fg-2">{pct(r.yield, 1)}</span> },
            {
              key: "cpq",
              label: "Cost / qualified",
              className: "text-right",
              render: (r) => <span className="tabular text-fg-2">{r.cost_per_qualified != null ? usd(r.cost_per_qualified, 3) : "—"}</span>,
            },
            { key: "c", label: "Cost", className: "text-right", render: (r) => <span className="tabular text-fg">{usd(r.cost_usd, 3)}</span> },
          ]}
        />
      </Block>
    </Page>
  );
}
