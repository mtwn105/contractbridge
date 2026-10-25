"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@/components/icon";
import type { Migration, Rule } from "@/lib/types";

type Connection = {
  id: string;
  name: string;
  revision: number;
  state: string;
  sourcePath?: string;
  targetPath?: string | null;
  successCount: number;
  failureCount: number;
  fixtureCount: number;
  route: { mode: string; buildId?: string; migrationId?: string };
  repository?: string | null;
  sentryProject?: string | null;
  application?: { environment?: string } | null;
};

type Candidate = {
  tests: number;
  artifactHash: string;
  inputHash?: string;
  verificationMode: string;
};

type Review = {
  decision?: string;
  comment?: string;
  candidateHash?: string;
  at?: string;
  actor?: string;
};

type Lifecycle = {
  phase?: string;
  updatedAt?: string;
  sharedMappingHash?: string;
  message?: string;
  review?: Review;
  mitigation?: { status?: string; artifactHash?: string; buildId?: string; message?: string };
  permanent?: { status?: string; patchHash?: string; tests?: number; applicationFiles?: string[] };
  merge?: { sha?: string };
  deployment?: { sha?: string; state?: string; environment?: string };
  verification?: { passed?: boolean; samples?: unknown[]; commit?: string };
};

type Incident = {
  id: string;
  revision: number;
  state: string;
  running: boolean;
  occurrences?: number;
  kind?: string;
  upstreamStatus?: number;
  createdAt?: string;
  updatedAt?: string;
  lastSeen?: string;
  message?: string;
  error?: string;
  publicationError?: string;
  candidate?: Candidate | null;
  publication?: { url?: string; status?: string; artifactHash?: string; patchHash?: string; number?: number } | null;
  lifecycle?: Lifecycle | null;
  review?: Review;
  reviewHistory?: Review[];
  migration?: Migration;
};

type StatusResponse = {
  enabled: boolean;
  connection: Connection;
  incident: Incident | null;
  github: { configured: boolean; repository: string | null };
  sentryUrl?: string | null;
};

type ConnectionsResponse = { connections: Connection[] };
type AuditEvent = {
  id: string;
  action: string;
  revision: number;
  createdAt: string;
  metadata?: { candidateArtifactHash?: string | null; publicationAuthorizationType?: string | null };
};
type MappingRow = { direction: string; from: string; op: string; to: string; note: string; review: boolean };

async function getJson<T>(endpoint: string): Promise<T> {
  const response = await fetch(`/api/bridge/${endpoint}`, { cache: "no-store" });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.title || "Control plane request failed");
  return value as T;
}

function label(value?: string | null) {
  return (value || "not reported").replaceAll("_", " ").toLowerCase();
}

