import { Navigate, Route, Routes } from "react-router-dom";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Clients from "./pages/Clients";
import ClientDetail from "./pages/ClientDetail";
import CapitalGains from "./pages/CapitalGains";
import Reports from "./pages/Reports";
import Uploads from "./pages/Uploads";
import Admin from "./pages/Admin";
import Layout from "./components/Layout";
import { useDataVersionPoller } from "./hooks/useDataVersionPoller";

function Private({ children }: { children: React.ReactNode }) {
    const token = localStorage.getItem("token");
    return token ? <>{children}</> : <Navigate to="/login" replace />;
}

export default function App() {
    useDataVersionPoller();
    return (
        <Routes>
            <Route path="/login" element={<Login />} />
            <Route
                path="/*"
                element={
                    <Private>
                        <Layout>
                            <Routes>
                                <Route path="/" element={<Dashboard />} />
                                <Route path="/clients" element={<Clients />} />
                                <Route path="/clients/:code" element={<ClientDetail />} />
                                <Route path="/capital-gains" element={<CapitalGains />} />
                                <Route path="/reports" element={<Reports />} />
                                <Route path="/uploads" element={<Uploads />} />
                                <Route path="/admin" element={<Admin />} />
                            </Routes>
                        </Layout>
                    </Private>
                }
            />
        </Routes>
    );
}