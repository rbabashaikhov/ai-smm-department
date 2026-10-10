import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";

import { ErrorCode, isApiError } from "../api/errors";
import { ErrorBanner } from "../components/ErrorBanner";
import { useAuth } from "../features/auth/AuthProvider";

export function LoginPage() {
  const { state, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);

  const from = (location.state as { from?: string } | null)?.from;
  const target = from && from !== "/login" ? from : "/projects";

  if (state.status === "authenticated" && !pending) {
    return <Navigate to={target} replace />;
  }

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError(null);

    try {
      await login(email, password);
      // The password is not kept a moment longer than the request.
      setPassword("");
      navigate(target, { replace: true });
    } catch (caught) {
      setPassword("");
      setError(caught);
    } finally {
      setPending(false);
    }
  };

  const invalid = isApiError(error) && error.code === ErrorCode.INVALID_CREDENTIALS;

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={onSubmit} aria-label="Login">
        <h1>SMM Control Center</h1>
        <p className="muted small">Private operator console</p>
        <label>
          Email
          <input
            type="email"
            name="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {invalid ? (
          <div className="banner banner-error" role="alert">
            Неверный email или пароль.
          </div>
        ) : error ? (
          <ErrorBanner error={error} />
        ) : null}
        <button type="submit" className="btn-primary" disabled={pending}>
          {pending ? "Вход…" : "Войти"}
        </button>
      </form>
    </div>
  );
}