function time(value?: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "—" : parsed.toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function severity(connection: Connection, incident?: Incident | null) {
  if (incident?.lifecycle?.phase === "RESOLVED") return { className: "ok", text: "resolved" };
  if (connection.route.mode === "BRIDGE") return { className: "warn", text: "mitigated" };
  if (incident?.lifecycle?.phase === "REVISING") return { className: "info", text: "revising mapping" };
  if (incident?.lifecycle?.phase === "APPROVING") return { className: "info", text: "activating bridge" };
  if (incident?.state === "READY_FOR_REVIEW") return { className: "warn", text: "review required" };
  if (connection.state === "DEGRADED" || incident?.error) return { className: "critical", text: "degraded" };
  if (incident?.running) return { className: "info", text: "investigating" };
  return { className: "ok", text: "healthy" };
}

function flatten(direction: string, rules: Rule[], fromPrefix = "", toPrefix = ""): MappingRow[] {
  return rules.flatMap((rule) => {
    const source = fromPrefix + (rule.from || "");
    const destination = toPrefix + (rule.to || "");
    if (rule.op === "array_map") {
      return flatten(direction, rule.fields || [], `${source}[]/`, `${destination}[]/`);
    }
    const cases = rule.op === "enum_map"
      ? Object.entries(rule.values || {}).map(([from, to]) => `${from} → ${to}`).join(", ")
      : rule.op === "constant" ? JSON.stringify(rule.value) : "";
    return [{
      direction,
      from: source || "—",
      op: rule.op,
      to: destination || "—",
      note: cases,
      review: Boolean(rule.requiresHumanApproval),
    }];
  });
}

function StateBadge({ state, tone = "neutral" }: { state?: string; tone?: string }) {
  return <span className={`ops-badge ${tone}`}><span aria-hidden="true" />{label(state)}</span>;
}

function IntegrationRail({ connections, selectedId, incidents, query, onQuery, onSelect }: {
  connections: Connection[];
  selectedId: string | null;
  incidents: Record<string, Incident | null>;
  query: string;
  onQuery: (value: string) => void;
  onSelect: (identifier: string) => void;
}) {
  const visible = connections.filter((connection) => connection.name.toLowerCase().includes(query.toLowerCase()) || connection.id.includes(query));
  return <aside className="ops-rail" aria-label="Integrations">
    <div className="rail-tools"><div className="rail-title"><strong>Integrations</strong><span>{connections.length}</span></div><div><Icon name="grid" size={14} /><label className="sr-only" htmlFor="integration-search">Filter integrations</label><input id="integration-search" type="search" placeholder="Filter integrations" value={query} onChange={(event) => onQuery(event.target.value)} /></div></div>
    <ul>{visible.map((connection) => {
      const status = severity(connection, incidents[connection.id]);
      return <li key={connection.id}><button type="button" className={selectedId === connection.id ? "selected" : ""} onClick={() => onSelect(connection.id)} aria-current={selectedId === connection.id ? "page" : undefined}>
        <span className={`rail-severity ${status.className}`} aria-hidden="true" />
        <span className="rail-name"><strong>{connection.name}</strong><small>{connection.sourcePath || connection.id}</small></span>
        <span className={`rail-state ${status.className}`}>{status.text}</span>
      </button></li>;
    })}</ul>
    {!visible.length ? <p className="rail-empty">No matching integrations.</p> : null}
  </aside>;
}

function IncidentSummary({ incident, connection, sentryUrl }: { incident: Incident; connection: Connection; sentryUrl?: string | null }) {
  const status = severity(connection, incident);
  return <section className={`incident-strip ${status.className}`} aria-labelledby="active-incident-heading">
    <div className="incident-severity"><span aria-hidden="true" /><strong>{status.text}</strong></div>
    <div className="incident-copy"><div><h2 id="active-incident-heading">{incident.kind ? label(incident.kind) : "API contract incident"}</h2><code>{incident.id}</code></div><p>{incident.error || incident.message || incident.lifecycle?.message || "The registered client contract no longer matches the upstream response."}</p></div>
    <dl><div><dt>Occurrences</dt><dd>{incident.occurrences ?? 0}</dd></div><div><dt>Upstream</dt><dd>{incident.upstreamStatus ?? "—"}</dd></div><div><dt>Last seen</dt><dd>{time(incident.lastSeen || incident.updatedAt)}</dd></div></dl>
    {sentryUrl ? <a href={sentryUrl} target="_blank" rel="noreferrer">Open in Sentry <Icon name="external" size={13} /></a> : null}
  </section>;
}

function RepairLanes({ incident, connection, github }: { incident: Incident; connection: Connection; github: StatusResponse["github"] }) {
  const life = incident.lifecycle;
  const bridgeActive = connection.route.mode === "BRIDGE";
  const runtimeState = life?.mitigation?.status || (bridgeActive ? "ACTIVE" : life?.phase === "AWAITING_RUNTIME_REVIEW" ? "REVIEW_REQUIRED" : ["APPROVING", "REVISING", "REVIEW_ACTION_FAILED"].includes(life?.phase || "") ? life?.phase : incident.state === "READY_FOR_REVIEW" ? "REVIEW_REQUIRED" : "PENDING");
  const permanentState = life?.permanent?.status || incident.publication?.status || (incident.candidate ? "PREPARING" : "PENDING");
  return <section className="repair-lanes" aria-label="Repair paths">
    <article className="repair-lane runtime-lane"><header><span>B</span><div><h2>Runtime mitigation</h2><p>Temporary compatibility bridge</p></div><StateBadge state={runtimeState} tone={bridgeActive ? "warning" : "neutral"} /></header><dl>
      <div><dt>Route mode</dt><dd>{connection.route.mode}</dd></div>
      <div><dt>Build ID</dt><dd><code>{life?.mitigation?.buildId || connection.route.buildId || "—"}</code></dd></div>
      <div><dt>Artifact SHA-256</dt><dd><code>{life?.mitigation?.artifactHash || incident.candidate?.artifactHash || "—"}</code></dd></div>
    </dl>{life?.mitigation?.message ? <p className="lane-note">{life.mitigation.message}</p> : null}</article>
    <article className="repair-lane permanent-lane"><header><span>A</span><div><h2>Permanent repair</h2><p>Application client change</p></div><StateBadge state={permanentState} tone={incident.publication?.url ? "success" : "neutral"} /></header><dl>
      <div><dt>Patch SHA-256</dt><dd><code>{life?.permanent?.patchHash || incident.publication?.patchHash || "—"}</code></dd></div>
      <div><dt>Pull request</dt><dd>{incident.publication?.url ? <a href={incident.publication.url} target="_blank" rel="noreferrer">#{incident.publication.number || "open"} <Icon name="external" size={12} /></a> : github.configured ? "Pending" : "GitHub not configured"}</dd></div>
      <div><dt>Merged commit</dt><dd><code>{life?.merge?.sha || "—"}</code></dd></div>
    </dl>{incident.publicationError ? <p className="lane-error" role="alert">{incident.publicationError}</p> : null}</article>
  </section>;
}

function ReviewPanel({ incident, onReviewed }: { incident: Incident; onReviewed: () => Promise<void> }) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState("");
  const candidate = incident.candidate;
  const phase = incident.lifecycle?.phase;
  const review = incident.lifecycle?.review;
  const currentReview = review && candidate && review.candidateHash === candidate.artifactHash ? review : null;
  const active = incident.state === "READY_FOR_REVIEW" && candidate && !["REVISING", "APPROVING"].includes(phase || "") && currentReview?.decision !== "approve";

  async function submit(decision: "approve" | "reject") {
    if (!candidate) return;
    if (decision === "reject" && comment.trim().length < 10) {
      setError("Describe the correction required before requesting changes.");
      return;
    }
    setBusy(decision); setError("");
    try {
      const response = await fetch(`/api/bridge/incidents/${incident.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, comment: comment.trim(), candidateHash: candidate.artifactHash }),
      });
      const value = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(value.title || "Review request failed");
      setComment("");
      await onReviewed();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(null); }
  }

  return <section className="ops-panel review-panel" aria-labelledby="review-heading">
    <header><div><h2 id="review-heading">Operator review</h2><p>Approve the exact verified candidate, or send a correction back to the model</p></div>{candidate ? <code>{candidate.artifactHash.slice(0, 12)}</code> : <span>No candidate</span>}</header>
    {review ? <div className={`review-decision ${review.decision || "pending"}`} role="status">
      <strong>{review.decision === "approve" ? "Approved" : review.decision === "reject" ? "Changes requested" : "Review recorded"}</strong>
      {review.comment ? <p>{review.comment}</p> : null}
      <small>{time(review.at)}{review.candidateHash && candidate && review.candidateHash !== candidate.artifactHash ? " · applies to an earlier candidate" : ""}</small>
    </div> : null}
    {active ? <div className="review-form">
      <label htmlFor="review-comment">Review comment</label>
      <textarea id="review-comment" rows={3} maxLength={1000} value={comment} onChange={(event) => setComment(event.target.value)} placeholder="What should change? Rejections send this comment back to Gemma for a new mapping. Approvals may leave it blank." />
      {error ? <p className="review-error" role="alert">{error}</p> : null}
      <div className="review-actions">
        <button type="button" className="approve" disabled={Boolean(busy)} onClick={() => void submit("approve")}>{busy === "approve" ? "Activating…" : "Approve & activate bridge"}</button>
        <button type="button" className="reject" disabled={Boolean(busy)} onClick={() => void submit("reject")}>{busy === "reject" ? "Requesting changes…" : "Request changes"}</button>
      </div>
    </div> : <p className="panel-empty">{phase === "REVISING" ? "Gemma is applying the review comment to a new mapping." : phase === "APPROVING" ? "The approved candidate is being built and activated." : candidate ? "Approval is recorded for this candidate." : "A verified candidate is required before review."}</p>}
  </section>;
}

function Activity({ events }: { events: AuditEvent[] }) {
  return <section className="ops-panel activity-panel" aria-labelledby="activity-heading"><header><div><h2 id="activity-heading">Incident activity</h2><p>Recorded state transitions</p></div><span>{events.length} events</span></header>
    {events.length ? <ol>{[...events].reverse().map((event) => <li key={event.id}><span className="event-line" aria-hidden="true" /><time dateTime={event.createdAt}>{time(event.createdAt)}</time><div><strong>{label(event.action)}</strong><small>revision {event.revision}</small>{event.metadata?.candidateArtifactHash ? <code>{event.metadata.candidateArtifactHash}</code> : null}</div></li>)}</ol> : <p className="panel-empty">No incident events recorded.</p>}
  </section>;
}

function MappingTable({ migration }: { migration?: Migration }) {
  const rows = migration?.plan ? [
    ...flatten("request", migration.plan.requestMappings),
    ...flatten("response", migration.plan.responseMappings),
  ] : [];
  return <section className="ops-panel mapping-table" aria-labelledby="mapping-heading"><header><div><h2 id="mapping-heading">Field mapping</h2><p>{migration?.plan ? `${migration.plan.sourceOperation.method} ${migration.plan.sourceOperation.path} → ${migration.plan.targetOperation.method} ${migration.plan.targetOperation.path}` : "No verified mapping"}</p></div><span>{rows.length} rules</span></header>
    {rows.length ? <div className="table-scroll"><table><thead><tr><th>Direction</th><th>Source</th><th>Operation</th><th>Destination</th><th>Semantics</th></tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.direction}-${row.from}-${row.to}-${index}`}><td><span className={`direction ${row.direction}`}>{row.direction}</span></td><td><code>{row.from}</code></td><td><code>{row.op}</code></td><td><code>{row.to}</code></td><td>{row.note || (row.review ? "Review recorded" : "Direct")}</td></tr>)}</tbody></table></div> : <p className="panel-empty">Mapping data appears after a contract change is classified and the candidate passes verification.</p>}
  </section>;
}

