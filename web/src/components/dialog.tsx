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
}
interface FormOpts { title?: string; message?: string; fields: FormField[]; confirmLabel?: string; }

type Kind = "notify" | "confirm" | "prompt" | "form";
interface ActiveDialog {
  kind: Kind;
  opts: NotifyOpts & ConfirmOpts & PromptOpts & FormOpts;
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
        for (const f of d.opts.fields ?? []) init[f.name] = f.defaultValue ?? "";
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
    finish(kind === "confirm" ? false : kind === "notify" ? undefined : null);
  }

  function submit() {
    if (kind === "notify") return finish(undefined);
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

  return (
    <div className="modal-backdrop" onClick={cancel}>
      <div className="modal-panel dialog-panel" onClick={(e) => e.stopPropagation()} onKeyDown={onKeyDown}>
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
          {kind === "form" && (
            <div className="stack" style={{ gap: 12, marginTop: opts.message ? 12 : 0 }}>
              {(opts.fields ?? []).map((f, i) => (
                <label className="stack" key={f.name} style={{ gap: 6 }}>
                  <span className="faint" style={{ fontSize: 11.5 }}>
                    {f.label}{f.required && <span style={{ color: "var(--danger)" }}> *</span>}
                  </span>
                  <input
                    ref={i === 0 ? firstInput : undefined}
                    className="input"
                    type={f.password ? "password" : "text"}
                    placeholder={f.placeholder}
                    value={form[f.name] ?? ""}
                    onChange={(e) => setForm((cur) => ({ ...cur, [f.name]: e.target.value }))}
                  />
                </label>
              ))}
            </div>
          )}
        </div>
        <div className="modal-foot dialog-foot">
          {kind !== "notify" && (
            <button className="btn ghost" onClick={cancel}>{opts.cancelLabel ?? "Cancel"}</button>
          )}
          <button className={`btn ${tone === "danger" ? "danger" : "primary"}`} onClick={submit}>
            {opts.confirmLabel ?? opts.okLabel ?? (kind === "notify" ? "OK" : kind === "confirm" ? "Confirm" : "Save")}
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
