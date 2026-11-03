// Одно действие за раз: пока предыдущее не закончилось, новый вызов отбрасывается.
// Нужен для клавиш ревью: быстрые нажатия не должны слать запросы со старым отпечатком записи.
export type SingleFlight = {
  isBusy: () => boolean;
  run: <T>(task: () => Promise<T>) => Promise<{ ran: true; value: T } | { ran: false }>;
};

export const createSingleFlight = (): SingleFlight => {
  let busy = false;
  return {
    isBusy: () => busy,
    run: async (task) => {
      if (busy) return { ran: false };
      busy = true;
      try {
        return { ran: true, value: await task() };
      } finally {
        busy = false;
      }
    },
  };
};
