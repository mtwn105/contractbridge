"use client";
import * as Sentry from "@sentry/nextjs";
import { useEffect } from "react";

export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => { Sentry.captureException(error); }, [error]);
  return <html lang="en"><body><main style={{ maxWidth: 560, margin: "80px auto", padding: 24, fontFamily: "system-ui" }}><h1>ContractBridge could not open this view.</h1><p>Try loading the view again. Error details are reported when monitoring is configured.</p><button onClick={reset}>Try again</button></main></body></html>;
}
