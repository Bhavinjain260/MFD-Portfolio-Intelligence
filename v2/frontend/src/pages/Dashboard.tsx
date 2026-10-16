import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export default function Dashboard() {
    const snapshot = useQuery({
        queryKey: ["nav-snapshot"],
        queryFn: async () => (await api.get("/nav/snapshot-status")).data,
    });
    const jobs = useQuery({
        queryKey: ["jobs", 10],
        queryFn: async () => (await api.get("/admin/jobs?limit=10")).data,
    });

    return (
        <div>
            <h1 className="text-2xl mb-6">Dashboard</h1>
            <div className="grid grid-cols-3 gap-4">
                <KpiCard label="Latest NAV date" value={snapshot.data?.latest_date ?? "—"} />
                <KpiCard label="Latest NAV size" value={snapshot.data?.latest_bytes
                    ? `${(snapshot.data.latest_bytes / 1024).toFixed(0)} KB` : "—"} />
                <KpiCard label="Today synced" value={snapshot.data?.has_today ? "✅" : "—"} />
            </div>
            <h2 className="text-lg mt-8 mb-3">Recent worker runs</h2>
            <table className="w-full text-sm">
                <thead className="text-muted">
                    <tr><th className="text-left">Task</th><th className="text-left">Trigger</th>
                        <th className="text-left">Started</th><th className="text-left">OK</th>
                        <th className="text-left">Msg</th></tr>
                </thead>
                <tbody>
                    {jobs.data?.map((j: any) => (
                        <tr key={j.id} className="border-t border-border">
                            <td className="py-1">{j.task}</td>
                            <td>{j.trigger}</td>
                            <td>{j.started_at}</td>
                            <td>{j.ok === null ? "…" : j.ok ? "✅" : "❌"}</td>
                            <td className="truncate max-w-xs">{j.msg}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

function KpiCard({ label, value }: { label: string; value: string }) {
    return (
        <div className="bg-card border border-border rounded-lg p-4">
            <div className="text-xs uppercase text-muted">{label}</div>
            <div className="text-xl font-semibold mt-1">{value}</div>
        </div>
    );
}