import { useEffect, useRef, useState } from "react";

import { useAuth } from "../auth";
import { LoadingBlock } from "../components/Feedback";
import { useNavigation } from "../navigation";

export function AuthCallbackPage({ signOut = false }: { signOut?: boolean }) {
  const auth = useAuth();
  const { navigate } = useNavigation();
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) {
      return;
    }
    started.current = true;
    const action = signOut ? auth.completeSignOut : auth.completeSignIn;
    void action()
      .then(() => navigate("/", { replace: true }))
      .catch((reason: unknown) => {
        setError(
          reason instanceof Error
            ? reason.message
            : "Провайдер идентификации отклонил ответ",
        );
      });
  }, [auth.completeSignIn, auth.completeSignOut, navigate, signOut]);

  if (error !== null) {
    return (
      <main className="callback-page" role="alert">
        <h1>Не удалось завершить вход</h1>
        <p>{error}</p>
        <button
          className="button button--secondary"
          type="button"
          onClick={() => navigate("/", { replace: true })}
        >
          Вернуться
        </button>
      </main>
    );
  }
  return (
    <main className="callback-page">
      <LoadingBlock
        label={
          signOut ? "Завершение защищённого сеанса" : "Проверка ответа SSO"
        }
      />
    </main>
  );
}