function Evidence({ incident }: { incident: Incident }) {
  const life = incident.lifecycle;
  const candidate = incident.candidate;
  return <section className="ops-panel evidence" aria-labelledby="evidence-heading"><header><div><h2 id="evidence-heading">Verification evidence</h2><p>Exact values used by the repair workflow</p></div>{candidate ? <a href={`/api/bridge/incidents/${incident.id}/patch`}><Icon name="download" size={13} />Patch</a> : null}</header><dl>
    <div><dt>Shared mapping hash</dt><dd><code>{life?.sharedMappingHash || candidate?.inputHash || "—"}</code></dd></div>
    <div><dt>Candidate artifact hash</dt><dd><code>{candidate?.artifactHash || "—"}</code></dd></div>
    <div><dt>Candidate verification</dt><dd>{candidate ? `${candidate.tests} passed · ${candidate.verificationMode}` : "—"}</dd></div>
    <div><dt>Deployment SHA</dt><dd><code>{life?.deployment?.sha || "—"}</code></dd></div>
    <div><dt>Deployment environment</dt><dd>{life?.deployment?.environment || "—"}</dd></div>
    <div><dt>Deployment status</dt><dd>{life?.deployment?.state || "—"}</dd></div>
    <div><dt>Bypass verification</dt><dd>{life?.verification?.passed ? `${life.verification.samples?.length || 0} samples passed` : "—"}</dd></div>
  </dl></section>;
}

