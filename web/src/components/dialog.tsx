// Promise-based modal dialogs that replace the browser's native
// alert()/confirm()/prompt(). Mount <DialogHost/> once at the app root, then
// call notify()/confirmDialog()/promptDialog()/formDialog() from anywhere.

import { useEffect, useRef, useState } from "react";
import { Icon, IconName } from "./Icon";

type Tone = "info" | "danger" | "ok" | "warn";

interface NotifyOpts { title?: string; message: string; tone?: Tone; okLabel?: string; }
interface ConfirmOpts {
  title?: string; message: string; tone?: Tone;
  confirmLabel?: string; cancelLabel?: string;
}
interface PromptOpts {
  title?: string; message?: string; label?: string;
  defaultValue?: string; placeholder?: string; password?: boolean;
  confirmLabel?: string;
}
export interface FormField {
  name: string; label: string; defaultValue?: string;
  placeholder?: string; password?: boolean; required?: boolean;
  type?: "textarea";
  options?: { label: string; value: string }[];
  // Renders a small heading above this field when it differs from the prior field's section.
  section?: string;
  hint?: string;
}
interface FormOpts { title?: string; message?: string; description?: string; fields: FormField[]; confirmLabel?: string; wide?: boolean; }

// Reusable informational steps dialog — e.g. post-connect setup like installing a
// provider app. Optionally shows a primary button that opens a link in a new tab.
interface StepsOpts {
  title?: string; message?: string; steps: string[];
  linkUrl?: string; linkLabel?: string; confirmLabel?: string;
}

type Kind = "notify" | "confirm" | "prompt" | "form" | "steps";
interface ActiveDialog {
  kind: Kind;
  opts: NotifyOpts & ConfirmOpts & PromptOpts & FormOpts & StepsOpts;
  resolve: (value: unknown) => void;
}

let mount: ((d: ActiveDialog) => void) | null = null;

function open(kind: Kind, opts: ActiveDialog["opts"]): Promise<unknown> {
  return new Promise((resolve) => {
    if (!mount) {
      // Host not mounted — fail safe rather than hang.
      resolve(kind === "confirm" ? false : null);
      return;
    }
    mount({ kind, opts, resolve });
  });
}

export const notify = (o: NotifyOpts): Promise<void> =>
  open("notify", o as ActiveDialog["opts"]) as Promise<void>;
export const confirmDialog = (o: ConfirmOpts): Promise<boolean> =>
  open("confirm", o as ActiveDialog["opts"]) as Promise<boolean>;
export const promptDialog = (o: PromptOpts): Promise<string | null> =>
  open("prompt", o as ActiveDialog["opts"]) as Promise<string | null>;
export const formDialog = (o: FormOpts): Promise<Record<string, string> | null> =>
  open("form", o as ActiveDialog["opts"]) as Promise<Record<string, string> | null>;
export const stepsDialog = (o: StepsOpts): Promise<void> =>
  open("steps", o as ActiveDialog["opts"]) as Promise<void>;

const TONE_ICON: Record<Tone, IconName> = {
  info: "info", danger: "alert", ok: "check", warn: "alert",
};

