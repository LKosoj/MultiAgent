// Обёртки возможностей модуля core (каркас studio).
import type { StorybookClient } from "./client";

export type MediaTokenResult = {
  t: string; // токен для ?t= (имя не "token": такой ключ маскируется)
  expires_at: number;
  ttl_seconds: number;
};

export const mediaToken = (c: StorybookClient, p: { project_id: string }) =>
  c.call<MediaTokenResult>("media_token", p);
