import { redirect } from "next/navigation";
import { loadGridData } from "@/lib/grid-data";
import { Dashboard } from "@/components/dashboard";
import { isAuthenticated, portalRedirectUrl } from "@/lib/admin-auth";

export const dynamic = "force-dynamic";

export default async function Home() {
  if (!(await isAuthenticated())) {
    const url = portalRedirectUrl("viewer");
    if (!url) {
      return (
        <div className="flex min-h-screen items-center justify-center px-4 text-center text-sm text-muted-foreground">
          INSURANCE_PORTAL_URL is not configured on this app -- can&apos;t redirect back to the portal.
        </div>
      );
    }
    redirect(url);
  }

  const data = loadGridData();
  return <Dashboard data={data} />;
}
