/**
 * Central runtime configuration, resolved from Vite environment variables.
 *
 * All values are read from `import.meta.env` at build time. Local development
 * falls back to a localhost API so the app runs without a `.env.local`.
 */

const DEFAULT_API_BASE_URL = "http://localhost:5000/v1";

export interface CognitoConfig {
  userPoolId: string;
  clientId: string;
  /** Hosted UI domain, e.g. `recipemate.auth.us-east-1.amazoncognito.com`. */
  domain: string;
  region: string;
  redirectSignIn: string;
  redirectSignOut: string;
}

export interface AppConfig {
  apiBaseUrl: string;
  cognito: CognitoConfig;
}

function env(key: keyof ImportMetaEnv, fallback = ""): string {
  return import.meta.env[key] ?? fallback;
}

/** Normalize a base URL by stripping a single trailing slash. */
function normalizeBaseUrl(url: string): string {
  return url.replace(/\/+$/, "");
}

export const config: AppConfig = {
  apiBaseUrl: normalizeBaseUrl(env("VITE_API_BASE_URL", DEFAULT_API_BASE_URL)),
  cognito: {
    userPoolId: env("VITE_COGNITO_USER_POOL_ID"),
    clientId: env("VITE_COGNITO_CLIENT_ID"),
    domain: env("VITE_COGNITO_DOMAIN"),
    region: env("VITE_COGNITO_REGION", "us-east-1"),
    redirectSignIn: env(
      "VITE_REDIRECT_SIGN_IN",
      `${window.location.origin}/`,
    ),
    redirectSignOut: env(
      "VITE_REDIRECT_SIGN_OUT",
      `${window.location.origin}/`,
    ),
  },
};

/**
 * True when the Cognito Hosted UI env vars are present. When false the auth
 * layer runs in a degraded mode (sign-in button is disabled) rather than
 * crashing at startup — useful for local scaffold work before pools exist.
 */
export const isCognitoConfigured =
  Boolean(config.cognito.userPoolId) &&
  Boolean(config.cognito.clientId) &&
  Boolean(config.cognito.domain);
