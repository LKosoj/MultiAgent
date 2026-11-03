import { describe, expect, it } from "vitest";

import { createSingleFlight } from "../singleFlight";

describe("createSingleFlight", () => {
  it("второй вызов во время первого отбрасывается", async () => {
    const flight = createSingleFlight();
    let release: () => void = () => undefined;
    const first = flight.run(() => new Promise<string>((resolve) => (release = () => resolve("a"))));
    expect(flight.isBusy()).toBe(true);
    expect(await flight.run(async () => "b")).toEqual({ ran: false });
    release();
    expect(await first).toEqual({ ran: true, value: "a" });
    expect(flight.isBusy()).toBe(false);
  });

  it("после ошибки замок снимается", async () => {
    const flight = createSingleFlight();
    await expect(flight.run(async () => Promise.reject(new Error("x")))).rejects.toThrow("x");
    expect(await flight.run(async () => 1)).toEqual({ ran: true, value: 1 });
  });
});
