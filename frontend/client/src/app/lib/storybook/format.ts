// Форматирование чисел, денег, времени и дат по правилам спеки (раздел 5).
const NBSP = " ";

export const formatMaybe = (value: number | string | null | undefined): string =>
  value === null || value === undefined || value === "" ? "—" : String(value);

export const formatBytes = (bytes: number | null | undefined): string => {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  if (unit === 0) return `${Math.round(value)}${NBSP}${units[0]}`;
  const text = value >= 100 ? String(Math.round(value)) : value.toFixed(1);
  return `${text}${NBSP}${units[unit]}`;
};

// «1 204 ₽»: неразрывный пробел между тысячами и перед знаком
export const formatRub = (amount: number | null | undefined): string => {
  if (typeof amount !== "number" || !Number.isFinite(amount)) return "—";
  const rounded = Math.round(amount);
  const digits = String(Math.abs(rounded)).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
  return `${rounded < 0 ? "-" : ""}${digits}${NBSP}₽`;
};

// «42 мин», «2 ч 5 мин»; меньше минуты - в секундах
export const formatDuration = (seconds: number | null | undefined): string => {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)} с`;
  const totalMinutes = Math.round(seconds / 60);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours === 0) return `${minutes} мин`;
  return minutes === 0 ? `${hours} ч` : `${hours} ч ${minutes} мин`;
};

const pad2 = (n: number) => String(n).padStart(2, "0");

// «02:41», от часа - «1:02:41»
export const formatClock = (seconds: number | null | undefined): string => {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${pad2(m)}:${pad2(s)}` : `${pad2(m)}:${pad2(s)}`;
};

const ISO_PARTS = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/;

// «27.10.2026» из ISO-строки (локальное время сервера, часовой пояс не пересчитываем)
export const formatDateRu = (iso: string | null | undefined): string => {
  const m = typeof iso === "string" ? ISO_PARTS.exec(iso) : null;
  return m ? `${m[3]}.${m[2]}.${m[1]}` : "—";
};

// «27.10.2026 14:05»
export const formatDateTimeRu = (iso: string | null | undefined): string => {
  const m = typeof iso === "string" ? ISO_PARTS.exec(iso) : null;
  if (!m) return "—";
  return m[4] ? `${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}` : `${m[3]}.${m[2]}.${m[1]}`;
};

// Время сообщения для нижней строки («14:05:09»)
export const formatTimeOfDay = (ms: number): string => {
  const d = new Date(ms);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
};

// Момент времени из статуса: UTC-строка («...Z») переводится в местное время, без пояса - как есть
export const formatDateTimeAny = (iso: string | null | undefined): string => {
  if (typeof iso !== "string") return "—";
  if (/(?:Z|[+-]\d{2}:?\d{2})$/.test(iso)) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    const p = (n: number) => String(n).padStart(2, "0");
    return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  return formatDateTimeRu(iso);
};
