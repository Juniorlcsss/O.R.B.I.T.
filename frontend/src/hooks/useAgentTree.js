import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../lib/api.js";

export default function useAgentTree() {
  const [tree, setTree] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiFetch("/api/agent_tree");
      setTree(data);
      setError(null);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { tree, error, loading, refresh };
}
