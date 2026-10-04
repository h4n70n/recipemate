import { useEffect, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import { isCognitoConfigured } from "../config";

interface FromState {
  from?: { pathname?: string };
}

/**
 * Sign in screen (web-ui-spec 5.1): a single branded button that hands off to
 * the Cognito Hosted UI. No local password form. States: default,
 * redirecting (spinner), auth error.
 */
export function SignIn() {
  const { isAuthenticated, signIn } = useAuth();
  const location = useLocation();
  const [redirecting, setRedirecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // If a redirect sign-in just completed, bounce back to the attempted page.
  const from = (location.state as FromState | null)?.from?.pathname ?? "/";

  useEffect(() => {
    if (!isCognitoConfigured) {
      setError(
        "Authentication is not configured. Set the VITE_COGNITO_* environment variables.",
      );
    }
  }, []);

  if (isAuthenticated) {
    return <Navigate to={from} replace />;
  }

  const handleSignIn = async () => {
    setError(null);
    setRedirecting(true);
    try {
      await signIn();
    } catch (err) {
      setRedirecting(false);
      setError(err instanceof Error ? err.message : "Sign in failed.");
    }
  };

  return (
    <main
      style={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        padding: "var(--space-6)",
      }}
    >
      <section
        style={{
          background: "var(--color-surface)",
          borderRadius: "var(--radius-card)",
          padding: "var(--space-12)",
          textAlign: "center",
          maxWidth: 360,
          width: "100%",
          boxShadow: "0 1px 3px rgba(31, 27, 22, 0.08)",
        }}
      >
        <h1 style={{ color: "var(--color-primary)", marginTop: 0 }}>
          RecipeMate
        </h1>
        <p style={{ color: "var(--color-text-secondary)" }}>
          Your searchable digital cookbook.
        </p>

        {error && (
          <p role="alert" style={{ color: "var(--color-error)" }}>
            {error}
          </p>
        )}

        <button
          type="button"
          onClick={handleSignIn}
          disabled={redirecting || !isCognitoConfigured}
          style={{
            marginTop: "var(--space-4)",
            width: "100%",
            padding: "var(--space-3) var(--space-4)",
            fontSize: "1rem",
            color: "#fff",
            background: "var(--color-primary)",
            border: "none",
            borderRadius: "var(--radius-input)",
            cursor:
              redirecting || !isCognitoConfigured ? "not-allowed" : "pointer",
            opacity: redirecting || !isCognitoConfigured ? 0.7 : 1,
          }}
        >
          {redirecting ? "Redirecting…" : "Sign in"}
        </button>
      </section>
    </main>
  );
}