export function DialogHost() {
  const [active, setActive] = useState<ActiveDialog | null>(null);
  const [text, setText] = useState("");
  const [form, setForm] = useState<Record<string, string>>({});
  const firstInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    mount = (d) => {
      if (d.kind === "prompt") setText(d.opts.defaultValue ?? "");
      if (d.kind === "form") {
        const init: Record<string, string> = {};
        for (const f of d.opts.fields ?? []) init[f.name] = f.defaultValue ?? (f.options?.[0]?.value ?? "");
        setForm(init);
      }
      setActive(d);
    };
    return () => { mount = null; };
  }, []);

  useEffect(() => {
    if (active) setTimeout(() => firstInput.current?.focus(), 30);
  }, [active]);

  if (!active) return null;
  const { kind, opts } = active;
  const tone: Tone = opts.tone ?? (kind === "confirm" ? "danger" : "info");

  function finish(value: unknown) {
    active!.resolve(value);
    setActive(null);
    setText("");
    setForm({});
  }

  function cancel() {
    finish(kind === "confirm" ? false : kind === "notify" || kind === "steps" ? undefined : null);
  }

  function submit() {
    if (kind === "notify" || kind === "steps") return finish(undefined);
    if (kind === "confirm") return finish(true);
    if (kind === "prompt") return finish(text);
    if (kind === "form") {
      const missing = (opts.fields ?? []).find((f) => f.required && !form[f.name]?.trim());
      if (missing) { firstInput.current?.focus(); return; }
      return finish(form);
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") cancel();
    if (e.key === "Enter" && kind !== "form") { e.preventDefault(); submit(); }
  }

  function renderFormField(f: FormField, autofocus: boolean) {
    return (
      <div key={f.name} className="stack" style={{ gap: 6 }}>
        <label className="stack" style={{ gap: 6 }}>
          <span className="faint" style={{ fontSize: 11.5 }}>
            {f.label}{f.required && <span style={{ color: "var(--danger)" }}> *</span>}
          </span>
          {f.options ? (
            <select
              className="input"
              value={form[f.name] ?? ""}
              onChange={(e) => setForm((cur) => ({ ...cur, [f.name]: e.target.value }))}
            >
              {f.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          ) : f.type === "textarea" ? (
            <textarea
              className="input"
              placeholder={f.placeholder}
              value={form[f.name] ?? ""}
              onChange={(e) => setForm((cur) => ({ ...cur, [f.name]: e.target.value }))}
              style={{ minHeight: 120, fontFamily: "ui-monospace, monospace", fontSize: 12.5 }}
            />
          ) : (
            <input
              ref={autofocus ? firstInput : undefined}
              className="input"
              type={f.password ? "password" : "text"}
              placeholder={f.placeholder}
              value={form[f.name] ?? ""}
              onChange={(e) => setForm((cur) => ({ ...cur, [f.name]: e.target.value }))}
            />
          )}
        </label>
        {f.hint && <span className="faint" style={{ fontSize: 11 }}>{f.hint}</span>}
      </div>
    );
  }

  // Two-column layout (opts.wide): sectioned fields (e.g. Feature flags) get their
  // own column so the main form stays compact.
  const formFields = opts.fields ?? [];
  const sectioned = formFields.filter((f) => f.section);
  const mainFields = formFields.filter((f) => !f.section);
  const twoCol = kind === "form" && !!opts.wide && sectioned.length > 0;
  const sectionName = sectioned[0]?.section;

  return (
    <div className="modal-backdrop" onClick={cancel}>
      <div className={`modal-panel dialog-panel${twoCol ? " dialog-panel-wide" : ""}`}
           onClick={(e) => e.stopPropagation()} onKeyDown={onKeyDown}>
        <div className="dialog-head">
          <span className={`dialog-icon ${tone}`}><Icon name={TONE_ICON[tone]} size={17} /></span>
          <div className="dialog-title">{opts.title ?? defaultTitle(kind, tone)}</div>
        </div>
        <div className="modal-body dialog-body">
          {opts.message && <div className="dialog-message">{opts.message}</div>}
          {kind === "prompt" && (
            <label className="stack" style={{ gap: 6, marginTop: opts.message ? 12 : 0 }}>
              {opts.label && <span className="faint" style={{ fontSize: 11.5 }}>{opts.label}</span>}
              <input
                ref={firstInput}
                className="input"
                type={opts.password ? "password" : "text"}
                placeholder={opts.placeholder}
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
            </label>
          )}
          {kind === "form" && twoCol && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 22, marginTop: opts.message ? 12 : 0 }}>
              <div className="stack" style={{ gap: 12 }}>
                {mainFields.map((f, i) => renderFormField(f, i === 0))}
              </div>
              <div className="stack" style={{ gap: 12 }}>
                <div className="nav-section" style={{ padding: 0 }}>{sectionName}</div>
                {sectioned.map((f) => renderFormField(f, false))}
              </div>
            </div>
          )}
          {kind === "form" && !twoCol && (
            <div className="stack" style={{ gap: 12, marginTop: opts.message ? 12 : 0 }}>
              {formFields.map((f, i) => (
                <div key={f.name} className="stack" style={{ gap: 6 }}>
                  {f.section && formFields[i - 1]?.section !== f.section && (
                    <div className="nav-section" style={{ padding: "8px 0 0" }}>{f.section}</div>
                  )}
                  {renderFormField(f, i === 0)}
                </div>
              ))}
            </div>
          )}
          {kind === "steps" && (
            <div className="stack" style={{ gap: 12, marginTop: opts.message ? 12 : 0 }}>
              <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.7 }}>
                {(opts.steps ?? []).map((s, i) => <li key={i}>{s}</li>)}
              </ol>
              {opts.linkUrl && (
                <button className="btn primary sm" style={{ alignSelf: "flex-start" }}
                        onClick={() => window.open(opts.linkUrl, "_blank", "noopener,noreferrer")}>
                  <Icon name="link" size={14} /> {opts.linkLabel ?? "Open"}
                </button>
              )}
            </div>
          )}
        </div>
        <div className="modal-foot dialog-foot">
          {kind !== "notify" && kind !== "steps" && (
            <button className="btn ghost" onClick={cancel}>{opts.cancelLabel ?? "Cancel"}</button>
          )}
          <button className={`btn ${tone === "danger" ? "danger" : "primary"}`} onClick={submit}>
            {opts.confirmLabel ?? opts.okLabel ?? (kind === "notify" ? "OK" : kind === "confirm" ? "Confirm" : kind === "steps" ? "Done" : "Save")}
          </button>
        </div>
      </div>
    </div>
  );
}

function defaultTitle(kind: Kind, tone: Tone): string {
  if (kind === "confirm") return "Please confirm";
  if (tone === "danger") return "Something went wrong";
  return "Notice";
}
