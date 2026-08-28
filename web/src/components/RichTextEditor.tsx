import { useEffect, useRef } from "react";
import { Icon } from "./Icon";

// A lightweight, dependency-free WYSIWYG editor for the docs CMS. Uses the
// browser's native rich-editing on a contentEditable region with a formatting
// toolbar (headings, font family/size, bold/italic/underline, colour, lists,
// quote, link). Emits HTML via onChange.

function exec(cmd: string, value?: string) {
  document.execCommand(cmd, false, value);
}

export function RichTextEditor({ value, onChange, minHeight = 340 }: {
  value: string; onChange: (html: string) => void; minHeight?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  // Seed the editable region once (and if the external value is swapped for a
  // different document). We don't re-sync on every keystroke to preserve the
  // caret — edits flow out via onInput.
  useEffect(() => {
    const el = ref.current;
    if (el && el.innerHTML !== value) el.innerHTML = value || "";
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value === undefined]);

  const emit = () => { if (ref.current) onChange(ref.current.innerHTML); };
  const run = (cmd: string, arg?: string) => { exec(cmd, arg); emit(); ref.current?.focus(); };

  // Buttons use onMouseDown + preventDefault so the selection in the editor
  // isn't lost when the toolbar is clicked.
  const TBtn = ({ cmd, arg, label, title, style }: {
    cmd: string; arg?: string; label: React.ReactNode; title: string; style?: React.CSSProperties;
  }) => (
    <button type="button" className="rte-btn" title={title} style={style}
            onMouseDown={(e) => { e.preventDefault(); run(cmd, arg); }}>
      {label}
    </button>
  );

  function addLink() {
    const url = window.prompt("Link URL (https://… or /support/…)");
    if (url) run("createLink", url);
  }

  return (
    <div className="rte">
      <div className="rte-toolbar">
        <select className="rte-sel" title="Text style" defaultValue=""
                onMouseDown={(e) => e.stopPropagation()}
                onChange={(e) => { run("formatBlock", e.target.value); e.currentTarget.value = ""; }}>
          <option value="" disabled>Style…</option>
          <option value="P">Body text</option>
          <option value="H1">Heading 1</option>
          <option value="H2">Heading 2</option>
          <option value="H3">Heading 3</option>
          <option value="BLOCKQUOTE">Quote</option>
        </select>
        <select className="rte-sel" title="Font" defaultValue=""
                onChange={(e) => { run("fontName", e.target.value); e.currentTarget.value = ""; }}>
          <option value="" disabled>Font…</option>
          <option value="-apple-system, Segoe UI, Roboto, sans-serif">Sans‑serif</option>
          <option value="Georgia, 'Times New Roman', serif">Serif</option>
          <option value="ui-monospace, Menlo, monospace">Monospace</option>
        </select>
        <select className="rte-sel" title="Size" defaultValue=""
                onChange={(e) => { run("fontSize", e.target.value); e.currentTarget.value = ""; }}>
          <option value="" disabled>Size…</option>
          <option value="2">Small</option>
          <option value="3">Normal</option>
          <option value="5">Large</option>
          <option value="6">X‑Large</option>
        </select>
        <span className="rte-div" />
        <TBtn cmd="bold" title="Bold" label={<b>B</b>} />
        <TBtn cmd="italic" title="Italic" label={<i>I</i>} />
        <TBtn cmd="underline" title="Underline" label={<u>U</u>} />
        <label className="rte-btn" title="Text colour" style={{ padding: 0, position: "relative", overflow: "hidden" }}>
          <span style={{ fontWeight: 700 }}>A</span>
          <input type="color" defaultValue="#4f7cff"
                 style={{ position: "absolute", inset: 0, opacity: 0, cursor: "pointer" }}
                 onChange={(e) => run("foreColor", e.target.value)} />
        </label>
        <span className="rte-div" />
        <TBtn cmd="insertUnorderedList" title="Bullet list" label="•" />
        <TBtn cmd="insertOrderedList" title="Numbered list" label="1." />
        <span className="rte-div" />
        <button type="button" className="rte-btn" title="Insert link"
                onMouseDown={(e) => { e.preventDefault(); addLink(); }}>
          <Icon name="link" size={14} />
        </button>
        <TBtn cmd="removeFormat" title="Clear formatting" label={<Icon name="trash" size={14} />} />
      </div>
      <div
        ref={ref}
        className="rte-body"
        contentEditable
        suppressContentEditableWarning
        style={{ minHeight }}
        onInput={emit}
        onBlur={emit}
      />
    </div>
  );
}
