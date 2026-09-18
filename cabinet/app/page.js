"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { post } from "./lib/api";

/** Вход. Регистрации нет: владелец один, логин и пароль задаются в .env бота. */
export default function LoginPage() {
  const router = useRouter();
  const [login, setLogin] = useState("owner");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await post("/login", { login, password });
      router.push("/conversations");
    } catch (problem) {
      setError(problem.unauthorized ? "Неверный логин или пароль" : problem.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="center">
      <form className="login" onSubmit={submit}>
        <h1>Кабинет школы</h1>
        <div className="field">
          <label htmlFor="login">Логин</label>
          <input
            id="login"
            value={login}
            onChange={(e) => setLogin(e.target.value)}
            autoComplete="username"
          />
        </div>
        <div className="field">
          <label htmlFor="password">Пароль</label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </div>
        <div className="field">
          <button className="btn btn--primary" type="submit" disabled={busy || !password}>
            {busy ? "Вхожу…" : "Войти"}
          </button>
        </div>
        {error ? <p className="error">{error}</p> : null}
      </form>
    </div>
  );
}
