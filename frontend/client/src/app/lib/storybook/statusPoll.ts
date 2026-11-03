// Опрос состояния проекта: один запрос за раз, пауза при скрытой вкладке, рост интервала при сбоях.
// Без React и без прямых таймеров: их подставляют снаружи, поэтому тесты идут на фальшивых часах.

export type PollFetchInfo = { manual: boolean };

export type StatusPollerOptions<T> = {
  fetch: (info: PollFetchInfo) => Promise<T>;
  intervalFor: (data: T | null) => number;
  onData: (data: T) => void;
  onError: (err: unknown) => void;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  isHidden?: () => boolean;
  maxBackoffMs?: number;
};

export type StatusPoller = {
  start(): void;
  stop(): void;
  refreshNow(): void;
  notifyVisible(): void;
};

export const POLL_MAX_BACKOFF_MS = 30_000;

export const createStatusPoller = <T>(options: StatusPollerOptions<T>): StatusPoller => {
  const setTimer = options.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = options.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  const isHidden = options.isHidden ?? (() => false);
  const maxBackoff = options.maxBackoffMs ?? POLL_MAX_BACKOFF_MS;

  let stopped = true;
  let generation = 0; // номер запуска: ответы прошлых запусков отбрасываются
  let flightGeneration = -1; // в каком запуске идёт запрос
  let timer: unknown = null;
  let failures = 0;
  let last: T | null = null;
  let again = false;

  const clear = () => {
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
  };

  const schedule = (ms: number) => {
    clear();
    if (stopped) return;
    timer = setTimer(() => {
      timer = null;
      void tick(false);
    }, ms);
  };

  const tick = async (manual: boolean): Promise<void> => {
    if (stopped) return;
    if (!manual && isHidden()) return; // продолжим по notifyVisible()
    if (flightGeneration === generation) {
      if (manual) again = true;
      return;
    }
    clear();
    const mine = generation;
    flightGeneration = mine;
    try {
      const data = await options.fetch({ manual });
      if (mine !== generation || stopped) return;
      last = data;
      failures = 0;
      options.onData(data);
      schedule(options.intervalFor(data));
    } catch (err) {
      if (mine !== generation || stopped) return;
      failures += 1;
      options.onError(err);
      schedule(Math.min(maxBackoff, options.intervalFor(last) * 2 ** failures));
    } finally {
      if (flightGeneration === mine) flightGeneration = -1;
      if (again && !stopped && mine === generation) {
        again = false;
        void tick(true);
      }
    }
  };

  return {
    start() {
      stopped = false;
      generation += 1;
      failures = 0;
      again = false;
      void tick(false);
    },
    stop() {
      stopped = true;
      generation += 1;
      again = false;
      clear();
    },
    refreshNow() {
      if (stopped) return;
      clear();
      void tick(true);
    },
    notifyVisible() {
      if (stopped || timer !== null || flightGeneration === generation) return;
      void tick(false);
    },
  };
};
