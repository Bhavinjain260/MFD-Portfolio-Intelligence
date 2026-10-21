import { Link, useLocation, useNavigate } from "react-router-dom";

const NAV = [
    { to: "/", label: "Dashboard" },
    { to: "/clients", label: "Clients" },
    { to: "/capital-gains", label: "Capital Gains" },
    { to: "/reports", label: "Reports" },
    { to: "/uploads", label: "Uploads" },
    { to: "/admin", label: "Admin" },
];

export default function Layout({ children }: { children: React.ReactNode }) {
    const { pathname } = useLocation();
    const nav = useNavigate();
    return (
        <div className="flex min-h-screen bg-bg">
            <aside className="w-56 border-r border-border p-4">
                <div className="text-lg font-semibold mb-6">📊 MFD</div>
                <nav className="space-y-1">
                    {NAV.map((n) => (
                        <Link
                            key={n.to}
                            to={n.to}
                            className={`block px-3 py-2 rounded-md text-sm ${pathname === n.to
                                    ? "bg-card text-accent"
                                    : "text-muted hover:bg-card hover:text-text"
                                }`}
                        >
                            {n.label}
                        </Link>
                    ))}
                </nav>
                <button
                    onClick={() => {
                        localStorage.removeItem("token");
                        nav("/login");
                    }}
                    className="mt-8 text-xs text-muted hover:text-text"
                >
                    Sign out
                </button>
            </aside>
            <main className="flex-1 p-6 overflow-auto">{children}</main>
        </div>
    );
}