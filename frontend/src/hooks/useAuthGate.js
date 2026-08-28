import { useEffect, useState } from "react";
import { AUTH_FAILED_EVENT } from "../lib/api.js";


export default function useAuthGate() {
  const [rejected, setRejected] = useState(false);

  useEffect(() => {
    const onRejected = () => setRejected(true);
    window.addEventListener(AUTH_FAILED_EVENT, onRejected);
    return () => window.removeEventListener(AUTH_FAILED_EVENT, onRejected);
  }, []);

  return rejected;
}
