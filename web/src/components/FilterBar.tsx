import { ReactNode } from "react";
import { Icon } from "./Icon";

export interface FilterSelect {
  label?: string;
  value: string;
  onChange: (v: string) => void;
  options: { label: string; value: string }[];
}

// A reusable search + filters toolbar styled exactly like the unified-search bar:
// a full-width search pill on top, then a row of labeled, compact dropdowns.
export function FilterBar({ query, onQuery, placeholder, filters, right }: {
  query: string;
  onQuery: (v: string) => void;
  placeholder?: string;
  filters?: FilterSelect[];
  right?: ReactNode;
}) {
  const fs = filters ?? [];
  return (
    <div className="filter-toolbar">
      <div className="search-bar">
        <Icon name="search" size={18} />
        <input value={query} placeholder={placeholder ?? "Search…"}
               onChange={(e) => onQuery(e.target.value)} />
        {query && (
          <button className="filter-bar-clear" title="Clear" onClick={() => onQuery("")}>×</button>
        )}
      </div>
      {(fs.length > 0 || right) && (
        <div className="filter-bar">
          {fs.map((f, i) => (
            <label key={i} className="filter-select">
              {f.label && <span>{f.label}</span>}
              <select value={f.value} onChange={(e) => f.onChange(e.target.value)}>
                {f.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
          ))}
          {right}
        </div>
      )}
    </div>
  );
}
