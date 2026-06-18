import { redirect } from "next/navigation";

// Authentication is handled by the host platform's Keycloak (via the reverse
// proxy). Metron has no login page of its own — land users straight on the app.
export default function Home() {
  redirect("/dashboard");
}
