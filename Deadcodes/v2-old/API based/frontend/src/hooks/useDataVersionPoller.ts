import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

export function useDataVersionPoller(intervalMs = 60_000) {
  const qc = useQueryClient();
  const last = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const { data } = await api.get<{ data_version: number }>("/admin/data-version");
        if (!alive) return;
        if (last.current === null) {
          last.current = data.data_version;
          return;
        }
        if (data.data_version !== last.current) {
          last.current = data.data_version;
          qc.invalidateQueries();
        }
      } catch {
        /* not admin or offline; ignore */
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => { alive = false; clearInterval(id); };
  }, [qc, intervalMs]);
}