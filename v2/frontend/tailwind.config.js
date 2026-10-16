/** @type {import('tailwindcss').Config} */
export default {
    darkMode: "class",
    content: ["./index.html", "./src/**/*.{ts,tsx}"],
    theme: {
        extend: {
            colors: {
                bg: "rgb(var(--bg) / <alpha-value>)",
                card: "rgb(var(--card) / <alpha-value>)",
                accent: "rgb(var(--accent) / <alpha-value>)",
                text: "rgb(var(--text) / <alpha-value>)",
                muted: "rgb(var(--muted) / <alpha-value>)",
                border: "rgb(var(--border) / <alpha-value>)",
            },
        },
    },
    plugins: [],
};