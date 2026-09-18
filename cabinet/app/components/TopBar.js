"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "../lib/api";

const POLL_MS = 5000;

/** Шапка со счётчиками. На странице входа её нет. */
export default function TopBar() {
  const pathname = usePathname();
  const [counters, setCounters] = useState(null);

  useEffect(() => {
    if (pathname === "/") return undefined;
    let alive = true;
    const load = async () => {
      try {
        const me = await api("/me");
        if (alive) setCounters(me.counters);
      } catch {
        if (alive) setCounters(null);
      }
    };
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [pathname]);

  if (pathname === "/") return null;

  const link = (href, title) => (
    <Link
      href={href}
      className={`topbar__link ${pathname.startsWith(href) ? "topbar__link--active" : ""}`}
    >
      {title}
    </Link>
  );

  return (
    <header className="topbar">
      <div className="topbar__brand">Кабинет школы</div>
      <nav className="topbar__nav">
        {link("/conversations", "Разговоры")}
        {link("/leads", "Заявки")}
      </nav>
      {counters ? (
        <div className="topbar__counters">
          У вас: {counters.human} · ждут ответа: {counters.requested} · новых заявок:{" "}
          {counters.new_leads}
        </div>
      ) : null}
    </header>
  );
}
