import { ReactNode, useEffect, useRef, useState } from "react";
import { Icon, IconName } from "./Icon";

export interface MenuAction {
  label: string;
  icon?: IconName;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
}
export type MenuEntry = MenuAction | "divider";

// A small accessible dropdown menu (click to open, closes on outside click / Esc).
// Use for row action overflow so cards stay uncluttered.
export function Menu({ items, trigger, align = "right", triggerClassName }: {
  items: MenuEntry[];
  trigger?: ReactNode;
  align?: "left" | "right";
  triggerClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="menu-wrap" ref={ref}>
      <button className={triggerClassName ?? "btn sm ghost menu-trigger"} aria-haspopup="menu" aria-expanded={open}
              onClick={() => setOpen((v) => !v)} title="Actions">
        {trigger ?? <span className="menu-dots">⋯</span>}
      </button>
      {open && (
        <div className={`menu-pop ${align}`} role="menu">
          {items.map((it, i) => it === "divider"
            ? <div key={i} className="menu-divider" />
            : (
              <button key={i} role="menuitem" disabled={it.disabled}
                      className={`menu-item${it.danger ? " danger" : ""}`}
                      onClick={() => { setOpen(false); it.onClick(); }}>
                {it.icon && <Icon name={it.icon} size={14} />}
                <span>{it.label}</span>
              </button>
            ))}
        </div>
      )}
    </div>
  );
}
