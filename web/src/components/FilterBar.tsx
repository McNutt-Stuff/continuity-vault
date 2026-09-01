import { ReactNode } from "react";
import { Icon } from "./Icon";

export interface FilterSelect {
  value: string;
  onChange: (v: string) => void;
  options: { label: string; value: string }[];
}

// A reusable search + filters bar styled like the unified-search bar. The search
// box uses the same look as Search; filter dropdowns sit alongside it.
export function FilterBar({ query, onQuery, placeholder, filters, right }: {
  query: string;
  onQuery: (v: string) => void;
  placeholder?: string;
  filters?: FilterSelect[];
  right?: ReactNode;
}) {
  return (
    <div className="filter-bar filter-toolbar">
      <div className="search-bar filter-bar-search">
        <Icon name="search" size={16} />
        <input value={query} placeholder={placeholder ?? "Search…"}
               onChange={(e) => onQuery(e.target.value)} />
        {query && (
          <button className="filter-bar-clear" title="Clear" onClick={() => onQuery("")}>×</button>
        )}
      </div>
      {(filters ?? []).map((f, i) => (
        <select key={i} className="input filter-bar-select" value={f.value}
                onChange={(e) => f.onChange(e.target.value)}>
          {f.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      ))}
      {right}
    </div>
  );
}
