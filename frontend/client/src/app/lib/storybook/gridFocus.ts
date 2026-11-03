// Карточка, от которой работают стрелки и Delete: та, что в фокусе; иначе выбранная.
export const gridBaseId = (focusedId: string | null, selectedId: string | null): string | null =>
  focusedId ?? selectedId;

export const focusedCardId = (target: EventTarget | null): string | null => {
  const el = target as { closest?: (s: string) => { getAttribute: (n: string) => string | null } | null } | null;
  return el?.closest?.("[data-project-id]")?.getAttribute("data-project-id") ?? null;
};
