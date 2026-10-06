"use client";

import { Badge, Button, cn, Input, Kbd, Segmented, Switch, Textarea } from "@scout/design-system";
import { SHORTCUTS } from "@scout/shared";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Bookmark, Play, Plus, Settings, Trash2, Webhook } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Block, DataTable, Page, Panel } from "@/components/common/page";
import { api } from "@/lib/api";
import { relTime } from "@/lib/format";
import { qk, useMe } from "@/lib/queries";

const FRESHNESS_FIELDS: { key: string; label: string; def: number }[] = [
  { key: "website", label: "Website content", def: 30 },
  { key: "company_description", label: "Company description", def: 30 },
  { key: "role", label: "Person role / title", def: 45 },
  { key: "email", label: "Email verification", def: 60 },
  { key: "mx", label: "MX records", def: 30 },
  { key: "email_pattern", label: "Email pattern", def: 180 },
  { key: "technology", label: "Technologies", def: 30 },
  { key: "grounded_research", label: "Web research", def: 14 },
];

type Section = "workspace" | "data" | "suppression" | "searches" | "integrations" | "shortcuts";

export function SettingsPage() {
  const [section, setSection] = useState<Section>("workspace");
  return (
    <Page title="Settings" icon={<Settings className="size-4 text-fg-3" />} width="narrow">
      <div className="mb-5 overflow-x-auto">
        <Segmented<Section>
          value={section}
          onChange={setSection}
          options={[
            { value: "workspace", label: "Workspace" },
            { value: "data", label: "Data & freshness" },
            { value: "suppression", label: "Suppression" },
            { value: "searches", label: "Saved searches" },
            { value: "integrations", label: "Webhooks" },
            { value: "shortcuts", label: "Shortcuts" },
          ]}
        />
      </div>
      {section === "workspace" && <WorkspaceSettings />}
      {section === "data" && <FreshnessSettings />}
      {section === "suppression" && <SuppressionSettings />}
      {section === "searches" && <SavedSearches />}
      {section === "integrations" && <Webhooks />}
      {section === "shortcuts" && <Shortcuts />}
    </Page>
  );
}

function useWorkspace() {
  const me = useMe();
  return { me, ws: me.data?.workspaces.find((w) => w.id === me.data?.current_workspace_id) ?? me.data?.workspaces[0] };
}

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-1 gap-2 px-4 py-3 sm:grid-cols-[220px_1fr] sm:items-center">
      <div>
        <div className="text-body text-fg">{label}</div>
        {hint && <div className="text-meta text-fg-3">{hint}</div>}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

type WorkspaceInfo = NonNullable<ReturnType<typeof useWorkspace>["ws"]>;

function WorkspaceSettings() {
  const { ws } = useWorkspace();
  if (!ws) return <p className="text-meta text-fg-3">Loading…</p>;
  return <WorkspaceForm key={ws.id} ws={ws} />;
}

