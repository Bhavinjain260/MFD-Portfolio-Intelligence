import { useState } from "react";
import { useNavigate } from "react-router-dom";
import axios from "axios";

export default function Login() {
    const [u, setU] = useState("admin");
    const [p, setP] = useState("");
    const [err, setErr] = useState("");
    const nav = useNavigate();

    const onSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        try {
            const form = new URLSearchParams({ username: u, password: p });
            const { data } = await axios.post(
                `${import.meta.env.VITE_API_BASE ?? ""}/api/v1/auth/login`,
                form,
            );
            localStorage.setItem("token", data.access_token);
            nav("/");
        } catch {
            setErr("Invalid credentials");
        }
    };

    return (
        <div className="min-h-screen flex items-center justify-center">
            <form onSubmit={onSubmit} className="w-80 bg-card p-6 rounded-lg border border-border">
                <h1 className="text-xl mb-4 text-text">Sign in</h1>
                <label className="text-xs text-muted">Username</label>
                <input
                    value={u} onChange={(e) => setU(e.target.value)}
                    className="w-full mb-3 bg-bg border border-border rounded px-2 py-1 text-text"
                />
                <label className="text-xs text-muted">Password</label>
                <input
                    type="password" value={p} onChange={(e) => setP(e.target.value)}
                    className="w-full mb-3 bg-bg border border-border rounded px-2 py-1 text-text"
                />
                {err && <div className="text-red-400 text-xs mb-2">{err}</div>}
                <button className="w-full bg-accent text-white rounded py-2 mt-2">Login</button>
            </form>
        </div>
    );
}