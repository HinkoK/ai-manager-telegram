import TopBar from "./components/TopBar";
import "./globals.css";

export const metadata = {
  title: "Кабинет школы",
};

export default function RootLayout({ children }) {
  return (
    <html lang="ru">
      <body>
        <TopBar />
        {children}
      </body>
    </html>
  );
}
