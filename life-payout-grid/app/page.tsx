import { loadGridData } from "@/lib/grid-data";
import { Dashboard } from "@/components/dashboard";

export const dynamic = "force-dynamic";

export default function Home() {
  const data = loadGridData();
  return <Dashboard data={data} />;
}
