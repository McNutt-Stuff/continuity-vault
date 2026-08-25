import { Icon } from "./Icon";

// Differentiates the two collection tracks: the fast recent/scheduled "Sync" vs
// the paced deep-history "Backfill" crawl.
export function JobKindBadge({ kind, size = 11 }: { kind?: string; size?: number }) {
  const backfill = kind === "backfill";
  return (
    <span className={`job-kind ${backfill ? "backfill" : "sync"}`}
          title={backfill
            ? "Deep-history backfill — a paced background crawl of full history"
            : "Recent / scheduled sync"}>
      <Icon name={backfill ? "clock" : "repeat"} size={size} />
      {backfill ? "Backfill" : "Sync"}
    </span>
  );
}
