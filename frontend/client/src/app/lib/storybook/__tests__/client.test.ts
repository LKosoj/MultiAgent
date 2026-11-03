import { describe, expect, it, vi } from "vitest";

import { apiBaseFromAgentUrl, createStorybookClient, StudioError } from "../client";
import { mediaToken } from "../core";

const tokenReply = (expiresAt: number, token = "tok") => ({
  status: "success",
  t: token,
  expires_at: expiresAt,
  ttl_seconds: 3600,
});

describe("storybook client", () => {
  it("добавляет префикс storybook. и отдаёт результат", async () => {
    const transport = vi.fn().mockResolvedValue(tokenReply(9_999_999_999));
    const client = createStorybookClient(transport, { apiBase: "http://h" });
    const result = await mediaToken(client, { project_id: "p" });
    expect(result.status).toBe("success");
    expect(transport).toHaveBeenCalledWith("storybook.media_token", { project_id: "p" }, { timeoutMs: 20000 });
  });

  it("status:error превращает в StudioError с кодом", async () => {
    const transport = vi.fn().mockResolvedValue({
      status: "error",
      error: { code: "not_found", message: "Нет проекта", details: { id: 1 } },
    });
    const client = createStorybookClient(transport, { apiBase: "http://h" });
    await expect(client.call("x")).rejects.toMatchObject({ code: "not_found", message: "Нет проекта" });
    await expect(client.call("x")).rejects.toBeInstanceOf(StudioError);
  });

  it("ответ без status - ошибка формата", async () => {
    const client = createStorybookClient(vi.fn().mockResolvedValue({ ok: true }), { apiBase: "http://h" });
    await expect(client.call("x")).rejects.toMatchObject({ code: "invalid_result" });
  });

  it("токен медиа кэшируется, обновляется по часам, параллельные вызовы - один запрос", async () => {
    let clock = 1_000_000;
    const transport = vi.fn().mockImplementation(async () => tokenReply(clock / 1000 + 3600));
    const client = createStorybookClient(transport, { apiBase: "http://h/", now: () => clock });

    await Promise.all([client.ensureMediaToken("p"), client.ensureMediaToken("p")]);
    expect(transport).toHaveBeenCalledTimes(1);
    await client.ensureMediaToken("p");
    expect(transport).toHaveBeenCalledTimes(1);

    clock += (3600 - 200) * 1000; // осталось меньше 5 минут
    await client.ensureMediaToken("p");
    expect(transport).toHaveBeenCalledTimes(2);
  });

  it("force: свежий токен из кэша игнорируется, запрашивается новый", async () => {
    const clock = 1_000_000;
    const transport = vi.fn().mockImplementation(async () => tokenReply(clock / 1000 + 3600));
    const client = createStorybookClient(transport, { apiBase: "http://h/", now: () => clock });
    await client.ensureMediaToken("p");
    await client.ensureMediaToken("p", { force: true });
    expect(transport).toHaveBeenCalledTimes(2);
    await client.ensureMediaToken("p", { force: false });
    expect(transport).toHaveBeenCalledTimes(2);
  });

  it("mediaUrl: null до токена, потом корректно кодирует путь", async () => {
    const client = createStorybookClient(
      vi.fn().mockResolvedValue(tokenReply(Date.now() / 1000 + 3600, "a.b")),
      { apiBase: "http://h:8000/" },
    );
    expect(client.mediaUrl("p", "a.png")).toBeNull();
    await client.ensureMediaToken("p");
    const url = client.mediaUrl("p", "dir/кадр #1 ..png");
    expect(url).toBe(
      `http://h:8000/v1/storybook/projects/p/media?path=${encodeURIComponent("dir/кадр #1 ..png")}&t=a.b`,
    );
    expect(client.thumbUrl("p", "a.png", 128)).toBe(
      "http://h:8000/v1/storybook/projects/p/thumb?path=a.png&size=128&t=a.b",
    );
  });

  it("apiBaseFromAgentUrl убирает /agent", () => {
    expect(apiBaseFromAgentUrl("http://h:8000/agent/")).toBe("http://h:8000");
    expect(apiBaseFromAgentUrl("http://h:8000/agent")).toBe("http://h:8000");
    expect(apiBaseFromAgentUrl("http://h:8000")).toBe("http://h:8000");
  });
});
