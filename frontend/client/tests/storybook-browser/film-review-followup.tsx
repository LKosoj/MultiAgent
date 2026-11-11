import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { FilmReview } from "../../src/app/components/sections/storybook/film/FilmReview";

const pending: ((value: unknown) => void)[] = [];
const client = { call: () => new Promise((resolve) => pending.push(resolve)) } as never;
Object.assign(window, { resolveReview: (index: number, value: unknown) => pending[index](value), reviewCalls: () => pending.length });
function App() {
  const [tick, setTick] = useState(0);
  const [series, setSeries] = useState("a");
  const [run, setRun] = useState<"idle" | "running" | "completed">("idle");
  return <><button onClick={() => setTick((value) => value + 1)}>F5</button>
    <button onClick={() => setRun("running")}>Start</button><button onClick={() => setRun("completed")}>Done</button>
    <button onClick={() => setSeries((value) => value === "a" ? "b" : "a")}>Other</button>
    <FilmReview client={client} projectId="p" seriesId={series} refreshTick={tick} runState={run} />
  </>;
}
createRoot(document.getElementById("root")!).render(<App />);
