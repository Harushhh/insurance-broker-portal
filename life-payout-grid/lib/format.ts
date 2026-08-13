export function formatPayoutValue(value: number | string | null): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    const pct = value * 100;
    const rounded = Math.round(pct * 100) / 100;
    return `${rounded % 1 === 0 ? rounded.toFixed(1) : rounded}%`;
  }
  return value;
}

export function isNumericPayout(value: number | string | null): value is number {
  return typeof value === "number";
}

export function formatDate(iso: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString("en-IN", {
      day: "numeric",
      month: "short",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}
