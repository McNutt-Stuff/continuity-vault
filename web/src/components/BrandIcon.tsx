// Full-color brand marks for data sources. The monochrome Icon component forces
// fill:none/stroke:currentColor, so brand logos (which need real fills) live here.

export type BrandName = "gmail" | "onepassword" | "microsoft";

// Maps a connector/source type to its brand mark (Outlook/OneDrive share the
// Microsoft logo). Returns null when there is no dedicated brand icon.
export function brandForSource(sourceType: string): BrandName | null {
  switch (sourceType) {
    case "gmail": return "gmail";
    case "onepassword": return "onepassword";
    case "outlook":
    case "onedrive":
    case "microsoft": return "microsoft";
    default: return null;
  }
}

export function BrandIcon({ name, size = 18 }: { name: BrandName | string; size?: number }) {
  const common = { width: size, height: size, "aria-hidden": true };
  switch (name) {
    case "gmail":
      return (
        <svg {...common} viewBox="0 0 48 48">
          <path fill="#4caf50" d="M45 16.2l-5 2.75-5 4.75L35 40h7c1.657 0 3-1.343 3-3V16.2z" />
          <path fill="#1e88e5" d="M3 16.2l3.614 1.71L13 23.7V40H6c-1.657 0-3-1.343-3-3V16.2z" />
          <polygon fill="#e53935" points="35,11.2 24,19.45 13,11.2 12,17 13,23.7 24,31.95 35,23.7 36,17" />
          <path fill="#c62828" d="M3 12.298V16.2l10 7.5V11.2L9.876 8.859A3.878 3.878 0 007.298 8 4.298 4.298 0 003 12.298z" />
          <path fill="#fbc02d" d="M45 12.298V16.2l-10 7.5V11.2l3.124-2.341A3.878 3.878 0 0140.702 8 4.298 4.298 0 0145 12.298z" />
        </svg>
      );
    case "microsoft":
      return (
        <svg {...common} viewBox="0 0 23 23">
          <path fill="#f35325" d="M1 1h10v10H1z" />
          <path fill="#81bc06" d="M12 1h10v10H12z" />
          <path fill="#05a6f0" d="M1 12h10v10H1z" />
          <path fill="#ffba08" d="M12 12h10v10H12z" />
        </svg>
      );
    case "onepassword":
      return (
        <svg {...common} viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="12" fill="#0a2636" />
          <circle cx="12" cy="12" r="7.8" fill="none" stroke="#2e9bff" strokeWidth="1.6" />
          <circle cx="12" cy="10.4" r="2.4" fill="none" stroke="#2e9bff" strokeWidth="1.6" />
          <path stroke="#2e9bff" strokeWidth="1.6" strokeLinecap="round" d="M12 12.5v3.7" />
        </svg>
      );
    default:
      return null;
  }
}
