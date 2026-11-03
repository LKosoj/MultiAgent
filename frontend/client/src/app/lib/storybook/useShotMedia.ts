"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { BackgroundQueue } from "./backgroundQueue";
import type { StorybookClient, ThumbSize } from "./client";

// Хвост ссылки для обхода кэша: версия файла плюс счётчик сброса (media.bust), чтобы ссылка менялась и при той же версии
export const mediaSuffix = (rev: string | null | undefined, busted: number): string => {
  const v = `${rev ?? ""}${busted ? `b${busted}` : ""}`;
  return v ? `&v=${encodeURIComponent(v)}` : "";
};

type Args = { client: StorybookClient; background: BackgroundQueue; projectId: string; enabled: boolean };

export type ShotMediaApi = {
  // токен получен: ссылки можно строить
  ready: boolean;
  thumb: (rel: string | null | undefined, size: ThumbSize, rev?: string | null) => string | null;
  media: (rel: string | null | undefined, rev?: string | null) => string | null;
  // картинка не загрузилась: один раз просим токен заново
  onImageError: (ev: { currentTarget: { src: string } }) => void;
  // файлы шота поменялись (возврат версии, перегенерация): браузерный кэш обходим новым номером
  bust: () => void;
  // принудительно берём новый токен (кнопка «Повторить»), ссылки пересобираются
  // успех - когда токен получен и ссылки пересобраны; сбой - промис отклоняется
  refresh: () => Promise<void>;
};

// Адрес без параметра t= (токен): смена токена не делает адрес «новым»
export const stripToken = (src: string): string => src.replace(/([?&])t=[^&]*(&|$)/, (_m, lead: string, tail: string) => (tail ? lead : "")).replace(/[?&]$/, "");

export const tokenOf = (src: string | null): string | null => {
  const m = src ? /[?&]t=([^&]*)/.exec(src) : null;
  return m ? m[1] : null;
};

// Повтор разрешён один раз на каждый адрес картинки (токен в адресе не считается)
export const createRetryOnce = () => {
  const seen = new Set<string>();
  return (src: string): boolean => {
    const key = stripToken(src);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  };
};

const REFRESH_MS = 10 * 60 * 1000;

// Токен медиа ("media_token") один на проект; запрашиваем через фоновую очередь, чтобы не мешать действиям пользователя
export const useShotMedia = ({ client, background, projectId, enabled }: Args): ShotMediaApi => {
  const [ready, setReady] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const [busted, setBusted] = useState(0);
  const retried = useRef(createRetryOnce());
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let stop = false;
    const ensure = () => {
      const before = client.mediaUrl(projectId, ".");
      background
        .run(() => client.ensureMediaToken(projectId))
        .then(() => {
          if (stop || !alive.current) return;
          setReady(true);
          // токен сменился - ссылки новые, браузер скачает картинки заново
          if (client.mediaUrl(projectId, ".") !== before) setEpoch((n) => n + 1);
        })
        .catch(() => undefined);
    };
    ensure();
    const timer = setInterval(ensure, REFRESH_MS);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [client, background, projectId, enabled]);

  // параллельные принудительные запросы токена склеиваем в один
  const forcing = useRef<Promise<void> | null>(null);
  const forceToken = useCallback((): Promise<void> => {
    if (!forcing.current) {
      forcing.current = background.run(() => client.ensureMediaToken(projectId, { force: true })).finally(() => {
        forcing.current = null;
      });
    }
    return forcing.current;
  }, [background, client, projectId]);

  const onImageError = useCallback((ev: { currentTarget: { src: string } }) => {
    if (!retried.current(ev.currentTarget.src)) return;
    // токен в упавшем адресе уже старый: клиент его сменил, хватит пересобрать ссылки
    const current = tokenOf(client.mediaUrl(projectId, "."));
    const failedWith = tokenOf(ev.currentTarget.src);
    if (current !== null && failedWith !== null && failedWith !== current) {
      setEpoch((n) => n + 1);
      return;
    }
    forceToken()
      .then(() => alive.current && setEpoch((n) => n + 1))
      .catch(() => undefined);
  }, [client, projectId, forceToken]);

  const refresh = useCallback(
    () =>
      forceToken().then(() => {
        if (!alive.current) return;
        setReady(true);
        setEpoch((n) => n + 1);
        setBusted((n) => n + 1);
      }),
    [forceToken],
  );

  const bust = useCallback(() => setBusted((n) => n + 1), []);

  return useMemo(() => {
    void epoch;
    const suffix = (rev?: string | null) => mediaSuffix(rev, busted);
    return {
      ready,
      thumb: (rel, size, rev) => {
        const url = rel ? client.thumbUrl(projectId, rel, size) : null;
        return url ? url + suffix(rev) : null;
      },
      media: (rel, rev) => {
        const url = rel ? client.mediaUrl(projectId, rel) : null;
        return url ? url + suffix(rev) : null;
      },
      onImageError,
      bust,
      refresh,
    };
  }, [client, projectId, ready, epoch, busted, onImageError, bust, refresh]);
};
