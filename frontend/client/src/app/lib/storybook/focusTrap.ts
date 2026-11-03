// Ловушка фокуса в окне: куда перейти по Tab. null - браузер сам справится.
export const trapTarget = <T>(items: readonly T[], active: T | null, shift: boolean): T | null => {
  if (items.length === 0) return null;
  const first = items[0];
  const last = items[items.length - 1];
  const idx = active === null ? -1 : items.indexOf(active);
  if (idx === -1) return shift ? last : first; // фокус вне окна (клик по тексту)
  if (shift && active === first) return last;
  if (!shift && active === last) return first;
  return null;
};
