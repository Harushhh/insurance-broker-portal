/**
 * Color-by-identity for plan type badges (Term, ULIP, Par, ...). Palette is
 * the validated categorical set from the dataviz skill's reference palette
 * (colorblind-safe, fixed hue order) -- reused here as badge accent colors
 * rather than chart series. "Protection" and "Traditional / Investment"
 * share a neutral slate treatment: they're catch-all buckets, not a
 * distinct plan family brokers scan for the way Term/ULIP/Par are.
 */
const CATEGORY_STYLES: Record<string, string> = {
  Term: "bg-[#2a78d6]/10 text-[#1c5cab] border-[#2a78d6]/25",
  ULIP: "bg-[#1baf7a]/10 text-[#0f7a54] border-[#1baf7a]/25",
  TULIP: "bg-[#4a3aa7]/10 text-[#4a3aa7] border-[#4a3aa7]/25",
  "Non-Par": "bg-[#eda100]/15 text-[#8a5d00] border-[#eda100]/30",
  Par: "bg-[#e87ba4]/10 text-[#b1497b] border-[#e87ba4]/25",
  Pension: "bg-[#008300]/10 text-[#006300] border-[#008300]/25",
  Annuity: "bg-[#e34948]/10 text-[#c22e2d] border-[#e34948]/25",
};

const DEFAULT_STYLE = "bg-stone-500/10 text-stone-600 border-stone-500/25";

export function categoryBadgeClass(category: string | null | undefined): string {
  if (!category) return DEFAULT_STYLE;
  return CATEGORY_STYLES[category] ?? DEFAULT_STYLE;
}
