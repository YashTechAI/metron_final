/**
 * Authenticated fetch.
 *
 * Metron runs embedded behind the host platform's reverse proxy, which injects
 * the `Authorization: Bearer <keycloak-token>` header on every request before it
 * reaches the backend. The frontend therefore does NOT handle tokens itself — it
 * just forwards cookies (credentials) so the proxy can identify the session.
 */
export async function authFetch(
  input: RequestInfo | URL,
  init?: RequestInit
): Promise<Response> {
  return fetch(input, { ...init, credentials: "include" });
}
