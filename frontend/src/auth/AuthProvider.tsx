import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  fetchAuthSession,
  getCurrentUser,
  signInWithRedirect,
  signOut as amplifySignOut,
  type AuthUser,
} from "aws-amplify/auth";
import { Hub } from "aws-amplify/utils";
import { isCognitoConfigured } from "../config";

export interface AuthContextValue {
  /** The signed-in Cognito user, or null when unauthenticated. */
  user: AuthUser | null;
  isAuthenticated: boolean;
  /** True while the initial session check (or a redirect) is in flight. */
  isLoading: boolean;
  /** Kick off the Hosted UI OAuth redirect. */
  signIn: () => Promise<void>;
  /** Sign out locally and via the Hosted UI. */
  signOut: () => Promise<void>;
  /**
   * Return the current access token (JWT) or null if there is no session.
   * Amplify refreshes the token transparently when it is near expiry.
   */
  getToken: () => Promise<string | null>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

/**
 * Resolve the current access token from the Amplify session. Returns null
 * (rather than throwing) when there is no authenticated session.
 */
async function readToken(): Promise<string | null> {
  try {
    const session = await fetchAuthSession();
    return session.tokens?.accessToken?.toString() ?? null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const refreshUser = useCallback(async () => {
    if (!isCognitoConfigured) {
      setUser(null);
      setIsLoading(false);
      return;
    }
    try {
      const current = await getCurrentUser();
      setUser(current);
    } catch {
      setUser(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  // Initial session check + subscribe to Hosted UI redirect outcomes.
  useEffect(() => {
    void refreshUser();

    const unsubscribe = Hub.listen("auth", ({ payload }) => {
      switch (payload.event) {
        case "signedIn":
        case "signInWithRedirect":
          void refreshUser();
          break;
        case "signedOut":
          setUser(null);
          break;
        case "signInWithRedirect_failure":
          setUser(null);
          setIsLoading(false);
          break;
        default:
          break;
      }
    });

    return unsubscribe;
  }, [refreshUser]);

  const signIn = useCallback(async () => {
    if (!isCognitoConfigured) {
      throw new Error(
        "Cognito is not configured. Set the VITE_COGNITO_* env vars.",
      );
    }
    setIsLoading(true);
    await signInWithRedirect();
  }, []);

  const signOut = useCallback(async () => {
    await amplifySignOut();
    setUser(null);
  }, []);

  const getToken = useCallback(readToken, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isAuthenticated: user !== null,
      isLoading,
      signIn,
      signOut,
      getToken,
    }),
    [user, isLoading, signIn, signOut, getToken],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === undefined) {
    throw new Error("useAuth must be used within an <AuthProvider>");
  }
  return ctx;
}
