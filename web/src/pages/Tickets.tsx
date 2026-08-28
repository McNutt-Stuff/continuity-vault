import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { notify } from "../components/dialog";

interface TicketMsg {
  id: string;
  author_name: string;
  is_staff: boolean;
  body: string;
  created_at: string | null;
}
interface Ticket {
  id: string;
  ref: string;
  subject: string;
  category: string;
  priority: string;
  status: string;
  last_activity_at: string | null;
  created_at: string | null;
  message_count: number;
  messages?: TicketMsg[];
}
interface Category { key: string; label: string; icon: string }

type Tone = "info" | "ok" | "warn" | "danger";
const STATUS_TONE: Record<string, Tone> = {
  open: "info", pending: "warn", resolved: "ok", closed: "info",
};
const PRIORITY_TONE: Record<string, Tone> = {
  low: "info", normal: "info", high: "warn", urgent: "danger",
};

function when(s: string | null): string {
  if (!s) return "";
  const d = new Date(s);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export default function Tickets() {
  const { id } = useParams();
  return id ? <TicketDetail id={id} /> : <TicketList />;
}

// --------------------------------------------------------------------------- //
// List + new ticket                                                           //
// --------------------------------------------------------------------------- //

function TicketList() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [tickets, setTickets] = useState<Ticket[] | null>(null);
  const [cats, setCats] = useState<Category[]>([]);
  const [priorities, setPriorities] = useState<string[]>(["low", "normal", "high", "urgent"]);
  const [creating, setCreating] = useState(params.get("new") === "1");

  async function load() {
    try {
      const [t, m] = await Promise.all([
        api.get<{ tickets: Ticket[] }>("/support/tickets"),
        api.get<{ categories: Category[]; priorities: string[] }>("/support/meta"),
      ]);
      setTickets(t.tickets);
      setCats(m.categories);
      setPriorities(m.priorities);
    } catch (e: any) {
      notify({ message: e.message || "Could not load tickets", tone: "danger" });
      setTickets([]);
    }
  }
  useEffect(() => { void load(); }, []);

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="spread" style={{ alignItems: "center" }}>
        <div className="muted" style={{ fontSize: 13.5 }}>
          Open a ticket and our team will help. You'll get email updates and can reply here or by email.
        </div>
        {!creating && (
          <button className="btn primary" onClick={() => setCreating(true)}>
            <Icon name="edit" size={14} /> New ticket
          </button>
        )}
      </div>

      {creating && (
        <NewTicket
          cats={cats}
          priorities={priorities}
          onCancel={() => setCreating(false)}
          onCreated={(t) => nav(`/support/tickets/${t.id}`)}
        />
      )}

      {tickets === null ? (
        <Loading label="Loading your tickets…" />
      ) : tickets.length === 0 && !creating ? (
        <Card>
          <div style={{ textAlign: "center", padding: "26px 0" }}>
            <div style={{ opacity: 0.6, marginBottom: 8 }}><Icon name="mail" size={26} /></div>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>No support tickets yet</div>
            <div className="muted" style={{ fontSize: 13 }}>
              When you open a ticket it will appear here so you can track it.
            </div>
          </div>
        </Card>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          {tickets.map((t) => (
            <Card key={t.id} onClick={() => nav(`/support/tickets/${t.id}`)}>
              <div className="spread" style={{ alignItems: "flex-start", gap: 12, cursor: "pointer" }}>
                <div style={{ minWidth: 0 }}>
                  <div className="row" style={{ gap: 8, alignItems: "center" }}>
                    <span className="faint" style={{ fontSize: 12, fontFamily: "ui-monospace, monospace" }}>{t.ref}</span>
                    <span style={{ fontWeight: 600 }}>{t.subject}</span>
                  </div>
                  <div className="faint" style={{ fontSize: 12, marginTop: 3 }}>
                    {cats.find((c) => c.key === t.category)?.label || t.category}
                    {" · "}{t.message_count} message{t.message_count === 1 ? "" : "s"}
                    {" · updated "}{when(t.last_activity_at)}
                  </div>
                </div>
                <div className="row" style={{ gap: 6 }}>
                  <Pill tone={PRIORITY_TONE[t.priority] || "info"}>{t.priority}</Pill>
                  <Pill tone={STATUS_TONE[t.status] || "info"} dot>{t.status}</Pill>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function NewTicket({ cats, priorities, onCancel, onCreated }: {
  cats: Category[]; priorities: string[];
  onCancel: () => void; onCreated: (t: Ticket) => void;
}) {
  const [subject, setSubject] = useState("");
  const [category, setCategory] = useState("technical");
  const [priority, setPriority] = useState("normal");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!subject.trim() || !body.trim()) {
      notify({ message: "Please add a subject and a description.", tone: "danger" });
      return;
    }
    setBusy(true);
    try {
      const t = await api.post<Ticket>("/support/tickets", { subject, category, priority, body });
      onCreated(t);
    } catch (e: any) {
      notify({ message: e.message || "Could not open the ticket", tone: "danger" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>New support ticket</h3>
        <button className="btn sm ghost" onClick={onCancel}>Cancel</button>
      </div>
      <div className="stack" style={{ gap: 12 }}>
        <label className="stack" style={{ gap: 5 }}>
          <span className="faint" style={{ fontSize: 12 }}>Subject</span>
          <input className="input" value={subject} onChange={(e) => setSubject(e.target.value)}
                 placeholder="Briefly, what do you need help with?" />
        </label>
        <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
          <label className="stack flex1" style={{ gap: 5, minWidth: 200 }}>
            <span className="faint" style={{ fontSize: 12 }}>Category</span>
            <select className="input" value={category} onChange={(e) => setCategory(e.target.value)}>
              {cats.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
            </select>
          </label>
          <label className="stack flex1" style={{ gap: 5, minWidth: 160 }}>
            <span className="faint" style={{ fontSize: 12 }}>Priority</span>
            <select className="input" value={priority} onChange={(e) => setPriority(e.target.value)}>
              {priorities.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </label>
        </div>
        <label className="stack" style={{ gap: 5 }}>
          <span className="faint" style={{ fontSize: 12 }}>Describe the issue</span>
          <textarea className="input" rows={6} value={body} onChange={(e) => setBody(e.target.value)}
                    placeholder="What were you doing, what did you expect, and what happened?" />
        </label>
        <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
          <button className="btn primary" disabled={busy} onClick={submit}>
            {busy ? "Opening…" : "Open ticket"}
          </button>
        </div>
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------- //
// Detail                                                                      //
// --------------------------------------------------------------------------- //

function TicketDetail({ id }: { id: string }) {
  const nav = useNavigate();
  const [t, setT] = useState<Ticket | null>(null);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      setT(await api.get<Ticket>(`/support/tickets/${id}`));
    } catch (e: any) {
      notify({ message: e.message || "Ticket not found", tone: "danger" });
      nav("/support/tickets");
    }
  }
  useEffect(() => { void load(); }, [id]);

  async function sendReply() {
    if (!reply.trim()) return;
    setBusy(true);
    try {
      setT(await api.post<Ticket>(`/support/tickets/${id}/reply`, { body: reply }));
      setReply("");
    } catch (e: any) {
      notify({ message: e.message || "Could not send reply", tone: "danger" });
    } finally {
      setBusy(false);
    }
  }

  async function close() {
    setBusy(true);
    try {
      setT(await api.post<Ticket>(`/support/tickets/${id}/close`, {}));
    } catch (e: any) {
      notify({ message: e.message || "Could not close the ticket", tone: "danger" });
    } finally {
      setBusy(false);
    }
  }

  if (!t) return <Loading label="Loading ticket…" />;

  const closed = t.status === "closed";
  return (
    <div className="stack" style={{ gap: 16 }}>
      <button className="btn sm ghost" style={{ alignSelf: "flex-start" }} onClick={() => nav("/support/tickets")}>
        <Icon name="logout" size={13} /> All tickets
      </button>

      <Card>
        <div className="spread" style={{ alignItems: "flex-start", gap: 12 }}>
          <div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <span className="faint" style={{ fontSize: 12.5, fontFamily: "ui-monospace, monospace" }}>{t.ref}</span>
              <Pill tone={PRIORITY_TONE[t.priority] || "info"}>{t.priority}</Pill>
              <Pill tone={STATUS_TONE[t.status] || "info"} dot>{t.status}</Pill>
            </div>
            <h2 style={{ margin: "8px 0 2px" }}>{t.subject}</h2>
            <div className="faint" style={{ fontSize: 12 }}>Opened {when(t.created_at)}</div>
          </div>
          {!closed && (
            <button className="btn sm ghost" disabled={busy} onClick={close}>
              <Icon name="check" size={13} /> Close ticket
            </button>
          )}
        </div>
      </Card>

      <div className="stack" style={{ gap: 10 }}>
        {(t.messages || []).map((m) => (
          <div key={m.id} className={`ticket-msg ${m.is_staff ? "staff" : ""}`}>
            <div className="spread" style={{ marginBottom: 6 }}>
              <span style={{ fontWeight: 600, fontSize: 13 }}>
                {m.is_staff && <Icon name="shield" size={12} />} {m.author_name || (m.is_staff ? "Arkive Support" : "You")}
              </span>
              <span className="faint" style={{ fontSize: 11.5 }}>{when(m.created_at)}</span>
            </div>
            <div style={{ fontSize: 14, lineHeight: 1.55, whiteSpace: "pre-wrap" }}>{m.body}</div>
          </div>
        ))}
      </div>

      <Card>
        {closed ? (
          <div className="muted" style={{ fontSize: 13, textAlign: "center", padding: "8px 0" }}>
            This ticket is closed. Replying will reopen it.
          </div>
        ) : null}
        <div className="stack" style={{ gap: 10 }}>
          <textarea className="input" rows={4} value={reply} onChange={(e) => setReply(e.target.value)}
                    placeholder="Write a reply…" />
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button className="btn primary" disabled={busy || !reply.trim()} onClick={sendReply}>
              {busy ? "Sending…" : closed ? "Reply & reopen" : "Send reply"}
            </button>
          </div>
        </div>
      </Card>
    </div>
  );
}
