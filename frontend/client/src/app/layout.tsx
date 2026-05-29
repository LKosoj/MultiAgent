import type { Metadata } from "next";
import "./globals.css";
import "@copilotkitnext/react/styles.css";

export const metadata: Metadata = {
  title: "MultiAgent Studio",
  description: "Клиент для сервисных функций MultiAgent.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ru">
      <body className="antialiased">{children}</body>
    </html>
  );
}