function NoIncident({ connection }: { connection: Connection }) {
  return <section className="no-incident"><header><span className="ok" aria-hidden="true" /><div><h2>No active incident</h2><p>The integration is being monitored. Model inference does not run on normal traffic.</p></div></header><dl><div><dt>Route</dt><dd>{connection.route.mode}</dd></div><div><dt>Successful requests</dt><dd>{connection.successCount}</dd></div><div><dt>Failures</dt><dd>{connection.failureCount}</dd></div><div><dt>Fixtures</dt><dd>{connection.fixtureCount}</dd></div></dl></section>;
}

export function RepairConsole() {
  const [authorized, setAuthorized] = useState<boolean | null>(null);
  const [token, setToken] = useState("");
  const [connections, setConnections] = useState<Connection[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<StatusResponse | null>(null);
  const [incidents, setIncidents] = useState<Record<string, Incident | null>>({});
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selectedId;

  const loadConnections = useCallback(async () => {
    const value = await getJson<ConnectionsResponse>("connections");
    setConnections(value.connections);
    setSelectedId((current) => current && value.connections.some((item) => item.id === current) ? current : value.connections[0]?.id || null);
  }, []);

  const refresh = useCallback(async (identifier: string) => {
    const value = await getJson<StatusResponse>(`connections/${identifier}/status`);
    if (selectedRef.current !== identifier) return;
    setSnapshot(value);
    setConnections((current) => current.map((item) => item.id === identifier ? value.connection : item));
    setIncidents((current) => ({ ...current, [identifier]: value.incident }));
    setUpdatedAt(new Date());
    setError("");
    if (value.incident) {
      const audit = await getJson<AuditEvent[]>(`incidents/${value.incident.id}/audit`);
      if (selectedRef.current === identifier) setEvents(audit);
    } else {
      setEvents([]);
    }
  }, []);

  useEffect(() => {
    fetch("/api/session", { cache: "no-store" }).then((response) => response.json()).then((value) => setAuthorized(Boolean(value.authenticated))).catch(() => setAuthorized(false));
  }, []);

  useEffect(() => {
    if (authorized) loadConnections().catch((reason: Error) => setError(reason.message));
  }, [authorized, loadConnections]);

  useEffect(() => {
    if (!authorized || !selectedId) { setSnapshot(null); setEvents([]); return; }
    let active = true;
    let running = false;
    const update = async () => {
      if (!active || running || document.visibilityState === "hidden") return;
      running = true;
      try { await refresh(selectedId); } catch (reason) { if (active) setError((reason as Error).message); }
      finally { running = false; }
    };
    void update();
    const interval = window.setInterval(update, 5000);
    const visible = () => { if (document.visibilityState === "visible") void update(); };
    document.addEventListener("visibilitychange", visible);
    return () => { active = false; window.clearInterval(interval); document.removeEventListener("visibilitychange", visible); };
  }, [authorized, refresh, selectedId]);

  async function signIn(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const response = await fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
      const value = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(value.title || "Invalid operator token");
      setToken(""); setAuthorized(true);
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  async function signOut() {
    await fetch("/api/session", { method: "DELETE" });
    setAuthorized(false); setConnections([]); setSnapshot(null);
  }

  const selected = snapshot?.connection || connections.find((connection) => connection.id === selectedId) || null;
  const incident = snapshot?.incident || null;
  const environment = incident?.lifecycle?.deployment?.environment || selected?.application?.environment || "not configured";
  const updated = useMemo(() => updatedAt ? updatedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "not refreshed", [updatedAt]);

  if (authorized === null) return <main className="ops-loading" role="status"><Icon name="terminal" size={18} />Connecting to control plane…</main>;

  if (!authorized) return <main className="ops-login"><form onSubmit={signIn}><header><span><Icon name="bridge" size={20} /></span><div><strong>ContractBridge</strong><small>Operator console</small></div></header><h1>Authenticate</h1><p>Enter the operator token configured for this control plane.</p>{error ? <div className="login-error" role="alert">{error}</div> : null}<label htmlFor="operator-token">Operator token</label><input id="operator-token" type="password" autoComplete="current-password" required value={token} onChange={(event) => setToken(event.target.value)} /><button type="submit" disabled={busy}>{busy ? "Authenticating…" : "Continue"}</button></form></main>;

  return <div className="ops-shell">
    <header className="ops-topbar"><a href="/" className="ops-brand"><span><Icon name="bridge" size={18} /></span><strong>ContractBridge</strong></a><div className="ops-context"><span><b>Environment</b>{environment}</span><span><b>Last refresh</b>{updated}</span><button type="button" disabled={!selectedId || busy} onClick={async () => { if (!selectedId) return; setBusy(true); try { await refresh(selectedId); } catch (reason) { setError((reason as Error).message); } finally { setBusy(false); } }}><Icon name="clock" size={14} />{busy ? "Refreshing" : "Refresh"}</button><button type="button" className="sign-out" onClick={signOut}>Sign out</button></div></header>
    <div className="ops-layout"><IntegrationRail connections={connections} selectedId={selectedId} incidents={incidents} query={query} onQuery={setQuery} onSelect={(identifier) => { selectedRef.current = identifier; setSnapshot(null); setEvents([]); setSelectedId(identifier); }} />
      <main className="ops-main">{error ? <div className="ops-error" role="alert"><span>{error}</span><button type="button" onClick={() => setError("")} aria-label="Dismiss error"><Icon name="close" size={15} /></button></div> : null}
        {!connections.length ? <section className="setup-empty"><h1>No integrations registered</h1><p>Register an allowlisted upstream and its baseline contract, then obtain the connection-scoped gateway URL.</p><ol><li>Set <code>UPSTREAM_ALLOWLIST</code> for the provider origin.</li><li>Call <code>POST /api/v1/connections/discover</code> with the integration name, upstream URL and OpenAPI path.</li><li>Call <code>POST /api/v1/connections/&lt;id&gt;/access</code> and configure the returned scoped gateway in the client.</li></ol></section> : !selected || !snapshot ? <div className="ops-loading" role="status">Loading integration…</div> : <>
          <section className="integration-bar"><div><div className="title-row"><h1>{selected.name}</h1><StateBadge state={severity(selected, incident).text} tone={severity(selected, incident).className} /></div><div className="endpoint"><span>POST</span><code>{selected.sourcePath || "—"}</code><span className="route-arrow">via</span><StateBadge state={selected.route.mode} tone={selected.route.mode === "BRIDGE" ? "warning" : "success"} /></div></div><dl><div><dt>Connection ID</dt><dd><code>{selected.id}</code></dd></div><div><dt>Success / failure</dt><dd>{selected.successCount} / {selected.failureCount}</dd></div><div><dt>Sentry project</dt><dd>{selected.sentryProject || "—"}</dd></div><div><dt>Repository</dt><dd>{selected.repository || snapshot.github.repository || "—"}</dd></div></dl></section>
          {incident ? <><IncidentSummary incident={incident} connection={selected} sentryUrl={snapshot.sentryUrl} /><RepairLanes incident={incident} connection={selected} github={snapshot.github} />{incident.candidate ? <ReviewPanel incident={incident} onReviewed={async () => { if (selectedId) await refresh(selectedId); }} /> : null}<div className="ops-columns"><Activity events={events} /><Evidence incident={incident} /></div><MappingTable migration={incident.migration} /></> : <NoIncident connection={selected} />}
        </>}
      </main>
    </div>
  </div>;
}
