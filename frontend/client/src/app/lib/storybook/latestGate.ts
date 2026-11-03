// Защита от устаревших ответов: применять результат только у последнего запущенного запроса.
export type LatestGate = { next: () => number; isCurrent: (token: number) => boolean };

export const createLatestGate = (): LatestGate => {
  let seq = 0;
  return {
    next: () => ++seq,
    isCurrent: (token) => token === seq,
  };
};