function WorkspaceForm({ ws }: { ws: WorkspaceInfo }) {
  const qc = useQueryClient();
  const me = useMe();
  const [name, setName] = useState(ws.name);
  const [budget, setBudget] = useState(String(ws.monthly_budget_usd ?? ""));
  const [hardCap, setHardCap] = useState(ws.hard_budget_cap);
  const canEdit = ws.role === "owner" || ws.role === "admin";

  async function save() {
    try {
      await api("workspace", { method: "PATCH", body: { name: name.trim() || null, monthly_budget_usd: budget === "" ? null : Number(budget), hard_budget_cap: hardCap } });
      void qc.invalidateQueries({ queryKey: qk.me });
      toast("Workspace saved");
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <>
      <Block title="Workspace">
        <Panel className="divide-y divide-line">
          <Row label="Name">
            <Input value={name} onChange={(e) => setName(e.target.value)} disabled={!canEdit} />
          </Row>
          <Row label="Monthly AI budget" hint="Paid AI calls stop or warn past this amount">
            <div className="flex items-center gap-2">
              <span className="text-fg-3">$</span>
              <Input type="number" min={0} step={1} value={budget} onChange={(e) => setBudget(e.target.value)} className="w-32" disabled={!canEdit} />
            </div>
          </Row>
          <Row label="Hard cap" hint="When on, searches pause instead of exceeding the budget">
            <Switch checked={hardCap} onCheckedChange={setHardCap} label="Hard budget cap" />
          </Row>
          <Row label="Your role">
            <Badge tone="neutral">{ws.role}</Badge>
          </Row>
        </Panel>
        <div className="mt-2 flex justify-end">
          <Button variant="primary" disabled={!canEdit} onClick={() => void save()}>
            Save
          </Button>
        </div>
      </Block>
      <Block title="AI">
        <Panel className="divide-y divide-line">
          <Row label="Provider" hint="Set with AI_PROVIDER on the API">
            <Badge tone={me.data?.ai_provider === "gemini" ? "success" : "warning"} dot>
              {me.data?.ai_provider ?? "—"}
            </Badge>
          </Row>
          {Object.entries(me.data?.ai_models ?? {}).map(([role, model]) => (
            <Row key={role} label={`${role.replace(/_/g, " ")} model`}>
              <span className="font-mono text-[12px] text-fg-2">{model}</span>
            </Row>
          ))}
        </Panel>
        {me.data?.ai_provider !== "gemini" && (
          <p className="mt-2 text-meta text-fg-3">Running without a Gemini key: parsing, planning and extraction use deterministic rules; web research is unavailable.</p>
        )}
      </Block>
    </>
  );
}

function FreshnessSettings() {
  const { ws } = useWorkspace();
  if (!ws) return <p className="text-meta text-fg-3">Loading…</p>;
  return <FreshnessForm key={ws.id} ws={ws} />;
}

function FreshnessForm({ ws }: { ws: WorkspaceInfo }) {
  const qc = useQueryClient();
  const [values, setValues] = useState<Record<string, number>>(() => {
    const o = ((ws.settings as Record<string, unknown> | undefined)?.freshness as Record<string, number> | undefined) ?? {};
    return Object.fromEntries(FRESHNESS_FIELDS.map((f) => [f.key, o[f.key] ?? f.def]));
  });

  async function save() {
    try {
      await api("workspace", { method: "PATCH", body: { settings: { freshness: values } } });
      void qc.invalidateQueries({ queryKey: qk.me });
      toast("Freshness rules saved");
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <Block title="Freshness rules" aside={<span className="text-meta text-fg-3">Data older than this is refreshed on demand, never silently reused</span>}>
      <Panel className="divide-y divide-line">
        {FRESHNESS_FIELDS.map((f) => (
          <Row key={f.key} label={f.label} hint={`Default ${f.def} days`}>
            <div className="flex items-center gap-2">
              <Input
                type="number"
                min={1}
                max={730}
                value={values[f.key] ?? f.def}
                onChange={(e) => setValues((v) => ({ ...v, [f.key]: Math.max(1, Number(e.target.value) || f.def) }))}
                className="w-24"
              />
              <span className="text-meta text-fg-3">days</span>
            </div>
          </Row>
        ))}
      </Panel>
      <div className="mt-2 flex justify-end">
        <Button variant="primary" onClick={() => void save()}>
          Save
        </Button>
      </div>
    </Block>
  );
}

interface Suppression {
  id: string;
  entity_type: string;
  value: string;
  label: string;
  reason: string;
  note: string | null;
  created_at: string;
}

function SuppressionSettings() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["suppression"], queryFn: () => api<Suppression[]>("suppression") });
  const [text, setText] = useState("");
  const [reason, setReason] = useState("do_not_contact");

  async function add() {
    const items = text
      .split(/[\s,;]+/)
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean);
    const emails = items.filter((i) => i.includes("@"));
    const domains = items
      .filter((i) => !i.includes("@"))
      .map((d) =>
        d
          .replace(/^https?:\/\//, "")
          .replace(/^www\./, "")
          .replace(/\/.*$/, ""),
      );
    if (!items.length) return;
    try {
      const r = await api<{ suppressed: number }>("suppression", { body: { emails, domains, reason } });
      toast(`Suppressed ${r.suppressed} entr${r.suppressed === 1 ? "y" : "ies"}`);
      setText("");
      void qc.invalidateQueries({ queryKey: ["suppression"] });
      void qc.invalidateQueries({ queryKey: ["rows"] });
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  async function remove(id: string) {
    try {
      await api(`suppression/${id}`, { method: "DELETE" });
      void qc.invalidateQueries({ queryKey: ["suppression"] });
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <>
      <Block title="Add to suppression list">
        <Panel className="space-y-2 p-3">
          <Textarea rows={3} placeholder="Emails or domains, separated by commas or new lines" value={text} onChange={(e) => setText(e.target.value)} />
          <div className="flex items-center justify-between gap-2">
            <select
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="h-7 rounded-sm bg-surface-2 px-1.5 text-body text-fg outline-none shadow-[inset_0_0_0_1px_var(--border-strong)]"
              aria-label="Reason"
            >
              <option value="do_not_contact">Do not contact</option>
              <option value="opt_out">Opted out</option>
              <option value="user_request">User request</option>
              <option value="gdpr">GDPR request</option>
              <option value="invalid">Invalid</option>
              <option value="manual">Manual</option>
            </select>
            <Button variant="primary" disabled={!text.trim()} onClick={() => void add()}>
              <Ban /> Suppress
            </Button>
          </div>
          <p className="text-meta text-fg-3">Suppressed contacts are never exported, added to lists, or rediscovered by any search.</p>
        </Panel>
      </Block>
      <Block title={`Suppressed (${q.data?.length ?? 0})`}>
        <DataTable<Suppression>
          rows={q.data}
          loading={q.isLoading}
          rowKey={(r) => r.id}
          empty="Nothing suppressed"
          columns={[
            { key: "label", label: "Entry", render: (r) => <span className="text-fg">{r.label}</span> },
            { key: "type", label: "Type", render: (r) => <Badge tone="muted">{r.entity_type}</Badge> },
            { key: "reason", label: "Reason", render: (r) => <span className="text-fg-2">{r.reason.replace(/_/g, " ")}</span> },
            { key: "at", label: "Added", className: "text-right", render: (r) => <span className="text-fg-3">{relTime(r.created_at)}</span> },
            {
              key: "x",
              label: "",
              className: "w-10",
              render: (r) => (
                <button type="button" onClick={() => void remove(r.id)} className="text-fg-3 hover:text-danger" aria-label="Remove">
                  <Trash2 className="size-3.5" />
                </button>
              ),
            },
          ]}
        />
      </Block>
    </>
  );
}

interface Template {
  id: string;
  name: string;
  prompt: string | null;
  last_run_at: string | null;
  created_at: string;
}

function SavedSearches() {
  const router = useRouter();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["templates"], queryFn: () => api<Template[]>("campaign-templates") });
  async function run(t: Template) {
    try {
      const c = await api<{ id: string; target_list_id: string | null }>(`campaign-templates/${t.id}/run`, { body: { only_new: true } });
      toast.success(`Running “${t.name}” — only new leads`);
      void qc.invalidateQueries({ queryKey: qk.campaigns });
      router.push(c.target_list_id ? `/lists/${c.target_list_id}` : `/campaigns/${c.id}`);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }
  return (
    <Block title="Saved searches" aside={<span className="text-meta text-fg-3">Reruns skip every lead you have already seen</span>}>
      <DataTable<Template>
        rows={q.data}
        loading={q.isLoading}
        rowKey={(t) => t.id}
        empty={
          <span className="inline-flex items-center gap-1.5">
            <Bookmark className="size-3.5" /> Save a search from its campaign page to rerun it later
          </span>
        }
        columns={[
          { key: "n", label: "Name", render: (t) => <span className="font-medium text-fg">{t.name}</span> },
          { key: "p", label: "Prompt", render: (t) => <span className="line-clamp-1 text-fg-3">{t.prompt ?? ""}</span> },
          { key: "l", label: "Last run", className: "text-right", render: (t) => <span className="text-fg-3">{t.last_run_at ? relTime(t.last_run_at) : "never"}</span> },
          {
            key: "r",
            label: "",
            className: "w-24 text-right",
            render: (t) => (
              <Button size="xs" variant="ghost" onClick={() => void run(t)}>
                <Play /> Run
              </Button>
            ),
          },
        ]}
      />
    </Block>
  );
}

interface Hook {
  id: string;
  url: string;
  events: string[];
  is_active: boolean;
  last_status: number | null;
  last_delivery_at: string | null;
}

function Webhooks() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["webhooks"], queryFn: () => api<Hook[]>("webhooks") });
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  async function add() {
    try {
      const r = await api<{ id: string; secret?: string }>("webhooks", { body: { url: url.trim(), events: ["lead.qualified", "campaign.completed"] } });
      setSecret(r.secret ?? null);
      setUrl("");
      void qc.invalidateQueries({ queryKey: ["webhooks"] });
    } catch (e) {
      const err = e as Error & { hint?: string };
      toast.error(err.message, err.hint ? { description: err.hint } : undefined);
    }
  }
  async function remove(id: string) {
    try {
      await api(`webhooks/${id}`, { method: "DELETE" });
      void qc.invalidateQueries({ queryKey: ["webhooks"] });
    } catch (e) {
      toast.error((e as Error).message);
    }
  }
  return (
    <>
      <Block title="Add webhook">
        <Panel className="flex items-center gap-2 p-3">
          <Webhook className="size-4 shrink-0 text-fg-3" />
          <Input placeholder="https://example.com/hooks/scout" value={url} onChange={(e) => setUrl(e.target.value)} />
          <Button variant="primary" disabled={!/^https:\/\//.test(url.trim())} onClick={() => void add()}>
            <Plus /> Add
          </Button>
        </Panel>
        {secret && (
          <Panel className="mt-2 p-3 text-meta">
            <p className="text-fg-2">
              Signing secret — shown once. Deliveries carry an <code className="font-mono">X-Scout-Signature</code> HMAC-SHA256 header.
            </p>
            <code className="mt-1 block break-all rounded-xs bg-surface-2 px-2 py-1 font-mono text-[12px] text-fg">{secret}</code>
          </Panel>
        )}
      </Block>
      <Block title="Webhooks">
        <DataTable<Hook>
          rows={q.data}
          loading={q.isLoading}
          rowKey={(h) => h.id}
          empty="No webhooks"
          columns={[
            { key: "u", label: "URL", render: (h) => <span className="font-mono text-[12px] text-fg">{h.url}</span> },
            { key: "e", label: "Events", render: (h) => <span className="text-fg-3">{h.events.join(", ")}</span> },
            {
              key: "s",
              label: "Last delivery",
              render: (h) => (
                <span className={cn(h.last_status && h.last_status >= 400 ? "text-danger" : "text-fg-3")}>
                  {h.last_delivery_at ? `${h.last_status ?? "—"} · ${relTime(h.last_delivery_at)}` : "never"}
                </span>
              ),
            },
            {
              key: "x",
              label: "",
              className: "w-10",
              render: (h) => (
                <button type="button" onClick={() => void remove(h.id)} className="text-fg-3 hover:text-danger" aria-label="Delete webhook">
                  <Trash2 className="size-3.5" />
                </button>
              ),
            },
          ]}
        />
      </Block>
    </>
  );
}

function Shortcuts() {
  const groups: ["global" | "table" | "chat", string][] = [
    ["global", "Everywhere"],
    ["table", "Table"],
    ["chat", "Assistant"],
  ];
  return (
    <>
      {groups.map(([scope, title]) => (
        <Block key={scope} title={title}>
          <Panel className="divide-y divide-line">
            {SHORTCUTS.filter((s) => s.scope === scope).map((s) => (
              <div key={s.label} className="flex items-center justify-between px-4 py-2 text-body">
                <span className="text-fg-2">{s.label}</span>
                <span className="flex gap-1">
                  {s.keys.map((k) => (
                    <Kbd key={k}>{k}</Kbd>
                  ))}
                </span>
              </div>
            ))}
          </Panel>
        </Block>
      ))}
    </>
  );
}
