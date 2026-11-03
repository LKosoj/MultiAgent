// Очередь фоновых запросов (размеры, токены обложек, отчёты): по одному, с паузой,
// и только когда нет действий пользователя. Все вызовы раздела идут через общую очередь страницы,
// так что фон не должен её занимать впрок.

export type BackgroundQueue = {
  run<T>(task: () => Promise<T>): Promise<T>;
  userStart(): void;
  userEnd(): void;
  userCount(): number;
};

export type BackgroundQueueOptions = {
  gapMs?: number;
  sleep?: (ms: number) => Promise<void>;
};

export const createBackgroundQueue = (options: BackgroundQueueOptions = {}): BackgroundQueue => {
  const gapMs = options.gapMs ?? 150;
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  let users = 0;
  let waiters: (() => void)[] = [];
  let chain: Promise<unknown> = Promise.resolve();

  const whenIdle = (): Promise<void> =>
    users === 0 ? Promise.resolve() : new Promise<void>((resolve) => waiters.push(resolve));

  const release = () => {
    if (users !== 0) return;
    const list = waiters;
    waiters = [];
    list.forEach((resolve) => resolve());
  };

  return {
    run<T>(task: () => Promise<T>): Promise<T> {
      const result = chain.then(async () => {
        while (users > 0) await whenIdle();
        try {
          return await task();
        } finally {
          if (gapMs > 0) await sleep(gapMs);
        }
      });
      chain = result.catch(() => undefined);
      return result;
    },
    userStart() {
      users += 1;
    },
    userEnd() {
      users = Math.max(0, users - 1);
      release();
    },
    userCount: () => users,
  };
};
