import { Amplify } from "aws-amplify";
import { config, isCognitoConfigured } from "../config";

/**
 * Configure AWS Amplify (v6) for Cognito Hosted UI / OAuth redirect auth.
 *
 * Called once at startup from `main.tsx` before React renders. When the
 * Cognito env vars are missing we skip configuration so the scaffold still
 * boots locally; `isCognitoConfigured` lets the UI reflect the degraded state.
 */
export function configureAmplify(): void {
  if (!isCognitoConfigured) {
    // eslint-disable-next-line no-console
    console.warn(
      "[auth] Cognito env vars are not set — Hosted UI sign-in is disabled. " +
        "See frontend/.env.example.",
    );
    return;
  }

  const { cognito } = config;

  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: cognito.userPoolId,
        userPoolClientId: cognito.clientId,
        loginWith: {
          oauth: {
            domain: cognito.domain,
            scopes: ["openid", "email", "profile"],
            redirectSignIn: [cognito.redirectSignIn],
            redirectSignOut: [cognito.redirectSignOut],
            responseType: "code",
          },
        },
      },
    },
  });
}
